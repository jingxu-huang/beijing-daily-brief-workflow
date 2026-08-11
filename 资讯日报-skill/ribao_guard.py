#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single scheduler/orchestrator for the daily information brief.

launchd wakes this program at a fixed interval. All business-time decisions
use trusted network UTC converted to Asia/Shanghai, never the host wall clock.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from beijing_time import BEIJING, TrustedClock, TimeAuthorityError, monotonic_seconds, to_beijing


CHECK_1 = dt.time(13, 30)
CHECK_2 = dt.time(14, 30)
WINDOW_END = dt.time(15, 0)
DEADLINE_ALERT = dt.time(15, 30)
DELIVERY_DEADLINE = dt.time(15, 30)
DAILY_RETRY_BACKOFF = dt.timedelta(minutes=10)
EXPECTED_FIXED_TIMES = {"check_1": "13:30", "check_2": "14:30"}
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def iso_bj(value: dt.datetime) -> str:
    return value.astimezone(BEIJING).isoformat(timespec="seconds")


def combine_bj(day: dt.date, at: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, at, tzinfo=BEIJING)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        return {"corrupted_previous": str(exc)[:300]}


def collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from collect_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from collect_strings(item)


def dig_message_id(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "message_id" and isinstance(item, str) and item:
                return item
            found = dig_message_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = dig_message_id(item)
            if found:
                return found
    return ""


def find_lark_cli() -> str:
    configured = os.getenv("LARK_CLI", "")
    if configured:
        return configured
    found = shutil.which("lark-cli")
    if found:
        return found
    candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/lark-cli"), reverse=True)
    return str(candidates[0]) if candidates else "/opt/homebrew/bin/lark-cli"


def osa_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def start_dialog(title: str, message: str) -> dict:
    script = (
        f"display dialog {osa_quote(message)} buttons {{\"知道了\"}} default button \"知道了\" "
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
        return {"started": False, "error": str(exc)[:300]}


def send_lark_verified(chat_id: str, text: str, key: str, send_as: str = "bot") -> dict:
    if not chat_id:
        return {"verified": False, "error": "FEISHU_REPORT_CHAT_ID is not configured"}
    cli = find_lark_cli()
    send = subprocess.run(
        [
            cli,
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--as",
            send_as,
            "--text",
            text,
            "--idempotency-key",
            key,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if send.returncode != 0:
        return {"verified": False, "error": send.stderr.strip()[:500], "exit": send.returncode}
    try:
        payload = json.loads(send.stdout)
    except json.JSONDecodeError:
        return {"verified": False, "error": "lark send returned non-JSON", "raw": send.stdout[:300]}
    message_id = dig_message_id(payload)
    if not message_id:
        return {"verified": False, "error": "lark send returned no message_id"}
    readback = subprocess.run(
        [
            cli,
            "im",
            "+messages-mget",
            "--as",
            send_as,
            "--message-ids",
            message_id,
            "--no-reactions",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if readback.returncode != 0:
        return {"verified": False, "message_id": message_id, "error": readback.stderr.strip()[:500]}
    try:
        returned = json.loads(readback.stdout)
        haystack = "\n".join(collect_strings(returned))
    except json.JSONDecodeError:
        haystack = readback.stdout
    expected = text[:80]
    verified = expected in haystack or text in haystack
    return {
        "verified": verified,
        "message_id": message_id,
        "error": "" if verified else "message readback did not contain expected text",
    }


def load_wewe_module(skill_dir: Path):
    path = skill_dir / "wewe_preflight.py"
    spec = importlib.util.spec_from_file_location("ribao_wewe_preflight", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_wewe(skill_dir: Path) -> dict:
    try:
        return load_wewe_module(skill_dir).probe_health()
    except Exception as exc:  # noqa: BLE001
        return {"status": "service_down", "reason": str(exc)[:1000]}


class Guard:
    def __init__(self, root: Path, runtime: Path, shadow: bool = False):
        self.root = root.expanduser().resolve()
        self.data_root = Path(os.getenv("RIBAO_DATA_ROOT", str(self.root))).expanduser().resolve()
        self.runtime = runtime
        self.shadow = shadow
        self.skill_dir = root / "资讯日报-skill"
        self.runtime_state_dir = runtime / "state"
        self.runtime_state_dir.mkdir(parents=True, exist_ok=True)
        self.chat_id = os.getenv("FEISHU_REPORT_CHAT_ID", "").strip()

    def probe_wewe(self) -> dict:
        if self.shadow:
            return {
                "status": os.getenv("RIBAO_SHADOW_WEWE_STATUS", "healthy"),
                "reason": os.getenv("RIBAO_SHADOW_WEWE_REASON", "shadow probe"),
            }
        return probe_wewe(self.skill_dir)

    def state_path(self, ymd: str) -> Path:
        return self.runtime_state_dir / f"beijing-guard-{ymd}.json"

    def save(self, state: dict) -> None:
        state["updated_at_bj"] = state.get("last_now_bj", "")
        path = self.state_path(state["target_ymd"])
        atomic_json(path, state)
        project_state = self.data_root / "日报状态"
        if self.data_root.exists():
            try:
                atomic_json(project_state / path.name, state)
                state.pop("project_mirror_error", None)
            except OSError as exc:
                state["project_mirror_error"] = str(exc)[:500]
                atomic_json(path, state)

    def mark_stage(self, state: dict, name: str, status: str, now_bj: dt.datetime, **extra) -> dict:
        entry = state.setdefault("stages", {}).setdefault(name, {})
        entry.update({"status": status, "at_bj": iso_bj(now_bj), **extra})
        self.save(state)
        return entry

    def fixed_config_ok(self) -> bool:
        configured = {
            "check_1": os.getenv("RIBAO_WEWE_CHECK_1", "13:30"),
            "check_2": os.getenv("RIBAO_WEWE_CHECK_2", "14:30"),
        }
        return configured == EXPECTED_FIXED_TIMES

    def reminder(self, state: dict, stage: str, probe: dict, now_bj: dt.datetime, catchup=False) -> None:
        entry = state.setdefault("stages", {}).setdefault(stage, {})
        entry["probe"] = probe
        # 只要尚未恢复到可用状态，固定提醒就必须执行。service_down 也算
        # “未恢复”，不能因为探测错误类型变化而吞掉 14:30 的第二次提醒。
        if probe.get("status") in {"healthy", "stale"}:
            entry.update(
                {
                    "status": "ok",
                    "at_bj": iso_bj(now_bj),
                    "reminder_required": False,
                    "catchup": catchup,
                    "sla_status": "missed" if catchup else "met",
                }
            )
            self.save(state)
            return
        if self.shadow:
            entry.update(
                {
                    "status": "shadow",
                    "at_bj": iso_bj(now_bj),
                    "reminder_required": True,
                    "catchup": catchup,
                    "sla_status": "missed" if catchup else "met",
                }
            )
            self.save(state)
            return

        previous = entry.get("reminder") or {}
        try:
            module = load_wewe_module(self.skill_dir)
            result = module.send_fixed_guard_reminder(
                target_ymd=state["target_ymd"],
                stage=stage.replace("wewe_check_", ""),
                reason=probe.get("reason", "微信读书账号失效"),
                send_feishu=not bool(previous.get("feishu_verified")),
                show_dialog=not bool(previous.get("dialog_started")),
                catchup=catchup,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)[:1000]}
        merged = dict(previous)
        merged.update(result)
        entry.update(
            {
                "status": "ok" if merged.get("feishu_verified") and merged.get("dialog_started") else "fail",
                "at_bj": iso_bj(now_bj),
                "reminder_required": True,
                "reminder": merged,
                "catchup": catchup,
                "sla_status": "missed" if catchup else "met",
            }
        )
        self.save(state)

    def process_wewe_checks(self, state: dict, now_bj: dt.datetime) -> None:
        stages = state.setdefault("stages", {})
        at_1330 = combine_bj(now_bj.date(), CHECK_1)
        at_1430 = combine_bj(now_bj.date(), CHECK_2)
        if now_bj < at_1330:
            return
        if now_bj < at_1430:
            entry = stages.get("wewe_check_1330", {})
            if entry.get("status") not in {"ok", "shadow"}:
                self.reminder(
                    state,
                    "wewe_check_1330",
                    self.probe_wewe(),
                    now_bj,
                    catchup=now_bj > at_1330 + dt.timedelta(minutes=3),
                )
            return

        first = stages.get("wewe_check_1330")
        second = stages.get("wewe_check_1430", {})
        if second.get("status") in {"ok", "shadow"}:
            return
        probe = self.probe_wewe()
        if not first:
            stages["wewe_check_1330"] = {
                "status": "missed",
                "at_bj": iso_bj(now_bj),
                "reason": "device or guard was unavailable at 13:30",
            }
            self.reminder(state, "wewe_check_1430", probe, now_bj, catchup=True)
        else:
            self.reminder(state, "wewe_check_1430", probe, now_bj, catchup=now_bj > at_1430 + dt.timedelta(minutes=3))

    def prefetch(self, state: dict, now_utc: dt.datetime, now_bj: dt.datetime) -> None:
        entry = state.setdefault("stages", {}).get("prefetch", {})
        if entry.get("status") == "ok" or self.shadow or not self.root.exists() or not self.data_root.exists():
            return
        probe = (
            state.get("stages", {}).get("wewe_check_1430", {}).get("probe")
            or self.probe_wewe()
        )
        excluded = probe.get("status") in {"auth_required", "service_down"}
        output_dir = self.data_root / "日报状态" / "prefetch"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"资讯日报-预抓-{state['target_ymd']}.md"
        env = os.environ.copy()
        env.update(
            {
                "RIBAO_TRUSTED_UTC": now_utc.isoformat(),
                "RIBAO_EXCLUDE_WEWE": "1" if excluded else "0",
                "RIBAO_PROJECT_ROOT": str(self.root),
                "RIBAO_DATA_ROOT": str(self.data_root),
                "RIBAO_DRAFT_DIR": str(self.data_root / "日报草稿"),
                "RIBAO_REPORT_DIR": str(self.data_root / "日报文档"),
                "RIBAO_STATE_DIR": str(self.data_root / "日报状态"),
            }
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.skill_dir / "fetch_news.py"),
                    "--date",
                    state["target_ymd"],
                    "--allow-open-window",
                    "--out",
                    str(output),
                ],
                cwd=self.skill_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=12 * 60,
                check=False,
            )
            returncode = result.returncode
            stderr = result.stderr[-1500:]
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stderr = f"prefetch timed out after {exc.timeout}s"
        self.mark_stage(
            state,
            "prefetch",
            "ok" if returncode == 0 else "fail",
            now_bj,
            output=str(output),
            wewe_status=probe.get("status"),
            stderr=stderr,
        )

    def existing_sent(self, ymd: str) -> dict:
        manifest = self.data_root / "日报状态" / f"run-manifest-{ymd}.json"
        data = load_json(manifest)
        return data if data.get("final") == "sent" else {}

    @staticmethod
    def daily_retry_due(daily: dict, now_bj: dt.datetime) -> bool:
        if daily.get("status") != "fail":
            return True
        raw = daily.get("next_retry_at_bj", "")
        if not raw:
            return True
        try:
            return now_bj >= dt.datetime.fromisoformat(raw).astimezone(BEIJING)
        except (TypeError, ValueError):
            return True

    def freeze_wewe_at_window_close(self, state: dict, now_bj: dt.datetime) -> dict:
        """Freeze the 15:00 WeWe decision for every later retry that day."""
        frozen = state.get("wewe_at_window_close")
        if frozen:
            return frozen
        probe = self.probe_wewe()
        exclude_wewe = probe.get("status") in {"auth_required", "service_down"}
        frozen = {
            "status": probe.get("status"),
            "reason": probe.get("reason", ""),
            "wewe_included": not exclude_wewe,
            "at_bj": iso_bj(now_bj),
            "immutable": True,
        }
        state["wewe_at_window_close"] = frozen
        self.save(state)
        return frozen

    def deadline_alert(self, state: dict, now_bj: dt.datetime, reason: str) -> None:
        previous = state.get("deadline_alert") or {}
        if previous.get("feishu_verified") and previous.get("dialog_started"):
            return
        title = f"资讯日报 {state['target_ymd']} 未在 15:30 前送达"
        text = f"【日报失败告警】{state['target_ymd']}：{reason}。后台会继续处理，通过质检后补发完整正式版。"
        if self.shadow:
            state["deadline_alert"] = {"shadow": True, "reason": reason, "at_bj": iso_bj(now_bj)}
            self.save(state)
            return
        sent = previous
        if not previous.get("feishu_verified"):
            sent = send_lark_verified(
                self.chat_id,
                text,
                f"ribao-{state['target_ymd']}-deadline-failed",
            )
        dialog = previous
        if not previous.get("dialog_started"):
            dialog = start_dialog(title, text)
        state["deadline_alert"] = {
            "at_bj": iso_bj(now_bj),
            "reason": reason,
            "feishu_verified": bool(sent.get("verified") or previous.get("feishu_verified")),
            "message_id": sent.get("message_id") or previous.get("message_id", ""),
            "feishu_error": sent.get("error", ""),
            "dialog_started": bool(dialog.get("started") or previous.get("dialog_started")),
            "dialog_error": dialog.get("error", ""),
        }
        self.save(state)

    def run_daily(self, state: dict, now_utc: dt.datetime, now_bj: dt.datetime) -> None:
        if self.shadow:
            self.mark_stage(state, "daily", "shadow", now_bj)
            return
        existing = self.existing_sent(state["target_ymd"])
        if existing:
            self.mark_stage(
                state,
                "daily",
                "sent",
                now_bj,
                adopted_existing=True,
                doc_url=existing.get("doc_url", ""),
            )
            return
        if not self.root.exists() or not self.data_root.exists():
            self.mark_stage(state, "daily", "fail", now_bj, error="project root/external disk unavailable")
            if now_bj >= combine_bj(now_bj.date(), DEADLINE_ALERT):
                self.deadline_alert(state, now_bj, "外接硬盘或日报项目目录不可用")
            return

        frozen_wewe = self.freeze_wewe_at_window_close(state, now_bj)
        exclude_wewe = not bool(frozen_wewe.get("wewe_included"))

        previous_daily = state.setdefault("stages", {}).get("daily", {})
        attempts = int(previous_daily.get("attempts") or 0) + 1
        draft = self.data_root / "日报草稿" / f"资讯日报-草稿-{state['target_ymd']}.md"
        env = os.environ.copy()
        env.update(
            {
                "RIBAO_TARGET_YMD": state["target_ymd"],
                "RIBAO_TRUSTED_UTC": now_utc.isoformat(),
                "RIBAO_TRUSTED_MONOTONIC": str(monotonic_seconds()),
                "RIBAO_EXCLUDE_WEWE": "1" if exclude_wewe else "0",
                "RIBAO_WEWE_STATUS": frozen_wewe.get("status", "unknown"),
                "RIBAO_GUARD_MANAGED": "1",
                "RIBAO_PROJECT_ROOT": str(self.root),
                "RIBAO_DATA_ROOT": str(self.data_root),
                "RIBAO_DRAFT_DIR": str(self.data_root / "日报草稿"),
                "RIBAO_REPORT_DIR": str(self.data_root / "日报文档"),
                "RIBAO_CHECK_DIR": str(self.data_root / "日报质检"),
                "RIBAO_STATE_DIR": str(self.data_root / "日报状态"),
                # All Codex state, caches, temporary files and candidates live on
                # the external volume. There is deliberately no local fallback.
                "RIBAO_CODEX_WORK_BASE": str(self.data_root / "日报状态" / "codex-work"),
                "RIBAO_CODEX_CONTROL_HOME": str(self.runtime / "codex-control"),
                "RIBAO_REUSE_DRAFT": "1" if attempts > 1 and draft.is_file() else "0",
            }
        )
        entry = self.mark_stage(
            state,
            "daily",
            "running",
            now_bj,
            wewe_included=not exclude_wewe,
            attempts=attempts,
            next_retry_at_bj="",
        )
        process = subprocess.Popen(
            ["/bin/bash", str(self.root / "run_daily_with_feishu.sh")],
            cwd=self.root,
            env=env,
        )
        entry["pid"] = process.pid
        self.save(state)

        run_started_mono = time.monotonic()
        alert_at = combine_bj(now_bj.date(), DEADLINE_ALERT)
        seconds_until_alert = max(0.0, (alert_at - now_bj).total_seconds())
        alert_mono = time.monotonic() + seconds_until_alert
        alerted = False
        while process.poll() is None:
            if not alerted and time.monotonic() >= alert_mono:
                self.deadline_alert(state, alert_at, "正式日报仍在生成、质检或上传")
                alerted = True
            time.sleep(2)

        final_manifest = load_json(self.data_root / "日报状态" / f"run-manifest-{state['target_ymd']}.json")
        sent = final_manifest.get("final") == "sent"
        finish_bj = now_bj + dt.timedelta(seconds=max(0, time.monotonic() - run_started_mono))
        next_retry = "" if sent else iso_bj(finish_bj + DAILY_RETRY_BACKOFF)
        self.mark_stage(
            state,
            "daily",
            "sent" if sent else "fail",
            finish_bj,
            exit_code=process.returncode,
            final=final_manifest.get("final", ""),
            doc_url=final_manifest.get("doc_url", ""),
            attempts=attempts,
            next_retry_at_bj=next_retry,
        )
        if not sent and finish_bj >= combine_bj(now_bj.date(), DEADLINE_ALERT):
            self.deadline_alert(state, finish_bj, f"日报流程退出码 {process.returncode}，终态 {final_manifest.get('final', 'unknown')}")

    def run(self, now_utc: dt.datetime, time_meta: dict) -> dict:
        now_bj = to_beijing(now_utc)
        ymd = now_bj.strftime("%Y%m%d")
        state = load_json(self.state_path(ymd))
        state.setdefault("target_ymd", ymd)
        state.setdefault("created_at_bj", iso_bj(now_bj))
        state["last_now_utc"] = now_utc.isoformat()
        state["last_now_bj"] = iso_bj(now_bj)
        state["time_authority"] = time_meta
        state["shadow"] = self.shadow
        self.save(state)

        activate = os.getenv("RIBAO_GUARD_ACTIVATE_YMD", "")
        if activate and ymd < activate:
            state["activation_wait"] = activate
            self.save(state)
            return state
        if not self.fixed_config_ok():
            self.deadline_alert(state, now_bj, "固定 WeWe 提醒时间配置被修改，守门器拒绝运行")
            state["config_error"] = True
            self.save(state)
            return state

        self.process_wewe_checks(state, now_bj)
        if now_bj >= combine_bj(now_bj.date(), CHECK_2) and now_bj < combine_bj(now_bj.date(), WINDOW_END):
            self.prefetch(state, now_utc, now_bj)
        if now_bj >= combine_bj(now_bj.date(), WINDOW_END):
            daily = state.setdefault("stages", {}).get("daily", {})
            if daily.get("status") != "sent":
                running_pid = daily.get("pid") if daily.get("status") == "running" else None
                if running_pid:
                    try:
                        os.kill(int(running_pid), 0)
                        if now_bj >= combine_bj(now_bj.date(), DELIVERY_DEADLINE):
                            self.deadline_alert(state, now_bj, "正式日报仍在后台生成、质检或上传")
                        return state
                    except (OSError, TypeError, ValueError):
                        pass
                if not self.daily_retry_due(daily, now_bj):
                    if now_bj >= combine_bj(now_bj.date(), DELIVERY_DEADLINE):
                        self.deadline_alert(state, now_bj, "正式日报失败后等待后台重试")
                    return state
                if now_bj >= combine_bj(now_bj.date(), DELIVERY_DEADLINE):
                    self.deadline_alert(state, now_bj, "守门器在 15:30 后才获得执行机会")
                self.run_daily(state, now_utc, now_bj)
        return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=os.getenv("RIBAO_PROJECT_ROOT", str(DEFAULT_ROOT)))
    parser.add_argument(
        "--runtime-dir",
        default=os.getenv(
            "RIBAO_GUARD_RUNTIME_DIR",
            str(Path.home() / "Library/Application Support/ribao-guard"),
        ),
    )
    parser.add_argument("--shadow", action="store_true", default=os.getenv("RIBAO_GUARD_SHADOW", "0") == "1")
    parser.add_argument("--now-utc", default="", help="test/shadow only; ISO UTC")
    parser.add_argument("--print-status", action="store_true")
    parser.add_argument(
        "--codex-probe",
        action="store_true",
        help="launchd-only external storage/permission/model probe; never sends Feishu",
    )
    parser.add_argument("--codex-probe-model", default=os.getenv("RIBAO_CODEX_MODEL", "gpt-5.6-sol"))
    parser.add_argument("--codex-probe-daily-date", default="")
    args = parser.parse_args()
    if args.now_utc and not args.shadow:
        parser.error("--now-utc is allowed only with --shadow")

    runtime = Path(args.runtime_dir).expanduser()
    runtime.mkdir(parents=True, exist_ok=True)
    lock_handle = (runtime / "guard.lock").open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    clock = TrustedClock(runtime / "trusted-time.json")
    try:
        now_utc, meta = clock.now(args.now_utc)
    except TimeAuthorityError as exc:
        print(f"[ribao-guard] trusted time unavailable: {exc}", file=sys.stderr)
        return 2

    if args.codex_probe:
        root = Path(args.project_root).resolve()
        helper = root / "资讯日报-skill" / "codex_external.py"
        codex = shutil.which("codex") or ""
        ymd = to_beijing(now_utc).strftime("%Y%m%d")
        if not codex or not helper.is_file():
            print("[ribao-guard] Codex probe prerequisites missing", file=sys.stderr)
            return 3
        result = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--root",
                str(root),
                "prompt-probe",
                "--date",
                ymd,
                "--codex",
                codex,
                "--control-home",
                str(runtime / "codex-control"),
                "--model",
                args.codex_probe_model,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=12 * 60,
            check=False,
        )
        try:
            prompt_payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            prompt_payload = {"ok": False, "raw_stdout": result.stdout[-2000:]}
        daily_payload = None
        daily_exit = 0
        daily_stderr = ""
        if args.codex_probe_daily_date:
            daily_result = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "--root",
                    str(root),
                    "daily-probe",
                    "--date",
                    args.codex_probe_daily_date,
                    "--codex",
                    codex,
                    "--control-home",
                    str(runtime / "codex-control"),
                    "--model",
                    args.codex_probe_model,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20 * 60,
                check=False,
            )
            daily_exit = daily_result.returncode
            daily_stderr = daily_result.stderr[-2000:]
            try:
                daily_payload = json.loads(daily_result.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                daily_payload = {"ok": False, "raw_stdout": daily_result.stdout[-2000:]}
        overall_ok = (
            result.returncode == 0
            and prompt_payload.get("ok")
            and (daily_payload is None or (daily_exit == 0 and daily_payload.get("ok")))
        )
        payload = {
            "ok": bool(overall_ok),
            "prompt_only": prompt_payload,
            "daily_no_send": daily_payload,
        }
        payload.update(
            {
                "source": "com.example.beijing-daily-brief-guard launchd job",
                "no_send": True,
                "exit_code": result.returncode if result.returncode else daily_exit,
                "stderr": (result.stderr[-2000:] + "\n" + daily_stderr).strip(),
                "trusted_time": meta,
            }
        )
        data_root = Path(os.getenv("RIBAO_DATA_ROOT", str(root))).expanduser().resolve()
        atomic_json(data_root / "日报状态" / "codex-launchd-probe.json", payload)
        if args.print_status:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 4

    guard = Guard(Path(args.project_root), runtime, shadow=args.shadow)
    state = guard.run(now_utc, meta)
    if args.print_status:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
