#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send a finished daily report to Feishu with lark-cli.

Flow (mode=new, 当天首发):
1. import the Markdown report as a Feishu Docx with user identity;
2. record {ymd: doc_token/url} into 日报状态/feishu-docs.json (registry);
3. grant the daily report chat full-access (manage) permission on the Docx;
4. send the Docx link to the chat with the configured IM identity.

Flow (mode=update, 当天已发过 —— 默认行为，2026-07-08 阶段1加固):
1. look up doc_token in the registry;
2. `docs +update --command overwrite` 原地覆盖同一篇 Docx（URL 不变）;
3. re-verify chat permission; do NOT send a new chat message.

注册表先写、群消息后发：即使发消息失败，重跑也会走同链更新，
从结构上杜绝"同一天重复导入新 Docx / 重复发链接"。
"""

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_ENV = SCRIPT_DIR / "feishu_bot.env"
DATA_ROOT = Path(os.environ.get("RIBAO_DATA_ROOT") or PROJECT_ROOT)
STATE_DIR = Path(os.environ.get("RIBAO_STATE_DIR") or (DATA_ROOT / "日报状态"))
# Test matrix workers keep their manifests isolated per profile, but must use
# one registry so the single index writer can see all three document URLs.
# Production remains byte-for-byte on the default STATE_DIR path.
REGISTRY_DIR = Path(os.environ.get("RIBAO_FEISHU_REGISTRY_DIR") or STATE_DIR)
REGISTRY_PATH = REGISTRY_DIR / "feishu-docs.json"
MANIFEST_HELPER = PROJECT_ROOT / "资讯日报-skill" / "manifest.py"
STAGING_DIR = SCRIPT_DIR / ".staging"
REMOTE_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<)?[^)\n]+(?:>)?\)")
LOCAL_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<)?(@\./[^)>\n]+)(?:>)?\)")
RAW_URL_RE = re.compile(r"(?m)^(https?://\S+)\s*$")
QA_SKIP_COMMENT_RE = re.compile(r"<!--\s*QA_SKIP\b.*?-->", re.IGNORECASE | re.DOTALL)
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
IMAGE_SIGNATURES = {
    "image/png": lambda payload: payload.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda payload: payload.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda payload: payload.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda payload: (
        len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    ),
    "image/bmp": lambda payload: payload.startswith(b"BM"),
}


def detect_image_content_type(payload):
    for content_type, signature in IMAGE_SIGNATURES.items():
        if signature(payload):
            return content_type
    return ""


def load_registry():
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[feishu-cli] WARN: 注册表读取失败（{exc}），按空处理", file=sys.stderr)
        return {}


def save_registry(reg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def update_report_in_place(doc_token, report_path, update_as="user", dry_run=False):
    """docs +update overwrite：原地覆盖同一篇 Docx，URL 与群里已发的链接保持不变。"""
    return run_lark(
        [
            "docs",
            "+update",
            "--doc",
            doc_token,
            "--command",
            "overwrite",
            "--content",
            "@" + project_relative(report_path),
            "--doc-format",
            "markdown",
            "--as",
            update_as,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def _doc_image_tokens(doc_token, fetch_as="user", dry_run=False):
    if dry_run:
        return []
    data = run_lark(
        [
            "docs",
            "+fetch",
            "--doc",
            doc_token,
            "--as",
            fetch_as,
            "--doc-format",
            "xml",
            "--detail",
            "full",
            "--format",
            "json",
        ]
    )
    content = dig(data, "data", "document", "content") or ""
    tokens = []
    for tag in re.findall(r"<img\b[^>]*>", content):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        token = html.unescape(attrs.get("src") or attrs.get("token") or "").strip()
        if token:
            tokens.append(token)
    return tokens


def _plain_xml_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _markdown_source_rows(markdown):
    marker = re.search(r"(?m)^Source 来源：\s*$", markdown)
    if not marker:
        raise RuntimeError("本地成稿缺少 Source 来源表")
    rows = []
    for line in markdown[marker.end() :].splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"来源", "---"} or set(cells[0]) <= {"-", ":"}:
            continue
        if not cells[1].isdigit():
            continue
        rows.append((cells[0], int(cells[1])))
    if not rows:
        raise RuntimeError("本地成稿 Source 来源表没有有效数据行")
    return rows


def _xml_source_rows(content):
    for table in re.findall(r"<table\b.*?</table>", content or "", re.DOTALL | re.IGNORECASE):
        if "本期采用条数" not in _plain_xml_text(table):
            continue
        rows = []
        for row in re.findall(r"<tr\b.*?</tr>", table, re.DOTALL | re.IGNORECASE):
            cells = [
                _plain_xml_text(cell)
                for cell in re.findall(
                    r"<t[dh]\b[^>]*>(.*?)</t[dh]>",
                    row,
                    re.DOTALL | re.IGNORECASE,
                )
            ]
            if len(cells) >= 2 and cells[0] != "来源" and cells[1].isdigit():
                rows.append((cells[0], int(cells[1])))
        if rows:
            return rows
    return []


def _markdown_visible_text(value):
    """Match the visible text after Feishu renders Markdown escapes."""
    return re.sub(r"\\([\\`*_{}\[\]()<>#+.!|~\-])", r"\1", value or "")


def verify_report_content_readback(doc_token, report_path, verify_as="user", dry_run=False):
    """Test-only hard gate for visible report text and exact source-table identity."""
    if dry_run:
        return True
    markdown = Path(report_path).read_text(encoding="utf-8")
    expected_titles = [
        _markdown_visible_text(title.strip())
        for title in re.findall(r"(?m)^###\s+\d+[）)]\s*(.+?)\s*$", markdown)
    ]
    if not expected_titles:
        raise RuntimeError("本地成稿没有可回读核验的资讯标题")
    expected_sources = _markdown_source_rows(markdown)
    if sum(count for _source, count in expected_sources) != len(expected_titles):
        raise RuntimeError("本地成稿 Source 来源表合计与正文条数不一致")

    data = run_lark(
        [
            "docs",
            "+fetch",
            "--doc",
            doc_token,
            "--as",
            verify_as,
            "--doc-format",
            "xml",
            "--detail",
            "full",
            "--format",
            "json",
        ]
    )
    content = dig(data, "data", "document", "content") or ""
    if not content:
        raise RuntimeError("飞书回读成功但文档正文为空")
    visible_text = _plain_xml_text(content)
    if "QA_SKIP" in visible_text or "QA_SKIP" in content:
        raise RuntimeError("飞书文档仍包含内部 QA_SKIP 标记")
    missing_titles = [title for title in expected_titles if title not in visible_text]
    if missing_titles:
        raise RuntimeError(f"飞书正文缺少 {len(missing_titles)} 条资讯标题")
    actual_sources = _xml_source_rows(content)
    if actual_sources != expected_sources:
        raise RuntimeError("飞书 Source 来源表与本地成稿不一致")
    return True


def _preview_image_token(token, output_stem, preview_as="user", dry_run=False):
    if dry_run:
        return True
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    run_lark(
        [
            "docs",
            "+media-preview",
            "--token",
            token,
            "--output",
            project_relative(output_stem),
            "--overwrite",
            "--as",
            preview_as,
            "--format",
            "json",
        ]
    )
    candidates = [
        path
        for path in output_stem.parent.glob(output_stem.name + "*")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not candidates:
        raise RuntimeError("飞书图片预览成功但未生成非空本地文件")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def verify_report_images(doc_token, report_path, ymd, verify_as="user", dry_run=False):
    """Hard-gate delivery on Feishu images that exactly match staged originals."""
    report_text = Path(report_path).read_text(encoding="utf-8")
    expected = len(MARKDOWN_IMAGE_RE.findall(report_text))
    if dry_run or expected == 0:
        return True
    local_refs = LOCAL_IMAGE_RE.findall(report_text)
    if len(local_refs) != expected:
        raise RuntimeError("飞书投递前仍有图片未转换为本地上传引用")
    tokens = _doc_image_tokens(doc_token, fetch_as=verify_as)
    if len(tokens) != expected:
        raise RuntimeError(f"飞书图片块数量不符：期望 {expected}，实际 {len(tokens)}")
    staging_namespace = os.environ.get("FEISHU_STAGING_NAMESPACE", "").strip() or (ymd or "unknown")
    verify_dir = STAGING_DIR / staging_namespace / "verified-media"
    for ordinal, (token, local_ref) in enumerate(zip(tokens, local_refs), 1):
        source = (PROJECT_ROOT / local_ref[1:]).resolve()
        try:
            source.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise RuntimeError("本地图片引用越出日报项目目录") from exc
        if not source.is_file() or source.stat().st_size == 0:
            raise RuntimeError(f"本地原图不存在或为空（第 {ordinal} 张）")
        preview = _preview_image_token(
            token,
            verify_dir / f"image-{ordinal:02d}",
            preview_as=verify_as,
        )
        source_hash = hashlib.sha256(source.read_bytes()).digest()
        preview_hash = hashlib.sha256(preview.read_bytes()).digest()
        if source_hash != preview_hash:
            raise RuntimeError(f"飞书图片内容与本地原图不一致（第 {ordinal} 张）")
    return True


def record_manifest(ymd, kvs):
    """把发布结果写进 run-manifest（尽力而为，失败不影响发送流程）。"""
    if not MANIFEST_HELPER.exists():
        return
    try:
        cmd = [sys.executable, str(MANIFEST_HELPER), "--date", ymd, "--state-dir", str(STATE_DIR)]
        for k, v in kvs.items():
            cmd += ["--kv", f"{k}={v}"]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


def load_env_file(path):
    path = Path(path)
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def check_verdict(check_path):
    text = read_text(check_path)
    m = re.search(r"结论：\s*(PASS|WARN|FAIL)", text)
    return m.group(1) if m else ""


def check_sendable(check_path):
    return check_verdict(check_path) in ("PASS", "WARN")


def title_from_path(report_path):
    explicit = os.environ.get("FEISHU_REPORT_TITLE", "").strip()
    if explicit:
        return explicit
    stem = Path(report_path).stem
    m = re.search(r"(\d{1,2}\.\d{1,2})", stem)
    title = f"资讯类简报-{m.group(1)}日" if m else stem
    suffix = os.environ.get("FEISHU_REPORT_TITLE_SUFFIX", "").strip()
    return f"{title}{suffix}"


def registry_key_for(ymd):
    key = os.environ.get("FEISHU_REGISTRY_KEY", "").strip() or ymd
    if not re.fullmatch(r"\d{8}(?:-test-[A-Za-z0-9._-]{1,32})?", key):
        raise RuntimeError("飞书日报注册键格式无效")
    return key


def ymd_from_path(report_path):
    stem = Path(report_path).stem
    m = re.search(r"(\d{1,2})\.(\d{1,2})", stem)
    if not m:
        return time.strftime("%Y%m%d")
    year = time.strftime("%Y")
    return f"{year}{int(m.group(1)):02d}{int(m.group(2)):02d}"


def draft_path_for_report(report_path, ymd=""):
    ymd = ymd or ymd_from_path(report_path)
    return DATA_ROOT / "日报草稿" / f"资讯日报-草稿-{ymd}.md"


def wewe_warning_from_draft(report_path, ymd=""):
    draft = draft_path_for_report(report_path, ymd)
    if not draft.exists():
        return ""
    for raw in draft.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if "WeWe RSS unavailable" in line:
            return line.lstrip("- ").strip()
        if line.startswith("- 微信公众号(WeWe RSS):") or line.startswith("微信公众号(WeWe RSS):"):
            return line.lstrip("- ").strip()
    return ""


def project_relative(path):
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RuntimeError(f"文件必须位于日报项目目录内：{resolved}") from exc
    return "./" + rel.as_posix()


def _assert_public_image_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("图片 URL 必须是公开的 HTTP(S) 地址")
    if parsed.username or parsed.password:
        raise RuntimeError("图片 URL 不允许包含 userinfo")

    host = parsed.hostname.rstrip(".")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            raise RuntimeError(f"图片域名解析失败：{host}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise RuntimeError(f"图片 URL 不允许访问非公网地址：{host}")


class _PublicImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _article_referer_after(markdown, image_end):
    tail = markdown[image_end:]
    next_heading = re.search(r"(?m)^###\s+", tail)
    if next_heading:
        tail = tail[: next_heading.start()]
    match = RAW_URL_RE.search(tail)
    if not match:
        return ""
    referer = match.group(1).strip()
    _assert_public_image_url(referer)
    return referer


def _download_remote_image(url, referer, assets_dir):
    _assert_public_image_url(url)
    headers = {
        "User-Agent": IMAGE_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_PublicImageRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            final_url = response.geturl()
            _assert_public_image_url(final_url)
            declared_content_type = response.headers.get_content_type().lower()
            declared_size = response.headers.get("Content-Length", "").strip()
            if declared_size.isdigit() and int(declared_size) > IMAGE_MAX_BYTES:
                raise RuntimeError("图片超过 20MB 上限")
            chunks = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, IMAGE_MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > IMAGE_MAX_BYTES:
                    raise RuntimeError("图片超过 20MB 上限")
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        host = urllib.parse.urlsplit(url).hostname or "unknown"
        raise RuntimeError(f"图片下载失败（{host}）：{exc}") from exc

    payload = b"".join(chunks)
    if not payload:
        raise RuntimeError("图片下载结果为空")
    content_type = detect_image_content_type(payload)
    if not content_type:
        raise RuntimeError(f"图片内容不是支持的图像格式（声明：{declared_content_type}）")
    extension = IMAGE_EXTENSIONS[content_type]
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20] + extension
    target = assets_dir / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return target


def _image_url_candidates(url):
    """Return a short, deterministic fallback list without changing article content."""
    candidates = [url]
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"baijingapp.com", "www.baijingapp.com"}:
        normalized = urllib.parse.urlunsplit(
            (parsed.scheme or "https", "www.baijing.cn", parsed.path, parsed.query, "")
        )
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _localize_report_images(markdown, assets_dir):
    matches = list(REMOTE_IMAGE_RE.finditer(markdown))
    if not matches:
        return markdown, []
    replacements = []
    warnings = []
    for match in matches:
        alt, url = match.group(1), match.group(2)
        local_image = None
        try:
            referer = _article_referer_after(markdown, match.end())
        except RuntimeError:
            referer = ""
        last_error = None
        for candidate_url in _image_url_candidates(url):
            try:
                local_image = _download_remote_image(candidate_url, referer, assets_dir)
                break
            except Exception as exc:
                last_error = exc
        if local_image is not None:
            local_ref = "@" + project_relative(local_image)
            replacement = f"![{alt}]({local_ref})"
        else:
            parsed = urllib.parse.urlsplit(url)
            warnings.append(
                {
                    "host": parsed.hostname or "unknown",
                    "action": "image_omitted_text_preserved",
                    "attempts": len(_image_url_candidates(url)),
                    "error_category": type(last_error).__name__ if last_error else "unknown",
                }
            )
            replacement = ""
        replacements.append((match.start(), match.end(), replacement))
    for start, end, replacement in reversed(replacements):
        markdown = markdown[:start] + replacement + markdown[end:]
    return markdown, warnings


def stage_report_for_lark(report_path, ymd, localize_images=True):
    """Stage reports locally and upload images instead of relying on remote hotlinks."""
    resolved = Path(report_path).resolve()
    if not resolved.exists():
        raise RuntimeError(f"成稿文件不存在：{resolved}")
    source_markdown = resolved.read_text(encoding="utf-8")
    markdown = QA_SKIP_COMMENT_RE.sub("", source_markdown)
    if "QA_SKIP" in markdown:
        raise RuntimeError("飞书发布副本仍包含内部 QA_SKIP 标记")
    has_remote_images = bool(REMOTE_IMAGE_RE.search(markdown))
    try:
        resolved.relative_to(PROJECT_ROOT)
        inside_project = True
    except ValueError:
        inside_project = False
    if inside_project and markdown == source_markdown and not (localize_images and has_remote_images):
        return str(resolved)
    # Parallel test profiles may share one calendar date. Their local image
    # staging must still be isolated or one profile can overwrite another
    # report with the same filename before lark-cli uploads it.
    staging_namespace = os.environ.get("FEISHU_STAGING_NAMESPACE", "").strip() or (ymd or "unknown")
    target_dir = STAGING_DIR / staging_namespace
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / resolved.name
    target.write_text(markdown, encoding="utf-8")
    if localize_images and has_remote_images:
        localized, warnings = _localize_report_images(markdown, target_dir / "assets")
        target.write_text(localized, encoding="utf-8")
        warning_path = target_dir / "image-warnings.json"
        if warnings:
            warning_path.write_text(
                json.dumps({"ymd": ymd, "warnings": warnings}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            warning_path.unlink(missing_ok=True)
    return str(target)


def parse_json_output(stdout):
    text = stdout.strip()
    if not text:
        raise RuntimeError("lark-cli 未返回 JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def summarize_completed(cmd, result):
    sensitive_values = {"--token", "--chat-id", "--data"}
    safe_cmd = []
    redact_next = False
    for part in cmd:
        if redact_next:
            safe_cmd.append("***")
            redact_next = False
            continue
        safe_cmd.append(part)
        if part in sensitive_values:
            redact_next = True
    print("[feishu-cli] " + " ".join(safe_cmd))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def run_lark(args, dry_run=False):
    cmd = ["lark-cli", *args]
    if dry_run:
        print("[feishu-cli] dry-run:", " ".join(cmd))
        return {"ok": True, "dry_run": True}
    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        summarize_completed(cmd, result)
        raise RuntimeError(f"lark-cli 执行失败，退出码 {result.returncode}")
    data = parse_json_output(result.stdout)
    if data.get("ok") is False:
        raise RuntimeError("lark-cli 返回失败：" + json.dumps(data, ensure_ascii=False)[:800])
    return data


def dig(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from collect_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from collect_strings(child)


def message_id_from_result(message, dry_run=False):
    message_id = dig(message, "data", "message_id") or message.get("message_id")
    if not message_id and dry_run:
        return "dry-run"
    if not message_id:
        raise RuntimeError("飞书发送返回成功但没有 message_id，不能判定日报已送达")
    return message_id


def verify_chat_message(message_id, doc_url, send_as, dry_run=False):
    """Read back the URL message; only this verification permits final=sent."""
    if dry_run:
        return True
    result = run_lark(
        [
            "im",
            "+messages-mget",
            "--as",
            send_as,
            "--message-ids",
            message_id,
            "--no-reactions",
            "--format",
            "json",
        ]
    )
    haystack = "\n".join(collect_strings(result))
    if doc_url not in haystack:
        raise RuntimeError(f"飞书消息回读未找到当日文档 URL（message_id={message_id}）")
    return True


def find_string_by_key(obj, key):
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        for child in obj.values():
            found = find_string_by_key(child, key)
            if found:
                return found
    if isinstance(obj, list):
        for child in obj:
            found = find_string_by_key(child, key)
            if found:
                return found
    return ""


def imported_doc(data):
    token = (
        dig(data, "data", "token")
        or dig(data, "data", "result", "token")
        or dig(data, "result", "token")
        or data.get("token")
        or find_string_by_key(data, "token")
    )
    url = (
        dig(data, "data", "url")
        or dig(data, "data", "result", "url")
        or dig(data, "result", "url")
        or data.get("url")
        or find_string_by_key(data, "url")
    )
    ticket = (
        dig(data, "data", "ticket")
        or dig(data, "ticket")
        or find_string_by_key(data, "ticket")
    )
    return token, url, ticket


def created_doc(data):
    document = dig(data, "data", "document") or {}
    token = document.get("document_id", "")
    url = document.get("url", "")
    return token, url


def import_report(report_path, title, import_as="user", dry_run=False):
    data = run_lark(
        [
            "docs",
            "+create",
            "--content",
            "@" + project_relative(report_path),
            "--doc-format",
            "markdown",
            "--title",
            title,
            "--as",
            import_as,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )
    if dry_run:
        return "dry-run-doc-token", "https://example.com/docx/dry-run"
    token, url = created_doc(data)
    if not token or not url:
        raise RuntimeError("飞书创建结果缺少 document_id/url")
    return token, url


def validate_group_permission(perm):
    """Keep production at full_access while allowing test docs to be editable."""
    if perm == "full_access":
        return
    if os.environ.get("RIBAO_TEST_MODE", "0") == "1" and perm == "edit":
        return
    raise RuntimeError("正式日报群成员必须可管理；测试日报仅允许 edit")


def grant_chat_permission(doc_token, chat_id, perm, permission_as="user", dry_run=False):
    """Grant the configured report group a collaborator role on the Docx.

    Production delivery requires ``full_access``.  Explicit test-mode documents
    use ``edit`` so the group can review them without receiving manage rights.
    """
    validate_group_permission(perm)
    payload = json.dumps(
        {
            "member_type": "openchat",
            "member_id": chat_id,
            "perm": perm,
            "type": "chat",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return run_lark(
        [
            "drive",
            "permission.members",
            "create",
            "--token",
            doc_token,
            "--type",
            "docx",
            "--data",
            payload,
            "--as",
            permission_as,
            "--yes",
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def send_chat_message(chat_id, doc_url, send_as, idempotency_key, dry_run=False):
    return run_lark(
        [
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--as",
            send_as,
            "--text",
            doc_url,
            "--idempotency-key",
            idempotency_key,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def send_wewe_warning(chat_id, warning, send_as, idempotency_key, dry_run=False):
    if not warning:
        return {}
    return run_lark(
        [
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--as",
            send_as,
            "--text",
            f"WeWe RSS 公众号源本次未正常抓取：{warning}。本期日报会继续发送，但公众号源可能缺失。",
            "--idempotency-key",
            idempotency_key,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def with_idempotency_suffix(key, suffix):
    suffix = (suffix or "").strip()
    return f"{key}-{suffix}" if suffix else key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, help="生成后的日报 Markdown")
    parser.add_argument("--check", required=True, help="质检报告 Markdown")
    parser.add_argument("--env", default=str(DEFAULT_ENV), help="可选 env 文件")
    parser.add_argument("--chat-id", default="", help="日报接收群 chat_id")
    parser.add_argument("--perm", default="", help="授予日报接收群的文档权限，生产要求 full_access（可管理）")
    parser.add_argument("--send-as", default="", choices=("", "bot", "user"), help="IM 发送身份，默认 bot")
    parser.add_argument("--import-as", default="", choices=("", "bot", "user"), help="文档导入身份，默认 user")
    parser.add_argument("--permission-as", default="", choices=("", "bot", "user"), help="文档授权身份，默认跟随导入身份")
    parser.add_argument("--allow-fail", action="store_true", help="即使质检 FAIL 也发送")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 lark-cli 命令")
    parser.add_argument("--doc-only", action="store_true", help="只导入/更新飞书 Docx 并授予目标群可管理权限，不发送 IM 消息")
    parser.add_argument("--grant-only", action="store_true", help="仅为已登记的当日日报补授目标群可管理权限，不导入或改写文档")
    parser.add_argument(
        "--skip-permission",
        action="store_true",
        help="仅测试模式可用：写入独立测试文档但不授予任何群权限",
    )
    parser.add_argument("--date", default="", help="目标日期 YYYYMMDD；默认从成稿文件名推断（会猜年份，wrapper 应显式传入）")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=("auto", "new", "update"),
        help="auto=注册表有当天记录则同链更新、否则首发（默认）；new=强制新建 Docx 并发群消息（会覆盖注册表记录，旧链接不再被更新）；update=必须已有记录，原地更新不发消息",
    )
    args = parser.parse_args()

    load_env_file(args.env)

    if not shutil.which("lark-cli"):
        raise RuntimeError("找不到 lark-cli，请确认 @larksuite/cli 已安装并在 PATH 中")

    verdict = check_verdict(args.check)
    if not args.allow_fail and not check_sendable(args.check):
        print(f"[feishu-cli] 质检未通过发送门槛（结论：{verdict or '未知'}），跳过发送。")
        return 0

    chat_id = (
        args.chat_id
        or os.environ.get("FEISHU_REPORT_CHAT_ID", "").strip()
        or os.environ.get("FEISHU_CHAT_ID", "").strip()
    )
    perm = (
        args.perm
        or os.environ.get("FEISHU_REPORT_CHAT_PERM", "").strip()
        or "full_access"
    )
    send_as = (
        args.send_as
        or os.environ.get("FEISHU_CLI_SEND_AS", "").strip()
        or "bot"
    )
    import_as = (
        args.import_as
        or os.environ.get("FEISHU_CLI_IMPORT_AS", "").strip()
        or "user"
    )
    permission_as = (
        args.permission_as
        or os.environ.get("FEISHU_CLI_PERMISSION_AS", "").strip()
        or import_as
    )

    skip_permission = args.skip_permission or (
        os.environ.get("FEISHU_TEST_SKIP_PERMISSION", "0") == "1"
    )
    if skip_permission:
        if os.environ.get("RIBAO_TEST_MODE", "0") != "1":
            raise RuntimeError("跳过群权限只允许在显式测试模式使用")
        if not args.doc_only:
            raise RuntimeError("跳过群权限的测试发布必须使用 doc-only，禁止发送群消息")
        if import_as != "user":
            raise RuntimeError("无群权限测试文档必须使用用户身份导入")
        if args.grant_only:
            raise RuntimeError("skip-permission 与 grant-only 不能同时使用")
        chat_id = ""
    else:
        if not chat_id:
            raise RuntimeError("缺少日报接收群 chat_id")
        validate_group_permission(perm)

    title = title_from_path(args.report)
    ymd = args.date.strip() or ymd_from_path(args.report)
    registry_key = registry_key_for(ymd)
    lark_report = stage_report_for_lark(args.report, ymd, localize_images=not args.dry_run)
    idempotency_suffix = os.environ.get("FEISHU_IDEMPOTENCY_SUFFIX", "").strip()

    registry = load_registry()
    entry = dict(registry.get(registry_key) or {})
    if args.grant_only:
        doc_token = entry.get("doc_token", "")
        if not doc_token:
            raise RuntimeError(f"注册表 {REGISTRY_PATH.name} 中没有 {registry_key} 的 doc_token，无法补授权限")
        grant_chat_permission(doc_token, chat_id, perm, permission_as=permission_as, dry_run=args.dry_run)
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["group_permission"] = perm
        if not args.dry_run:
            registry[registry_key] = entry
            save_registry(registry)
        print(json.dumps({"ok": True, "ymd": ymd, "group_permission": perm, "grant_only": True}, ensure_ascii=False))
        return 0
    mode = args.mode
    if mode == "auto":
        mode = "update" if entry.get("doc_token") else "new"
    if mode == "update" and not entry.get("doc_token"):
        raise RuntimeError(
            f"注册表 {REGISTRY_PATH.name} 中没有 {registry_key} 的 doc_token，无法同链更新；首发请用 --mode new 或 auto"
        )

    if mode == "update":
        doc_token = entry["doc_token"]
        doc_url = entry.get("url", "")
        print(f"[feishu-cli] {ymd} 已有当日文档，原地更新：{doc_url or doc_token}")
        update_report_in_place(doc_token, lark_report, update_as=import_as, dry_run=args.dry_run)
        verify_report_images(
            doc_token,
            lark_report,
            ymd,
            verify_as=import_as,
            dry_run=args.dry_run,
        )
        if os.environ.get("RIBAO_TEST_MODE", "0") == "1":
            verify_report_content_readback(
                doc_token,
                lark_report,
                verify_as=import_as,
                dry_run=args.dry_run,
            )
        if not skip_permission:
            grant_chat_permission(doc_token, chat_id, perm, permission_as=permission_as, dry_run=args.dry_run)
        if args.doc_only:
            message_id = entry.get("message_id", "")
        else:
            message_id = entry.get("message_id", "")
        # 正常同链更新不重复发群消息；若首发曾卡在“文档已登记、URL 消息未成功”之间，
        # 必须用同一幂等键补发，不能把只有文档、没有群消息的状态误判为已送达。
        if not args.doc_only and not message_id:
            message = send_chat_message(
                chat_id,
                doc_url,
                send_as,
                with_idempotency_suffix(f"ribao-{ymd}-cli-docx-{send_as}", idempotency_suffix),
                dry_run=args.dry_run,
            )
            message_id = message_id_from_result(message, dry_run=args.dry_run)
            entry["message_id"] = message_id
    else:
        doc_token, doc_url = import_report(lark_report, title, import_as=import_as, dry_run=args.dry_run)
        # 关键顺序：先写注册表、再授权发消息——后续任何一步失败，重跑都会走同链更新，
        # 不会再新建第二篇 Docx。
        entry = {
            "doc_token": doc_token,
            "url": doc_url,
            "title": title,
            "message_id": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not args.dry_run:
            registry[registry_key] = entry
            save_registry(registry)
        verify_report_images(
            doc_token,
            lark_report,
            ymd,
            verify_as=import_as,
            dry_run=args.dry_run,
        )
        if os.environ.get("RIBAO_TEST_MODE", "0") == "1":
            verify_report_content_readback(
                doc_token,
                lark_report,
                verify_as=import_as,
                dry_run=args.dry_run,
            )
        if not skip_permission:
            grant_chat_permission(doc_token, chat_id, perm, permission_as=permission_as, dry_run=args.dry_run)
        if args.doc_only:
            message_id = ""
        else:
            warning = wewe_warning_from_draft(args.report, ymd)
            if warning:
                send_wewe_warning(
                    chat_id,
                    warning,
                    send_as,
                    with_idempotency_suffix(f"ribao-{ymd}-wewe-warning-{send_as}", idempotency_suffix),
                    dry_run=args.dry_run,
                )
            message = send_chat_message(
                chat_id,
                doc_url,
                send_as,
                with_idempotency_suffix(f"ribao-{ymd}-cli-docx-{send_as}", idempotency_suffix),
                dry_run=args.dry_run,
            )
            message_id = message_id_from_result(message, dry_run=args.dry_run)
            entry["message_id"] = message_id

    if not args.doc_only:
        verify_chat_message(message_id, doc_url, send_as, dry_run=args.dry_run)

    entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["last_verdict"] = verdict
    entry["doc_only"] = bool(args.doc_only)
    entry["group_permission"] = "skipped" if skip_permission else perm
    entry["test_version"] = (
        registry_key.removeprefix(f"{ymd}-test-") if registry_key != ymd else ""
    )
    if not args.dry_run:
        registry[registry_key] = entry
        save_registry(registry)
        record_manifest(ymd, {"doc_url": entry.get("url", ""), "publish_mode": "doc-only" if args.doc_only else mode})

    print(
        json.dumps(
            {
                "ok": True,
                "mode": mode,
                "ymd": ymd,
                "title": title,
                "doc_url": doc_url,
                "doc_token": "" if skip_permission else doc_token,
                "chat_id": "" if skip_permission else chat_id,
                "perm": "" if skip_permission else perm,
                "group_permission": "skipped" if skip_permission else perm,
                "registry_key": registry_key,
                "import_as": import_as,
                "permission_as": "" if skip_permission else permission_as,
                "send_as": send_as,
                "message_id": message_id,
                "doc_only": bool(args.doc_only),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"[feishu-cli] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
