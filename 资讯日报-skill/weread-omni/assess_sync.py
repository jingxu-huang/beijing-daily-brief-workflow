#!/usr/bin/env python3
"""Assess one weread-omni observation without exposing account data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


HEALTHY_STATUSES = {"healthy", "cached"}


def assess(payload: dict, feed_text: str, target_ymd: str) -> dict:
    status = str(payload.get("status") or "invalid")
    target_matches = payload.get("targetDate") == target_ymd
    stale_sources = int(payload.get("staleSources") or 0)
    fresh_sources = int(payload.get("freshSources") or 0)
    account_count = int(payload.get("subscriptionCount") or 0)
    unassessed_sources = max(account_count - fresh_sources - stale_sources, 0)
    failure_classes = payload.get("failureClasses") or {}
    failure_class = payload.get("failureClass")
    if not failure_class and failure_classes:
        failure_class = next(iter(failure_classes))

    try:
        root = ET.fromstring(feed_text)
        feed_items = len(root.findall("./channel/item"))
    except (ET.ParseError, TypeError):
        feed_items = -1

    declared_items = payload.get("windowItemCount")
    item_count_matches = isinstance(declared_items, int) and declared_items == feed_items
    feed_targets_window = f"for {target_ymd}" in feed_text
    observation_valid = target_matches and feed_targets_window and item_count_matches
    coverage_complete = (
        observation_valid and status in HEALTHY_STATUSES and stale_sources == 0
    )
    usable = observation_valid and feed_items > 0

    return {
        "status": status,
        "target_date": target_ymd,
        "observation_valid": observation_valid,
        "coverage_complete": coverage_complete,
        "usable": usable,
        "item_count": max(feed_items, 0),
        "fresh_source_count": fresh_sources,
        "stale_source_count": stale_sources,
        "account_count": account_count,
        "unassessed_source_count": unassessed_sources,
        "failure_class": str(failure_class or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", required=True)
    parser.add_argument("--feed", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--format", choices=("json", "tsv"), default="json")
    args = parser.parse_args()

    try:
        payload = json.loads(Path(args.observation).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    try:
        feed_text = Path(args.feed).read_text(encoding="utf-8")
    except OSError:
        feed_text = ""

    result = assess(payload, feed_text, args.date)
    if args.format == "tsv":
        print(
            "\t".join(
                [
                    result["status"],
                    "1" if result["usable"] else "0",
                    "1" if result["coverage_complete"] else "0",
                    str(result["item_count"]),
                    str(result["fresh_source_count"]),
                    str(result["stale_source_count"]),
                    str(result["account_count"]),
                    str(result["unassessed_source_count"]),
                    result["failure_class"],
                ]
            )
        )
    else:
        print(json.dumps(result, ensure_ascii=False))

    if result["failure_class"] == "human_verification" or result["status"] == "human_verification_required":
        return 7
    if not result["observation_valid"]:
        return 4
    return 0 if result["coverage_complete"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
