#!/usr/bin/env python3
"""Fail-closed external-volume runtime for the daily-brief Codex step."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_ROOT = Path(os.getenv("RIBAO_PROJECT_ROOT", str(SOURCE_ROOT)))
DEFAULT_DATA_ROOT = Path(os.getenv("RIBAO_DATA_ROOT", str(DEFAULT_PROJECT_ROOT)))
DEFAULT_CONTROL_HOME = Path(
    os.getenv(
        "RIBAO_CODEX_CONTROL_HOME",
        str(Path.home() / "Library/Application Support/beijing-daily-brief/codex-control"),
    )
)
EXISTING_CODEX_HOME = Path(
    os.getenv("RIBAO_EXISTING_CODEX_HOME", str(Path.home() / ".codex"))
)
WORK_NAME_RE = re.compile(r"^ribao-(\d{8})-[A-Za-z0-9._-]+$")
FORBIDDEN_SYSTEM_ROOTS = (
    Path("/Users"),
    Path("/tmp"),
    Path("/private/tmp"),
    Path("/var/tmp"),
    Path("/private/var/tmp"),
)
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


class ExternalRuntimeError(RuntimeError):
    pass


def resolved(path: Path, *, strict: bool = False) -> Path:
    return path.expanduser().resolve(strict=strict)


def is_within(path: Path, parent: Path) -> bool:
    try:
        resolved(path).relative_to(resolved(parent))
        return True
    except ValueError:
        return False


def require_within(path: Path, parent: Path, label: str) -> Path:
    actual = resolved(path)
    boundary = resolved(parent)
    if actual != boundary and boundary not in actual.parents:
        raise ExternalRuntimeError(f"{label} escaped required boundary: {actual}")
    return actual


def reject_system_path(path: Path, label: str) -> None:
    actual = resolved(path)
    for forbidden in FORBIDDEN_SYSTEM_ROOTS:
        if actual == forbidden or forbidden in actual.parents:
            raise ExternalRuntimeError(f"{label} points to forbidden system storage: {actual}")


def configured_project_root() -> Path:
    return Path(os.getenv("RIBAO_PROJECT_ROOT", str(DEFAULT_PROJECT_ROOT)))


def configured_data_root() -> Path:
    return Path(os.getenv("RIBAO_DATA_ROOT", str(DEFAULT_DATA_ROOT)))


def mount_for(path: Path) -> Path:
    current = resolved(path, strict=True)
    while current.parent != current and not current.is_mount():
        current = current.parent
    return current


def validate_data_root() -> Path:
    data_root = resolved(configured_data_root(), strict=True)
    if os.getenv("RIBAO_ALLOW_SYSTEM_DATA_ROOT", "0") != "1":
        reject_system_path(data_root, "RIBAO_DATA_ROOT")
        mount = mount_for(data_root)
        if mount == Path("/"):
            raise ExternalRuntimeError(
                "RIBAO_DATA_ROOT must live on a dedicated non-root mount; "
                "set it to an external-volume directory"
            )
    if shutil.disk_usage(data_root).free < MIN_FREE_BYTES:
        raise ExternalRuntimeError("data volume has less than 2 GiB free")
    return data_root


def validate_root(root: Path) -> Path:
    actual = resolved(root, strict=True)
    expected = resolved(configured_project_root(), strict=True)
    if actual != expected:
        raise ExternalRuntimeError(f"unexpected project root: {actual}; expected {expected}")
    validate_data_root()
    return actual


def validate_workflow_paths(root: Path) -> None:
    validate_root(root)
    data_root = validate_data_root()
    if os.getenv("RIBAO_CODEX_STAGING_BASE"):
        raise ExternalRuntimeError("legacy RIBAO_CODEX_STAGING_BASE is forbidden")
    checks = {
        "RIBAO_DRAFT_DIR": data_root / "日报草稿",
        "RIBAO_REPORT_DIR": data_root / "日报文档",
        "RIBAO_CHECK_DIR": data_root / "日报质检",
        "RIBAO_STATE_DIR": data_root / "日报状态",
        "RIBAO_CODEX_HOME": data_root / "日报状态" / "codex-home",
        "RIBAO_CODEX_WORK_BASE": data_root / "日报状态" / "codex-work",
    }
    for env_name, fallback in checks.items():
        path = Path(os.getenv(env_name, str(fallback)))
        if os.getenv("RIBAO_ALLOW_SYSTEM_DATA_ROOT", "0") != "1":
            reject_system_path(path, env_name)
        require_within(path, data_root, env_name)


def ensure_link(link: Path, target: Path, label: str) -> None:
    if link.is_symlink():
        if resolved(link, strict=True) != resolved(target, strict=True):
            raise ExternalRuntimeError(f"{label} symlink target mismatch: {link}")
        return
    if link.exists():
        raise ExternalRuntimeError(f"{label} must be a symlink: {link}")
    link.symlink_to(target)


def prepare(root: Path, ymd: str) -> Path:
    if not re.fullmatch(r"\d{8}", ymd):
        raise ExternalRuntimeError(f"invalid target date: {ymd}")
    root = validate_root(root)
    validate_workflow_paths(root)
    data_root = validate_data_root()
    state = data_root / "日报状态"
    codex_home = state / "codex-home"
    work_base = state / "codex-work"
    codex_home.mkdir(parents=True, exist_ok=True)
    work_base.mkdir(parents=True, exist_ok=True)
    require_within(codex_home, data_root, "CODEX_HOME")
    require_within(work_base, data_root, "Codex work base")

    config = root / "资讯日报-skill" / "config" / "codex-ribao-prompt-only.config.toml"
    auth = EXISTING_CODEX_HOME / "auth.json"
    if not config.is_file():
        raise ExternalRuntimeError(f"missing Codex config: {config}")
    if not auth.is_file():
        raise ExternalRuntimeError(f"missing existing Codex auth: {auth}")
    ensure_link(codex_home / "config.toml", config, "Codex config")
    ensure_link(codex_home / "auth.json", auth, "Codex auth")

    work = Path(tempfile.mkdtemp(prefix=f"ribao-{ymd}-", dir=work_base))
    for name in ("tmp", "home", "cache", "config", "data"):
        (work / name).mkdir()
    metadata = {
        "target_ymd": ymd,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "work_dir": str(work),
        "codex_home": str(codex_home),
        "storage_policy": "external-only",
    }
    atomic_write(work / "run.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return work


def validate_work_dir(root: Path, work: Path, ymd: Optional[str] = None) -> Path:
    root = validate_root(root)
    work_base = validate_data_root() / "日报状态" / "codex-work"
    actual = require_within(work, work_base, "Codex work directory")
    match = WORK_NAME_RE.fullmatch(actual.name)
    if not match or (ymd and match.group(1) != ymd):
        raise ExternalRuntimeError(f"invalid Codex work directory name: {actual.name}")
    return actual


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def extract_report(root: Path, work: Path, ymd: str) -> Path:
    work = validate_work_dir(root, work, ymd)
    result_path = work / "result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalRuntimeError(f"invalid Codex result JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"report_markdown"}:
        raise ExternalRuntimeError("Codex result must contain only report_markdown")
    report = payload["report_markdown"]
    if not isinstance(report, str) or not 100 <= len(report) <= 200_000:
        raise ExternalRuntimeError("report_markdown length is invalid")
    stripped = report.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise ExternalRuntimeError("report_markdown must not be wrapped in a code fence")
    candidate = work / "report.md"
    atomic_write(candidate, stripped + "\n")
    return candidate


def validate_publish(root: Path, work: Path, report: Path, check: Path) -> None:
    root = validate_root(root)
    data_root = validate_data_root()
    work = validate_work_dir(root, work)
    candidate = work / "report.md"
    candidate_check = work / "check.md"
    if not candidate.is_file() or not candidate_check.is_file():
        raise ExternalRuntimeError("candidate report or check file is missing")
    expected_report_dir = resolved(data_root / "日报文档", strict=True)
    expected_check_dir = resolved(data_root / "日报质检", strict=True)
    if resolved(report.parent, strict=True) != expected_report_dir:
        raise ExternalRuntimeError(f"unexpected final report directory: {report.parent}")
    if resolved(check.parent, strict=True) != expected_check_dir:
        raise ExternalRuntimeError(f"unexpected final check directory: {check.parent}")
    if report.is_symlink() or check.is_symlink():
        raise ExternalRuntimeError("final report/check path must not be a symlink")


def cleanup_success(root: Path, ymd: str) -> int:
    root = validate_root(root)
    work_base = validate_data_root() / "日报状态" / "codex-work"
    if not work_base.is_dir():
        return 0
    removed = 0
    for item in work_base.iterdir():
        match = WORK_NAME_RE.fullmatch(item.name)
        if not match or match.group(1) != ymd or not item.is_dir():
            continue
        actual = validate_work_dir(root, item, ymd)
        shutil.rmtree(actual)
        removed += 1
    return removed


def runtime_env(root: Path, work: Path) -> dict[str, str]:
    root = validate_root(root)
    work = validate_work_dir(root, work)
    codex_home = validate_data_root() / "日报状态" / "codex-home"
    return {
        "CODEX_HOME": str(codex_home),
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "XDG_CACHE_HOME": str(work / "cache"),
        "XDG_CONFIG_HOME": str(work / "config"),
        "XDG_DATA_HOME": str(work / "data"),
    }


def build_exec_command(
    codex: str,
    work: Path,
    model: str,
    schema: Path,
    result: Path,
    prompt: str,
) -> list[str]:
    command = [
        codex,
        "exec",
        "--strict-config",
        "--model",
        model,
        "--cd",
        str(work),
        "--ephemeral",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(result),
        "--json",
        prompt,
    ]
    forbidden = {"workspace-write", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"}
    if "--ephemeral" not in command or any(value in command for value in forbidden):
        raise ExternalRuntimeError("unsafe Codex production command")
    return command


def validate_control_home(control_home: Path) -> Path:
    actual = resolved(control_home, strict=True)
    expected = resolved(
        Path(os.getenv("RIBAO_CODEX_CONTROL_HOME", str(DEFAULT_CONTROL_HOME))),
        strict=True,
    )
    if actual != expected:
        raise ExternalRuntimeError(f"unexpected Codex control home: {actual}")
    required = ("config.toml", "report-output.schema.json", "auth.json")
    missing = [name for name in required if not (actual / name).is_file()]
    if missing:
        raise ExternalRuntimeError(f"Codex control home is incomplete: {', '.join(missing)}")
    return actual


def build_prompt_only_command(
    codex: str,
    control_home: Path,
    model: str,
) -> list[str]:
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
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--disable",
        "shell_snapshot",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "browser_use",
        "--disable",
        "computer_use",
        "--disable",
        "multi_agent",
        "--output-schema",
        str(control_home / "report-output.schema.json"),
        "--json",
        "-",
    ]
    joined = " ".join(command)
    if "--ephemeral" not in command or "danger-full-access" in joined or "workspace-write" in joined:
        raise ExternalRuntimeError("unsafe prompt-only Codex command")
    if "--output-last-message" in command or "--add-dir" in command:
        raise ExternalRuntimeError("Codex must return over stdout only")
    return command


def last_agent_message(events_text: str) -> str:
    last = ""
    for line in events_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            last = item["text"]
    if not last:
        raise ExternalRuntimeError("Codex JSONL contained no completed agent message")
    return last


def run_prompt_only(
    root: Path,
    work: Path,
    codex: str,
    control_home: Path,
    model: str,
    prompt: str,
) -> Path:
    root = validate_root(root)
    work = validate_work_dir(root, work)
    control_home = validate_control_home(control_home)
    command = build_prompt_only_command(codex, control_home, model)
    env = os.environ.copy()
    env.update(
        {
            # Reuse the already-existing Codex auth/state control plane.  With
            # --ephemeral it does not create a daily rollout/session; daily
            # inputs and outputs never enter this directory.
            "CODEX_HOME": str(EXISTING_CODEX_HOME),
            "TMPDIR": str(work / "tmp"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_DATA_HOME": str(work / "data"),
        }
    )
    try:
        result = subprocess.run(
            command,
            cwd="/",
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=12 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        atomic_write(work / "codex.stderr.log", f"prompt-only Codex timed out after {exc.timeout}s\n")
        raise ExternalRuntimeError("prompt-only Codex timed out") from exc
    atomic_write(work / "codex.events.jsonl", result.stdout)
    atomic_write(work / "codex.stderr.log", result.stderr)
    if result.returncode != 0:
        raise ExternalRuntimeError(f"prompt-only Codex exited {result.returncode}")
    final_text = last_agent_message(result.stdout)
    try:
        payload = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise ExternalRuntimeError(f"final agent message was not JSON: {exc}") from exc
    atomic_write(work / "result.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return work / "result.json"


def build_daily_prompt(root: Path, ymd: str, draft: Path) -> str:
    root = validate_root(root)
    data_root = validate_data_root()
    draft = require_within(draft, data_root / "日报草稿", "daily draft")
    if not draft.is_file() or draft.name != f"资讯日报-草稿-{ymd}.md":
        raise ExternalRuntimeError(f"unexpected daily draft: {draft}")
    skill = root / "资讯日报-skill" / "SKILL.md"
    format_spec = root / "资讯日报-skill" / "format" / "格式规范.md"
    reports = []
    report_dir = data_root / "日报文档"
    if report_dir.is_dir():
        candidates = sorted(
            (path for path in report_dir.glob("资讯类简报-*.md") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates[:3]:
            reports.append(f"\n<prior_report name={json.dumps(path.name, ensure_ascii=False)}>\n{path.read_text(encoding='utf-8')}\n</prior_report>")
    prompt = f"""
你要生成目标日期 {ymd} 的资讯日报。以下是全部且唯一的输入；不要调用任何工具，不要读写任何文件，不要联网，不要发送飞书。
严格执行 skill 与格式规范，逐条做主题准入、按正文而非来源归类、合并同一事件，并在输出前完成自检。
最终回复必须严格符合 JSON schema，只包含 report_markdown；值是完整 Markdown 正文，不要用代码围栏包裹。

<skill>
{skill.read_text(encoding='utf-8')}
</skill>

<format_spec>
{format_spec.read_text(encoding='utf-8')}
</format_spec>

<daily_draft name={json.dumps(draft.name, ensure_ascii=False)}>
{draft.read_text(encoding='utf-8')}
</daily_draft>

<style_references>
{''.join(reports)}
</style_references>
""".strip()
    if len(prompt) > 1_500_000:
        raise ExternalRuntimeError(f"embedded daily prompt is unexpectedly large: {len(prompt)} chars")
    return prompt


def compose_prompt_only(
    root: Path,
    work: Path,
    ymd: str,
    draft: Path,
    codex: str,
    control_home: Path,
    model: str,
) -> Path:
    prompt = build_daily_prompt(root, ymd, draft)
    run_prompt_only(root, work, codex, control_home, model, prompt)
    return extract_report(root, work, ymd)


def repair_prompt_only(
    root: Path,
    work: Path,
    ymd: str,
    draft: Path,
    candidate: Path,
    check: Path,
    codex: str,
    control_home: Path,
    model: str,
) -> Path:
    root = validate_root(root)
    work = validate_work_dir(root, work, ymd)
    candidate = require_within(candidate, work, "repair candidate")
    check = require_within(check, work, "repair check")
    if not candidate.is_file() or not check.is_file():
        raise ExternalRuntimeError("repair candidate/check is missing")
    base = build_daily_prompt(root, ymd, draft)
    prompt = f"""{base}

<previous_candidate>
{candidate.read_text(encoding='utf-8')}
</previous_candidate>

<quality_check>
{check.read_text(encoding='utf-8')}
</quality_check>

上一版候选稿未通过独立质检。请修复“必须修复”中的每一项，返回完整的修订后日报，不是差异或解释。
若草稿的精选条目因主题准入、旧闻或其他规则应被排除，必须按 skill/格式规范在成稿中加入精确 QA_SKIP 声明，不得只是沉默遗漏。
继续严格输出只含 report_markdown 的 schema JSON，不调用任何工具。"""
    run_prompt_only(root, work, codex, control_home, model, prompt)
    return extract_report(root, work, ymd)


def run_quality_check(root: Path, work: Path, draft: Path, candidate: Path) -> tuple[int, str, Path, str]:
    check = work / "check.md"
    qc = subprocess.run(
        [
            "/usr/bin/python3",
            str(root / "资讯日报-skill" / "check_report.py"),
            "--draft",
            str(draft),
            "--report",
            str(candidate),
            "--write",
            str(check),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5 * 60,
        check=False,
    )
    verdict = ""
    if check.is_file():
        for line in check.read_text(encoding="utf-8").splitlines():
            if "结论：" in line:
                verdict = line.split("结论：", 1)[1].strip()
                break
    return qc.returncode, verdict, check, qc.stderr


def prompt_only_probe(
    root: Path,
    codex: str,
    control_home: Path,
    ymd: str,
    model: str,
) -> dict:
    work = prepare(root, ymd)
    marker = "RIBAO_PROMPT_ONLY_LAUNCHD_PROBE_OK"
    prompt = (
        "This is a fixed no-send launchd probe. Do not use tools. Return schema-valid "
        f"report_markdown of at least 100 characters containing the exact marker {marker}. "
        "State that Codex received the input over an in-memory pipe and did not access daily files."
    )
    error = ""
    extracted = ""
    try:
        run_prompt_only(root, work, codex, control_home, model, prompt)
        candidate = extract_report(root, work, ymd)
        extracted = candidate.read_text(encoding="utf-8")
    except ExternalRuntimeError as exc:
        error = str(exc)
    events = (work / "codex.events.jsonl").read_text(encoding="utf-8") if (work / "codex.events.jsonl").is_file() else ""
    tool_events = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
            tool_events.append(item.get("type"))
    ok = marker in extracted and not error and not tool_events
    payload = {
        "ok": ok,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "work_dir": str(work),
        "marker_verified": marker in extracted,
        "tool_events": tool_events,
        "ephemeral": True,
        "stdin_only": True,
        "no_send": True,
        "error": error,
    }
    atomic_write(work / "prompt-only-probe.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def daily_no_send_probe(
    root: Path,
    codex: str,
    control_home: Path,
    ymd: str,
    model: str,
) -> dict:
    root = validate_root(root)
    work = prepare(root, ymd)
    draft = validate_data_root() / "日报草稿" / f"资讯日报-草稿-{ymd}.md"
    error = ""
    verdict = ""
    qc_exit = -1
    try:
        candidate = compose_prompt_only(root, work, ymd, draft, codex, control_home, model)
        check = work / "check.md"
        for repair_round in range(3):
            qc_exit, verdict, check, qc_stderr = run_quality_check(root, work, draft, candidate)
            if qc_exit == 0:
                break
            if repair_round >= 2:
                error = f"quality check exited {qc_exit}: {qc_stderr[-500:]}"
                break
            candidate = repair_prompt_only(
                root,
                work,
                ymd,
                draft,
                candidate,
                check,
                codex,
                control_home,
                model,
            )
    except (ExternalRuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        error = str(exc)
    events = (work / "codex.events.jsonl").read_text(encoding="utf-8") if (work / "codex.events.jsonl").is_file() else ""
    tool_events = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
            tool_events.append(item.get("type"))
    ok = qc_exit == 0 and verdict in {"PASS", "WARN"} and not tool_events and not error
    payload = {
        "ok": ok,
        "target_ymd": ymd,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "work_dir": str(work),
        "verdict": verdict,
        "qc_exit": qc_exit,
        "tool_events": tool_events,
        "ephemeral": True,
        "stdin_only": True,
        "no_send": True,
        "official_files_modified": False,
        "error": error,
    }
    atomic_write(work / "daily-no-send-probe.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def permission_probe(root: Path, codex: str, ymd: str) -> dict:
    work = prepare(root, ymd)
    root = validate_root(root)
    probe_script = work / "probe.py"
    data_root = validate_data_root()
    targets = {
        "work": str(work / "allowed.txt"),
        "project_root": str(root / "codex-probe-denied"),
        "draft_dir": str(data_root / "日报草稿" / "codex-probe-denied"),
        "skill_dir": str(root / "资讯日报-skill" / "codex-probe-denied"),
    }
    script = "#!/bin/zsh\nset +e\n"
    script += "probe_one() {\n"
    script += "  local name=\"$1\" target=\"$2\" moved=\"$2.moved\"\n"
    script += "  if : > \"$target\" 2>/dev/null && /bin/mv \"$target\" \"$moved\" 2>/dev/null; then\n"
    script += "    /bin/rm -f \"$moved\" 2>/dev/null\n"
    script += "    /usr/bin/printf '%s=allowed\\n' \"$name\"\n"
    script += "  else\n"
    script += "    /bin/rm -f \"$target\" \"$moved\" 2>/dev/null\n"
    script += "    /usr/bin/printf '%s=denied\\n' \"$name\"\n"
    script += "  fi\n"
    script += "}\n"
    for name, target in targets.items():
        script += f"probe_one {shlex.quote(name)} {shlex.quote(target)}\n"
    atomic_write(probe_script, script)
    env = os.environ.copy()
    env.update(runtime_env(root, work))
    command = [
        codex,
        "sandbox",
        "-P",
        "ribao_external",
        "-C",
        str(work),
        "/bin/zsh",
        str(probe_script),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
    matrix = {}
    for line in result.stdout.splitlines():
        name, separator, status = line.strip().partition("=")
        if separator and name in targets and status in {"allowed", "denied"}:
            matrix[name] = status
    ok = (
        result.returncode == 0
        and matrix.get("work") == "allowed"
        and all(matrix.get(name) == "denied" for name in ("project_root", "draft_dir", "skill_dir"))
    )
    payload = {
        "ok": ok,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "work_dir": str(work),
        "matrix": matrix,
        "exit_code": result.returncode,
        "stderr": result.stderr[-2000:],
        "runtime_env": runtime_env(root, work),
        "system_temp_note": "Codex 0.144.4 may expose /private/tmp inside its macOS sandbox; TMPDIR and all HOME/XDG paths are forced to this external work directory.",
    }
    atomic_write(work / "permission-probe.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def model_probe(root: Path, codex: str, ymd: str, model: str) -> dict:
    root = validate_root(root)
    work = prepare(root, ymd)
    marker = "RIBAO_EXTERNAL_LAUNCHD_PROBE_OK"
    source = work / "fixed-source.txt"
    atomic_write(
        source,
        "This is a fixed, non-production launchd probe. It must never be sent to Feishu.\n"
        f"Required marker: {marker}\n",
    )
    schema = root / "资讯日报-skill" / "config" / "report-output.schema.json"
    result_path = work / "result.json"
    events = work / "codex.events.jsonl"
    stderr_path = work / "codex.stderr.log"
    prompt = (
        f"Read {source}. Return schema-valid report_markdown of at least 100 characters. "
        f"It must contain the exact marker {marker} and clearly say this is a no-send launchd "
        "storage probe, not a real daily report. Do not write or modify any project file."
    )
    command = build_exec_command(codex, work, model, schema, result_path, prompt)
    env = os.environ.copy()
    env.update(runtime_env(root, work))
    with events.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        result = subprocess.run(
            command,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=8 * 60,
            check=False,
        )
    extracted = ""
    error = ""
    if result.returncode == 0:
        try:
            candidate = extract_report(root, work, ymd)
            extracted = candidate.read_text(encoding="utf-8")
        except (ExternalRuntimeError, OSError) as exc:
            error = str(exc)
    ok = result.returncode == 0 and marker in extracted and not error
    payload = {
        "ok": ok,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "work_dir": str(work),
        "exit_code": result.returncode,
        "marker_verified": marker in extracted,
        "ephemeral": "--ephemeral" in command,
        "no_send": True,
        "error": error,
    }
    atomic_write(work / "model-probe.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(DEFAULT_PROJECT_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--date", required=True)
    env_parser = sub.add_parser("env")
    env_parser.add_argument("--work-dir", required=True)
    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("--date", required=True)
    extract_parser.add_argument("--work-dir", required=True)
    publish_parser = sub.add_parser("validate-publish")
    publish_parser.add_argument("--work-dir", required=True)
    publish_parser.add_argument("--report", required=True)
    publish_parser.add_argument("--check", required=True)
    cleanup_parser = sub.add_parser("cleanup-success")
    cleanup_parser.add_argument("--date", required=True)
    compose_parser = sub.add_parser("compose")
    compose_parser.add_argument("--date", required=True)
    compose_parser.add_argument("--work-dir", required=True)
    compose_parser.add_argument("--draft", required=True)
    compose_parser.add_argument("--codex", required=True)
    compose_parser.add_argument("--control-home", required=True)
    compose_parser.add_argument("--model", required=True)
    repair_parser = sub.add_parser("repair")
    repair_parser.add_argument("--date", required=True)
    repair_parser.add_argument("--work-dir", required=True)
    repair_parser.add_argument("--draft", required=True)
    repair_parser.add_argument("--candidate", required=True)
    repair_parser.add_argument("--check", required=True)
    repair_parser.add_argument("--codex", required=True)
    repair_parser.add_argument("--control-home", required=True)
    repair_parser.add_argument("--model", required=True)
    prompt_probe_parser = sub.add_parser("prompt-probe")
    prompt_probe_parser.add_argument("--date", required=True)
    prompt_probe_parser.add_argument("--codex", required=True)
    prompt_probe_parser.add_argument("--control-home", required=True)
    prompt_probe_parser.add_argument("--model", required=True)
    daily_probe_parser = sub.add_parser("daily-probe")
    daily_probe_parser.add_argument("--date", required=True)
    daily_probe_parser.add_argument("--codex", required=True)
    daily_probe_parser.add_argument("--control-home", required=True)
    daily_probe_parser.add_argument("--model", required=True)
    probe_parser = sub.add_parser("probe")
    probe_parser.add_argument("--date", required=True)
    probe_parser.add_argument("--codex", required=True)
    probe_parser.add_argument("--model", default="")
    args = parser.parse_args()
    root = Path(args.root)
    try:
        if args.command == "prepare":
            print(prepare(root, args.date))
        elif args.command == "env":
            print(json.dumps(runtime_env(root, Path(args.work_dir)), ensure_ascii=False))
        elif args.command == "extract":
            print(extract_report(root, Path(args.work_dir), args.date))
        elif args.command == "validate-publish":
            validate_publish(root, Path(args.work_dir), Path(args.report), Path(args.check))
        elif args.command == "cleanup-success":
            print(cleanup_success(root, args.date))
        elif args.command == "compose":
            print(
                compose_prompt_only(
                    root,
                    Path(args.work_dir),
                    args.date,
                    Path(args.draft),
                    args.codex,
                    Path(args.control_home),
                    args.model,
                )
            )
        elif args.command == "repair":
            print(
                repair_prompt_only(
                    root,
                    Path(args.work_dir),
                    args.date,
                    Path(args.draft),
                    Path(args.candidate),
                    Path(args.check),
                    args.codex,
                    Path(args.control_home),
                    args.model,
                )
            )
        elif args.command == "prompt-probe":
            payload = prompt_only_probe(
                root,
                args.codex,
                Path(args.control_home),
                args.date,
                args.model,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1
        elif args.command == "daily-probe":
            payload = daily_no_send_probe(
                root,
                args.codex,
                Path(args.control_home),
                args.date,
                args.model,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1
        elif args.command == "probe":
            permission = permission_probe(root, args.codex, args.date)
            model_result = model_probe(root, args.codex, args.date, args.model) if args.model else None
            payload = {
                "ok": permission["ok"] and (model_result is None or model_result["ok"]),
                "permission": permission,
                "model": model_result,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1
    except ExternalRuntimeError as exc:
        print(f"codex external runtime rejected: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
