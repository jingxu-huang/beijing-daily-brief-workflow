#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeWe RSS 远程重登服务。

背景：微信读书登录二维码只能用手机微信「扫一扫」的摄像头识别（相册识别、
长按识别、链接直开都被微信官方拦截），所以掉线时需要一块「第二屏幕」来
显示二维码。本服务提供一个极简重登页：任何浏览器打开后自动出码、到期
自动换码、扫码成功后自动把新 token 写回 WeWe RSS 并提示完成。

配合 cloudflared 按需隧道使用：wewe_preflight.py 检测到账号失效时，调用
本服务的 admin API 启动隧道，把随机公网 URL 发进飞书告警；登录恢复后
隧道自动关闭（成功后延迟关闭 + TTL 兜底）。

端点：
  GET  /r/<RELOGIN_TOKEN>            重登页（公开，凭长随机 token）
  GET  /api/qr?k=<token>             生成新登录二维码（返回 SVG）
  GET  /api/status?k=<token>&id=...  查询扫码结果；成功时自动保存账号
  POST /api/tunnel/start             启动 cloudflared 隧道（需 X-Admin-Token）
  POST /api/tunnel/stop              关闭隧道（需 X-Admin-Token）
  GET  /api/tunnel/status            隧道状态（需 X-Admin-Token）
  GET  /healthz                      健康检查（本机排查用）

配置：环境变量优先，其次 ../local-only/relogin.env（KEY=VALUE）。
依赖：python3 标准库 + qrcode（pip install qrcode，无需 PIL）。
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import qrcode
    import qrcode.image.svg
except ImportError:
    print(
        "[wewe-relogin] 缺少 qrcode 库：请运行 python3 -m pip install --user qrcode",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)


# ---------------------------------------------------------------- 配置加载

def _load_env_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _first_env_file():
    """按优先级找配置：显式指定 > 脚本同目录（安装副本）> 项目 local-only（开发目录）。"""
    candidates = [
        os.getenv("RELOGIN_ENV_FILE", ""),
        os.path.join(_SCRIPT_DIR, "relogin.env"),
        os.path.join(_SCRIPT_DIR, "..", "local-only", "relogin.env"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


_ENV = _load_env_file(_first_env_file())


def _cfg(key, default=""):
    return os.getenv(key) or _ENV.get(key) or default


WEWE_URL = _cfg("WEWE_RSS_URL", "http://127.0.0.1:4000").rstrip("/")
WEWE_AUTH = _cfg("WEWE_AUTH_CODE", "")
PORT = int(_cfg("RELOGIN_PORT", "4001"))
TOKEN = _cfg("RELOGIN_TOKEN")
ADMIN_TOKEN = _cfg("RELOGIN_ADMIN_TOKEN")
CLOUDFLARED = _cfg("CLOUDFLARED_BIN", "")
TUNNEL_TTL_MIN = int(_cfg("TUNNEL_TTL_MINUTES", "360"))
STOP_DELAY_SEC = int(_cfg("TUNNEL_STOP_DELAY_SECONDS", "120"))
QR_TTL_SEC = int(_cfg("QR_TTL_SECONDS", "60"))

if not TOKEN or not ADMIN_TOKEN or not WEWE_AUTH:
    print(
        "[wewe-relogin] 缺少 RELOGIN_TOKEN / RELOGIN_ADMIN_TOKEN / WEWE_AUTH_CODE，"
        "请先运行 automation/install_wewe_relogin.command 生成并补全 relogin.env",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)


def log(message):
    print(f"[{time.strftime('%F %T')}] {message}", flush=True)


def find_cloudflared():
    if CLOUDFLARED and os.path.exists(CLOUDFLARED):
        return CLOUDFLARED
    for candidate in (
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
    ):
        if os.path.exists(candidate):
            return candidate
    from shutil import which

    return which("cloudflared") or ""


# ---------------------------------------------------------------- WeWe RSS 客户端
# 调用格式已用真实后台验证：
#   POST /trpc/platform.createLoginUrl（无 body）→ {result:{data:{uuid, scanUrl}}}
#   GET  /trpc/platform.getLoginResult?batch=1&input={"0":{"id":...}} → [{result:{data:{message, vid?, token?, username?}}}]
#   POST /trpc/account.add（raw JSON body {id, token, name, status}）

class WeweError(RuntimeError):
    pass


def _wewe_request(method, path, body=None, timeout=30):
    data = None
    headers = {"authorization": WEWE_AUTH}
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(WEWE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise WeweError(f"HTTP {exc.code}: {text[:200]}")
    except Exception as exc:
        raise WeweError(f"无法连接 WeWe RSS：{exc}")
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise WeweError(f"WeWe RSS 响应异常：{text[:200]}")


def _unwrap(data):
    if isinstance(data, list):
        if not data:
            raise WeweError("WeWe RSS 返回空响应")
        data = data[0]
    if "error" in data:
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise WeweError(message or "tRPC error")
    return (data.get("result") or {}).get("data")


def wewe_create_login_url():
    data = _unwrap(_wewe_request("POST", "/trpc/platform.createLoginUrl", timeout=30))
    if not data or not data.get("uuid") or not data.get("scanUrl"):
        raise WeweError(f"createLoginUrl 响应缺字段：{data}")
    return data["uuid"], data["scanUrl"]


def wewe_get_login_result(uuid):
    qs = urllib.parse.urlencode({"batch": 1, "input": json.dumps({"0": {"id": uuid}})})
    data = _unwrap(_wewe_request("GET", f"/trpc/platform.getLoginResult?{qs}", timeout=40))
    return data or {}


def wewe_account_add(vid, token, name):
    body = {"id": str(vid), "token": token, "name": name or f"account-{vid}", "status": 1}
    _unwrap(_wewe_request("POST", "/trpc/account.add", body=body, timeout=30))


# ---------------------------------------------------------------- 二维码

def make_qr_svg(text):
    image = qrcode.make(
        text,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=16,
        border=2,
    )
    svg = image.to_string()
    if isinstance(svg, bytes):
        svg = svg.decode("utf-8")
    return svg


# ---------------------------------------------------------------- 限速（防止公开页被滥刷登录会话）

_rate_lock = threading.Lock()
_last_create = 0.0
_hour_window = []
MIN_CREATE_INTERVAL = 3.0
HOURLY_CAP = 80


def rate_limit_ok():
    global _last_create
    now = time.time()
    with _rate_lock:
        while _hour_window and now - _hour_window[0] > 3600:
            _hour_window.pop(0)
        if now - _last_create < MIN_CREATE_INTERVAL or len(_hour_window) >= HOURLY_CAP:
            return False
        _last_create = now
        _hour_window.append(now)
        return True


# ---------------------------------------------------------------- cloudflared 隧道管理

class TunnelManager:
    URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._url = ""
        self._timers = []
        self._tail = []

    def status(self):
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {"running": running, "url": self._url if running else ""}

    def start(self):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._url:
                return self._url
            binary = find_cloudflared()
            if not binary:
                raise RuntimeError("未找到 cloudflared，请运行「安装重登服务.command」")
            log("启动 cloudflared 隧道 ...")
            self._url = ""
            self._tail = []
            found = threading.Event()
            self._proc = subprocess.Popen(
                [binary, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            def reader(proc):
                for line in proc.stdout or []:
                    line = line.rstrip()
                    self._tail.append(line)
                    if len(self._tail) > 40:
                        self._tail.pop(0)
                    match = self.URL_RE.search(line)
                    if match and not self._url:
                        self._url = match.group(0)
                        found.set()

            threading.Thread(target=reader, args=(self._proc,), daemon=True).start()

        if not found.wait(timeout=45):
            tail = "\n".join(self._tail[-8:])
            self.stop(reason="启动超时")
            raise RuntimeError(f"cloudflared 启动超时，未拿到公网 URL。日志尾部：\n{tail}")

        with self._lock:
            ttl_timer = threading.Timer(TUNNEL_TTL_MIN * 60, self.stop, kwargs={"reason": "TTL 到期"})
            ttl_timer.daemon = True
            ttl_timer.start()
            self._timers.append(ttl_timer)
            log(f"隧道已就绪：{self._url}（TTL {TUNNEL_TTL_MIN} 分钟）")
            return self._url

    def stop(self, reason=""):
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers = []
            proc, self._proc, self._url = self._proc, None, ""
        if proc is not None and proc.poll() is None:
            log(f"关闭隧道（{reason or '手动'}）")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def schedule_stop(self, delay, reason):
        if not self.status()["running"]:
            return
        timer = threading.Timer(delay, self.stop, kwargs={"reason": reason})
        timer.daemon = True
        timer.start()
        with self._lock:
            self._timers.append(timer)
        log(f"登录成功，{delay} 秒后自动关闭隧道")


TUNNEL = TunnelManager()


# ---------------------------------------------------------------- 重登页

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>WeWe RSS 重新登录</title>
<style>
  body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f5f6f8; color: #1a1a1a; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; }
  .card { background: #fff; border-radius: 16px; padding: 32px 28px; width: min(88vw, 380px);
          box-shadow: 0 6px 24px rgba(0,0,0,.08); text-align: center; }
  h1 { font-size: 19px; margin: 0 0 6px; }
  p.sub { color: #666; font-size: 14px; margin: 0 0 20px; line-height: 1.6; }
  #qr { min-height: 280px; display: flex; align-items: center; justify-content: center; }
  #qr svg { width: min(70vw, 280px); height: auto; }
  #state { font-size: 14px; color: #666; margin-top: 14px; min-height: 20px; }
  #state.ok { color: #0a7d32; font-weight: 600; font-size: 16px; }
  #state.err { color: #c0392b; }
  .count { color: #999; font-variant-numeric: tabular-nums; }
  .done-icon { font-size: 52px; line-height: 1; margin: 40px 0 12px; }
</style>
</head>
<body>
<div class="card">
  <h1>微信读书重新登录</h1>
  <p class="sub">用手机微信「扫一扫」扫描下方二维码<br>（只能摄像头扫，别用相册识别）</p>
  <div id="qr"><span style="color:#999">正在生成二维码…</span></div>
  <div id="state"></div>
</div>
<script>
(function () {
  var K = "__TOKEN__", TTL = __QR_TTL__;
  var uuid = null, left = 0, dead = false;
  var qrBox = document.getElementById("qr"), state = document.getElementById("state");

  function setState(text, cls) { state.textContent = text; state.className = cls || ""; }

  function newQr() {
    if (dead) return;
    fetch("/api/qr?k=" + K).then(function (r) { return r.json(); }).then(function (d) {
      if (dead) return;
      if (!d.ok) { setState(d.error || "生成失败，稍后自动重试…", "err"); setTimeout(newQr, 5000); return; }
      uuid = d.uuid; left = d.expires_in || TTL;
      qrBox.innerHTML = d.svg;
      setState("二维码有效 " + left + " 秒，过期自动刷新");
    }).catch(function () {
      if (!dead) { setState("网络中断，5 秒后重试…", "err"); setTimeout(newQr, 5000); }
    });
  }

  setInterval(function () {
    if (dead || !uuid) return;
    left -= 1;
    if (left <= 0) { uuid = null; newQr(); return; }
    setState("二维码有效 " + left + " 秒，过期自动刷新");
  }, 1000);

  function poll() {
    if (dead) return;
    if (!uuid) { setTimeout(poll, 2500); return; }
    fetch("/api/status?k=" + K + "&id=" + encodeURIComponent(uuid))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (dead) return;
        if (d.done) {
          dead = true;
          qrBox.innerHTML = '<div class="done-icon">✅</div>';
          setState("登录成功（" + (d.name || "账号") + "），日报会自动恢复，可以关掉这个页面了", "ok");
          return;
        }
        setTimeout(poll, 2500);
      })
      .catch(function () { setTimeout(poll, 4000); });
  }

  newQr();
  poll();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 服务

def _same(a, b):
    return bool(a) and bool(b) and secrets.compare_digest(str(a), str(b))


class Handler(BaseHTTPRequestHandler):
    server_version = "wewe-relogin/1.0"

    # 日志里绝不能出现 token：只打印去掉 query 的路径。
    def log_message(self, fmt, *args):  # noqa: N802
        path = self.path.split("?", 1)[0]
        log(f"{self.command} {path} -> {getattr(self, '_status', '?')}")

    def _send(self, status, content, content_type="application/json; charset=utf-8"):
        self._status = status
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _is_admin(self):
        # 管理接口只信 X-Admin-Token；经隧道进来的请求（带 Cf-* 头）一律拒绝，
        # 保证公网侧即使拿到 admin token 也无法操作隧道。
        if any(h.lower().startswith("cf-") for h in self.headers.keys()):
            return False
        return _same(self.headers.get("X-Admin-Token"), ADMIN_TOKEN)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/healthz":
            return self._json({"ok": True, "tunnel": TUNNEL.status()["running"]})

        if path == f"/r/{TOKEN}":
            page = PAGE_HTML.replace("__TOKEN__", TOKEN).replace("__QR_TTL__", str(QR_TTL_SEC))
            return self._send(200, page, "text/html; charset=utf-8")

        if path == "/api/qr":
            if not _same((query.get("k") or [""])[0], TOKEN):
                return self._json({"ok": False, "error": "unauthorized"}, 404)
            if not rate_limit_ok():
                return self._json({"ok": False, "error": "操作太频繁，稍等几秒"}, 429)
            try:
                uuid, scan_url = wewe_create_login_url()
                return self._json(
                    {"ok": True, "uuid": uuid, "svg": make_qr_svg(scan_url), "expires_in": QR_TTL_SEC}
                )
            except Exception as exc:
                log(f"生成二维码失败:{exc}")
                return self._json({"ok": False, "error": f"生成二维码失败：{exc}"})

        if path == "/api/status":
            if not _same((query.get("k") or [""])[0], TOKEN):
                return self._json({"ok": False, "error": "unauthorized"}, 404)
            uuid = (query.get("id") or [""])[0]
            if not uuid:
                return self._json({"ok": True, "done": False, "message": "no-uuid"})
            try:
                result = wewe_get_login_result(uuid)
            except Exception as exc:
                return self._json({"ok": True, "done": False, "message": f"query-error: {exc}"})
            vid, token = result.get("vid"), result.get("token")
            if vid and token:
                name = result.get("username") or ""
                try:
                    wewe_account_add(vid, token, name)
                except Exception as exc:
                    log(f"保存账号失败:{exc}")
                    return self._json({"ok": True, "done": False, "message": f"save-error: {exc}"})
                log(f"账号已恢复：{name or vid}")
                TUNNEL.schedule_stop(STOP_DELAY_SEC, "登录成功")
                return self._json({"ok": True, "done": True, "name": name or str(vid)})
            return self._json({"ok": True, "done": False, "message": result.get("message", "waiting")})

        if path == "/api/tunnel/status":
            if not self._is_admin():
                return self._json({"ok": False}, 404)
            return self._json({"ok": True, **TUNNEL.status()})

        return self._json({"ok": False}, 404)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/tunnel/start":
            if not self._is_admin():
                return self._json({"ok": False}, 404)
            try:
                base = TUNNEL.start()
                return self._json({"ok": True, "url": f"{base}/r/{TOKEN}"})
            except Exception as exc:
                log(f"隧道启动失败:{exc}")
                return self._json({"ok": False, "error": str(exc)}, 500)

        if path == "/api/tunnel/stop":
            if not self._is_admin():
                return self._json({"ok": False}, 404)
            TUNNEL.stop(reason="admin 请求")
            return self._json({"ok": True})

        return self._json({"ok": False}, 404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def shutdown(*_):
        log("收到退出信号，关闭隧道与服务 ...")
        TUNNEL.stop(reason="服务退出")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"wewe-relogin 已启动：http://127.0.0.1:{PORT}/r/<token>（WeWe：{WEWE_URL}）")
    server.serve_forever()
    log("已退出")


if __name__ == "__main__":
    main()
