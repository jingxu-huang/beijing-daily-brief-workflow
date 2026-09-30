#!/usr/bin/env python3
"""Fail-closed external-volume runtime for the daily-brief Codex step."""

from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import sys


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


class PromptOnlyTimeout(ExternalRuntimeError):
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


def python_bin() -> str:
    return os.getenv("RIBAO_PYTHON_BIN", sys.executable)


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


QA_SKIP_COMMENT_RE = re.compile(
    r"^[ \t]*<!--\s*QA_SKIP\b(?:(?!-->).)*-->[ \t]*(?:\n|$)",
    flags=re.I | re.S | re.M,
)


def strip_internal_publish_comments(text: str) -> str:
    """Remove QC-only annotations that must never appear in the published Docx."""
    sanitized = QA_SKIP_COMMENT_RE.sub("", text or "")
    sanitized = re.sub(r"\n{4,}", "\n\n\n", sanitized)
    return sanitized.strip() + "\n"


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
    # QA_SKIP comments are intentionally present during QC so check_report.py can
    # distinguish explicitly rejected candidates from accidental omissions.
    # Feishu Docx import renders HTML comments as visible text, so the trusted
    # publish wrapper strips these internal annotations only after QC has passed.
    original = candidate.read_text(encoding="utf-8")
    sanitized = strip_internal_publish_comments(original)
    if sanitized != original:
        atomic_write(candidate, sanitized)


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
    output_schema: Optional[Path] = None,
) -> list[str]:
    schema = output_schema or (control_home / "report-output.schema.json")
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
        str(schema),
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


def prompt_timeout_seconds() -> int:
    raw = os.getenv("RIBAO_CODEX_TIMEOUT_SECONDS", str(7 * 60)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ExternalRuntimeError("RIBAO_CODEX_TIMEOUT_SECONDS must be an integer") from exc
    if not 1 <= value <= 60 * 60:
        raise ExternalRuntimeError("RIBAO_CODEX_TIMEOUT_SECONDS must be between 1 and 3600")
    return value


def prompt_retry_timeout_seconds() -> int:
    raw = os.getenv("RIBAO_CODEX_RETRY_TIMEOUT_SECONDS", "").strip()
    if not raw:
        raw = str(min(120, prompt_timeout_seconds()))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ExternalRuntimeError("RIBAO_CODEX_RETRY_TIMEOUT_SECONDS must be an integer") from exc
    if not 1 <= value <= prompt_timeout_seconds():
        raise ExternalRuntimeError(
            "RIBAO_CODEX_RETRY_TIMEOUT_SECONDS must be positive and no longer than the primary timeout"
        )
    return value


def run_prompt_process(command, env, prompt, timeout):
    """Run Codex in its own process group so a timeout cannot leave orphan workers."""
    process = subprocess.Popen(
        command,
        cwd="/",
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        error = PromptOnlyTimeout(f"prompt-only Codex timed out after {timeout}s")
        error.stdout = stdout
        error.stderr = stderr
        raise error from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_prompt_only(
    root: Path,
    work: Path,
    codex: str,
    control_home: Path,
    model: str,
    prompt: str,
    attempt_label: str = "",
    timeout_seconds: Optional[int] = None,
    output_schema: Optional[Path] = None,
) -> Path:
    root = validate_root(root)
    work = validate_work_dir(root, work)
    control_home = validate_control_home(control_home)
    if output_schema is not None:
        output_schema = require_within(
            output_schema,
            root / "资讯日报-skill" / "schemas",
            "prompt-only output schema",
        )
        if not output_schema.is_file():
            raise ExternalRuntimeError(f"prompt-only output schema is missing: {output_schema}")
    command = build_prompt_only_command(codex, control_home, model, output_schema)
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
    label = re.sub(r"[^a-z0-9-]", "", attempt_label.lower())
    events_path = work / (f"codex.{label}.events.jsonl" if label else "codex.events.jsonl")
    stderr_path = work / (f"codex.{label}.stderr.log" if label else "codex.stderr.log")
    timeout = timeout_seconds if timeout_seconds is not None else prompt_timeout_seconds()
    try:
        result = run_prompt_process(command, env, prompt, timeout)
    except PromptOnlyTimeout as exc:
        stdout = getattr(exc, "stdout", "") or ""
        stderr = getattr(exc, "stderr", "") or ""
        atomic_write(events_path, stdout)
        atomic_write(stderr_path, stderr + f"\nprompt-only Codex timed out after {timeout}s\n")
        atomic_write(work / "codex.events.jsonl", stdout)
        atomic_write(work / "codex.stderr.log", stderr)
        raise
    atomic_write(events_path, result.stdout)
    atomic_write(stderr_path, result.stderr)
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


def configured_report_floor(root: Path) -> int:
    """Read the report floor from the same sources.yaml quality-gate contract."""
    import yaml

    config = yaml.safe_load(
        (root / "资讯日报-skill" / "sources.yaml").read_text(encoding="utf-8")
    )
    root_config = (config or {}).get("config", {})
    profile = os.getenv("RIBAO_EDITORIAL_POLICY", "").strip() or (
        root_config.get("editorial_policy", {}).get("active_profile", "legacy")
    )
    gate = dict(root_config.get("quality_gate", {}))
    gate.update(
        root_config.get("editorial_policy", {})
        .get("profiles", {})
        .get(profile, {})
        .get("quality_gate", {})
    )
    raw = os.getenv("RIBAO_MIN_REPORT_ITEMS", str(gate.get("min_report_items", 1)))
    try:
        floor = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExternalRuntimeError("min_report_items quality gate must be an integer") from exc
    return max(1, floor)


def test_funding_research_context(data_root: Path, ymd: str, profile: str) -> str:
    """Return only frozen, test-profile financing research evidence for drafting.

    The writer itself remains offline.  This deliberately separates the
    interactive Codex/web-research step from the report-writing step, so the
    latter cannot invent a company profile or silently lose its citations.
    """
    if profile not in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        return "不适用：正式 legacy 日报不读取测试版融资研究资料。"
    path = require_within(
        data_root / "日报状态" / f"测试版-融资企业资料-{ymd}.json",
        data_root,
        "test financing research record",
    )
    if not path.is_file():
        return "本次未提供已核验的融资企业资料；不得输出被投方速览或补写公司信息。"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalRuntimeError(f"invalid test financing research record: {exc}") from exc
    if not isinstance(payload, list):
        raise ExternalRuntimeError("test financing research record must be a JSON list")
    if len(payload) > 100:
        raise ExternalRuntimeError("test financing research record has too many entries")
    for record in payload:
        if not isinstance(record, dict):
            raise ExternalRuntimeError("test financing research record item must be an object")
        required = {"company", "report_item_url", "researched_at", "business", "business_model", "valuation", "sources_checked"}
        if not required.issubset(record):
            raise ExternalRuntimeError("test financing research record is missing traceability fields")
        if not isinstance(record["sources_checked"], list) or not record["sources_checked"]:
            raise ExternalRuntimeError("test financing research record has no checked source")
        business = record["business"]
        if not isinstance(business, dict) or not {"value", "source_url", "publisher"}.issubset(business):
            raise ExternalRuntimeError("test financing business record lacks source traceability")
        for field in ("business_model", "valuation"):
            value = record[field]
            if not isinstance(value, dict):
                raise ExternalRuntimeError(f"test financing {field} record must be an object")
            if value.get("status") == "not_disclosed":
                continue
            if not {"value", "source_url", "publisher"}.issubset(value):
                raise ExternalRuntimeError(f"test financing {field} record lacks source traceability")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_daily_prompt(root: Path, ymd: str, draft: Path) -> str:
    root = validate_root(root)
    data_root = validate_data_root()
    draft = require_within(draft, data_root / "日报草稿", "daily draft")
    if not draft.is_file() or draft.name != f"资讯日报-草稿-{ymd}.md":
        raise ExternalRuntimeError(f"unexpected daily draft: {draft}")
    skill = root / "资讯日报-skill" / "SKILL.md"
    format_spec = root / "资讯日报-skill" / "format" / "格式规范.md"
    test_funding_spec = root / "资讯日报-skill" / "format" / "测试版融资补充规范.md"
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
    floor = configured_report_floor(root)
    test_contract = ""
    active_profile = os.getenv("RIBAO_EDITORIAL_POLICY", "").strip()
    delivery_contract = build_test_delivery_contract(
        draft.read_text(encoding="utf-8"), active_profile, floor
    )
    funding_research = test_funding_research_context(data_root, ymd, active_profile)
    if active_profile == "business_v1_test":
        test_contract = (
            "\n测试版额外硬约束：备选条目也必须使用草稿所附摘要写出完整事实，"
            "不得把标题改写成一句话补数；若正式候选与备选仍不足最低条数，可以逐条复核‘业务灰区复核’中正文状态为 complete 的条目，确认业务证据后采用，但不得自动补数，也不得采用 partial/title_only 条目；公众号条目正文末尾必须使用草稿中的真实公众号名称，"
            "不得写‘微信公众号’等渠道统称；Source 表必须按这些真实名称逐项统计并与正文完全一致。"
        )
    elif active_profile in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        test_contract = (
            "\n测试 v2 额外硬约束：备选条目也必须使用草稿所附摘要写出完整事实，"
            "不得把标题改写成一句话补数；不得使用业务灰区复核、partial/title_only 或拒绝项补数；"
            "公众号条目正文末尾必须使用草稿中的真实公众号名称，Source 表必须与正文完全一致。"
        )
    if active_profile in {"business_v2_balanced", "business_v2_recall"}:
        test_contract += (
            "\n本版本最低质量线是 10 条；不得采用硬拒绝项、低价值 AI 信息或明确不相关内容凑数；"
            "必须主动尝试达到 25 条。达到下限后不得因此停止扩写；只可继续采用草稿中本版本已放行的备选条目。"
            "涉及产品的条目，正文必须保留该具体功能事实，不得只写标题。"
        )
    if delivery_contract:
        test_contract += f"""

<test_delivery_contract>
{delivery_contract}
</test_delivery_contract>

上述交付合同是程序从冻结草稿生成的硬边界：
1. required_selected_urls 每个 URL 都必须在正文中保留；同一事件可合并为一条，但不得丢失任一条独立来源 URL。
2. 正文不得出现 allowed_urls 之外的 URL；不得使用灰区、已拒绝条目或模型记忆补数。
3. 平衡/高召回版应按 backup_urls 顺序尽量达到 publication_target；安全备选耗尽时可低于目标，不可以低质内容凑数。
4. 测试 v2 不允许写作模型自行 QA_SKIP 已放行精选；候选取舍已在上游完成。
"""
    funding_research_contract = ""
    if active_profile in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        funding_research_contract = (
            "\\n测试版融资条目仅在已提供可审计资料时增加‘被投方速览’；"
            "速览只写主营/商业模式与最近公开估值，缺失字段必须如实写未披露，"
            "不得根据模型记忆或草稿外信息自行推断；仅可使用 report_item_url 与本条融资链接一致的记录。"
            "audit_scope=source_only 时不得超出 evidence_excerpt。补充资料链接只进审计记录，不在正文堆叠。"
        )
    if delivery_contract:
        selection_instruction = (
            "草稿中每条 ⭐精选的 URL 都必须保留在正文；同一事件可合并成一条，但必须保留所有独立来源 URL。"
            "不得使用 QA_SKIP 剔除已放行精选。数量不足时只能从 test_delivery_contract.backup_urls 补选，"
            "不得使用灰区、拒绝项或模型记忆。"
        )
    else:
        selection_instruction = (
            f"草稿中每条 ⭐精选 都必须进入正文，或在 report_markdown 中用独立的质检注释 "
            "`<!-- QA_SKIP 原文URL | 简明且可核验的剔除理由 -->` 显式说明。每个被剔除的 URL 单独一条，不得静默遗漏。"
            "该注释只供质检，质检通过后由受控发布器自动剔除，不会进入最终 Markdown 或飞书文档。"
            f"如果严格合格的 ⭐精选在合理剔除后会使成稿少于 {floor} 条，只能按草稿现有顺序从“备选”池补到 {floor} 条；"
            "不得使用排查区、被剔除项或无关资讯补数。"
        )
    prompt = f"""
你要生成目标日期 {ymd} 的资讯日报。以下是全部且唯一的输入；不要调用任何工具，不要读写任何文件，不要联网，不要发送飞书。
严格执行 skill 与格式规范，逐条做主题准入、按正文而非来源归类、合并同一事件，并在输出前完成自检。
{selection_instruction}
{test_contract}
{funding_research_contract}
最终回复必须严格符合 JSON schema，只包含 report_markdown；值是完整 Markdown 正文，不要用代码围栏包裹。

<skill>
{skill.read_text(encoding='utf-8')}
</skill>

<format_spec>
{format_spec.read_text(encoding='utf-8')}
</format_spec>

<test_funding_spec>
{test_funding_spec.read_text(encoding='utf-8') if funding_research_contract else '不适用：正式 legacy 日报保持原融资模板。'}
</test_funding_spec>

<test_funding_research_records>
{funding_research}
</test_funding_research_records>

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


def build_compact_daily_prompt(root: Path, ymd: str, draft: Path) -> str:
    """Second-attempt prompt: same contract, without historical style payload."""
    root = validate_root(root)
    data_root = validate_data_root()
    draft = require_within(draft, data_root / "日报草稿", "daily draft")
    format_spec = root / "资讯日报-skill" / "format" / "格式规范.md"
    test_funding_spec = root / "资讯日报-skill" / "format" / "测试版融资补充规范.md"
    floor = configured_report_floor(root)
    test_contract = ""
    active_profile = os.getenv("RIBAO_EDITORIAL_POLICY", "").strip()
    if active_profile == "business_v1_test":
        test_contract = (
            "测试版额外硬约束：备选必须用草稿摘要写完整事实，不能用标题级一句话补数；正式候选和备选不足时，只能人工判断业务灰区中正文状态 complete 的条目，不得自动使用灰区或 partial/title_only 条目；"
            "公众号来源必须写真实公众号名，Source 表逐项精确对应，禁止使用‘微信公众号’统称。"
        )
    delivery_contract = build_test_delivery_contract(
        draft.read_text(encoding="utf-8"), active_profile, floor
    )
    if delivery_contract:
        test_contract += (
            "\n测试 v2 必须严格按下方 test_delivery_contract 交付：保留全部 required_selected_urls，"
            "正文仅能使用 allowed_urls，不得自行 QA_SKIP；平衡/高召回按安全备选顺序尽量达到目标。"
            f"\n<test_delivery_contract>\n{delivery_contract}\n</test_delivery_contract>"
        )
    funding_research = test_funding_research_context(data_root, ymd, active_profile)
    funding_research_contract = ""
    if active_profile in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        funding_research_contract = (
            "测试版融资条目仅在已提供可审计资料时增加‘被投方速览’；只写主营/商业模式与最近公开估值，"
            "缺失字段如实写未披露，禁止根据模型记忆或草稿外信息推断；仅使用 report_item_url 匹配本条融资链接的资料，"
            "audit_scope=source_only 时不得超出 evidence_excerpt。"
        )
    if delivery_contract:
        compact_selection = (
            "每条 ⭐精选 URL 必须在正文保留；同事件合并时保留所有独立来源 URL。"
            "不得使用 QA_SKIP，不得静默遗漏。数量不足只能按 test_delivery_contract.backup_urls 补选。"
        )
    else:
        compact_selection = (
            "草稿中每条 ⭐精选 都必须进入正文，或以独立质检注释 "
            "`<!-- QA_SKIP 原文URL | 简明剔除理由 -->` 逐 URL 显式剔除，不得静默遗漏。"
            "注释仅供质检，发布前自动剔除。"
        )
    return f"""
生成 {ymd} 资讯日报。禁止调用工具、联网、读写文件或发送飞书。严格输出 schema JSON，且只含 report_markdown。
固定三大一级板块、二级赛道、标题图标、条目结构和 Source 表，不得改变日报格式。只写输入中的事实，不分析、不补写。
逐条做业务准入、跨源合并和旧闻排除。优先采用 ⭐精选；若严格合格条目少于 {floor} 条，只能从同一草稿的“备选”池按原顺序补到 {floor} 条，不能用排查区或无关内容补数。
{compact_selection}
{test_contract}
{funding_research_contract}

<format_spec>
{format_spec.read_text(encoding='utf-8')}
</format_spec>

<test_funding_spec>
{test_funding_spec.read_text(encoding='utf-8') if funding_research_contract else '不适用：正式 legacy 日报保持原融资模板。'}
</test_funding_spec>

<test_funding_research_records>
{funding_research}
</test_funding_research_records>

<daily_draft name={json.dumps(draft.name, ensure_ascii=False)}>
{draft.read_text(encoding='utf-8')}
</daily_draft>
""".strip()


BOARD_HEADINGS = {
    "泛娱乐·出海": "# 🌍 泛娱乐·出海",
    "创新·AI娱乐": "# ✨ 创新·AI娱乐",
    "全球融资": "# 💸 全球融资",
}
TRACK_HEADINGS = {
    "休闲游戏": "## 🕹️ 休闲游戏",
    "短剧/漫剧": "## 🎬 短剧/漫剧",
    "小说·网文": "## 📚 小说·网文",
    "社交·Dating": "## 💬 社交·Dating",
    "其他": "## 🌐 其他",
    "AI模型与应用（娱乐向）": "## 🧠 AI模型与应用（娱乐向）",
    "创新硬件（娱乐向）": "## 🎧 创新硬件（娱乐向）",
    "泛娱乐融资": "## 🎭 泛娱乐融资",
    "泛AI·AI应用融资": "## 🧩 泛AI·AI应用融资",
}


def parse_draft_candidates(text: str) -> list[dict]:
    """Parse only accepted selected/backup pools; diagnostic sections are ignored."""
    rows = []
    board = track = ""
    current = None

    def finish():
        nonlocal current
        if current:
            rows.append(current)
            current = None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## ") and line[3:] in BOARD_HEADINGS:
            finish()
            board, track = line[3:], ""
            continue
        if line.startswith("## ") and line[3:] not in BOARD_HEADINGS:
            finish()
            board = track = ""
            continue
        if board and line.startswith("### "):
            finish()
            track = re.sub(r"（精选.*$", "", line[4:]).strip()
            continue
        if not board or track not in TRACK_HEADINGS:
            continue
        selected = re.match(r"^- ⭐\*\*(.+?)\*\*(?:（score [^）]+）)?｜(.+)$", line)
        if selected:
            finish()
            source = re.sub(r"\s*⏱.*$|（发布[^）]+）", "", selected.group(2)).strip()
            current = {
                "board": board,
                "track": track,
                "title": selected.group(1).strip(),
                "source": source,
                "summary": "",
                "link": "",
                "alternate_links": [],
                "selected": True,
            }
            continue
        backup = re.match(r"^- 备选：(.+?)｜(.+?)\s+—\s+(https?://\S+)$", line)
        if backup:
            finish()
            source = re.sub(r"\s*⏱.*$|（发布[^）]+）", "", backup.group(2)).strip()
            current = {
                "board": board,
                "track": track,
                "title": backup.group(1).strip(),
                "source": source,
                "summary": "",
                "link": backup.group(3).strip(),
                "alternate_links": [],
                "selected": False,
            }
            continue
        if current and line.startswith("- http"):
            current["link"] = line[2:].strip()
        elif current and line.startswith("- 同事件独立来源佐证："):
            current["alternate_links"].extend(
                re.findall(r"https?://[^\s|]+", line)
            )
        elif current and line.startswith("- ") and not line.startswith("- 候选图:"):
            summary = line[2:].strip()
            if summary.startswith("摘要："):
                summary = summary[3:].strip()
            current["summary"] = (current.get("summary", "") + " " + summary).strip()
    finish()
    return [row for row in rows if row["link"]]


def build_test_delivery_contract(draft_text: str, profile: str, floor: int) -> str:
    """Return a machine-readable v2 candidate boundary for the writer.

    The contract is deliberately absent from legacy and v1 prompts.  It turns
    the accepted draft from prose guidance into an executable allowlist while
    keeping the publication target separate from the hard quality floor.
    """
    if profile not in {
        "business_v2_strict", "business_v2_balanced", "business_v2_recall"
    }:
        return ""
    rows = parse_draft_candidates(draft_text)
    selected = [row for row in rows if row.get("selected")]
    backups = [row for row in rows if not row.get("selected")]

    def urls(rows_to_read: list[dict]) -> list[str]:
        output = []
        for row in rows_to_read:
            for url in [row.get("link", "")] + list(row.get("alternate_links", []) or []):
                if url and url not in output:
                    output.append(url)
        return output

    required_urls = urls(selected)
    backup_urls = urls(backups)
    payload = {
        "schema_version": 1,
        "profile": profile,
        "hard_floor": int(floor),
        "publication_target": 25 if profile in {
            "business_v2_balanced", "business_v2_recall"
        } else int(floor),
        "required_selected_urls": required_urls,
        "backup_urls": backup_urls,
        "allowed_urls": required_urls + [url for url in backup_urls if url not in required_urls],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_draft_fallback(root: Path, ymd: str, draft: Path, work: Path) -> Path:
    """Last-resort fixed-template draft; the normal full QA still decides delivery."""
    rows = parse_draft_candidates(draft.read_text(encoding="utf-8"))
    chosen = [row for row in rows if row["selected"]]
    floor = configured_report_floor(root)
    if len(chosen) < floor:
        backups = [row for row in rows if not row["selected"]]
        chosen.extend(backups[: floor - len(chosen)])
    if not chosen:
        raise ExternalRuntimeError("fixed-template fallback found no accepted draft candidates")

    target = datetime.strptime(ymd, "%Y%m%d")
    prior = target - timedelta(days=1)
    window = (
        f"{prior.month}月{prior.day}日15:00 — {target.month}月{target.day}日15:00"
        "（北京时间，左闭右开）"
    )
    grouped = OrderedDict()
    for row in chosen[:30]:
        grouped.setdefault((row["board"], row["track"]), []).append(row)

    lines = [f"# 资讯类简报-{target.month}.{target.day}日", "", f"> 时间范围：{window}", ""]
    sources = Counter()
    previous_board = ""
    first_section = True
    for (board, track), items in grouped.items():
        if board != previous_board:
            if not first_section:
                lines.extend(["<br>", ""])
            lines.extend([BOARD_HEADINGS[board], ""])
            previous_board = board
            first_section = False
        lines.extend([TRACK_HEADINGS[track], ""])
        for ordinal, row in enumerate(items, 1):
            body = re.sub(r"\s+", " ", (row["summary"] or row["title"]).strip())
            if body and body[-1] not in "。！？.!?":
                body += "。"
            annotation = f"（{row['source']}）"
            if not body.endswith(annotation):
                body += annotation
            lines.extend(
                [
                    f"### {ordinal}）{row['title']}",
                    "",
                    body,
                    "",
                    row["link"],
                ]
            )
            # Same-event corroboration is part of the selected delivery
            # contract.  The deterministic fallback must retain those URLs
            # just like the model writer; otherwise a fallback silently turns
            # one multi-source selection into a single-source claim.
            lines.extend(row.get("alternate_links", []) or [])
            lines.append("")
            sources[row["source"]] += 1
        lines.extend(["<br>", ""])
    while lines and lines[-1] in {"", "<br>"}:
        lines.pop()
    lines.extend(["", "", "---", "", "Source 来源：", "", "|来源|本期采用条数|", "|---|---:|"])
    lines.extend(f"|{source}|{count}|" for source, count in sources.items())
    candidate = work / "report.md"
    atomic_write(candidate, "\n".join(lines).strip() + "\n")
    return candidate


def write_compose_audit(work: Path, mode: str, attempts: list[dict]) -> None:
    atomic_write(
        work / "compose-mode.json",
        json.dumps(
            {"mode": mode, "attempts": attempts, "same_run": True},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def compose_prompt_only(
    root: Path,
    work: Path,
    ymd: str,
    draft: Path,
    codex: str,
    control_home: Path,
    model: str,
) -> Path:
    attempts = []
    prompts = (
        ("full", build_daily_prompt(root, ymd, draft), None),
        ("compact", build_compact_daily_prompt(root, ymd, draft), prompt_retry_timeout_seconds()),
    )
    for label, prompt, timeout in prompts:
        try:
            run_prompt_only(
                root, work, codex, control_home, model, prompt,
                attempt_label=label, timeout_seconds=timeout,
            )
            candidate = extract_report(root, work, ymd)
            attempts.append({"attempt": label, "status": "ok"})
            write_compose_audit(work, label, attempts)
            return candidate
        except ExternalRuntimeError as exc:
            attempts.append(
                {"attempt": label, "status": "failed", "error_category": type(exc).__name__}
            )
            (work / "result.json").unlink(missing_ok=True)
    candidate = render_draft_fallback(root, ymd, draft, work)
    attempts.append({"attempt": "fixed-template", "status": "ok"})
    write_compose_audit(work, "fixed-template", attempts)
    return candidate


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
    repair_plan: Optional[Path] = None,
) -> Path:
    root = validate_root(root)
    work = validate_work_dir(root, work, ymd)
    candidate = require_within(candidate, work, "repair candidate")
    check = require_within(check, work, "repair check")
    if not candidate.is_file() or not check.is_file():
        raise ExternalRuntimeError("repair candidate/check is missing")
    repair_contract = ""
    repair_selection_instruction = (
        "草稿精选不得静默遗漏。应恢复的条目必须恢复正文并满足完整度；"
        "重复、超窗、明显无关或信息不足的条目，必须以独立的 `<!-- QA_SKIP 原文URL | 简明且可核验的剔除理由 -->` 逐 URL 登记。"
        "QA_SKIP 是质检用隐藏注释，由质检器写入独立审计状态，质检通过后由受控发布器自动从最终 Markdown/飞书文档剔除。"
    )
    if repair_plan is not None:
        repair_plan = require_within(repair_plan, work, "repair plan")
        if not repair_plan.is_file():
            raise ExternalRuntimeError("repair plan is missing")
        try:
            plan_payload = json.loads(repair_plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalRuntimeError(f"invalid repair plan: {exc}") from exc
        if plan_payload.get("schema_version") != 2:
            raise ExternalRuntimeError("unsupported repair plan schema")
        repair_selection_instruction = (
            "测试 v2 不得使用 QA_SKIP 改写已冻结的候选取舍。"
            "missing_required_urls 必须恢复，blocked URL 必须从正文删除，全部 URL 必须位于 allowed_urls。"
        )
        repair_contract = f"""

<deterministic_repair_plan>
{json.dumps(plan_payload, ensure_ascii=False, indent=2)}
</deterministic_repair_plan>

该计划是由冻结源草稿和统一业务规则生成的可审计指令。必须遵守：
1. blocked 中的 URL 必须删除；不得以 QA_SKIP 继续绕过已放行精选的交付责任。
2. missing_required_urls 必须恢复；同事件可合并写作，但该事件的所有必需 URL 都要保留在同一条正文下。
3. 仅可按 replacements 现有顺序补入，且全部正文 URL 必须属于 allowed_urls；不得从灰区、草稿其他区域或模型记忆自行补数。
4. 平衡/高召回应向 publication_target 补选；当 safe_pool_exhausted=true 时可低于目标，不得恢复 blocked 项或采用硬拒绝项。
5. 新增条目只能使用计划中的标题、摘要、来源和 URL 写作，不得补写新事实。
"""
    base = build_daily_prompt(root, ymd, draft)
    prompt = f"""{base}

<previous_candidate>
{candidate.read_text(encoding='utf-8')}
</previous_candidate>

<quality_check>
{check.read_text(encoding='utf-8')}
</quality_check>
{repair_contract}

上一版候选稿未通过独立质检。请修复“必须修复”中的每一项，返回完整的修订后日报，不是差异或解释。
{repair_selection_instruction}
继续严格输出只含 report_markdown 的 schema JSON，不调用任何工具。"""
    run_prompt_only(root, work, codex, control_home, model, prompt)
    return extract_report(root, work, ymd)


def run_quality_check(root: Path, work: Path, draft: Path, candidate: Path) -> tuple[int, str, Path, str]:
    check = work / "check.md"
    command = [
            python_bin(),
            str(root / "资讯日报-skill" / "check_report.py"),
            "--draft",
            str(draft),
            "--report",
            str(candidate),
            "--write",
            str(check),
        ]
    if os.getenv("RIBAO_TEST_MODE", "0") == "1":
        command.extend(["--repair-plan", str(work / "repair-plan.json")])
    qc = subprocess.run(
        command,
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


def repair_plan_requires_action(path: Path) -> bool:
    """Return whether the deterministic v2 delivery plan still has work.

    WARN is an acceptable editorial verdict, but it is not proof that the
    selected/backup delivery contract was fulfilled.  This separate signal
    lets the repair loop continue without turning every advisory warning into
    a publishing failure.
    """
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return payload.get("schema_version") == 2 and bool(payload.get("requires_repair"))


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
            repair_plan = work / "repair-plan.json"
            plan_action = (
                os.getenv("RIBAO_TEST_MODE", "0") == "1"
                and repair_plan_requires_action(repair_plan)
            )
            if qc_exit == 0 and not plan_action:
                break
            if repair_round >= 2:
                if plan_action:
                    error = "test delivery contract remains unresolved after 3 repair rounds"
                else:
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
                repair_plan if os.getenv("RIBAO_TEST_MODE", "0") == "1" else None,
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
    repair_parser.add_argument("--repair-plan", default="")
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
                    Path(args.repair_plan) if args.repair_plan else None,
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
