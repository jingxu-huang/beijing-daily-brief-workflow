#!/usr/bin/env python3
"""Produce a small, human-readable audit for one frozen v2 replay.

The JSON ledger remains the source of truth.  This file is deliberately a
summary so people can compare the three profiles without reverse-engineering
the fetcher or trusting a Feishu document title.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--audit", action="append", required=True,
                        help="profile=筛选审计 JSON；可重复传入")
    parser.add_argument("--draft", action="append", default=[],
                        help="profile=候选草稿 Markdown；可重复传入，用于记录最终精选")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    snapshot_path = Path(args.snapshot)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    records = snapshot.get("records", [])
    source_counts = Counter(record.get("source") or "未标注来源" for record in records)
    lines = [
        f"# {snapshot.get('target_ymd', '未知日期')} · v2 筛选回放审计",
        "",
        f"- 时间窗：{snapshot.get('window', '未知')}",
        f"- 原始候选：{len(records)} 条；来源：{len(source_counts)} 个",
        f"- 共同快照：`{snapshot_path.name}`；SHA-256：`{sha256(snapshot_path)}`",
        "- 输入定义：已通过时间窗、尚未经过编辑准入的候选；三个版本使用同一输入。",
        f"- 冻结阶段：`{snapshot.get('capture_stage') or '历史快照（未记录冻结阶段）'}`。",
        "",
        "## 原始来源覆盖",
        "",
        "|来源|候选数|",
        "|---|---:|",
    ]
    lines.extend(f"|{name}|{count}|" for name, count in sorted(source_counts.items()))
    if snapshot.get("errors"):
        lines += ["", "## 抓取告警", ""]
        lines.extend(f"- {error}" for error in snapshot["errors"])
    enrichment = snapshot.get("enrichment") or []
    if enrichment:
        lines += ["", "## 限量全文补全", ""]
        for item in enrichment:
            lines.append(
                f"- {item.get('status', 'unknown')}：{item.get('title', '未命名')}"
                f"（{item.get('before_chars', 0)} → {item.get('after_chars', 0)} 字）"
            )

    draft_paths = {}
    for raw in args.draft:
        profile, separator, value = raw.partition("=")
        if not separator:
            raise SystemExit("--draft 必须为 profile=path")
        draft_paths[profile] = Path(value)

    for raw in args.audit:
        profile, separator, value = raw.partition("=")
        if not separator:
            raise SystemExit("--audit 必须为 profile=path")
        ledger = json.loads(Path(value).read_text(encoding="utf-8"))
        decisions = [record for record in ledger.get("records", []) if record.get("stage") == "business_relevance"]
        reason_counts = Counter(
            record.get("reason", "未说明") for record in decisions if record.get("decision") == "reject"
        )
        reviews = Counter(
            record.get("review_tier", "未分层") for record in decisions if record.get("decision") == "review"
        )
        expanded = sum(1 for record in ledger.get("records", []) if record.get("stage") == "profile_review_expansion")
        selected_titles, selected_sources = [], Counter()
        draft_path = draft_paths.get(profile)
        if draft_path and draft_path.exists():
            for line in draft_path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^- ⭐\*\*(.+?)\*\*.*?｜([^（]+?)(?:（发布|$)", line)
                if match:
                    selected_titles.append(match.group(1).strip())
                    selected_sources[match.group(2).strip()] += 1
        lines += [
            "",
            f"## {profile}",
            "",
            f"- 决策：接受 {sum(1 for r in decisions if r.get('decision') == 'accept')}；"
            f"候补 {sum(1 for r in decisions if r.get('decision') == 'review')}；"
            f"拒绝 {sum(1 for r in decisions if r.get('decision') == 'reject')}；"
            f"安全候补扩展 {expanded}。",
            "- 拒绝原因：" + ("；".join(f"{reason} {count}" for reason, count in sorted(reason_counts.items())) or "无"),
            "- 候补分层：" + ("；".join(f"{tier} {count}" for tier, count in sorted(reviews.items())) or "无"),
        ]
        if draft_path:
            lines.append(
                f"- 最终精选：{len(selected_titles)} 条；来源："
                + ("；".join(f"{name} {count}" for name, count in sorted(selected_sources.items())) or "未解析")
            )
            if selected_titles:
                lines.append("- 精选标题：" + "；".join(selected_titles))

    lines += [
        "",
        "## 验收口径",
        "",
        "- 严格版：至少 10 条且不得出现明确无关内容；不足即 FAIL，不补无关项。",
        "- 平衡版/高召回版：目标 25 条；未达目标只说明当日原始候选不足或安全候补不足，不视为可放宽硬拒绝。",
        "- 多源训练例外仅在审计账本中有同事件独立来源佐证时生效；不得从来源表或相似关键词推断。",
    ]
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
