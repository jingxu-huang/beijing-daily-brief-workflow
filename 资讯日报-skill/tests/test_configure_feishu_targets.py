import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/configure_feishu_targets.py"


def load_module():
    spec = importlib.util.spec_from_file_location("configure_feishu_targets", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigureFeishuTargetsTests(unittest.TestCase):
    def test_updates_only_managed_delivery_keys(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "config.env"
            env_path.write_text(
                "RIBAO_ENABLE_FEISHU=0\n"
                "FEISHU_REPORT_CHAT_ID=\n"
                "FEISHU_REPORT_CHAT_PERM=edit\n"
                "FEISHU_CLI_SEND_AS=bot\n"
                "FEISHU_CLI_IMPORT_AS=bot\n"
                "FEISHU_CLI_PERMISSION_AS=user\n"
                "FEISHU_DOC_ONLY=1\n"
                "FEISHU_DAILY_INDEX=0\n"
                "UNCHANGED=value\n",
                encoding="utf-8",
            )
            module.configure(env_path, "oc_Test123", True)
            text = env_path.read_text(encoding="utf-8")
        self.assertIn("RIBAO_ENABLE_FEISHU=1", text)
        self.assertIn("FEISHU_REPORT_CHAT_PERM=full_access", text)
        self.assertIn("FEISHU_DOC_ONLY=0", text)
        self.assertIn("FEISHU_DAILY_INDEX=1", text)
        self.assertIn("UNCHANGED=value", text)

    def test_index_only_uses_user_identity_and_disables_group_messages(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "config.env"
            env_path.write_text(
                "RIBAO_ENABLE_FEISHU=0\n"
                "FEISHU_REPORT_CHAT_ID=\n"
                "FEISHU_REPORT_CHAT_PERM=edit\n"
                "FEISHU_CLI_SEND_AS=bot\n"
                "FEISHU_CLI_IMPORT_AS=bot\n"
                "FEISHU_CLI_PERMISSION_AS=bot\n"
                "FEISHU_DOC_ONLY=0\n"
                "FEISHU_DAILY_INDEX=0\n",
                encoding="utf-8",
            )
            module.configure(env_path, "oc_Test123", True, index_only=True)
            text = env_path.read_text(encoding="utf-8")
        self.assertIn("FEISHU_CLI_SEND_AS=user", text)
        self.assertIn("FEISHU_CLI_IMPORT_AS=user", text)
        self.assertIn("FEISHU_CLI_PERMISSION_AS=user", text)
        self.assertIn("FEISHU_DOC_ONLY=1", text)
        self.assertIn("RIBAO_GROUP_MESSAGES=0", text)

    def test_rejects_invalid_chat_id(self):
        module = load_module()
        with self.assertRaisesRegex(RuntimeError, "格式无效"):
            module.configure("unused", "not-a-chat", True)


if __name__ == "__main__":
    unittest.main()
