import sys
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import update_daily_index  # noqa: E402


class IndexRecoveryTests(unittest.TestCase):
    def test_recovers_only_valid_rows_and_preserves_existing_dates(self):
        markdown = "\n".join(
            [
                "| 日期 | 日报文档 | 质检 | 更新时间 |",
                "|---|---|---|---|",
                "| 8.10 | [资讯类简报-8.10日](https://example.invalid/docx/abc123) | PASS | - |",
                "| 8.11 | [资讯类简报-8.11日](https://example.invalid/docx/def456) | WARN | - |",
                "| 8.12 | not-a-link | PASS | - |",
            ]
        )
        recovered = update_daily_index.recover_registry_from_markdown(
            markdown,
            "2026",
            {"20260811": {"title": "current", "url": "https://example.invalid/docx/current"}},
        )
        self.assertEqual(list(recovered), ["20260810"])
        self.assertEqual(recovered["20260810"]["last_verdict"], "PASS")
        self.assertTrue(recovered["20260810"]["recovered_from_index"])

    def test_rejects_invalid_recovery_year(self):
        with self.assertRaises(RuntimeError):
            update_daily_index.recover_registry_from_markdown("", "26", {})

    def test_extracts_markdown_from_lark_cli_json(self):
        raw = '{"ok": true, "data": {"content": "| 8.10 | entry |"}}'
        self.assertEqual(
            update_daily_index.extract_history_markdown(raw),
            "| 8.10 | entry |",
        )


if __name__ == "__main__":
    unittest.main()
