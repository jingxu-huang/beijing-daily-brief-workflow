import unittest

import json
import tempfile
from pathlib import Path

import check_report
import fetch_news


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.quality = {
            "min_selected_candidates": 8,
            "min_selected_sources": 3,
            "max_failed_public_sources": 2,
        }

    def test_healthy_public_coverage_passes(self):
        self.assertEqual(fetch_news.coverage_gate_failures(10, 4, 1, self.quality), [])

    def test_one_item_report_candidate_pool_is_rejected(self):
        failures = fetch_news.coverage_gate_failures(1, 1, 0, self.quality)
        self.assertTrue(any("精选候选仅 1 条" in item for item in failures))
        self.assertTrue(any("精选仅覆盖 1 个来源" in item for item in failures))

    def test_too_many_public_source_failures_are_rejected(self):
        failures = fetch_news.coverage_gate_failures(20, 5, 3, self.quality)
        self.assertTrue(any("公开源失败 3 个" in item for item in failures))

    def test_wewe_items_cannot_satisfy_public_coverage(self):
        selected = [
            {"_registry_source": "IT之家"},
            {"_registry_source": "WeWe-RSS"},
            {"_registry_source": "WeWe-RSS"},
        ]
        self.assertEqual(
            fetch_news.public_selection_stats(selected, ["IT之家", "DoNews"]),
            (1, 1),
        )

    def test_werss_items_cannot_satisfy_public_coverage(self):
        selected = [
            {"_registry_source": "IT之家"},
            {"_registry_source": "微信公众号(WeRSS)"},
        ]
        self.assertEqual(
            fetch_news.public_selection_stats(selected, ["IT之家", "DoNews"]),
            (1, 1),
        )

    def test_weread_omni_items_cannot_satisfy_public_coverage(self):
        selected = [
            {"_registry_source": "IT之家"},
            {"_registry_source": "微信公众号(weread-omni测试)"},
        ]
        self.assertEqual(
            fetch_news.public_selection_stats(selected, ["IT之家", "DoNews"]),
            (1, 1),
        )

    def test_report_volume_gate_rejects_one_item_one_source(self):
        failures = check_report.report_volume_gate_failures(
            1,
            1,
            {"min_report_items": 5, "min_report_sources": 2},
        )
        self.assertTrue(any("成稿仅 1 条" in item for item in failures))
        self.assertTrue(any("仅覆盖 1 个来源" in item for item in failures))

    def test_source_rows_only_reads_source_table(self):
        report = """Source 来源：

|来源|本期采用条数|
|---|---:|
|IT之家|5|
|DoNews|2|

<br>

# 其他
|错误计入的表|99|
"""
        self.assertEqual(
            check_report.source_rows(report),
            [("IT之家", 5), ("DoNews", 2)],
        )

    def test_quality_gate_is_a_config_sibling_of_select(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            config = fetch_news.yaml.safe_load(handle)["config"]
        self.assertNotIn("quality_gate", config["select"])
        self.assertEqual(config["quality_gate"]["min_selected_candidates"], 8)
        self.assertEqual(config["quality_gate"]["min_report_body_chars"], 120)

    def test_short_report_items_enforces_formal_body_floor(self):
        items = [
            {"title": "短条", "body_text": "某公司上线一项功能。（示例来源）"},
            {
                "title": "完整条",
                "body_text": "某公司正式上线面向内容创作者的 AI 产品，支持从自然语言生成初稿、"
                "编辑素材并在团队中协作。产品已开放给指定用户，厂商同时披露了主要操作流程、"
                "适用场景和输出方式，用户可在网页端继续修改生成内容。该版本还支持导入已有"
                "素材并导出多个内容规格，方便创作者将同一项目用于短视频、图文和互动页面。"
                "（示例来源）",
            },
        ]
        short = check_report.short_report_items(items, 120)
        self.assertEqual([item["title"] for item in short], ["短条"])

    def test_missing_quality_threshold_is_rejected_instead_of_using_hidden_default(self):
        with self.assertRaisesRegex(ValueError, "min_selected_candidates"):
            fetch_news.quality_gate_limit({}, "min_selected_candidates")

    def test_qa_skip_state_survives_publish_comment_stripping(self):
        links = {"https://example.com/a", "https://example.com/b"}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "qa-skip-20260901.json"
            check_report.save_qa_skip_state(state, "20260901", links)
            self.assertEqual(check_report.load_qa_skip_state(state), links)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["target_ymd"], "20260901")

    def test_invalid_qa_skip_state_fails_closed_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "qa-skip.json"
            state.write_text("not-json\n", encoding="utf-8")
            self.assertEqual(check_report.load_qa_skip_state(state), set())


if __name__ == "__main__":
    unittest.main()
