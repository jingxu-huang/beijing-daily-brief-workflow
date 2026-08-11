import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PublicPackageContractTests(unittest.TestCase):
    def test_feishu_chat_has_no_default(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        sender = (REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py").read_text(encoding="utf-8")
        self.assertIn('REPORT_CHAT_ID="${FEISHU_REPORT_CHAT_ID:-}"', wrapper)
        self.assertNotIn("DEFAULT_CHAT_ID", sender)

    def test_only_one_daily_scheduler_template(self):
        templates = list((REPO_ROOT / "automation").glob("*guard*.plist.template"))
        self.assertEqual([path.name for path in templates], ["com.example.beijing-daily-brief-guard.plist.template"])

    def test_audit_accepts_repository(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/audit_public_package.py"), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
