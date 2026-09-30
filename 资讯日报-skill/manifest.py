#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run-manifest 写入器：日报流水线每一步落一条状态到 日报状态/run-manifest-YYYYMMDD.json，
供飞书告警、Cowork 看门狗质检、事后复盘读取。

设计约束：本脚本绝不能拖垮主流程——任何异常只打 stderr 警告，退出码恒为 0。

用法（shell 调用）：
    python3 manifest.py --date 20260708 --step fetch --status ok
    python3 manifest.py --date 20260708 --step qc --status fail --detail "verdict=FAIL"
    python3 manifest.py --date 20260708 --kv verdict=PASS --kv doc_url=https://...
    python3 manifest.py --date 20260708 --final sent
    python3 manifest.py --date 20260708 --read-step-status fetch

manifest 结构：
    {
      "date": "20260708",
      "created_at": "...", "updated_at": "...",
      "steps": {"fetch": {"status": "ok", "at": "...", "detail": "..."}, ...},
      "verdict": "PASS", "doc_url": "...", "publish_mode": "new|update",
      "final": "sent | blocked | failed"
    }
时间戳一律为真实北京时间（UTC+8）。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

BJ_TZ = dt.timezone(dt.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(HERE, ".."))


def monotonic_seconds():
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return time.monotonic()


def now_str():
    """真实北京时间。系统 UTC 可能被时区/墙钟配错整体拨快或拨慢（2026-07-13 实测快约
    15 小时），wrapper 网络对表后经 RIBAO_CLOCK_SKEW_S（系统UTC−网络UTC，秒）传入偏差；
    超过 300s 时先扣除再换算，保证看门狗按 updated_at 判"卡死/生成中"不失真。"""
    trusted = os.environ.get("RIBAO_TRUSTED_UTC", "").strip()
    trusted_mono = os.environ.get("RIBAO_TRUSTED_MONOTONIC", "").strip()
    if trusted:
        try:
            now = dt.datetime.fromisoformat(trusted.replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=dt.timezone.utc)
            now = now.astimezone(dt.timezone.utc)
            if trusted_mono:
                elapsed = max(0.0, monotonic_seconds() - float(trusted_mono))
                now += dt.timedelta(seconds=elapsed)
            return now.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S+08:00")
        except (TypeError, ValueError):
            pass
    now = dt.datetime.now(dt.timezone.utc)
    try:
        skew = int(os.environ.get("RIBAO_CLOCK_SKEW_S", ""))
        if abs(skew) > 300:
            now -= dt.timedelta(seconds=skew)
    except (TypeError, ValueError):
        pass
    return now.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S+08:00")


def read_step_status(path, step):
    """Return a prior step status without mutating the manifest.

    Missing, malformed or incomplete state is deliberately treated as unknown so
    callers fail closed instead of reusing an unverified artifact.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        status = data.get("steps", {}).get(step, {}).get("status", "")
    except (OSError, TypeError, ValueError):
        return ""
    return status if isinstance(status, str) else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--step", default="", help="步骤名，如 fetch / compose / qc / publish")
    ap.add_argument("--status", default="", help="ok | fail | warn（配合 --step）")
    ap.add_argument("--detail", default="", help="补充说明（配合 --step）")
    ap.add_argument("--kv", action="append", default=[], help="顶层键值，如 verdict=PASS，可多次")
    ap.add_argument("--final", default="", help="终态：sent | blocked | failed")
    ap.add_argument("--read-step-status", default="", help="只读输出既有步骤状态，不更新 manifest")
    ap.add_argument("--state-dir", default=os.environ.get("RIBAO_STATE_DIR") or os.path.join(ROOT_DIR, "日报状态"))
    args = ap.parse_args()

    path = os.path.join(args.state_dir, f"run-manifest-{args.date}.json")
    if args.read_step_status:
        print(read_step_status(path, args.read_step_status))
        return

    os.makedirs(args.state_dir, exist_ok=True)

    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"corrupted_previous": True}

    data.setdefault("date", args.date)
    data.setdefault("created_at", now_str())
    data["updated_at"] = now_str()

    if args.step:
        entry = data.setdefault("steps", {}).setdefault(args.step, {})
        entry["status"] = args.status or "ok"
        entry["at"] = now_str()
        if args.detail:
            entry["detail"] = args.detail[:2000]
        else:
            entry.pop("detail", None)
    for kv in args.kv:
        if "=" in kv:
            k, v = kv.split("=", 1)
            data[k] = v
    if args.final:
        data["final"] = args.final
        if args.final in {"generated", "sent"}:
            data.pop("run_exit", None)
        if args.final == "sent":
            data.pop("reason", None)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 —— manifest 绝不阻断主流程
        print(f"[manifest] WARN: {exc}", file=sys.stderr)
    sys.exit(0)
