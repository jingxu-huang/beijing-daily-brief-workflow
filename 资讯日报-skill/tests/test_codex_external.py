import json
import os
import subprocess
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

    def test_prompt_timeout_terminates_the_process_group(self):
        process = mock.Mock()
        process.pid = 4321
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 1),
            ("partial", "timed out"),
        ]
        with mock.patch.object(codex_external.subprocess, "Popen", return_value=process), mock.patch.object(
            codex_external.os, "killpg"
        ) as killpg:
            with self.assertRaises(codex_external.PromptOnlyTimeout):
                codex_external.run_prompt_process(["codex"], {}, "prompt", 1)
        killpg.assert_called_once_with(4321, codex_external.signal.SIGTERM)

    def test_default_timeouts_fit_one_pre_deadline_retry(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RIBAO_CODEX_TIMEOUT_SECONDS", None)
            os.environ.pop("RIBAO_CODEX_RETRY_TIMEOUT_SECONDS", None)
            self.assertEqual(codex_external.prompt_timeout_seconds(), 7 * 60)
            self.assertEqual(codex_external.prompt_retry_timeout_seconds(), 2 * 60)

    def test_retry_timeout_defaults_to_two_minutes_or_primary_cap(self):
        with mock.patch.dict(
            os.environ, {"RIBAO_CODEX_TIMEOUT_SECONDS": "60"}, clear=False
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RIBAO_CODEX_RETRY_TIMEOUT_SECONDS", None)
            self.assertEqual(codex_external.prompt_retry_timeout_seconds(), 60)

    def test_prompt_contract_uses_configured_floor_and_accepted_backups_only(self):
        stack, data_root, _control = self.runtime_context()
        with stack, mock.patch.dict(
            os.environ,
            {"RIBAO_MIN_REPORT_ITEMS": "10"},
            clear=False,
        ):
            draft_dir = data_root / "日报草稿"
            draft_dir.mkdir()
            draft = draft_dir / "资讯日报-草稿-20991229.md"
            draft.write_text("# draft\n", encoding="utf-8")
            prompt = codex_external.build_daily_prompt(PROJECT_ROOT, "20991229", draft)
        self.assertIn("少于 10 条", prompt)
        self.assertIn("只能按草稿现有顺序从“备选”池补到 10 条", prompt)
        self.assertIn("不得使用排查区、被剔除项或无关资讯补数", prompt)
        self.assertIn("QA_SKIP", prompt)
        self.assertIn("不会进入最终 Markdown 或飞书文档", prompt)

    def test_balanced_target_is_an_active_safe_expansion_requirement(self):
        stack, data_root, _control = self.runtime_context()
        with stack, mock.patch.dict(
            os.environ,
            {"RIBAO_EDITORIAL_POLICY": "business_v2_balanced"},
            clear=False,
        ):
            draft_dir = data_root / "日报草稿"
            draft_dir.mkdir()
            draft = draft_dir / "资讯日报-草稿-20991228.md"
            draft.write_text("# draft\n", encoding="utf-8")
            prompt = codex_external.build_daily_prompt(PROJECT_ROOT, "20991228", draft)
        self.assertIn("最低质量线是 10 条", prompt)
        self.assertIn("主动尝试达到 25 条", prompt)
        self.assertIn("达到下限后不得因此停止扩写", prompt)
        self.assertIn("不得采用硬拒绝项、低价值 AI 信息或明确不相关内容凑数", prompt)
        self.assertIn("正文必须保留该具体功能事实", prompt)

    def test_v2_prompt_contains_an_executable_candidate_delivery_contract(self):
        stack, data_root, _control = self.runtime_context()
        with stack, mock.patch.dict(
            os.environ,
            {"RIBAO_EDITORIAL_POLICY": "business_v2_balanced"},
            clear=False,
        ):
            draft_dir = data_root / "日报草稿"
            draft_dir.mkdir()
            draft = draft_dir / "资讯日报-草稿-20991228.md"
            draft.write_text(
                "## 泛娱乐·出海\n"
                "### 休闲游戏（精选 1 / 候选 2）\n"
                "- ⭐**休闲游戏 A 上线**（score 5）｜来源甲\n"
                "  - 面向海外市场。\n"
                "  - https://example.com/a\n"
                "  - 同事件独立来源佐证：来源乙 — https://example.com/a2\n"
                "- 备选：休闲游戏 B 更新｜来源丙 — https://example.com/b\n"
                "  - 摘要：披露留存和流水。\n",
                encoding="utf-8",
            )
            prompt = codex_external.build_daily_prompt(PROJECT_ROOT, "20991228", draft)
        self.assertIn("<test_delivery_contract>", prompt)
        self.assertIn('"publication_target": 25', prompt)
        self.assertIn("https://example.com/a2", prompt)
        self.assertIn("https://example.com/b", prompt)
        self.assertIn("正文不得出现 allowed_urls 之外的 URL", prompt)
        self.assertNotIn("或在 report_markdown 中用独立的质检注释", prompt)
        self.assertNotIn("可以逐条复核‘业务灰区复核’", prompt)

    def test_test_profile_receives_only_frozen_financing_research_records(self):
        stack, data_root, _control = self.runtime_context()
        with stack, mock.patch.dict(
            os.environ,
            {"RIBAO_EDITORIAL_POLICY": "business_v2_strict"},
            clear=False,
        ):
            draft_dir = data_root / "日报草稿"
            state_dir = data_root / "日报状态"
            draft_dir.mkdir()
            state_dir.mkdir()
            ymd = "20991227"
            draft = draft_dir / f"资讯日报-草稿-{ymd}.md"
            draft.write_text("# draft\n", encoding="utf-8")
            no_record_prompt = codex_external.build_daily_prompt(PROJECT_ROOT, ymd, draft)
            self.assertIn("本次未提供已核验的融资企业资料", no_record_prompt)
            self.assertIn("不得输出被投方速览", no_record_prompt)

            record = [{
                "company": "Example AI",
                "report_item_url": "https://news.example/funding",
                "researched_at": "2099-12-27T12:00:00+08:00",
                "business": {"value": "创作者工具", "source_url": "https://example.com", "publisher": "Example AI"},
                "business_model": {"status": "not_disclosed"},
                "valuation": {"status": "not_disclosed"},
                "sources_checked": ["https://example.com"],
            }]
            (state_dir / f"测试版-融资企业资料-{ymd}.json").write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            prompt = codex_external.build_daily_prompt(PROJECT_ROOT, ymd, draft)
        self.assertIn("Example AI", prompt)
        self.assertIn("https://news.example/funding", prompt)

    def test_legacy_prompt_does_not_read_test_financing_research(self):
        stack, data_root, _control = self.runtime_context()
        with stack:
            draft_dir = data_root / "日报草稿"
            state_dir = data_root / "日报状态"
            draft_dir.mkdir()
            state_dir.mkdir()
            ymd = "20991226"
            draft = draft_dir / f"资讯日报-草稿-{ymd}.md"
            draft.write_text("# draft\n", encoding="utf-8")
            (state_dir / f"测试版-融资企业资料-{ymd}.json").write_text(
                '[{"company":"must-not-leak"}]', encoding="utf-8"
            )
            prompt = codex_external.build_daily_prompt(PROJECT_ROOT, ymd, draft)
        self.assertNotIn("must-not-leak", prompt)
        self.assertIn("正式 legacy 日报不读取测试版融资研究资料", prompt)

    def test_compact_legacy_and_v2_repair_prompts_keep_separate_skip_contracts(self):
        stack, data_root, control = self.runtime_context()
        with stack:
            ymd = "20991229"
            draft_dir = data_root / "日报草稿"
            draft_dir.mkdir()
            draft = draft_dir / f"资讯日报-草稿-{ymd}.md"
            draft.write_text("# draft\n", encoding="utf-8")
            compact = codex_external.build_compact_daily_prompt(PROJECT_ROOT, ymd, draft)
            self.assertIn("QA_SKIP", compact)
            self.assertIn("发布前自动剔除", compact)

            work = codex_external.prepare(PROJECT_ROOT, ymd)
            candidate = work / "report.md"
            check = work / "check.md"
            repair_plan = work / "repair-plan.json"
            candidate.write_text("# report\n", encoding="utf-8")
            check.write_text("FAIL\n", encoding="utf-8")
            repair_plan.write_text(json.dumps({
                "schema_version": 2,
                "blocked": [{"url": "https://example.com/remove"}],
                "allowed_urls": ["https://example.com/add"],
                "missing_required_urls": [],
                "publication_target": 25,
                "safe_pool_exhausted": False,
                "replacements": [{"url": "https://example.com/add", "title": "safe"}],
            }), encoding="utf-8")
            captured = {}

            def fake_run(_root, _work, _codex, _control, _model, prompt, **_kwargs):
                captured["prompt"] = prompt
                (_work / "result.json").write_text(
                    json.dumps({"report_markdown": "# report\n\n" + "fact " * 30}),
                    encoding="utf-8",
                )

            with mock.patch.object(
                codex_external, "run_prompt_only", side_effect=fake_run
            ), mock.patch.dict(
                os.environ,
                {"RIBAO_TEST_MODE": "1", "RIBAO_EDITORIAL_POLICY": "business_v2_balanced"},
                clear=False,
            ):
                codex_external.repair_prompt_only(
                    PROJECT_ROOT, work, ymd, draft, candidate, check,
                    "codex", control, "model", repair_plan,
                )
            self.assertIn("测试 v2 不得使用 QA_SKIP", captured["prompt"])
            self.assertIn("missing_required_urls 必须恢复", captured["prompt"])
            self.assertIn("blocked URL 必须从正文删除", captured["prompt"])
            self.assertIn("deterministic_repair_plan", captured["prompt"])
            self.assertIn("https://example.com/remove", captured["prompt"])
            self.assertIn("仅可按 replacements 现有顺序补入", captured["prompt"])

    def test_warn_can_still_require_a_deterministic_delivery_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repair-plan.json"
            path.write_text(
                json.dumps({"schema_version": 2, "requires_repair": True}),
                encoding="utf-8",
            )
            self.assertTrue(codex_external.repair_plan_requires_action(path))
            path.write_text(
                json.dumps({"schema_version": 2, "requires_repair": False}),
                encoding="utf-8",
            )
            self.assertFalse(codex_external.repair_plan_requires_action(path))

    def test_daily_runner_does_not_stop_at_warn_with_pending_delivery_plan(self):
        runner = (PROJECT_ROOT / "资讯日报-skill" / "run_daily.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('PLAN_ACTION=0', runner)
        self.assertIn(
            'if [ "$QC_RC" -eq 0 ] && [ "$PLAN_ACTION" -eq 0 ]; then',
            runner,
        )
        self.assertIn('候选交付合同在 2 轮修复后仍未完成', runner)

    def test_compose_retries_inside_same_work_dir(self):
        stack, data_root, control = self.runtime_context()
        with stack:
            ymd = "20991228"
            draft_dir = data_root / "日报草稿"
            draft_dir.mkdir()
            draft = draft_dir / f"资讯日报-草稿-{ymd}.md"
            draft.write_text("# draft\n", encoding="utf-8")
            work = codex_external.prepare(PROJECT_ROOT, ymd)

            def fake_run(_root, active_work, *_args, attempt_label="", **_kwargs):
                if attempt_label == "full":
                    raise codex_external.PromptOnlyTimeout("timeout")
                (active_work / "result.json").write_text(
                    json.dumps({"report_markdown": "# report\n\n" + "fact " * 30}),
                    encoding="utf-8",
                )
                return active_work / "result.json"

            with mock.patch.object(codex_external, "run_prompt_only", side_effect=fake_run):
                candidate = codex_external.compose_prompt_only(
                    PROJECT_ROOT, work, ymd, draft, "codex", control, "model"
                )
            audit = json.loads((work / "compose-mode.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["mode"], "compact")
            self.assertTrue(audit["same_run"])
            self.assertEqual([a["status"] for a in audit["attempts"]], ["failed", "ok"])
            self.assertEqual(candidate.parent, work)

    def test_fixed_template_fallback_promotes_only_existing_backup_pool(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"RIBAO_MIN_REPORT_ITEMS": "2"}, clear=False
        ):
            base = Path(tmp)
            draft = base / "draft.md"
            work = base / "work"
            work.mkdir()
            draft.write_text(
                "## 泛娱乐·出海\n"
                "### 休闲游戏（精选 1 / 候选 2）\n"
                "- ⭐**休闲游戏 A 上线**（score 5）｜来源甲\n"
                "  - 面向海外市场测试混合休闲玩法。\n"
                "  - https://example.com/a\n"
                "  - 同事件独立来源佐证：来源丁 — https://example.com/a2\n"
                "- 备选：休闲游戏 B 更新｜来源乙 — https://example.com/b\n"
                "  - 摘要：面向海外市场增加新关卡，并披露留存、买量和IAP转化变化。\n"
                "\n## 过滤统计\n"
                "- 备选：不应进入｜来源丙 — https://example.com/c\n",
                encoding="utf-8",
            )
            candidate = codex_external.render_draft_fallback(
                PROJECT_ROOT, "20991227", draft, work
            )
            text = candidate.read_text(encoding="utf-8")
        self.assertIn("休闲游戏 A 上线", text)
        self.assertIn("休闲游戏 B 更新", text)
        self.assertIn("https://example.com/a2", text)
        self.assertIn("披露留存、买量和IAP转化变化", text)
        self.assertNotIn("不应进入", text)
        self.assertEqual(text.count("### "), 2)


if __name__ == "__main__":
    unittest.main()
