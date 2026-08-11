import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import codex_external


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CodexExternalContractTests(unittest.TestCase):
    def runtime_context(self):
        stack = ExitStack()
        temp = stack.enter_context(tempfile.TemporaryDirectory())
        data_root = Path(temp) / "data"
        auth_home = Path(temp) / "codex-auth"
        control_home = Path(temp) / "control"
        data_root.mkdir()
        auth_home.mkdir()
        control_home.mkdir()
        (auth_home / "auth.json").write_text("{}\n", encoding="utf-8")
        for name in ("config.toml", "report-output.schema.json", "auth.json"):
            (control_home / name).write_text("{}\n", encoding="utf-8")
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "RIBAO_PROJECT_ROOT": str(PROJECT_ROOT),
                    "RIBAO_DATA_ROOT": str(data_root),
                    "RIBAO_ALLOW_SYSTEM_DATA_ROOT": "1",
                    "RIBAO_CODEX_CONTROL_HOME": str(control_home),
                },
                clear=False,
            )
        )
        stack.enter_context(mock.patch.object(codex_external, "EXISTING_CODEX_HOME", auth_home))
        return stack, data_root, control_home

    def test_project_root_is_configurable(self):
        stack, _data_root, _control = self.runtime_context()
        with stack:
            self.assertEqual(codex_external.validate_root(PROJECT_ROOT), PROJECT_ROOT)

    def test_system_disk_path_is_rejected_by_policy(self):
        system_path = Path("/") / "Users" / "example-user" / "daily-brief"
        with self.assertRaises(codex_external.ExternalRuntimeError):
            codex_external.reject_system_path(system_path, "test")

    def test_legacy_staging_is_rejected(self):
        stack, _data_root, _control = self.runtime_context()
        with stack, mock.patch.dict(
            os.environ, {"RIBAO_CODEX_STAGING_BASE": "/legacy/staging"}, clear=False
        ):
            with self.assertRaises(codex_external.ExternalRuntimeError):
                codex_external.validate_workflow_paths(PROJECT_ROOT)

    def test_command_is_ephemeral_and_has_no_sandbox_bypass(self):
        work = PROJECT_ROOT / "synthetic-work"
        command = codex_external.build_exec_command(
            "/path/to/codex",
            work,
            "gpt-test",
            PROJECT_ROOT / "资讯日报-skill" / "config" / "report-output.schema.json",
            work / "result.json",
            "probe",
        )
        self.assertIn("--ephemeral", command)
        self.assertNotIn("workspace-write", command)
        self.assertNotIn("danger-full-access", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_prompt_only_command_disables_model_tools_and_uses_stdout(self):
        command = codex_external.build_prompt_only_command(
            "/path/to/codex", PROJECT_ROOT / "synthetic-control", "gpt-test"
        )
        joined = " ".join(command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("read-only", command)
        for feature in (
            "shell_tool",
            "unified_exec",
            "plugins",
            "apps",
            "browser_use",
            "computer_use",
            "multi_agent",
        ):
            self.assertIn(feature, command)
        self.assertNotIn("--output-last-message", command)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("workspace-write", joined)

    def test_last_agent_message_uses_final_completed_message(self):
        events = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}),
            ]
        )
        self.assertEqual(codex_external.last_agent_message(events), "final")

    def test_prepare_extract_and_cleanup_stay_in_data_root(self):
        stack, data_root, _control = self.runtime_context()
        with stack:
            ymd = "20991231"
            work = codex_external.prepare(PROJECT_ROOT, ymd)
            try:
                self.assertTrue(codex_external.is_within(work, data_root))
                env = codex_external.runtime_env(PROJECT_ROOT, work)
                for value in env.values():
                    self.assertTrue(codex_external.is_within(Path(value), data_root), value)
                report = "# Synthetic report\n\n" + "Synthetic validation content. " * 20
                (work / "result.json").write_text(
                    json.dumps({"report_markdown": report}), encoding="utf-8"
                )
                candidate = codex_external.extract_report(PROJECT_ROOT, work, ymd)
                self.assertEqual(candidate.parent, work)
            finally:
                codex_external.cleanup_success(PROJECT_ROOT, ymd)
            self.assertFalse(work.exists())

    def test_unexpected_result_fields_are_rejected(self):
        stack, _data_root, _control = self.runtime_context()
        with stack:
            ymd = "20991230"
            work = codex_external.prepare(PROJECT_ROOT, ymd)
            try:
                (work / "result.json").write_text(
                    json.dumps({"report_markdown": "x" * 120, "path": "/tmp/report"}),
                    encoding="utf-8",
                )
                with self.assertRaises(codex_external.ExternalRuntimeError):
                    codex_external.extract_report(PROJECT_ROOT, work, ymd)
            finally:
                codex_external.cleanup_success(PROJECT_ROOT, ymd)


if __name__ == "__main__":
    unittest.main()
