import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_candidate_audit


class CandidateAuditContractTests(unittest.TestCase):
    def make_snapshot(self, directory: Path) -> Path:
        snapshot = directory / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "source": "可信源",
                            "board": "全球融资",
                            "track": "泛AI·AI应用融资",
                            "item": {
                                "title": "Creator AI startup raises $20m funding",
                                "summary": "Creator AI provides video creation software for studios and raised $20m. " * 8,
                                "link": "https://example.com/funding?utm_source=x",
                                "src": "可信源",
                            },
                        },
                        {
                            "source": "可信源",
                            "board": "创新·AI娱乐",
                            "track": "AI模型与应用（娱乐向）",
                            "item": {
                                "title": "Small model benchmark test",
                                "summary": "A small lab benchmarked a training experiment. " * 8,
                                "link": "https://example.com/benchmark",
                                "src": "可信源",
                            },
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return snapshot

    def valid_actions(self, package):
        return [
            {
                "url": item["url"],
                "actions": {profile: "keep" for profile in codex_candidate_audit.PROFILES},
                "reason": "与确定性规则一致",
                "confidence": 0.8,
            }
            for item in package["items"]
        ]

    def test_output_binds_every_url_and_normalizes_finance_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = codex_candidate_audit.build_input(self.make_snapshot(Path(tmp)), "20991231")
        finance = next(item for item in package["items"] if item["funding_candidate"])
        raw = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": package["date"],
            "snapshot_sha256": package["snapshot_sha256"],
            "items": self.valid_actions(package),
            "funding_records": [
                {
                    "url": finance["url"],
                    "company": "Creator AI",
                    "business": "面向工作室的视频创作软件",
                    "business_model": "原文未说明",
                    "valuation": "未披露",
                    "evidence_excerpt": "provides video creation software for studios and raised $20m",
                }
            ],
        }
        validated = codex_candidate_audit.validate_output(raw, package)
        stored = codex_candidate_audit.funding_storage_records(package, validated["funding_records"])
        self.assertEqual(stored[0]["report_item_url"], finance["url"])
        self.assertEqual(stored[0]["business_model"], {"status": "not_disclosed"})
        self.assertEqual(stored[0]["valuation"], {"status": "not_disclosed"})
        self.assertEqual(stored[0]["audit_scope"], "source_only")

    def test_promote_of_non_promotable_item_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = codex_candidate_audit.build_input(self.make_snapshot(Path(tmp)), "20991231")
        raw = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": package["date"],
            "snapshot_sha256": package["snapshot_sha256"],
            "items": self.valid_actions(package),
            "funding_records": [],
        }
        raw["items"][0]["actions"]["business_v2_recall"] = "promote"
        with self.assertRaises(codex_candidate_audit.CandidateAuditError):
            codex_candidate_audit.validate_output(raw, package)

    def test_strict_edition_can_promote_only_an_explicitly_promotable_review_item(self):
        package = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": "20991231",
            "snapshot_sha256": "c" * 64,
            "items": [{
                "url": "https://example.com/review",
                "title": "完整灰区候选",
                "summary": "具体产品事实" * 40,
                "source": "可信源",
                "funding_candidate": False,
                "promotable_profiles": ["business_v2_strict"],
            }],
        }
        raw = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": package["date"],
            "snapshot_sha256": package["snapshot_sha256"],
            "items": self.valid_actions(package),
            "funding_records": [],
        }
        promotable = package["items"][0]
        raw["items"][0]["actions"]["business_v2_strict"] = "promote"
        validated = codex_candidate_audit.validate_output(raw, package)
        result = next(row for row in validated["items"] if row["url"] == promotable["url"])
        self.assertEqual(result["actions"]["business_v2_strict"], "promote")

    def test_input_marks_only_complete_strict_reviews_as_promotable(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = codex_candidate_audit.build_input(self.make_snapshot(Path(tmp)), "20991231")
        for item in package["items"]:
            if "business_v2_strict" in item["promotable_profiles"]:
                self.assertEqual(item["policy"]["business_v2_strict"]["decision"], "review")
                self.assertGreaterEqual(len("".join(item["summary"].split())), 120)

    def test_response_schema_allows_validated_strict_promotion_and_pins_version(self):
        schema = json.loads(
            (Path(codex_candidate_audit.__file__).parent / "schemas" / "candidate-audit.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema_version"]["type"], "integer")
        self.assertEqual(schema["properties"]["schema_version"]["const"], codex_candidate_audit.SCHEMA_VERSION)
        strict_actions = schema["properties"]["items"]["items"]["properties"]["actions"]["properties"][
            "business_v2_strict"
        ]["enum"]
        self.assertIn("promote", strict_actions)

    def test_runtime_setup_error_becomes_safe_audit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = self.make_snapshot(directory)
            with mock.patch.object(
                codex_candidate_audit.codex_external,
                "prepare",
                side_effect=codex_candidate_audit.codex_external.ExternalRuntimeError("runtime unavailable"),
            ):
                with self.assertRaisesRegex(codex_candidate_audit.CandidateAuditError, "runtime unavailable"):
                    codex_candidate_audit.audit(
                        directory,
                        snapshot,
                        directory / "audit.json",
                        "20991231",
                    )

    def test_hard_rejects_are_not_sent_to_the_bounded_model_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = codex_candidate_audit.build_input(self.make_snapshot(Path(tmp)), "20991231")
        self.assertTrue(package["items"])
        for item in package["items"]:
            self.assertTrue(any(policy["decision"] != "reject" for policy in item["policy"].values()))

    def test_aggregate_item_cannot_promote_an_explicit_out_sector(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = self.make_snapshot(directory)
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            payload["records"].append({
                "source": "聚合源",
                "board": "全球融资",
                "track": "泛AI·AI应用融资",
                "item": {
                    "title": "早报：顺为披露宇树科技A轮投资",
                    "summary": "宇树科技主营人形机器人和四足机器人。" * 8,
                    "link": "https://example.com/robot-roundup",
                    "src": "聚合源",
                },
            })
            snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            package = codex_candidate_audit.build_input(snapshot, "20991231")

        self.assertNotIn(
            "https://example.com/robot-roundup",
            {item["url"] for item in package["items"]},
        )

    def test_audit_scope_keeps_all_accepts_and_bounds_grey_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = codex_candidate_audit.build_input(self.make_snapshot(Path(tmp)), "20991231")
        scope = package["audit_scope"]
        self.assertLessEqual(scope["review_items_included"], codex_candidate_audit.MAX_REVIEW_ITEMS)
        self.assertEqual(scope["review_items_left_native"], 0)
        self.assertEqual(len(package["items"]), scope["accept_items"] + scope["review_items_included"])

    def test_large_raw_snapshot_is_reduced_before_the_model_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = self.make_snapshot(directory)
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            for index in range(codex_candidate_audit.MAX_ITEMS):
                payload["records"].append({
                    "source": "宏观消息源",
                    "board": "排查",
                    "track": "非目标",
                    "item": {
                        "title": f"宏观政策通告 {index}",
                        "summary": "与泛娱乐、消费AI应用和目标融资无关的宏观政策信息。" * 8,
                        "link": f"https://example.com/macro/{index}",
                        "src": "宏观消息源",
                    },
                })
            snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            package = codex_candidate_audit.build_input(snapshot, "20991231")

        self.assertGreater(len(payload["records"]), codex_candidate_audit.MAX_ITEMS)
        self.assertLessEqual(len(package["items"]), codex_candidate_audit.MAX_ITEMS)

    def test_audit_packages_are_bounded_to_six_items(self):
        package = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": "20991231",
            "snapshot_sha256": "a" * 64,
            "items": [{"url": f"https://example.com/{index}"} for index in range(16)],
            "audit_scope": {"accept_items": 4, "review_items_available": 12,
                            "review_items_included": 12, "review_items_left_native": 0},
        }
        batches = codex_candidate_audit.batch_packages(package)
        self.assertEqual([len(batch["items"]) for batch in batches], [4, 4, 4, 4])
        self.assertEqual([batch["audit_scope"]["batch_index"] for batch in batches], [1, 2, 3, 4])
        self.assertTrue(all(batch["audit_scope"]["batch_total"] == 4 for batch in batches))

    def test_failed_batch_uses_neutral_native_actions(self):
        entries = codex_candidate_audit.native_fallback_entries(
            {"items": [{"url": "https://example.com/a"}]}, "batch unavailable"
        )
        self.assertEqual(entries[0]["actions"], {
            "business_v2_strict": "keep",
            "business_v2_balanced": "keep",
            "business_v2_recall": "keep",
        })
        self.assertIn("unavailable", entries[0]["reason"])

    def test_one_failed_batch_does_not_discard_successful_batches(self):
        package = {
            "schema_version": codex_candidate_audit.SCHEMA_VERSION,
            "date": "20991231",
            "snapshot_sha256": "b" * 64,
            "items": [
                {"url": f"https://example.com/{index}", "source": "测试源", "funding_candidate": False}
                for index in range(7)
            ],
            "audit_scope": {"accept_items": 0, "review_items_available": 7,
                            "review_items_included": 7, "review_items_left_native": 0},
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first_batch = codex_candidate_audit.batch_packages(package)[0]
            result = directory / "result.json"
            result.write_text(json.dumps({
                "schema_version": codex_candidate_audit.SCHEMA_VERSION,
                "date": package["date"],
                "snapshot_sha256": package["snapshot_sha256"],
                "items": [
                    {"url": item["url"], "actions": {profile: "keep" for profile in codex_candidate_audit.PROFILES},
                     "reason": "审核通过", "confidence": 0.8}
                    for item in first_batch["items"]
                ],
                "funding_records": [],
            }, ensure_ascii=False), encoding="utf-8")

            def fake_run(_root, _work, _codex, _control_home, _model, prompt, **_kwargs):
                payload = json.loads(
                    prompt.split("<frozen_candidate_package>\n", 1)[1].split(
                        "\n</frozen_candidate_package>", 1
                    )[0]
                )
                if payload["audit_scope"]["batch_index"] == 1:
                    return result
                raise codex_candidate_audit.codex_external.PromptOnlyTimeout("timeout")

            with mock.patch.object(codex_candidate_audit, "build_input", return_value=package), \
                 mock.patch.object(codex_candidate_audit.shutil, "which", return_value="codex"), \
                 mock.patch.object(codex_candidate_audit.codex_external, "prepare", return_value=directory), \
                 mock.patch.object(codex_candidate_audit.codex_external, "run_prompt_only", side_effect=fake_run):
                status = codex_candidate_audit.audit(directory, directory / "snapshot.json", directory / "audit.json", "20991231")
            self.assertEqual(status, "applied")
            saved = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["items"]), 7)
            self.assertIn("保留原生规则", saved["items"][-1]["reason"])
            batch_status = json.loads((directory / "audit-batches.json").read_text(encoding="utf-8"))
            self.assertEqual(batch_status["successful_batches"], 1)
            self.assertEqual(batch_status["fallback_batches"], 1)


if __name__ == "__main__":
    unittest.main()
