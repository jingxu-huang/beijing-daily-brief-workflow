import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import report_images


class ReportImageTests(unittest.TestCase):
    def test_parse_draft_collects_article_and_candidate_images(self):
        draft = """# 草稿

- ⭐**产品发布**（score 9）｜IT之家（发布 2026-09-16 09:00）
  - 摘要：这是产品正文。
  - 候选图: https://img.example.com/product.png | https://img.example.com/logo.png
  - https://www.example.com/article
"""
        rows = report_images.parse_draft(draft)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "IT之家")
        self.assertEqual(rows[0]["link"], "https://www.example.com/article")
        self.assertEqual(len(rows[0]["images"]), 2)

    def test_candidate_urls_filter_noise_and_wechat(self):
        row = {
            "source": "IT之家",
            "link": "https://www.example.com/article",
            "images": [
                "https://img.example.com/assets/logo.png",
                "https://img.example.com/products/device.png",
                "https://img.example.com/products/screen.png",
                "https://img.example.com/products/third.png",
            ],
        }
        self.assertEqual(
            report_images.candidate_urls(row),
            [
                "https://img.example.com/products/device.png",
                "https://img.example.com/products/screen.png",
            ],
        )
        row["source"] = "微信公众号/测试号"
        self.assertEqual(report_images.candidate_urls(row), [])

    def test_image_command_is_read_only_ephemeral_and_attaches_files(self):
        command = report_images.image_command(
            "/path/to/codex",
            "model",
            Path("/work/schema.json"),
            [Path("/work/one.png"), Path("/work/two.png")],
        )
        joined = " ".join(command)
        self.assertIn("--ephemeral", command)
        self.assertIn("read-only", command)
        self.assertIn("--image", command)
        self.assertIn("/work/one.png", command)
        self.assertIn("/work/two.png", command)
        self.assertNotIn("workspace-write", joined)
        self.assertNotIn("danger-full-access", joined)
        for feature in ("shell_tool", "plugins", "browser_use", "computer_use", "multi_agent"):
            self.assertIn(feature, command)

    def test_insert_images_places_image_before_article_link_without_rewriting(self):
        report = """# ✨ 创新·AI娱乐

## 🎧 创新硬件（娱乐向）

### 1）产品发布

产品完成发布。（IT之家）

https://www.example.com/article
"""
        updated, inserted = report_images.insert_images(
            report,
            {"https://www.example.com/article": "https://img.example.com/device.png"},
        )
        self.assertEqual(inserted, 1)
        self.assertIn(
            "产品完成发布。（IT之家）\n\n![](https://img.example.com/device.png)\n\n"
            "https://www.example.com/article",
            updated,
        )
        self.assertEqual(updated.count("产品完成发布。（IT之家）"), 1)

    def test_apply_images_inserts_audited_selection_and_writes_plan(self):
        report = """# ✨ 创新·AI娱乐

## 🎧 创新硬件（娱乐向）

### 1）产品发布

产品完成发布。（IT之家）

https://www.example.com/article
"""
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            image = work / "device.png"
            image.write_bytes(b"synthetic-image")
            report_path = work / "report.md"
            report_path.write_text(report, encoding="utf-8")
            draft_path = work / "draft.md"
            draft_path.write_text("# draft\n", encoding="utf-8")
            (work / "image-cache.json").write_text(
                json.dumps(
                    {
                        "downloads": {
                            "https://www.example.com/article": [
                                {
                                    "url": "https://img.example.com/device.png",
                                    "file": str(image),
                                    "sha256": "unused",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(report_images, "run_visual_audit", return_value={1: 1}):
                plan_path = report_images.apply_images(
                    draft_path,
                    report_path,
                    work,
                    "codex",
                    work,
                    "model",
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["inserted"], 1)
            self.assertIn("![](https://img.example.com/device.png)", report_path.read_text(encoding="utf-8"))

    def test_existing_writer_image_is_removed_then_independently_audited(self):
        report = """# ✨ 创新·AI娱乐

## 🎧 创新硬件（娱乐向）

### 1）产品发布

产品完成发布。（IT之家）

![](https://img.example.com/writer-choice.png)

https://www.example.com/article
"""
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            image = work / "device.png"
            image.write_bytes(b"synthetic-image")
            report_path = work / "report.md"
            report_path.write_text(report, encoding="utf-8")
            draft_path = work / "draft.md"
            draft_path.write_text("# draft\n", encoding="utf-8")
            (work / "image-cache.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "downloads": {
                            "https://www.example.com/article": [
                                {
                                    "url": "https://img.example.com/audited-choice.png",
                                    "file": str(image),
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(report_images, "run_visual_audit", return_value={1: 1}):
                plan_path = report_images.apply_images(
                    draft_path,
                    report_path,
                    work,
                    "codex",
                    work,
                    "model",
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            final = report_path.read_text(encoding="utf-8")
            self.assertEqual(plan["removed_unverified"], 1)
            self.assertNotIn("writer-choice.png", final)
            self.assertEqual(final.count("![](https://img.example.com/audited-choice.png)"), 1)

    def test_visual_failure_is_media_warning_and_leaves_text_unchanged(self):
        report = """# ✨ 创新·AI娱乐

## 🎧 创新硬件（娱乐向）

### 1）产品发布

产品完成发布。（IT之家）

https://www.example.com/article
"""
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            image = work / "device.png"
            image.write_bytes(b"synthetic-image")
            report_path = work / "report.md"
            report_path.write_text(report, encoding="utf-8")
            draft_path = work / "draft.md"
            draft_path.write_text("# draft\n", encoding="utf-8")
            (work / "image-cache.json").write_text(
                json.dumps(
                    {
                        "downloads": {
                            "https://www.example.com/article": [
                                {
                                    "url": "https://img.example.com/device.png",
                                    "file": str(image),
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(report_images, "run_visual_audit", side_effect=RuntimeError("offline")):
                plan_path = report_images.apply_images(
                    draft_path,
                    report_path,
                    work,
                    "codex",
                    work,
                    "model",
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "warn")
            self.assertEqual(plan["inserted"], 0)
            self.assertEqual(report_path.read_text(encoding="utf-8"), report)

    def test_total_prefetch_failure_is_visible_but_does_not_change_report(self):
        report = """# ✨ 创新·AI娱乐

## 🎧 创新硬件（娱乐向）

### 1）产品发布

产品完成发布。（IT之家）

https://www.example.com/article
"""
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            report_path = work / "report.md"
            report_path.write_text(report, encoding="utf-8")
            draft_path = work / "draft.md"
            draft_path.write_text("# draft\n", encoding="utf-8")
            (work / "image-cache.json").write_text(
                json.dumps(
                    {
                        "status": "download_failed",
                        "candidate_groups": 1,
                        "downloaded": 0,
                        "downloads": {"https://www.example.com/article": []},
                    }
                ),
                encoding="utf-8",
            )
            plan_path = report_images.apply_images(
                draft_path,
                report_path,
                work,
                "codex",
                work,
                "model",
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "warn")
            self.assertEqual(plan["error_category"], "candidate_download_failed")
            self.assertEqual(report_path.read_text(encoding="utf-8"), report)

    def test_daily_runner_prefetches_in_parallel_and_keeps_media_nonblocking(self):
        runner = (Path(__file__).resolve().parents[1] / "run_daily.sh").read_text(encoding="utf-8")
        self.assertIn('prefetch \\\n    --draft "$DRAFT"', runner)
        self.assertIn('2>"$CODEX_WORK_DIR/image-prefetch.stderr.log" &', runner)
        self.assertIn("text_unchanged_on_failure=true", runner)
        self.assertLess(runner.index(" prefetch \\"), runner.index(" compose \\"))


if __name__ == "__main__":
    unittest.main()
