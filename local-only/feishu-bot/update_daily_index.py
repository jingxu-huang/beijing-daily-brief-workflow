#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create/update a Feishu Docx index of daily brief links.

The daily report publisher records one entry per date in
``日报状态/feishu-docs.json``.  This script renders those entries to a local
Markdown index and syncs it to a stable Feishu Docx, so users have one cloud
document that always points to each daily report.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import send_to_feishu_cli as feishu


INDEX_REGISTRY_PATH = feishu.STATE_DIR / "feishu-daily-index.json"
DEFAULT_INDEX_MD = feishu.STATE_DIR / "日报链接索引.md"
DEFAULT_TITLE = "资讯日报链接索引"


def parse_docx_target(url):
    parsed = urlsplit(str(url or "").strip())
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or not parsed.netloc or len(parts) != 2 or parts[0] != "docx":
        raise RuntimeError("索引目标必须是 https 飞书 docx 链接")
    token = parts[1]
    if not token.isalnum():
        raise RuntimeError("索引 docx token 格式无效")
    canonical_url = urlunsplit((parsed.scheme, parsed.netloc, f"/docx/{token}", "", ""))
    return token, canonical_url


def register_index_target(url, title, replace=False, dry_run=False):
    token, canonical_url = parse_docx_target(url)
    registry = load_index_registry()
    existing = str(registry.get("doc_token") or "")
    if existing and existing != token and not replace:
        raise RuntimeError("已有不同的索引目标；如确需更换，必须显式使用 --replace-target")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    registry.update(
        {
            "doc_token": token,
            "url": canonical_url,
            "title": title,
            "updated_at": now,
        }
    )
    registry.setdefault("created_at", now)
    if not dry_run:
        save_index_registry(registry)
    return {"registered": True, "title": title}


def load_index_registry():
    try:
        return json.loads(INDEX_REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[feishu-index] WARN: 索引注册表读取失败（{exc}），按空处理", file=sys.stderr)
        return {}


def save_index_registry(data):
    feishu.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_REGISTRY_PATH.with_name(INDEX_REGISTRY_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_REGISTRY_PATH)


def md_date(ymd):
    if len(ymd) != 8 or not ymd.isdigit():
        return ymd
    return f"{int(ymd[4:6])}.{int(ymd[6:8])}"


def render_index(report_registry, title):
    rows = []
    for ymd, entry in sorted(report_registry.items(), reverse=True):
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        report_title = str(entry.get("title") or f"资讯类简报-{md_date(ymd)}日").strip()
        verdict = str(entry.get("last_verdict") or "").strip()
        updated_at = str(entry.get("updated_at") or entry.get("created_at") or "").strip()
        rows.append((ymd, report_title, url, verdict, updated_at))

    lines = [
        f"# {title}",
        "",
        "这里集中记录每日自动生成的资讯日报飞书文档链接。",
        "",
        "| 日期 | 日报文档 | 质检 | 更新时间 |",
        "|---|---|---|---|",
    ]
    if rows:
        for ymd, report_title, url, verdict, updated_at in rows:
            lines.append(f"| {md_date(ymd)} | [{report_title}]({url}) | {verdict or '-'} | {updated_at or '-'} |")
    else:
        lines.append("| - | 暂无日报链接 | - | - |")
    lines += [
        "",
        f"_自动更新：{time.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）_",
        "",
    ]
    return "\n".join(lines)


def recover_registry_from_markdown(markdown, year, existing_registry):
    """Recover dated report links from a prior version of this index.

    The index is the source of truth for a migration where the local private
    ``feishu-docs.json`` registry was not carried over.  Accept only the
    documented Markdown table rows; malformed rows or non-HTTPS links are
    ignored so an unrelated document revision cannot populate the registry.
    Existing local entries always win (for example, today's newly published
    report is not overwritten by an older index revision).
    """
    if not re.fullmatch(r"\d{4}", str(year)):
        raise RuntimeError("恢复索引需要四位年份，例如 2026")
    recovered = {}
    for line in str(markdown or "").splitlines():
        match = re.match(
            r"^\|\s*(\d{1,2})\.(\d{1,2})\s*\|\s*\[([^\]]+)\]\((https://[^\s)]+)\)\s*\|\s*([^|]*)\|",
            line.strip(),
        )
        if not match:
            continue
        month, day, title, url, verdict = match.groups()
        try:
            ymd = f"{year}{int(month):02d}{int(day):02d}"
        except ValueError:
            continue
        if ymd in existing_registry or ymd in recovered:
            continue
        recovered[ymd] = {
            "title": title.strip(),
            "url": url.strip(),
            "last_verdict": verdict.strip(),
            "recovered_from_index": True,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return recovered


def extract_history_markdown(raw_output):
    """Unwrap lark-cli JSON while retaining compatibility with plain Markdown."""
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return raw_output

    def find_content(value):
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str):
                return content
            for item in value.values():
                found = find_content(item)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = find_content(item)
                if found is not None:
                    return found
        return None

    return find_content(payload) or raw_output


def fetch_index_revision(doc_token, revision_id, import_as):
    """Fetch one existing index revision without printing its private links."""
    cmd = [
        "lark-cli", "docs", "+fetch", "--as", import_as,
        "--doc", doc_token,
        "--doc-format", "markdown", "--scope", "full",
        "--revision-id", str(revision_id),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"读取索引历史版本失败（lark-cli exit {result.returncode}）")
    if not result.stdout.strip():
        raise RuntimeError("索引历史版本为空，拒绝恢复")
    return extract_history_markdown(result.stdout)


def record_manifest(ymd, kvs):
    if not ymd:
        return
    helper = feishu.MANIFEST_HELPER
    if not helper.exists():
        return
    try:
        cmd = [sys.executable, str(helper), "--date", ymd, "--state-dir", str(feishu.STATE_DIR)]
        for k, v in kvs.items():
            cmd += ["--kv", f"{k}={v}"]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


def sync_index(md_path, title, import_as, mode, dry_run=False):
    registry = load_index_registry()
    doc_token = registry.get("doc_token", "")
    doc_url = registry.get("url", "")

    if mode == "auto":
        if not doc_token:
            raise RuntimeError(
                "缺少已确认的索引文档注册表；为避免重复创建索引，请先恢复/登记 "
                "feishu-daily-index.json。确需新建时须显式使用 --mode new"
            )
        mode = "update"
    if mode == "update" and not doc_token:
        raise RuntimeError("索引文档尚未创建，无法 update；请使用 --mode auto 或 --mode new")

    staged = feishu.stage_report_for_lark(md_path, "daily-index")

    if mode == "update":
        print(f"[feishu-index] 原地更新索引文档：{doc_url or doc_token}")
        feishu.update_report_in_place(doc_token, staged, update_as=import_as, dry_run=dry_run)
    else:
        doc_token, doc_url = feishu.import_report(staged, title, import_as=import_as, dry_run=dry_run)
        registry["doc_token"] = doc_token
        registry["url"] = doc_url
        registry["title"] = title
        registry["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    registry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not dry_run:
        save_index_registry(registry)
    return {
        "mode": mode,
        "doc_token": doc_token,
        "url": doc_url,
        "title": title,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="YYYYMMDD，用于写回当日 manifest")
    parser.add_argument("--title", default=os.environ.get("FEISHU_DAILY_INDEX_TITLE", DEFAULT_TITLE))
    parser.add_argument("--index-md", default=str(DEFAULT_INDEX_MD))
    parser.add_argument("--import-as", default=os.environ.get("FEISHU_CLI_IMPORT_AS", "bot"))
    parser.add_argument("--permission-as", default=os.environ.get("FEISHU_CLI_PERMISSION_AS", "user"))
    parser.add_argument("--chat-id", default=os.environ.get("FEISHU_REPORT_CHAT_ID", ""))
    parser.add_argument("--perm", default=os.environ.get("FEISHU_REPORT_CHAT_PERM", "full_access"))
    parser.add_argument("--mode", choices=["auto", "new", "update"], default="auto")
    parser.add_argument("--register-target", default="", help="仅登记已确认的固定索引 Docx，不更新正文")
    parser.add_argument("--replace-target", action="store_true", help="允许替换已有的不同索引目标")
    parser.add_argument(
        "--recover-history-revision",
        type=int,
        default=0,
        help="从当前固定索引的指定历史版本恢复缺失日报链接，再原地同步索引",
    )
    parser.add_argument("--recover-year", default="", help="历史索引日期的年份（与 --recover-history-revision 一起使用）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.register_target:
        result = register_index_target(
            args.register_target,
            args.title,
            replace=args.replace_target,
            dry_run=args.dry_run,
        )
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return

    if not args.chat_id:
        raise RuntimeError("索引文档需要日报接收群 chat_id 才能授予全组可管理权限")
    if args.perm != "full_access":
        raise RuntimeError("索引文档必须向日报接收群授予 full_access（可管理）")

    reports = feishu.load_registry()
    if args.recover_history_revision:
        index_registry = load_index_registry()
        doc_token = str(index_registry.get("doc_token") or "")
        if not doc_token:
            raise RuntimeError("固定索引未登记，无法从历史版本恢复")
        if not args.recover_year:
            raise RuntimeError("恢复索引历史版本必须显式提供 --recover-year")
        history_markdown = fetch_index_revision(
            doc_token, args.recover_history_revision, args.import_as
        )
        recovered = recover_registry_from_markdown(history_markdown, args.recover_year, reports)
        if not recovered:
            raise RuntimeError("历史版本未解析到可恢复的日报链接，拒绝覆盖当前索引")
        reports.update(recovered)
        if not args.dry_run:
            feishu.save_registry(reports)
        print(f"[feishu-index] 已从索引历史版本恢复 {len(recovered)} 条日报链接")
    md_path = Path(args.index_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_index(reports, args.title), encoding="utf-8")

    result = sync_index(md_path, args.title, args.import_as, args.mode, dry_run=args.dry_run)
    feishu.grant_chat_permission(
        result["doc_token"],
        args.chat_id,
        args.perm,
        permission_as=args.permission_as,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        record_manifest(args.date, {"index_url": result.get("url", "")})
    print(
        json.dumps(
            {
                "ok": True,
                "mode": result["mode"],
                "title": result["title"],
                "group_permission": "full_access",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
