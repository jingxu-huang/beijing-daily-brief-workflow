#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create/update the isolated Feishu index for test daily briefs.

The test index never touches the production index or sends chat messages. It
groups the latest strict/balanced/recall run for each date into one row, keeps
older run records locally for audit, and grants the configured test audience
edit access to the index document only.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import send_to_feishu_cli as feishu


INDEX_REGISTRY_PATH = feishu.STATE_DIR / "feishu-test-index.json"
RUN_REGISTRY_PATH = feishu.STATE_DIR / "feishu-test-runs.json"
DEFAULT_INDEX_MD = feishu.STATE_DIR / "测试日报索引.md"
DEFAULT_TITLE = "资讯日报测试版本"
PROFILE_COLUMNS = (
    ("business_v2_strict", "测试版v2-严格"),
    ("business_v2_balanced", "测试版v2-平衡"),
    ("business_v2_recall", "测试版v2-高召回"),
)
EXPECTED_PUBLIC_ACCOUNTS = 27
PUBLIC_STATUS_RE = re.compile(
    r"启用自动公开源\s*(\d+)\s*个；有候选来源\s*(\d+)\s*个；"
    r"精选覆盖\s*(\d+)\s*个；抓取失败\s*(\d+)\s*个"
)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, TypeError, ValueError):
        return {}


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_detail(detail):
    return {
        key: value
        for key, value in re.findall(r"([a-z_]+)=([^\s]+)", str(detail or ""))
    }


def public_source_status(data_root, ymd):
    draft = Path(data_root) / "日报草稿" / f"资讯日报-草稿-{ymd}.md"
    try:
        match = PUBLIC_STATUS_RE.search(draft.read_text(encoding="utf-8"))
    except OSError:
        match = None
    if not match:
        return {"status": "unknown"}
    enabled, candidate_sources, selected_sources, failed = map(int, match.groups())
    return {
        "status": "healthy" if failed == 0 else "degraded",
        "enabled": enabled,
        "candidate_sources": candidate_sources,
        "selected_sources": selected_sources,
        "failed": failed,
    }


def _source_channel(source, link, public_sources=()):
    source = str(source or "")
    link = str(link or "")
    if source.startswith("ai-bot/") or source in set(public_sources):
        return "public"
    if "mp.weixin.qq.com/" in link:
        return "account"
    return "public"


def article_flow_status(data_root, ymd, source_snapshot=None):
    """Return article-level flow counts without exposing titles or URLs."""
    root = Path(data_root)
    result = {
        "public": {"grabbed": 0, "selected": 0, "backup": 0, "adopted": 0},
        "account": {"grabbed": 0, "selected": 0, "backup": 0, "adopted": 0},
    }
    draft = root / "日报草稿" / f"资讯日报-草稿-{ymd}.md"
    try:
        draft_text = draft.read_text(encoding="utf-8")
    except OSError:
        draft_text = ""
    public_sources = {
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\|([^|]+)\|\d+\|(?:有候选|失败|本窗口无候选)\|$", draft_text)
    }
    audit = load_json(root / "日报状态" / f"资讯日报-筛选审计-{ymd}.json")
    link_channels = {}
    seen = set()
    for record in audit.get("records", []):
        if record.get("stage") != "keyword_recall":
            continue
        source = str(record.get("source") or "")
        link = str(record.get("link") or "")
        key = (source, link, str(record.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        channel = _source_channel(source, link, public_sources)
        result[channel]["grabbed"] += 1
        if link:
            link_channels[link] = channel

    # Matrix workers replay a single immutable raw snapshot rather than
    # fetching independently.  That replay correctly has no per-worker
    # keyword-recall records, so derive the raw article count from the shared
    # snapshot instead of falsely rendering "抓到 0" in the test index.
    if not any(bucket["grabbed"] for bucket in result.values()) and source_snapshot:
        payload = load_json(source_snapshot)
        seen_snapshot = set()
        for record in payload.get("records", []):
            if not isinstance(record, dict):
                continue
            item = record.get("item") or {}
            if not isinstance(item, dict):
                continue
            source = str(record.get("source") or item.get("src") or "")
            link = str(item.get("link") or "")
            title = str(item.get("title") or "")
            key = (source, link, title)
            if key in seen_snapshot:
                continue
            seen_snapshot.add(key)
            channel = _source_channel(source, link, public_sources)
            result[channel]["grabbed"] += 1
            if link:
                link_channels.setdefault(link, channel)

    draft_lines = draft_text.splitlines()
    starts = [
        index for index, line in enumerate(draft_lines)
        if line.startswith("- ⭐") or line.startswith("- 备选：")
    ]
    for position, start in enumerate(starts):
        line = draft_lines[start]
        bucket = "selected" if line.startswith("- ⭐") else "backup"
        if not bucket:
            continue
        end = starts[position + 1] if position + 1 < len(starts) else len(draft_lines)
        block = "\n".join(draft_lines[start:end])
        link_match = re.search(r"https?://\S+", block)
        link = link_match.group(0).rstrip(")。，") if link_match else ""
        source = line.rsplit("｜", 1)[-1].split(" — ", 1)[0].split("（发布", 1)[0]
        channel = link_channels.get(link) or _source_channel(source, link, public_sources)
        result[channel][bucket] += 1

    md = md_date(ymd)
    report = root / "日报文档" / f"资讯类简报-{md}日.md"
    try:
        report_text = report.read_text(encoding="utf-8")
    except OSError:
        report_text = ""
    starts = list(re.finditer(r"(?m)^###\s+\d+[)）]\s*", report_text))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(report_text)
        block = report_text[match.start():end]
        links = re.findall(r"https?://[^\s)>]+", block)
        link = links[-1].rstrip("。，") if links else ""
        channel = link_channels.get(link) or _source_channel("", link)
        result[channel]["adopted"] += 1
    return result


def public_account_status(manifest):
    step = (manifest.get("steps", {}).get("weread_omni_preflight", {}) or {})
    detail = parse_detail(step.get("detail", ""))
    return {
        "status": detail.get("status") or step.get("status") or "unknown",
        "accounts": int(detail.get("accounts", 0) or 0),
        "items": int(detail.get("items", 0) or 0),
        "fresh_sources": int(detail.get("fresh_sources", 0) or 0),
        "stale_sources": int(detail.get("stale_sources", 0) or 0),
        "unassessed_sources": int(detail.get("unassessed_sources", 0) or 0),
        "failure_class": detail.get("failure_class", "none"),
    }


def production_status(data_root, ymd, entry, source_snapshot=None):
    manifest = load_json(Path(data_root) / "日报状态" / f"run-manifest-{ymd}.json")
    return {
        "public": public_source_status(data_root, ymd),
        "public_account": public_account_status(manifest),
        "article_flow": article_flow_status(data_root, ymd, source_snapshot=source_snapshot),
        "qa": str(entry.get("last_verdict") or manifest.get("verdict") or "unknown"),
        "final": str(manifest.get("final") or "unknown"),
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def md_date(ymd):
    return f"{int(ymd[4:6])}.{int(ymd[6:8])}" if re.fullmatch(r"\d{8}", ymd) else ymd


def public_label(status):
    if status.get("status") == "unknown":
        return "未记录"
    return (
        f"{status['status']}；启用{status['enabled']} / 有候选{status['candidate_sources']} / "
        f"精选覆盖{status['selected_sources']} / 失败{status['failed']}"
    )


def account_label(status):
    if status.get("status") == "unknown":
        return "未记录"
    incomplete = status.get("stale_sources", 0) + status.get("unassessed_sources", 0)
    return (
        f"{status['status']}；订阅{status.get('accounts', 0)} / 窗口文章{status.get('items', 0)} / "
        f"新鲜源{status.get('fresh_sources', 0)} / 陈旧或未检查{incomplete}"
    )


def md_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def profile_from_entry(key, entry):
    explicit = str(entry.get("test_profile") or entry.get("profile") or "").strip()
    if explicit in dict(PROFILE_COLUMNS):
        return explicit
    haystack = " ".join(
        str(value or "").lower()
        for value in (key, entry.get("test_version"), entry.get("title"), entry.get("label"))
    )
    for profile, slug in (
        ("business_v2_strict", "strict"),
        ("business_v2_balanced", "balanced"),
        ("business_v2_recall", "recall"),
    ):
        if profile in haystack or re.search(rf"(?:^|[-_ ]){slug}(?:$|[-_ ])", haystack):
            return profile
    return ""


def entry_timestamp(entry):
    status = entry.get("production_status") or {}
    return str(
        entry.get("updated_at")
        or status.get("captured_at")
        or entry.get("created_at")
        or ""
    )


def run_candidate(key, entry, report_registry):
    if not isinstance(entry, dict):
        return None
    ymd = str(entry.get("ymd") or "")
    registry_key = str(entry.get("registry_key") or "")
    report_entry = report_registry.get(registry_key) if registry_key else None
    merged = dict(entry)
    # The published-document registry owns the URL/title, while the run
    # registry owns fresh QA and source metrics.  Do not let an older document
    # snapshot overwrite the just-recorded matrix status.
    run_status = entry.get("production_status")
    run_updated_at = entry.get("updated_at")
    if isinstance(report_entry, dict):
        merged.update(report_entry)
        if isinstance(run_status, dict):
            merged["production_status"] = run_status
        else:
            merged.setdefault("production_status", {})
        if run_updated_at:
            merged["updated_at"] = run_updated_at
        merged.setdefault("test_profile", entry.get("profile") or "")
        merged.setdefault("label", entry.get("label") or "")
    if not ymd:
        match = re.match(r"^(\d{8})", str(key))
        ymd = match.group(1) if match else ""
    profile = profile_from_entry(key, merged)
    if not re.fullmatch(r"\d{8}", ymd) or not profile:
        return None
    status = merged.get("production_status") or {}
    return {
        "ymd": ymd,
        "profile": profile,
        "version": str(merged.get("label") or merged.get("test_version") or "最新"),
        "title": str(merged.get("title") or f"资讯类简报-{md_date(ymd)}日"),
        "url": str(merged.get("url") or "").strip(),
        "qa": str(merged.get("qa") or status.get("qa") or merged.get("last_verdict") or "未记录"),
        "public": status.get("public") or {},
        "public_account": status.get("public_account") or {},
        "article_flow": status.get("article_flow") or {},
        "final": str(merged.get("status") or status.get("final") or "未记录"),
        "updated_at": entry_timestamp(merged),
    }


def report_candidate(key, entry):
    if not isinstance(entry, dict):
        return None
    match = re.match(r"^(\d{8})(?:-test-(.+))?$", str(key))
    url = str(entry.get("url") or "").strip()
    profile = profile_from_entry(key, entry)
    if not match or not url or not profile:
        return None
    ymd, key_version = match.groups()
    status = entry.get("production_status") or {}
    return {
        "ymd": ymd,
        "profile": profile,
        "version": str(entry.get("test_version") or key_version or "最新"),
        "title": str(entry.get("title") or f"资讯类简报-{md_date(ymd)}日"),
        "url": url,
        "qa": str(status.get("qa") or entry.get("last_verdict") or "未记录"),
        "public": status.get("public") or {},
        "public_account": status.get("public_account") or {},
        "article_flow": status.get("article_flow") or {},
        "final": str(status.get("final") or "未记录"),
        "updated_at": entry_timestamp(entry),
    }


def friendly_final(value):
    return {
        "generated": "已完成",
        "sent": "已完成",
        "failed": "生成失败",
        "blocked": "质检拦截",
        "running": "生成中",
    }.get(str(value), str(value or "未记录"))


def compact_public(status):
    if status.get("status") == "unknown" or not status:
        return "公开源：未记录"
    state = "正常" if status.get("status") == "healthy" else "部分源失败"
    return (
        f"公开源：{state}（候选来源{status.get('candidate_sources', 0)}，"
        f"入选来源{status.get('selected_sources', 0)}，失败{status.get('failed', 0)}）"
    )


def compact_account(status):
    if status.get("status") == "unknown" or not status:
        return "公众号：未记录"
    labels = {
        "healthy": "正常",
        "cached": "使用已验证缓存",
        "offline_replay": "未参与（离线重跑）",
        "unavailable": "不可用",
        "human_verification_required": "需要人工验证",
        "rate_limited": "触发限流",
    }
    state = labels.get(str(status.get("status")), str(status.get("status")))
    return f"公众号：{state}（窗口文章{status.get('items', 0)}）"


def render_public_metric(item):
    if not item:
        return "未生成"
    flow = item.get("article_flow") or {}
    public = flow.get("public") or {}
    return " / ".join((
        f"抓到 {int(public.get('grabbed', 0) or 0)}",
        f"精选 {int(public.get('selected', 0) or 0)}",
        f"候补 {int(public.get('backup', 0) or 0)}",
        f"采用 {int(public.get('adopted', 0) or 0)}",
    ))


def render_account_metric(item):
    if not item:
        return "未生成"
    flow = item.get("article_flow") or {}
    account = flow.get("account") or {}
    account_status = item.get("public_account") or {}
    enabled = str(account_status.get("status")) in {"healthy", "cached"}
    covered = account_status.get("fresh_sources", 0) if enabled else 0
    return " / ".join((
        "启用" if enabled else "未启用",
        f"覆盖 {int(covered or 0)}/{EXPECTED_PUBLIC_ACCOUNTS}",
        f"抓到 {int(account.get('grabbed', 0) or 0)}",
        f"精选 {int(account.get('selected', 0) or 0)}",
        f"候补 {int(account.get('backup', 0) or 0)}",
        f"采用 {int(account.get('adopted', 0) or 0)}",
    ))


def render_qa_metric(item):
    if not item:
        return "未生成"
    return f"{md_cell(item.get('qa') or '未记录')}<br>{friendly_final(item.get('final'))}"


def render_report_link(item, label):
    if not item or not item.get("url"):
        return "无文档"
    # A failed replay may still have an older document in the persistent
    # registry under the same date/profile key.  The fresh run status owns the
    # row: never present that stale URL as the output of the failed run.
    if str(item.get("final") or "").strip().lower() in {"failed", "blocked"}:
        return "无文档"
    return f"[{label}]({item['url']})"


def daily_summary_item(latest, ymd):
    items = [
        latest.get((ymd, profile))
        for profile, _label in PROFILE_COLUMNS
        if latest.get((ymd, profile))
    ]
    if not items:
        return None
    return max(items, key=lambda item: str(item.get("updated_at") or ""))


def render_test_index(report_registry, title, run_registry=None, active_since=""):
    latest = {}
    for key, entry in sorted(report_registry.items(), reverse=True):
        candidate = report_candidate(key, entry)
        if candidate and (not active_since or candidate["updated_at"] >= active_since):
            latest[(candidate["ymd"], candidate["profile"])] = max(
                latest.get((candidate["ymd"], candidate["profile"]), candidate),
                candidate,
                key=lambda value: (value["updated_at"], bool(value.get("url"))),
            )
    for key, entry in (run_registry or {}).items():
        candidate = run_candidate(key, entry, report_registry)
        if candidate and (not active_since or candidate["updated_at"] >= active_since):
            latest[(candidate["ymd"], candidate["profile"])] = max(
                latest.get((candidate["ymd"], candidate["profile"]), candidate),
                candidate,
                key=lambda value: (value["updated_at"], bool(value.get("url"))),
            )

    lines = [
        f"# {title}",
        "",
        "| 日期 | 测试版v2-严格 | 测试版v2-平衡 | 测试版v2-高召回 | 公开源 | 公众号源 | 质检结果 | 生成时间 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    dates = sorted({ymd for ymd, _profile in latest}, reverse=True)
    if dates:
        for ymd in dates:
            summary = daily_summary_item(latest, ymd)
            report_cells = [
                render_report_link(latest.get((ymd, profile)), label)
                for profile, label in PROFILE_COLUMNS
            ]
            lines.append(
                f"| {md_date(ymd)} | " + " | ".join(report_cells)
                + f" | {render_public_metric(summary)} | {render_account_metric(summary)} | "
                f"{md_cell((summary or {}).get('qa') or '未生成')} | "
                f"{md_cell((summary or {}).get('updated_at') or '-')} |"
            )
    else:
        lines.append("| - | 无文档 | 无文档 | 无文档 | 未生成 | 未生成 | 未生成 | - |")
    lines.append("")
    return "\n".join(lines)


def grant_test_index_edit_permission(doc_token, chat_id, permission_as="user", dry_run=False):
    if not doc_token or not chat_id:
        raise RuntimeError("测试索引缺少文档或目标群配置，无法授予编辑权限")
    payload = json.dumps(
        {"member_type": "openchat", "member_id": chat_id, "perm": "edit", "type": "chat"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return feishu.run_lark(
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


def sync_index(md_path, title, import_as, mode, chat_id, permission_as="user", dry_run=False):
    registry = load_json(INDEX_REGISTRY_PATH)
    doc_token = str(registry.get("doc_token") or "")
    doc_url = str(registry.get("url") or "")
    if mode == "auto":
        if not doc_token:
            raise RuntimeError("测试索引尚未登记；首次创建必须显式使用 --mode new")
        mode = "update"
    if mode == "new" and doc_token:
        raise RuntimeError("测试索引已经存在，拒绝重复创建")
    if mode == "update" and not doc_token:
        raise RuntimeError("测试索引尚未创建，无法更新")

    staged = feishu.stage_report_for_lark(md_path, "test-daily-index")
    if mode == "new":
        doc_token, doc_url = feishu.import_report(staged, title, import_as=import_as, dry_run=dry_run)
        registry = {
            "doc_token": doc_token,
            "url": doc_url,
            "title": title,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        feishu.update_report_in_place(doc_token, staged, update_as=import_as, dry_run=dry_run)
    grant_test_index_edit_permission(
        doc_token, chat_id, permission_as=permission_as, dry_run=dry_run
    )
    registry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not dry_run:
        save_json(INDEX_REGISTRY_PATH, registry)
    return {"mode": mode, "url": doc_url, "title": title, "group_permission": "edit"}


def register_index_target(url, title, replace=False, dry_run=False):
    """Reconnect an existing test index without creating or rewriting it."""
    token, canonical_url = feishu_index_target(url)
    registry = load_json(INDEX_REGISTRY_PATH)
    existing = str(registry.get("doc_token") or "")
    if existing and existing != token and not replace:
        raise RuntimeError("已有不同的测试索引目标；如确需更换，必须显式使用 --replace-target")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    registry.update({"doc_token": token, "url": canonical_url, "title": title, "updated_at": now})
    registry.setdefault("created_at", now)
    if not dry_run:
        save_json(INDEX_REGISTRY_PATH, registry)
    return {"registered": True, "title": title}


def feishu_index_target(url):
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(str(url or "").strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.netloc != "micoworld.feishu.cn" \
       or len(parts) != 2 or parts[0] != "docx" or not parts[1].isalnum():
        raise RuntimeError("测试索引目标必须是已确认的 micoworld.feishu.cn/docx 文档链接")
    return parts[1], urlunsplit((parsed.scheme, parsed.netloc, f"/docx/{parts[1]}", "", ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="当前测试运行日期 YYYYMMDD")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--index-md", default=str(DEFAULT_INDEX_MD))
    parser.add_argument("--import-as", default="user")
    parser.add_argument("--permission-as", default="user")
    parser.add_argument(
        "--chat-id",
        default=os.environ.get("FEISHU_TEST_INDEX_CHAT_ID", "").strip(),
        help="仅用于向测试索引授予编辑权限；不会发送群消息",
    )
    parser.add_argument("--mode", choices=("auto", "new", "update"), default="auto")
    parser.add_argument("--register-target", default="", help="仅登记已确认的固定测试索引 Docx，不更新正文")
    parser.add_argument("--replace-target", action="store_true", help="允许更换已有的不同测试索引目标")
    parser.add_argument("--active-since", default=os.environ.get("RIBAO_TEST_INDEX_ACTIVE_SINCE", ""))
    parser.add_argument("--profile", choices=tuple(dict(PROFILE_COLUMNS)), default="")
    parser.add_argument("--run-label", default="")
    parser.add_argument(
        "--status-data-root",
        default="",
        help="本次版本运行的隔离数据目录；仅用于读取其草稿、质检和 manifest 指标",
    )
    parser.add_argument(
        "--source-snapshot",
        default="",
        help="矩阵共享的冻结原始候选快照；仅用于准确统计‘抓到’数量，不会修改快照",
    )
    parser.add_argument(
        "--report-registry",
        action="append",
        default=[],
        help="并行测试版本的只读飞书文档注册表；可重复传入，不会写回这些文件",
    )
    parser.add_argument(
        "--run-status",
        choices=("running", "generated", "failed", "blocked"),
        default="",
    )
    parser.add_argument("--qa", choices=("PASS", "WARN", "FAIL"), default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="只登记一个测试版本状态；由矩阵协调器在全部版本完成后统一更新一次飞书索引",
    )
    args = parser.parse_args()

    if os.getenv("RIBAO_TEST_MODE", "0") != "1":
        raise RuntimeError("测试索引只允许在 RIBAO_TEST_MODE=1 时更新")
    if args.register_target:
        result = register_index_target(
            args.register_target, args.title, replace=args.replace_target, dry_run=args.dry_run
        )
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return
    if args.import_as != "user" or args.permission_as != "user":
        raise RuntimeError("测试索引写入和授权只允许使用已授权的用户身份")
    if not re.fullmatch(r"\d{8}", args.date):
        raise RuntimeError("测试索引日期必须是 YYYYMMDD")
    if not args.chat_id:
        raise RuntimeError("测试索引缺少战略小组会话配置，无法授予编辑权限")

    index_registry = load_json(INDEX_REGISTRY_PATH)
    active_since = str(args.active_since or index_registry.get("active_since") or "").strip()
    if active_since and not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", active_since):
        raise RuntimeError("测试索引起始时间必须是 YYYY-MM-DD HH:MM:SS")
    if args.active_since:
        index_registry["active_since"] = active_since
        if not args.dry_run:
            save_json(INDEX_REGISTRY_PATH, index_registry)

    reports = feishu.load_registry()
    for path in args.report_registry:
        reports.update(load_json(path))
    runs = load_json(RUN_REGISTRY_PATH)
    current_key = os.getenv("FEISHU_REGISTRY_KEY", "").strip()
    if current_key and current_key not in reports and args.run_status not in (
        "running",
        "failed",
        "blocked",
    ):
        raise RuntimeError("当前测试日报尚未登记，拒绝把未发布成稿标为成功")

    status_data_root = Path(args.status_data_root).resolve() if args.status_data_root else feishu.DATA_ROOT
    if current_key and current_key in reports:
        entry = reports[current_key]
        entry["production_status"] = production_status(
            status_data_root, args.date, entry, source_snapshot=args.source_snapshot or None
        )
        entry["test_profile"] = args.profile or os.getenv("RIBAO_EDITORIAL_POLICY", "")
        if not args.dry_run and not args.record_only:
            feishu.save_registry(reports)

    # The matrix keeps each profile's document registry in its isolated run
    # directory.  Persist the merged view in the shared test registry after
    # all workers finish; otherwise rebuilding a later/earlier date can make
    # already-published dates render as "无文档" even though their documents
    # still exist.  The per-profile files remain read-only inputs.
    if args.report_registry and not args.dry_run:
        feishu.save_registry(reports)

    if args.run_status:
        profile = args.profile or os.getenv("RIBAO_EDITORIAL_POLICY", "").strip()
        label = args.run_label or os.getenv("RIBAO_TEST_RUN_LABEL", "").strip() or "latest"
        if profile not in dict(PROFILE_COLUMNS):
            raise RuntimeError("记录测试运行状态时必须指定三种测试策略之一")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", label):
            raise RuntimeError("测试运行标识格式无效")
        snapshot = production_status(
            status_data_root,
            args.date,
            {"last_verdict": args.qa or "unknown"},
            source_snapshot=args.source_snapshot or None,
        )
        snapshot["final"] = args.run_status
        if args.qa:
            snapshot["qa"] = args.qa
        run_key = f"{args.date}:{profile}:{label}"
        runs[run_key] = {
            "ymd": args.date,
            "profile": profile,
            "label": label,
            "registry_key": current_key,
            "status": args.run_status,
            "qa": args.qa or snapshot.get("qa") or "unknown",
            "production_status": snapshot,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not args.dry_run:
            save_json(RUN_REGISTRY_PATH, runs)

    if args.record_only:
        print(json.dumps({"ok": True, "recorded": True, "messages_sent": False}, ensure_ascii=False))
        return

    md_path = Path(args.index_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        render_test_index(reports, args.title, runs, active_since),
        encoding="utf-8",
    )
    result = sync_index(
        md_path,
        args.title,
        args.import_as,
        args.mode,
        args.chat_id,
        permission_as=args.permission_as,
        dry_run=args.dry_run,
    )
    print(json.dumps({"ok": True, **result, "messages_sent": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
