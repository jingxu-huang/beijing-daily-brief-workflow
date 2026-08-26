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
import subprocess
import sys
import time
from pathlib import Path

import send_to_feishu_cli as feishu


INDEX_REGISTRY_PATH = feishu.STATE_DIR / "feishu-daily-index.json"
DEFAULT_INDEX_MD = feishu.STATE_DIR / "日报链接索引.md"
DEFAULT_TITLE = "资讯日报链接索引"


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
    staged = feishu.stage_report_for_lark(md_path, "daily-index")

    if mode == "auto":
        mode = "update" if doc_token else "new"
    if mode == "update" and not doc_token:
        raise RuntimeError("索引文档尚未创建，无法 update；请使用 --mode auto 或 --mode new")

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
    parser.add_argument("--mode", choices=["auto", "new", "update"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    reports = feishu.load_registry()
    md_path = Path(args.index_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_index(reports, args.title), encoding="utf-8")

    result = sync_index(md_path, args.title, args.import_as, args.mode, dry_run=args.dry_run)
    if not args.dry_run:
        record_manifest(args.date, {"index_url": result.get("url", "")})
    print(json.dumps({"ok": True, **result, "index_md": str(md_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
