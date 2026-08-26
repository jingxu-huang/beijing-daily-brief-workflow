#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeWe RSS preflight gate for the daily report.

The RSS endpoint can still return cached content after the WeRead account token
expires, so this script checks account state and actively refreshes one feed
before the daily report continues.
"""

import datetime as dt
import email.utils
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.getenv("WEWE_RSS_URL", "http://localhost:4000").rstrip("/")
AUTH_CODE = os.getenv("WEWE_AUTH_CODE", "")
DASH_URL = os.getenv("WEWE_RSS_DASH_URL", f"{BASE_URL}/dash/accounts")
POLL_SECONDS = int(os.getenv("WEWE_CHECK_INTERVAL_SECONDS", "60"))
NOTIFY_SECONDS = int(os.getenv("WEWE_NOTIFY_INTERVAL_MINUTES", "5")) * 60
MAX_WAIT_MINUTES = int(os.getenv("WEWE_MAX_WAIT_MINUTES", "360"))
SHOW_DIALOG = os.getenv("WEWE_SHOW_DIALOG", "1") != "0"
OPEN_DASH = os.getenv("WEWE_OPEN_DASH", "1") != "0"
ENABLE_NOTIFY = os.getenv("WEWE_ENABLE_NOTIFY", "1") != "0"
STALE_LEVEL = os.getenv("WEWE_STALE_LEVEL", "WARN").strip().upper()
ALL_FEED_URL = os.getenv("WEWE_ALL_FEED_URL", f"{BASE_URL}/feeds/all.rss")
RSS_READ_LIMIT_BYTES = int(os.getenv("WEWE_RSS_READ_LIMIT_BYTES", str(120 * 1024 * 1024)))
REFRESH_FEEDS = os.getenv("WEWE_REFRESH_FEEDS", "1") != "0"
REFRESH_MODE = os.getenv("WEWE_REFRESH_MODE", "all").strip().lower()


def _load_env_file(path):
    """Best-effort .env loader (KEY=VALUE), used to reuse feishu-bot 的配置。"""
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
_FEISHU_ENV = _load_env_file(
    os.path.join(_SCRIPT_DIR, "..", "local-only", "feishu-bot", "feishu_bot.env")
)


def _cfg(key, default=""):
    return os.getenv(key) or _FEISHU_ENV.get(key) or default


# 飞书推送（复用 feishu-bot 的应用与日报群）。
ENABLE_FEISHU = os.getenv("WEWE_FEISHU", "1") != "0"
# 账号失效属于关键告警：单独设置 WEWE_FEISHU=0 不得静默关闭。
# 只有隔离测试同时显式设置此开关，才允许跳过掉线链接告警。
ALLOW_SILENT_LOGIN_ALERT = os.getenv("WEWE_ALLOW_SILENT_LOGIN_ALERT", "0") == "1"
FEISHU_CHAT_ID = (
    _cfg("FEISHU_REPORT_CHAT_ID")
    or _cfg("FEISHU_CHAT_ID")
)
FEISHU_SEND_AS = _cfg("FEISHU_CLI_SEND_AS", "bot")


def find_lark_cli():
    configured = os.getenv("LARK_CLI")
    if configured:
        return configured
    found = shutil.which("lark-cli")
    if found:
        return found
    candidates = sorted(
        glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/lark-cli")),
        reverse=True,
    )
    if candidates:
        return candidates[0]
    return "/opt/homebrew/bin/lark-cli"


LARK_CLI_BIN = find_lark_cli()


# 远程重登服务（wewe_relogin_server.py + cloudflared 按需隧道）。
# 微信限制：登录二维码只能摄像头扫，掉线时给出一个公网重登页链接，
# 方便在外面借任意一块屏幕（办公电脑/平板）出码扫码。
_RELOGIN_ENV = _load_env_file(
    os.path.join(_SCRIPT_DIR, "..", "local-only", "relogin.env")
) or _load_env_file(
    os.path.expanduser("~/Library/Application Support/beijing-daily-brief/wewe-relogin/relogin.env")
)
RELOGIN_API = os.getenv("RELOGIN_API_URL") or (
    "http://127.0.0.1:" + _RELOGIN_ENV.get("RELOGIN_PORT", "4001")
)
RELOGIN_ADMIN_TOKEN = os.getenv("RELOGIN_ADMIN_TOKEN") or _RELOGIN_ENV.get(
    "RELOGIN_ADMIN_TOKEN", ""
)
RELOGIN_TOKEN = os.getenv("RELOGIN_TOKEN") or _RELOGIN_ENV.get("RELOGIN_TOKEN", "")
RELOGIN_PORT = _RELOGIN_ENV.get("RELOGIN_PORT", "4001")


def _relogin_api(path, timeout):
    request = urllib.request.Request(
        RELOGIN_API + path,
        data=b"",
        method="POST",
        headers={"X-Admin-Token": RELOGIN_ADMIN_TOKEN},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text) if text else {}


def request_remote_scan_url():
    """申请公网重登链接（启动 cloudflared 隧道）；失败返回空串，走原有文案。"""
    if not RELOGIN_ADMIN_TOKEN:
        return ""
    try:
        data = _relogin_api("/api/tunnel/start", timeout=75)
        url = data.get("url") or ""
        if url:
            log(f"WeWe RSS: 远程重登链接已就绪")
        return url
    except Exception as exc:
        log(f"WeWe RSS: 申请远程重登链接失败（不影响告警）：{exc}")
        return ""


def stop_remote_tunnel():
    """恢复后尽力关闭隧道（服务端登录成功时也会自动关，这里是兜底）。"""
    if not RELOGIN_ADMIN_TOKEN:
        return
    try:
        _relogin_api("/api/tunnel/stop", timeout=10)
    except Exception:
        pass


def remote_scan_line():
    url = request_remote_scan_url()
    if not url:
        return ""
    return (
        f"📱 不在电脑旁：找任意一台电脑/平板打开 {url} ，"
        "页面自动出码，用手机微信扫一扫即可（登录恢复后链接自动失效）。\n"
    )


def relogin_local_url():
    if not RELOGIN_TOKEN:
        return ""
    return f"http://127.0.0.1:{RELOGIN_PORT}/r/{RELOGIN_TOKEN}"


def open_relogin_page():
    """Open the local "add WeRead account" QR page when an auth alert fires."""
    if not OPEN_DASH:
        return
    url = relogin_local_url()
    if not url:
        log("WeWe RSS: 缺少 RELOGIN_TOKEN，无法自动打开本机扫码页")
        return
    try:
        subprocess.run(["open", url], check=False, timeout=10)
        log("WeWe RSS: 已自动打开本机扫码页")
    except Exception as exc:
        log(f"WeWe RSS: opening relogin page failed: {exc}")


def feishu_push(title, message, critical=False):
    """用 lark-cli 给日报群发消息；关键告警返回是否实际发送成功。"""
    enabled = ENABLE_FEISHU or (critical and not ALLOW_SILENT_LOGIN_ALERT)
    if not enabled or not FEISHU_CHAT_ID:
        if critical:
            log("WeWe RSS: 关键飞书告警被显式测试开关关闭")
        return False
    text = f"**{title}**\n{message}"
    cmd = [
        LARK_CLI_BIN, "im", "+messages-send",
        "--as", FEISHU_SEND_AS,
        "--chat-id", FEISHU_CHAT_ID,
        "--markdown", text,
    ]
    try:
        result = subprocess.run(cmd, check=False, timeout=60, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"WeWe RSS: 飞书推送失败（exit {result.returncode}）：{result.stderr.strip()[:300]}")
            return False
        message_id = ""
        try:
            payload = json.loads(result.stdout)
            data = payload.get("data") or payload
            message_id = data.get("message_id") or ""
        except (json.JSONDecodeError, AttributeError):
            pass
        log(
            "WeWe RSS: 飞书推送成功"
            + (f"（message_id={message_id}）" if message_id else "")
        )
        return True
    except Exception as exc:
        log(f"WeWe RSS: 飞书推送异常：{exc}")
        return False


def _dig_message_id(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "message_id" and isinstance(item, str) and item:
                return item
            found = _dig_message_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _dig_message_id(item)
            if found:
                return found
    return ""


def _collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _collect_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_strings(item)


def feishu_push_verified(title, message, idempotency_key, expected_text):
    """Send a fixed workflow reminder and verify it by message-id readback."""
    if not FEISHU_CHAT_ID:
        return {"verified": False, "error": "missing Feishu chat_id"}
    text = f"**{title}**\n{message}"
    command = [
        LARK_CLI_BIN,
        "im",
        "+messages-send",
        "--as",
        FEISHU_SEND_AS,
        "--chat-id",
        FEISHU_CHAT_ID,
        "--markdown",
        text,
        "--idempotency-key",
        idempotency_key,
        "--format",
        "json",
    ]
    try:
        sent = subprocess.run(command, check=False, timeout=60, capture_output=True, text=True)
        if sent.returncode != 0:
            return {"verified": False, "error": sent.stderr.strip()[:500]}
        payload = json.loads(sent.stdout)
        message_id = _dig_message_id(payload)
        if not message_id:
            return {"verified": False, "error": "send result has no message_id"}
        readback = subprocess.run(
            [
                LARK_CLI_BIN,
                "im",
                "+messages-mget",
                "--as",
                FEISHU_SEND_AS,
                "--message-ids",
                message_id,
                "--no-reactions",
                "--format",
                "json",
            ],
            check=False,
            timeout=45,
            capture_output=True,
            text=True,
        )
        if readback.returncode != 0:
            return {
                "verified": False,
                "message_id": message_id,
                "error": readback.stderr.strip()[:500],
            }
        try:
            returned = json.loads(readback.stdout)
            haystack = "\n".join(_collect_strings(returned))
        except json.JSONDecodeError:
            haystack = readback.stdout
        verified = expected_text in haystack
        return {
            "verified": verified,
            "message_id": message_id,
            "error": "" if verified else "readback did not contain the current public login URL",
        }
    except Exception as exc:  # noqa: BLE001
        return {"verified": False, "error": str(exc)[:500]}


def start_dialog_async(title, message):
    """Launch a GUI dialog without holding the guard or report pipeline."""
    script = (
        f"display dialog {osa_quote(message)} "
        'buttons {"知道了"} default button "知道了" '
        f"with title {osa_quote(title)} with icon caution giving up after 600"
    )
    try:
        process = subprocess.Popen(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"started": True, "pid": process.pid}
    except Exception as exc:  # noqa: BLE001
        return {"started": False, "error": str(exc)[:500]}


class WeweError(RuntimeError):
    pass


class WeweStaleWarning(RuntimeError):
    pass


def now_label():
    return dt.datetime.now().strftime("%F %T")


def log(message):
    print(f"[{now_label()}] {message}", flush=True)


def osa_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def run_osascript(script, timeout=20):
    if not ENABLE_NOTIFY:
        return
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=timeout)
    except Exception as exc:
        log(f"WeWe RSS: osascript notification failed: {exc}")


def notify(title, message):
    run_osascript(
        f"display notification {osa_quote(message)} with title {osa_quote(title)}",
        timeout=10,
    )


def open_dashboard():
    if not OPEN_DASH:
        return
    try:
        subprocess.run(["open", DASH_URL], check=False, timeout=10)
    except Exception as exc:
        log(f"WeWe RSS: opening dashboard failed: {exc}")


def alert_login_required(reason, first_alert=False):
    title = "WeWe RSS 需要重新登录"
    remote_line = remote_scan_line() if first_alert else ""
    message = (
        "日报任务已暂停。旧读书账号的 token 已不可用；不要只点“启用”旧账号。\n"
        "请使用自动打开的新增账号二维码，用手机微信扫一扫重新添加读书账号。\n\n"
        f"若未自动弹出二维码，可打开本机扫码页：{relogin_local_url() or '本机扫码页未配置'}\n"
        f"后台仅用于核对状态：{DASH_URL}\n"
        + remote_line
        + f"检测结果：{reason}\n\n"
        "扫码完成后不用重新运行任务；系统会继续检查账号、刷新公众号源，并在可用后继续生成当日日报。"
    )
    notify(title, message)
    if first_alert:
        open_relogin_page()
        feishu_push("⚠️ WeWe RSS 需要重新登录", message, critical=True)
    if SHOW_DIALOG:
        script = (
            f"display dialog {osa_quote(message)} "
            'buttons {"稍后提醒", "我已扫码"} default button "我已扫码" '
            f"with title {osa_quote(title)} with icon caution giving up after 900"
        )
        run_osascript(script, timeout=920)


def trpc_input(payload):
    raw = json.dumps({"json": payload}, separators=(",", ":"), ensure_ascii=False)
    return urllib.parse.urlencode({"input": raw})


def request_json(method, path, payload=None, timeout=45):
    body = None
    headers = {"authorization": AUTH_CODE}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"raw": text}
        return exc.code, data
    except Exception as exc:
        raise WeweError(f"无法连接 WeWe RSS：{exc}") from exc


def unwrap_trpc(data):
    if isinstance(data, list):
        if not data:
            raise WeweError("WeWe RSS 返回空响应")
        data = data[0]
    if "error" in data:
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise WeweError(message or "WeWe RSS tRPC error")
    return data.get("result", {}).get("data")


def query(path, payload=None, timeout=30):
    qs = "?" + trpc_input(payload or {})
    status, data = request_json("GET", path + qs, timeout=timeout)
    if status >= 400:
        raise WeweError(f"HTTP {status}: {data}")
    return unwrap_trpc(data)


def mutate(path, payload=None, timeout=45):
    status, data = request_json("POST", path, payload or {}, timeout=timeout)
    if status >= 400:
        raise WeweError(f"HTTP {status}: {data}")
    unwrap_trpc(data)


def read_text_url(url, timeout=45, limit=RSS_READ_LIMIT_BYTES):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except Exception as exc:
        raise WeweError(f"无法读取 WeWe RSS：{exc}") from exc
    if len(data) > limit:
        raise WeweError(f"WeWe RSS 过大，超过 {limit} bytes：{url}")
    return data.decode("utf-8", errors="replace")


def parse_rss_pubdate(raw):
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw.strip())
    except Exception:
        return None
    if parsed.tzinfo:
        return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def latest_item_pubdate_utc(xml):
    latest = None
    for raw in re_findall_pubdates(xml):
        published = parse_rss_pubdate(raw)
        if published and (latest is None or published > latest):
            latest = published
    return latest


def re_findall_pubdates(xml):
    import re

    return re.findall(r"<item\b.*?</item>", xml or "", flags=re.I | re.S) and re.findall(
        r"<pubDate>(.*?)</pubDate>", xml or "", flags=re.I | re.S
    )


def _parse_target_ymd(value):
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    return None


def current_bj_window():
    """与 fetch_news.py 保持一致：目标日报日的前日15:00至当日15:00。

    11:00/12:00/14:30 的守门器检查也必须检查“今天 15:00 即将关闭的日报窗口”，
    不能因为当前时间还早于 15:00 就回退到上一天窗口。
    """
    target_day = _parse_target_ymd(os.getenv("RIBAO_TARGET_YMD") or os.getenv("WEWE_TARGET_YMD"))
    if target_day:
        end_bj = dt.datetime.combine(target_day, dt.time(15, 0))
        return end_bj - dt.timedelta(days=1), end_bj

    trusted = os.getenv("RIBAO_TRUSTED_UTC", "").strip()
    if trusted:
        try:
            parsed = dt.datetime.fromisoformat(trusted.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            now_bj = parsed.astimezone(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
        except ValueError:
            now_bj = dt.datetime.utcnow() + dt.timedelta(hours=8)
    else:
        now_bj = dt.datetime.utcnow() + dt.timedelta(hours=8)
    end_bj = now_bj.replace(hour=15, minute=0, second=0, microsecond=0)
    if now_bj < end_bj:
        end_bj -= dt.timedelta(days=1)
    start_bj = end_bj - dt.timedelta(days=1)
    return start_bj, end_bj


def check_all_feed_freshness():
    xml = read_text_url(ALL_FEED_URL, timeout=60)
    latest_utc = latest_item_pubdate_utc(xml)
    start_bj, end_bj = current_bj_window()
    if not latest_utc:
        message = f"WeWe RSS all feed 无条目或无 pubDate：{ALL_FEED_URL}"
        if STALE_LEVEL == "FAIL":
            raise WeweError(message)
        raise WeweStaleWarning(message)
    latest_bj = latest_utc + dt.timedelta(hours=8)
    if latest_bj < start_bj:
        message = (
            "WeWe RSS all feed 新鲜度不足："
            f"最新条目 {latest_bj:%F %T} BJT，早于当前窗口起点 {start_bj:%F %T} BJT"
        )
        if STALE_LEVEL == "FAIL":
            raise WeweError(message)
        raise WeweStaleWarning(message)
    return latest_bj, start_bj, end_bj


def account_summary(accounts, blocks):
    parts = []
    status_name = {0: "失效", 1: "启用", 2: "禁用"}
    for account in accounts:
        status = account.get("status")
        blocked = "，今日小黑屋" if account.get("id") in blocks else ""
        parts.append(
            f"{account.get('name') or account.get('id')}({status_name.get(status, status)}{blocked})"
        )
    return "、".join(parts) if parts else "无账号"


def feed_name(feed):
    return feed.get("mpName") or feed.get("name") or feed.get("title") or feed.get("id")


def refresh_feeds(feeds):
    refreshed, failed = [], []
    for feed in feeds:
        name = feed_name(feed)
        try:
            mutate(
                "/trpc/feed.refreshArticles",
                {"mpId": feed["id"]},
                timeout=90,
            )
            refreshed.append(name)
        except WeweError as exc:
            failed.append(f"{name}: {exc}")
    return refreshed, failed


def check_once():
    account_data = query("/trpc/account.list", {"limit": 1000}, timeout=20)
    accounts = account_data.get("items", [])
    blocks = set(account_data.get("blocks") or [])
    enabled_accounts = [
        account
        for account in accounts
        if account.get("status") == 1 and account.get("id") not in blocks
    ]

    if not accounts:
        raise WeweError("WeWe RSS 没有微信读书账号，请添加账号并扫码登录")
    if not enabled_accounts:
        raise WeweError(f"暂无可用微信读书账号：{account_summary(accounts, blocks)}")

    feed_data = query("/trpc/feed.list", {"limit": 1000}, timeout=20)
    feeds = [feed for feed in feed_data.get("items", []) if feed.get("status") == 1]
    if not feeds:
        raise WeweError("WeWe RSS 没有启用的公众号源，请先订阅公众号")

    if REFRESH_FEEDS:
        selected_feeds = feeds[:1] if REFRESH_MODE == "single" else feeds
        refreshed, refresh_failed = refresh_feeds(selected_feeds)
        if refresh_failed:
            raise WeweError("WeWe RSS 刷新失败：" + "；".join(refresh_failed[:5]))
    else:
        refreshed = [feed_name(feed) for feed in feeds]

    # Re-read accounts because a 401 probe changes status to INVALID in WeWe RSS.
    account_data = query("/trpc/account.list", {"limit": 1000}, timeout=20)
    accounts = account_data.get("items", [])
    blocks = set(account_data.get("blocks") or [])
    enabled_accounts = [
        account
        for account in accounts
        if account.get("status") == 1 and account.get("id") not in blocks
    ]
    if not enabled_accounts:
        raise WeweError(f"探测后暂无可用微信读书账号：{account_summary(accounts, blocks)}")

    stale_warning = ""
    try:
        latest_bj, start_bj, end_bj = check_all_feed_freshness()
        freshness = (
            f"all.rss 最新条目 {latest_bj:%F %T} BJT；"
            f"当前窗口 {start_bj:%F %T} — {end_bj:%F %T} BJT"
        )
    except WeweStaleWarning as exc:
        stale_warning = str(exc)
        freshness = stale_warning

    names = "、".join(account.get("name") or account.get("id") for account in enabled_accounts)
    source_names = "、".join(refreshed[:8]) + (f" 等 {len(refreshed)} 个源" if len(refreshed) > 8 else "")
    status = "WARN" if stale_warning else "正常"
    action = "已验证并刷新" if REFRESH_MODE == "single" else ("已刷新" if REFRESH_FEEDS else "已检查")
    return f"WeWe RSS {status}：账号 {names}；{action} {len(refreshed)} 个源（{source_names}）；{freshness}"


def probe_health():
    """Return a non-blocking health classification for the Beijing guard."""
    try:
        message = check_once()
        status = "stale" if "WeWe RSS WARN" in message else "healthy"
        if status == "healthy":
            stop_remote_tunnel()
        return {"status": status, "reason": message}
    except WeweError as exc:
        reason = str(exc)
        auth_signals = (
            "暂无可用微信读书账号",
            "没有微信读书账号",
            "无可用读书账号",
            "账号失效",
            "账号：",
            "401",
            "Unauthorized",
            "UNAUTHORIZED",
        )
        status = "auth_required" if any(signal in reason for signal in auth_signals) else "service_down"
        return {"status": status, "reason": reason}


def send_fixed_guard_reminder(
    target_ymd,
    stage,
    reason,
    send_feishu=True,
    show_dialog=True,
    catchup=False,
):
    """Send the mandatory 11:00/12:00/14:30 WeWe alert on requested channels."""
    if stage not in {"1100", "1200", "1430"}:
        raise ValueError(f"unsupported fixed reminder stage: {stage}")
    login_signals = (
        "暂无可用微信读书账号",
        "没有微信读书账号",
        "无可用读书账号",
        "账号失效",
        "401",
        "Unauthorized",
        "UNAUTHORIZED",
    )
    needs_login = any(signal in reason for signal in login_signals)
    title = "WeWe RSS 需要重新登录" if needs_login else "WeWe RSS 全文源不可用"
    remote_url = request_remote_scan_url() if needs_login else ""
    delayed = "（延迟补提醒）" if catchup else ""
    stage_label = {
        "1100": "11:00 登录验证",
        "1200": "12:00 自动刷新失败",
        "1430": "14:30 最后一次自动刷新失败",
    }[stage]
    if needs_login and show_dialog:
        # The guard wakes every 30s. Only open the local QR page together with
        # the first local dialog for a reminder stage; otherwise a missing IM
        # chat_id or transient send failure can spawn dozens of tabs.
        open_relogin_page()
        action = (
            "旧读书账号 token 已不可用；不要只点“启用”旧账号。"
            f"请使用已自动打开的新增账号二维码扫码登录；不在电脑旁时打开公网扫码页："
            f"{remote_url or '公网扫码链接生成失败，系统会继续重试'}"
        )
    else:
        action = (
            "当前更像是 WeWe RSS 服务或 mp.weixin.qq.com 全文抓取链路不可用，"
            "不是微信读书账号失效；请先查看本机 WeWe 后台与容器日志，不需要立即扫码重登。"
        )
    message = "\n".join(
        [
            f"固定工作流 {stage_label}{delayed}：{title}。",
            action,
            f"本机扫码页：{relogin_local_url() or '本机扫码页未配置'}",
            f"后台仅用于核对状态：{DASH_URL}",
            f"检测结果：{reason}",
            "WeWe 仅为增强信息源；若北京时间 15:00 前仍未恢复，日报会排除 WeWe，继续使用其他渠道生成正式版。",
        ]
    )
    feishu_result = {"verified": True, "skipped": True}
    if send_feishu:
        if not FEISHU_CHAT_ID:
            # Current local-only mode writes the report link to the Feishu index
            # doc and intentionally leaves the report chat unset. A missing chat
            # must not make the fixed reminder stage fail and repeat forever.
            feishu_result = {"verified": True, "skipped": True, "error": "missing Feishu chat_id"}
        elif needs_login and remote_url:
            feishu_result = feishu_push_verified(
                f"⚠️ {title}（{stage_label}）",
                message,
                f"ribao-{target_ymd}-wewe-login-{stage}",
                remote_url,
            )
        elif not needs_login:
            sent = feishu_push(
                f"⚠️ {title}（{stage_label}）",
                message,
                critical=False,
            )
            feishu_result = {"verified": bool(sent) or not FEISHU_CHAT_ID, "skipped": not FEISHU_CHAT_ID}
        else:
            feishu_result = {"verified": False, "error": "public relogin URL unavailable"}
    dialog_result = {"started": True, "skipped": True}
    if show_dialog:
        dialog_result = start_dialog_async(title, message)
    return {
        "ok": bool(feishu_result.get("verified") and dialog_result.get("started")),
        "feishu_verified": bool(feishu_result.get("verified")),
        "message_id": feishu_result.get("message_id", ""),
        "feishu_error": feishu_result.get("error", ""),
        "dialog_started": bool(dialog_result.get("started")),
        "dialog_pid": dialog_result.get("pid"),
        "dialog_error": dialog_result.get("error", ""),
        "remote_url": remote_url,
    }


def precheck_once():
    """日报执行前的轻量预检：只查一次，失效就推飞书并打开后台，不阻塞、不轮询。

    退出码：0=正常；3=账号失效需重登；4=无法连接 WeWe RSS。
    兼容旧的手动单次预检；正式定时动作由北京时间守门器在 11:00/12:00/14:30 固定执行。
    """
    log("WeWe RSS 预检（单次，日报前）：检查账号 token、刷新全部启用源并校验 all.rss 新鲜度 ...")
    try:
        message = check_once()
        log(message)
        stop_remote_tunnel()  # 若掉线期间开过隧道、后来在后台手动扫码恢复，这里顺手关掉
        if "WeWe RSS WARN" in message:
            feishu_push(
                "⚠️ WeWe RSS 新鲜度告警（日报前预检）",
                message,
            )
        return 0
    except WeweError as exc:
        reason = str(exc)
        log(f"WeWe RSS unavailable: {reason}")
        notify("WeWe RSS 需要重新登录", reason)
        open_relogin_page()
        remote_line = remote_scan_line()
        sent = feishu_push(
            "⚠️ WeWe RSS 需要重新登录（日报前预检）",
            "微信读书账号已失效；不要只点“启用”旧账号，请用新增账号二维码重新扫码登录。\n"
            "若北京时间 15:00 前未恢复，今天的正式日报会排除 WeWe 并继续生成。\n"
            f"本机扫码页：{relogin_local_url() or '本机扫码页未配置'}\n"
            f"后台仅用于核对状态：{DASH_URL}\n"
            + remote_line
            + f"检测结果：{reason}\n"
            "（重登后无需手动操作；守门器会在 15:00 做最终判断。）",
            critical=True,
        )
        if not remote_line:
            log("WeWe RSS: CRITICAL：未能生成公网重登链接")
            return 5
        if not sent:
            log("WeWe RSS: CRITICAL：公网重登链接未能发送到飞书")
            return 5
        # 连接不上 WeWe RSS 与账号失效区分一下退出码，便于排查。
        return 4 if reason.startswith("无法连接 WeWe RSS") else 3


def main():
    log("WeWe RSS preflight: checking account token, refreshing all feeds and all.rss freshness...")
    max_wait_seconds = None if MAX_WAIT_MINUTES <= 0 else MAX_WAIT_MINUTES * 60
    started = time.time()
    last_alert = 0
    first_alert = True
    last_reason = ""

    while True:
        try:
            message = check_once()
            log(message)
            if "WeWe RSS WARN" in message:
                feishu_push("⚠️ WeWe RSS 新鲜度告警", message)
            if last_reason:
                notify("WeWe RSS 已恢复", "微信读书账号检查通过，正在继续生成当日日报。")
                feishu_push("✅ WeWe RSS 已恢复", "微信读书账号检查通过，正在继续生成当日日报。")
            stop_remote_tunnel()
            return 0
        except WeweError as exc:
            reason = str(exc)
            last_reason = reason
            elapsed = int(time.time() - started)
            if max_wait_seconds is not None and elapsed >= max_wait_seconds:
                final_msg = (
                    f"WeWe RSS 在 {MAX_WAIT_MINUTES} 分钟内没有恢复，今日日报未生成。"
                    f"最后错误：{reason}"
                )
                log(final_msg)
                notify("资讯日报未生成", final_msg)
                feishu_push("❌ 资讯日报未生成", final_msg)
                return 2

            log(f"WeWe RSS unavailable: {reason}")
            now = time.time()
            if first_alert or now - last_alert >= NOTIFY_SECONDS:
                alert_login_required(reason, first_alert=first_alert)
                last_alert = time.time()
                first_alert = False
            time.sleep(max(5, POLL_SECONDS))


if __name__ == "__main__":
    if "--once" in sys.argv or os.getenv("WEWE_ONCE") == "1":
        sys.exit(precheck_once())
    sys.exit(main())
