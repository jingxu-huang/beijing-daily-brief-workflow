import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SENDER_PATH = REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py"


def load_sender():
    spec = importlib.util.spec_from_file_location("send_to_feishu_cli", SENDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeishuGroupPermissionTests(unittest.TestCase):
    def test_failed_lark_command_redacts_permission_targets(self):
        sender = load_sender()
        result = mock.Mock(stdout="", stderr="", returncode=1)
        with mock.patch("builtins.print") as printer:
            sender.summarize_completed(
                ["lark-cli", "drive", "permission.members", "create", "--token", "doc-token-value", "--chat-id", "chat-id-value", "--data", "payload-value"],
                result,
            )
        rendered = " ".join(str(call.args) for call in printer.call_args_list)
        self.assertNotIn("doc-token-value", rendered)
        self.assertNotIn("chat-id-value", rendered)
        self.assertNotIn("payload-value", rendered)

    def test_grant_only_mode_is_available_for_safe_permission_repair(self):
        sender = SENDER_PATH.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--grant-only"', sender)
        self.assertIn("if args.grant_only:", sender)
        self.assertIn("无法补授权限", sender)

    def test_test_document_title_suffix_is_explicit(self):
        sender = load_sender()
        with mock.patch.dict("os.environ", {"FEISHU_REPORT_TITLE_SUFFIX": "（测试版）"}):
            self.assertEqual(
                sender.title_from_path("/tmp/资讯类简报-9.8日.md"),
                "资讯类简报-9.8日（测试版）",
            )

    def test_test_document_title_can_use_short_explicit_name(self):
        sender = load_sender()
        with mock.patch.dict("os.environ", {"FEISHU_REPORT_TITLE": "测试版v2-严格"}):
            self.assertEqual(
                sender.title_from_path("/tmp/资讯类简报-9.8日.md"),
                "测试版v2-严格",
            )

    def test_versioned_test_registry_key_is_supported(self):
        sender = load_sender()
        with mock.patch.dict(
            "os.environ",
            {"FEISHU_REGISTRY_KEY": "20260908-test-1540"},
        ):
            self.assertEqual(sender.registry_key_for("20260908"), "20260908-test-1540")

    def test_invalid_test_registry_key_is_rejected(self):
        sender = load_sender()
        with mock.patch.dict(
            "os.environ",
            {"FEISHU_REGISTRY_KEY": "20260908-test-版本一"},
        ):
            with self.assertRaisesRegex(RuntimeError, "注册键格式无效"):
                sender.registry_key_for("20260908")

    def test_skip_permission_contract_is_test_only_and_doc_only(self):
        sender = SENDER_PATH.read_text(encoding="utf-8")
        self.assertIn("跳过群权限只允许在显式测试模式使用", sender)
        self.assertIn("跳过群权限的测试发布必须使用 doc-only", sender)
        self.assertIn('"group_permission": "skipped" if skip_permission', sender)

    def test_test_doc_only_publish_never_grants_or_sends(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "资讯类简报-9.8日.md"
            check = Path(tmp) / "check.md"
            report.write_text("# 测试", encoding="utf-8")
            check.write_text("- 结论：PASS", encoding="utf-8")
            argv = [
                "send_to_feishu_cli.py",
                "--report", str(report),
                "--check", str(check),
                "--date", "20260908",
                "--doc-only",
                "--import-as", "user",
                "--dry-run",
            ]
            env = {
                "RIBAO_TEST_MODE": "1",
                "FEISHU_TEST_SKIP_PERMISSION": "1",
                "FEISHU_REGISTRY_KEY": "20260908-test-1540",
            }
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(sender.shutil, "which", return_value="/usr/bin/lark-cli"),
                mock.patch.object(sender, "load_env_file", return_value=False),
                mock.patch.object(sender, "stage_report_for_lark", return_value=report),
                mock.patch.object(sender, "load_registry", return_value={}),
                mock.patch.object(sender, "import_report", return_value=("doc-token", "https://example.test/doc")),
                mock.patch.object(sender, "verify_report_images", return_value=True),
                mock.patch.object(sender, "grant_chat_permission") as grant,
                mock.patch.object(sender, "send_chat_message") as send,
                mock.patch("builtins.print"),
            ):
                self.assertEqual(sender.main(), 0)
            grant.assert_not_called()
            send.assert_not_called()

    def test_group_full_access_payload(self):
        sender = load_sender()
        with mock.patch.object(sender, "run_lark", return_value={"ok": True}) as run_lark:
            sender.grant_chat_permission(
                "doc-token",
                "chat-id",
                "full_access",
                permission_as="user",
            )

        args = run_lark.call_args.args[0]
        payload = json.loads(args[args.index("--data") + 1])
        self.assertEqual(payload["member_type"], "openchat")
        self.assertEqual(payload["member_id"], "chat-id")
        self.assertEqual(payload["type"], "chat")
        self.assertEqual(payload["perm"], "full_access")
        self.assertIn("--yes", args)
        self.assertEqual(args[args.index("--as") + 1], "user")

    def test_test_group_edit_payload(self):
        sender = load_sender()
        with (
            mock.patch.dict(os.environ, {"RIBAO_TEST_MODE": "1"}),
            mock.patch.object(sender, "run_lark", return_value={"ok": True}) as run_lark,
        ):
            sender.grant_chat_permission(
                "doc-token",
                "chat-id",
                "edit",
                permission_as="user",
            )

        args = run_lark.call_args.args[0]
        payload = json.loads(args[args.index("--data") + 1])
        self.assertEqual(payload["member_type"], "openchat")
        self.assertEqual(payload["perm"], "edit")

    def test_weaker_group_permission_is_rejected_outside_test_mode(self):
        sender = load_sender()
        for perm in ("view", "edit"):
            with self.subTest(perm=perm):
                with (
                    mock.patch.dict(os.environ, {"RIBAO_TEST_MODE": "0"}),
                    self.assertRaisesRegex(RuntimeError, "测试日报仅允许 edit"),
                ):
                    sender.grant_chat_permission("doc-token", "chat-id", perm)

    def test_view_is_rejected_in_test_mode(self):
        sender = load_sender()
        with (
            mock.patch.dict(os.environ, {"RIBAO_TEST_MODE": "1"}),
            self.assertRaisesRegex(RuntimeError, "测试日报仅允许 edit"),
        ):
            sender.grant_chat_permission("doc-token", "chat-id", "view")


if __name__ == "__main__":
    unittest.main()
