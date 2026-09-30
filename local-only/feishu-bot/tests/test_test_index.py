import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("update_test_index", SCRIPT_DIR / "update_test_index.py")
test_index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(test_index)


def production_status(qa="PASS", final="generated", captured_at="2026-09-15 15:20:00"):
    return {
        "qa": qa,
        "final": final,
        "captured_at": captured_at,
        "public": {
            "status": "healthy",
            "enabled": 20,
            "candidate_sources": 11,
            "selected_sources": 7,
            "failed": 0,
        },
        "public_account": {
            "status": "healthy",
            "accounts": 12,
            "items": 8,
            "fresh_sources": 7,
            "stale_sources": 2,
            "unassessed_sources": 3,
        },
        "article_flow": {
            "public": {"grabbed": 120, "selected": 18, "backup": 32, "adopted": 9},
            "account": {"grabbed": 8, "selected": 2, "backup": 3, "adopted": 1},
        },
    }


class TestIndexTests(unittest.TestCase):
    def test_register_existing_index_without_creating_or_replacing_it(self):
        base = "https://micoworld.feishu.cn"
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "feishu-test-index.json"
            with mock.patch.object(test_index, "INDEX_REGISTRY_PATH", registry_path):
                result = test_index.register_index_target(
                    base + "/docx/ExampleToken123?from=copy",
                    "资讯日报测试版本",
                )
                self.assertTrue(result["registered"])
                saved = json.loads(registry_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["doc_token"], "ExampleToken123")
                self.assertEqual(saved["url"], base + "/docx/ExampleToken123")
                with self.assertRaisesRegex(RuntimeError, "不同的测试索引目标"):
                    test_index.register_index_target(
                        base + "/docx/AnotherToken123",
                        "资讯日报测试版本",
                    )
                self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8"))["doc_token"], "ExampleToken123")

    def test_register_rejects_non_feishu_target(self):
        with self.assertRaisesRegex(RuntimeError, "micoworld.feishu.cn/docx"):
            test_index.feishu_index_target("https://example.com" + "/docx/ExampleToken123")

    def test_public_source_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft_dir = Path(tmp) / "日报草稿"
            draft_dir.mkdir()
            (draft_dir / "资讯日报-草稿-20260911.md").write_text(
                "- 启用自动公开源 20 个；有候选来源 11 个；精选覆盖 7 个；抓取失败 1 个。\n",
                encoding="utf-8",
            )
            status = test_index.public_source_status(tmp, "20260911")
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["candidate_sources"], 11)

    def test_public_account_status(self):
        manifest = {
            "steps": {
                "weread_omni_preflight": {
                    "status": "ok",
                    "detail": (
                        "status=healthy accounts=12 items=8 fresh_sources=7 "
                        "stale_sources=2 unassessed_sources=3 failure_class=none"
                    ),
                }
            }
        }
        status = test_index.public_account_status(manifest)
        self.assertEqual(status["status"], "healthy")
        self.assertEqual(status["accounts"], 12)
        self.assertEqual(status["items"], 8)

    def test_render_uses_one_row_per_date_with_three_version_columns(self):
        reports = {
            "20260908-test-strict-old": {
                "url": "https://example.test/old",
                "title": "旧测试日报",
                "test_profile": "business_v2_strict",
                "updated_at": "2026-09-14 23:00:00",
                "production_status": production_status(captured_at="2026-09-14 23:00:00"),
            },
            "20260909-test-strict-new": {
                "url": "https://example.test/strict",
                "title": "严格测试日报",
                "test_profile": "business_v2_strict",
                "test_version": "strict-new",
                "updated_at": "2026-09-15 14:35:00",
                "production_status": production_status(qa="WARN"),
            },
        }
        runs = {
            "20260909:business_v2_balanced:balanced-new": {
                "ymd": "20260909",
                "profile": "business_v2_balanced",
                "label": "balanced-new",
                "status": "blocked",
                "qa": "FAIL",
                "updated_at": "2026-09-15 14:45:00",
                "production_status": production_status(qa="FAIL", final="blocked"),
            }
        }
        markdown = test_index.render_test_index(
            reports,
            "测试索引",
            runs,
            active_since="2026-09-15 00:00:00",
        )
        self.assertEqual(markdown.count("| 9.9 |"), 1)
        self.assertNotIn("9.8", markdown)
        self.assertIn(
            "| 日期 | 测试版v2-严格 | 测试版v2-平衡 | 测试版v2-高召回 | "
            "公开源 | 公众号源 | 质检结果 | 生成时间 |",
            markdown,
        )
        self.assertIn("测试版v2-严格", markdown)
        self.assertIn("测试版v2-平衡", markdown)
        self.assertIn("测试版v2-高召回", markdown)
        self.assertIn("[测试版v2-严格](https://example.test/strict)", markdown)
        self.assertIn("| FAIL | 2026-09-15 14:45:00 |", markdown)
        self.assertIn("无文档", markdown)
        self.assertIn("无文档", markdown)
        self.assertIn("抓到 120 / 精选 18 / 候补 32 / 采用 9", markdown)
        self.assertIn("启用 / 覆盖 7/27 / 抓到 8 / 精选 2 / 候补 3 / 采用 1", markdown)
        self.assertNotIn("严格：抓到", markdown)
        self.assertNotIn("平衡：抓到", markdown)
        self.assertIn("2026-09-15 14:45:00", markdown)
        self.assertNotIn("更新：", markdown)
        self.assertNotIn("版本：", markdown)

    def test_render_keeps_only_latest_run_for_same_date_and_profile(self):
        runs = {
            "old": {
                "ymd": "20260915",
                "profile": "business_v2_strict",
                "label": "old",
                "status": "failed",
                "qa": "FAIL",
                "updated_at": "2026-09-15 15:10:00",
            },
            "new": {
                "ymd": "20260915",
                "profile": "business_v2_strict",
                "label": "new",
                "status": "generated",
                "qa": "PASS",
                "updated_at": "2026-09-15 15:20:00",
            },
        }
        markdown = test_index.render_test_index({}, "测试索引", runs)
        self.assertIn("| PASS | 2026-09-15 15:20:00 |", markdown)
        self.assertNotIn("| FAIL |", markdown)

    def test_run_metrics_override_stale_published_registry_metrics(self):
        reports = {
            "20260915-test-strict-daily": {
                "url": "https://example.test/strict",
                "title": "测试版v2-严格",
                "test_profile": "business_v2_strict",
                "production_status": production_status(),
            }
        }
        fresh = production_status(captured_at="2026-09-15 15:30:00")
        fresh["article_flow"]["public"]["grabbed"] = 109
        runs = {
            "20260915:business_v2_strict:daily": {
                "ymd": "20260915",
                "profile": "business_v2_strict",
                "label": "daily",
                "registry_key": "20260915-test-strict-daily",
                "status": "generated",
                "qa": "WARN",
                "updated_at": "2026-09-15 15:30:00",
                "production_status": fresh,
            }
        }
        markdown = test_index.render_test_index(reports, "测试索引", runs)
        self.assertIn("抓到 109 / 精选 18 / 候补 32 / 采用 9", markdown)
        self.assertIn("2026-09-15 15:30:00", markdown)

    def test_failed_fresh_run_hides_stale_published_document(self):
        reports = {
            "20260915-test-strict-daily": {
                "url": "https://example.test/stale-strict",
                "title": "测试版v2-严格",
                "test_profile": "business_v2_strict",
                "production_status": production_status(),
            }
        }
        runs = {
            "20260915:business_v2_strict:daily": {
                "ymd": "20260915",
                "profile": "business_v2_strict",
                "label": "daily",
                "registry_key": "20260915-test-strict-daily",
                "status": "blocked",
                "qa": "FAIL",
                "updated_at": "2026-09-15 15:30:00",
                "production_status": production_status(qa="FAIL", final="blocked"),
            }
        }
        markdown = test_index.render_test_index(reports, "测试索引", runs)
        self.assertIn("| 9.15 | 无文档 |", markdown)
        self.assertNotIn("https://example.test/stale-strict", markdown)
        self.assertIn("| FAIL | 2026-09-15 15:30:00 |", markdown)

    def test_article_flow_counts_public_and_public_account_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "日报状态").mkdir()
            (root / "日报草稿").mkdir()
            (root / "日报文档").mkdir()
            (root / "日报状态" / "资讯日报-筛选审计-20260915.json").write_text(
                json.dumps({"records": [
                    {"stage": "keyword_recall", "source": "公开站", "link": "https://public.test/a", "title": "A"},
                    {"stage": "keyword_recall", "source": "公众号甲", "link": "https://mp.weixin.qq.com/s/a", "title": "B"},
                ]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "日报草稿" / "资讯日报-草稿-20260915.md").write_text(
                "- ⭐**A**（score 9）｜公开站\n  - https://public.test/a\n"
                "- 备选：B｜公众号甲 — https://mp.weixin.qq.com/s/a\n",
                encoding="utf-8",
            )
            (root / "日报文档" / "资讯类简报-9.15日.md").write_text(
                "### 1）A\n\n正文（公开站）\n\nhttps://public.test/a\n",
                encoding="utf-8",
            )
            status = test_index.article_flow_status(tmp, "20260915")
        self.assertEqual(status["public"], {"grabbed": 1, "selected": 1, "backup": 0, "adopted": 1})
        self.assertEqual(status["account"], {"grabbed": 1, "selected": 0, "backup": 1, "adopted": 0})

    def test_article_flow_uses_shared_snapshot_when_replay_has_no_keyword_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "日报状态").mkdir()
            (root / "日报草稿").mkdir()
            (root / "日报文档").mkdir()
            snapshot = root / "shared.json"
            snapshot.write_text(json.dumps({"records": [
                {"source": "公开站", "item": {"title": "A", "link": "https://public.test/a"}},
                {"source": "公众号甲", "item": {"title": "B", "link": "https://mp.weixin.qq.com/s/b"}},
            ]}, ensure_ascii=False), encoding="utf-8")
            (root / "日报草稿" / "资讯日报-草稿-20260915.md").write_text(
                "- ⭐**A**（score 9）｜公开站\n  - https://public.test/a\n",
                encoding="utf-8",
            )
            status = test_index.article_flow_status(tmp, "20260915", source_snapshot=snapshot)
        self.assertEqual(status["public"]["grabbed"], 1)
        self.assertEqual(status["account"]["grabbed"], 1)

    def test_test_index_permission_is_edit_and_silent(self):
        with mock.patch.object(test_index.feishu, "run_lark", return_value={"ok": True}) as run:
            test_index.grant_test_index_edit_permission(
                "doc-token",
                "chat-id",
                permission_as="user",
            )
        args = run.call_args.args[0]
        payload = json.loads(args[args.index("--data") + 1])
        self.assertEqual(payload["member_type"], "openchat")
        self.assertEqual(payload["member_id"], "chat-id")
        self.assertEqual(payload["perm"], "edit")
        self.assertIn("--yes", args)
        self.assertNotIn("--need-notification", args)
        self.assertNotIn("im", args)

    def test_guard_requires_test_mode_user_identity_and_group_target(self):
        source = (SCRIPT_DIR / "update_test_index.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("RIBAO_TEST_MODE", "0") != "1"', source)
        self.assertIn('args.import_as != "user" or args.permission_as != "user"', source)
        self.assertIn("grant_test_index_edit_permission", source)
        self.assertIn('"messages_sent": False', source)
        self.assertIn("if args.report_registry and not args.dry_run", source)
        self.assertIn("feishu.save_registry(reports)", source)


if __name__ == "__main__":
    unittest.main()
