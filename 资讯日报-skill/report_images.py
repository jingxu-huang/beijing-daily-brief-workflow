#!/usr/bin/env python3
"""Bounded, non-blocking image selection for a completed daily brief.

The writer remains text-only.  Candidate images are prefetched while the writer
runs, then a separate image-capable Codex call sees the local files and chooses
at most one relevant image per report item.  Any media failure is recorded in
image-plan.json and leaves the report text untouched.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import codex_external


SELECTED_RE = re.compile(r"^- ⭐\*\*(.+?)\*\*(?:（score [^）]+）)?｜(.+)$")
BACKUP_RE = re.compile(r"^- 备选：(.+?)｜(.+?)\s+—\s+(https?://\S+)$")
HEADING_RE = re.compile(r"^###\s+\d+）(.+)$")
RAW_URL_RE = re.compile(r"^https?://\S+$")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<)?[^)\n]+(?:>)?\)")
IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\((?:<)?[^)\n]+(?:>)?\)\s*$")
NOISE_IMAGE_RE = re.compile(
    r"(?:^|[/_.-])(logo|avatar|qrcode|qr-code|contact|wechat|weixin|head|icon|"
    r"favicon|loading|placeholder|banner|footer|100x100)(?:[/_.-]|$)",
    flags=re.I,
)

MAX_IMAGES_PER_ITEM = 2
MAX_REPORT_ITEMS = 30


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def normalize_url(value: str) -> str:
    return (value or "").strip().rstrip(".,;)]}，。；）】》")


def parse_draft(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current and current.get("link"):
            rows.append(current)
        current = None

    for raw in text.splitlines():
        line = raw.strip()
        selected = SELECTED_RE.match(line)
        if selected:
            finish()
            current = {
                "title": selected.group(1).strip(),
                "source": re.sub(r"\s*⏱.*$|（发布[^）]+）", "", selected.group(2)).strip(),
                "summary": "",
                "link": "",
                "images": [],
                "selected": True,
            }
            continue
        backup = BACKUP_RE.match(line)
        if backup:
            finish()
            current = {
                "title": backup.group(1).strip(),
                "source": re.sub(r"\s*⏱.*$|（发布[^）]+）", "", backup.group(2)).strip(),
                "summary": "",
                "link": normalize_url(backup.group(3)),
                "images": [],
                "selected": False,
            }
            continue
        if not current:
            continue
        if line.startswith("- 候选图:"):
            current["images"] = [
                normalize_url(url) for url in line.split(":", 1)[1].split("|") if normalize_url(url)
            ]
        elif line.startswith("- http"):
            current["link"] = normalize_url(line[2:])
        elif line.startswith("- "):
            summary = line[2:].strip()
            if summary.startswith("摘要："):
                summary = summary[3:].strip()
            current["summary"] = (current["summary"] + " " + summary).strip()
    finish()
    return rows


def parse_report(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if HEADING_RE.match(line.rstrip())]
    rows: list[dict[str, Any]] = []
    for offset, start in enumerate(starts):
        end = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        title_match = HEADING_RE.match(lines[start].rstrip())
        section = lines[start:end]
        article_line = -1
        article_url = ""
        for relative, line in enumerate(section[1:], 1):
            stripped = line.strip()
            if RAW_URL_RE.fullmatch(stripped):
                article_line = start + relative
                article_url = normalize_url(stripped)
                break
        if article_line < 0:
            continue
        body = " ".join(
            line.strip()
            for line in section[1 : article_line - start]
            if line.strip() and not IMAGE_LINE_RE.match(line)
        )
        rows.append(
            {
                "title": title_match.group(1).strip() if title_match else "",
                "body": body,
                "link": article_url,
                "article_line": article_line,
                "has_image": any(MARKDOWN_IMAGE_RE.search(line) for line in section),
            }
        )
    return rows


def candidate_urls(row: dict[str, Any]) -> list[str]:
    if "微信公众号" in row.get("source", "") or "mp.weixin.qq.com" in row.get("link", ""):
        return []
    accepted: list[str] = []
    for url in row.get("images", []):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if NOISE_IMAGE_RE.search(parsed.path.lower()):
            continue
        if url not in accepted:
            accepted.append(url)
        if len(accepted) >= MAX_IMAGES_PER_ITEM:
            break
    return accepted


def load_image_transport(project_root: Path):
    sender_path = project_root / "local-only" / "feishu-bot" / "send_to_feishu_cli.py"
    spec = importlib.util.spec_from_file_location("ribao_image_transport", sender_path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load Feishu image transport")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download_one(transport, url: str, referer: str, assets_dir: Path) -> Path:
    last_error: Exception | None = None
    for candidate in transport._image_url_candidates(url):
        try:
            return transport._download_remote_image(candidate, referer, assets_dir)
        except Exception as exc:  # download errors are audited, never fatal to text
            last_error = exc
    raise RuntimeError(type(last_error).__name__ if last_error else "download_failed")


def prefetch(draft: Path, work: Path, project_root: Path) -> Path:
    rows = parse_draft(draft.read_text(encoding="utf-8"))
    cache_dir = work / "image-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    transport = load_image_transport(project_root)
    tasks: dict[Any, tuple[str, str, str]] = {}
    downloads: dict[str, list[dict[str, str]]] = {}
    failures: list[dict[str, str]] = []
    eligible = [row for row in rows if candidate_urls(row)][:MAX_REPORT_ITEMS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for row in eligible:
            link = row["link"]
            for url in candidate_urls(row):
                future = pool.submit(_download_one, transport, url, link, cache_dir)
                tasks[future] = (link, url, row["title"])
        for future in as_completed(tasks):
            link, url, title = tasks[future]
            try:
                local = future.result()
                downloads.setdefault(link, []).append(
                    {"url": url, "file": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
                )
            except Exception as exc:
                failures.append(
                    {
                        "article": link,
                        "host": urlsplit(url).hostname or "unknown",
                        "error_category": type(exc).__name__,
                    }
                )
    # Restore source order after concurrent downloads.
    for row in eligible:
        order = {url: index for index, url in enumerate(candidate_urls(row))}
        downloads[row["link"]] = sorted(
            downloads.get(row["link"], []), key=lambda item: order.get(item["url"], 999)
        )
    downloaded_count = sum(len(values) for values in downloads.values())
    if downloaded_count:
        cache_status = "ready"
    elif eligible and failures:
        cache_status = "download_failed"
    else:
        cache_status = "no_valid_candidates"
    payload = {
        "status": cache_status,
        "candidate_groups": len(eligible),
        "downloaded": downloaded_count,
        "downloads": downloads,
        "failures": failures,
    }
    path = work / "image-cache.json"
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def image_command(codex: str, model: str, schema: Path, images: list[Path]) -> list[str]:
    command = [
        codex,
        "exec",
        "--strict-config",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--config",
        'approval_policy="never"',
        "--model",
        model,
        "--cd",
        "/",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
    ]
    for feature in (
        "shell_tool",
        "unified_exec",
        "shell_snapshot",
        "plugins",
        "apps",
        "browser_use",
        "computer_use",
        "multi_agent",
    ):
        command.extend(["--disable", feature])
    command.extend(["--image", *[str(path) for path in images]])
    command.extend(["--output-schema", str(schema), "--json", "-"])
    return command


def _visual_schema(path: Path) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["selections"],
        "properties": {
            "selections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_id", "candidate_number", "reason"],
                    "properties": {
                        "item_id": {"type": "integer", "minimum": 1},
                        "candidate_number": {"type": "integer", "minimum": 0},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }
    atomic_write(path, json.dumps(schema, ensure_ascii=False, indent=2) + "\n")


def run_visual_audit(
    codex: str,
    model: str,
    control_home: Path,
    work: Path,
    items: list[dict[str, Any]],
    attachments: list[Path],
) -> dict[int, int]:
    codex_external.validate_control_home(control_home)
    schema = work / "image-output.schema.json"
    _visual_schema(schema)
    mapping_lines = []
    global_index = 1
    for item in items:
        count = len(item["candidates"])
        attached = ", ".join(str(value) for value in range(global_index, global_index + count))
        mapping_lines.append(
            f"item_id={item['item_id']} | title={item['title']} | body={item['body'][:500]} | "
            f"attached_images={attached} | candidate_numbers=1..{count}"
        )
        global_index += count
    prompt = """
你只负责日报配图的视觉相关性审核，不改写新闻。附件图片按 attached_images 给出的全局顺序排列。
每个 item 最多选一张：只有榜单/数据图表、产品内 UI、游戏实机、真实创新硬件/产品图才能选。
公司 logo、头图装饰、网站首页、氛围图、人物肖像、广告、二维码、联系图、与正文核心对象不符的图都必须拒绝。
非榜单/数据、非产品形态条目必须返回 candidate_number=0。无法确认时也返回 0。
对每个 item_id 返回一条 selection，candidate_number 是该 item 内的 1 开始序号，0 表示不配图。

""".strip() + "\n" + "\n".join(mapping_lines)
    command = image_command(codex, model, schema, attachments)
    env = os.environ.copy()
    env.update(
        {
            "CODEX_HOME": str(codex_external.EXISTING_CODEX_HOME),
            "TMPDIR": str(work / "tmp"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_DATA_HOME": str(work / "data"),
        }
    )
    timeout_raw = os.getenv("RIBAO_IMAGE_AUDIT_TIMEOUT_SECONDS", "45")
    try:
        timeout = max(10, min(120, int(timeout_raw)))
    except ValueError:
        timeout = 45
    result = codex_external.run_prompt_process(command, env, prompt, timeout)
    atomic_write(work / "image-audit.events.jsonl", result.stdout)
    atomic_write(work / "image-audit.stderr.log", result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"image audit exited {result.returncode}")
    payload = json.loads(codex_external.last_agent_message(result.stdout))
    selections: dict[int, int] = {}
    limits = {item["item_id"]: len(item["candidates"]) for item in items}
    for selection in payload.get("selections", []):
        item_id = int(selection.get("item_id", 0))
        choice = int(selection.get("candidate_number", 0))
        if item_id in limits and 0 <= choice <= limits[item_id]:
            selections[item_id] = choice
    return selections


def insert_images(text: str, choices: dict[str, str]) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    rows = parse_report(text)
    insertions: list[tuple[int, str]] = []
    for row in rows:
        image_url = choices.get(row["link"], "")
        if not image_url or row["has_image"]:
            continue
        insertions.append((row["article_line"], f"![]({image_url})\n\n"))
    for line_number, fragment in reversed(insertions):
        lines.insert(line_number, fragment)
    return "".join(lines), len(insertions)


def strip_unverified_images(text: str) -> tuple[str, int]:
    """Remove writer-selected image lines before the independent media audit."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removed = 0
    for line in lines:
        if IMAGE_LINE_RE.match(line):
            removed += 1
            continue
        kept.append(line)
    return "".join(kept), removed


def apply_images(
    draft: Path,
    report: Path,
    work: Path,
    codex: str,
    control_home: Path,
    model: str,
) -> Path:
    original = report.read_text(encoding="utf-8")
    audited_base, removed_unverified = strip_unverified_images(original)
    report_rows = parse_report(audited_base)
    try:
        cache = json.loads((work / "image-cache.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {"downloads": {}}
    downloads = cache.get("downloads", {}) if isinstance(cache, dict) else {}
    items: list[dict[str, Any]] = []
    attachments: list[Path] = []
    for row in report_rows:
        candidates = [value for value in downloads.get(row["link"], []) if Path(value.get("file", "")).is_file()]
        if not candidates:
            continue
        item = dict(row)
        item["item_id"] = len(items) + 1
        item["candidates"] = candidates[:MAX_IMAGES_PER_ITEM]
        items.append(item)
        attachments.extend(Path(value["file"]) for value in item["candidates"])
    cache_status = cache.get("status", "unknown") if isinstance(cache, dict) else "unknown"
    plan: dict[str, Any] = {
        "status": "warn" if not items and cache_status == "download_failed" else (
            "no_report_candidates" if not items else "pending"
        ),
        "cache_status": cache_status,
        "report_items_with_candidates": len(items),
        "removed_unverified": removed_unverified,
        "inserted": 0,
        "selections": [],
    }
    if not items and cache_status == "download_failed":
        plan["error_category"] = "candidate_download_failed"
    if not items:
        if audited_base != original:
            atomic_write(report, audited_base)
        plan_path = work / "image-plan.json"
        atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        return plan_path
    try:
        selected = run_visual_audit(codex, model, control_home, work, items, attachments)
        choices: dict[str, str] = {}
        for item in items:
            choice = selected.get(item["item_id"], 0)
            if choice:
                candidate = item["candidates"][choice - 1]
                choices[item["link"]] = candidate["url"]
                plan["selections"].append(
                    {"article": item["link"], "image_host": urlsplit(candidate["url"]).hostname or "unknown"}
                )
        updated, inserted = insert_images(audited_base, choices)
        if updated != original:
            atomic_write(report, updated)
        plan["status"] = "ok" if inserted else "no_relevant_images"
        plan["inserted"] = inserted
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        # Text delivery is deliberately independent from the media layer.
        plan["status"] = "warn"
        plan["error_category"] = type(exc).__name__
    plan_path = work / "image-plan.json"
    atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return plan_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prefetch_parser = sub.add_parser("prefetch")
    prefetch_parser.add_argument("--draft", required=True)
    prefetch_parser.add_argument("--work-dir", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--draft", required=True)
    apply_parser.add_argument("--report", required=True)
    apply_parser.add_argument("--work-dir", required=True)
    apply_parser.add_argument("--codex", required=True)
    apply_parser.add_argument("--control-home", required=True)
    apply_parser.add_argument("--model", required=True)
    args = parser.parse_args()

    root = codex_external.validate_root(Path(args.root))
    work = codex_external.validate_work_dir(root, Path(args.work_dir))
    data_root = codex_external.validate_data_root()
    draft = codex_external.require_within(Path(args.draft), data_root / "日报草稿", "image draft")
    if args.command == "prefetch":
        print(prefetch(draft, work, root))
        return 0
    report = codex_external.require_within(Path(args.report), work, "image report")
    print(
        apply_images(
            draft,
            report,
            work,
            args.codex,
            Path(args.control_home),
            args.model,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
