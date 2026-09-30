import importlib.util
import json
from email.message import Message
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SENDER_PATH = REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py"


def load_sender():
    spec = importlib.util.spec_from_file_location("send_to_feishu_cli_images", SENDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeishuImageStagingTests(unittest.TestCase):
    def test_content_readback_requires_titles_and_exact_source_table(self):
        sender = load_sender()
        markdown = """### 1）第一条资讯

正文内容。（白鲸出海）

### 2）第二条资讯

正文内容。（36氪）

Source 来源：

|来源|本期采用条数|
|---|---:|
|白鲸出海|1|
|36氪|1|
"""
        xml = """<doc><h3>1）第一条资讯</h3><p>正文内容。（白鲸出海）</p>
<h3>2）第二条资讯</h3><p>正文内容。（36氪）</p><p>Source 来源：</p>
<table><tr><th>来源</th><th>本期采用条数</th></tr>
<tr><td>白鲸出海</td><td>1</td></tr><tr><td>36氪</td><td>1</td></tr></table></doc>"""
        response = {"ok": True, "data": {"document": {"content": xml}}}
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            report = Path(tmp) / "report.md"
            report.write_text(markdown, encoding="utf-8")
            with mock.patch.object(sender, "run_lark", return_value=response):
                self.assertTrue(
                    sender.verify_report_content_readback("doc-token", report)
                )

    def test_content_readback_rejects_missing_or_changed_source_rows(self):
        sender = load_sender()
        markdown = """### 1）第一条资讯

正文内容。（白鲸出海）

Source 来源：

|来源|本期采用条数|
|---|---:|
|白鲸出海|1|
"""
        xml = """<doc><h3>1）第一条资讯</h3><p>正文内容。（白鲸出海）</p>
<table><tr><th>来源</th><th>本期采用条数</th></tr>
<tr><td>微信公众号</td><td>1</td></tr></table></doc>"""
        response = {"ok": True, "data": {"document": {"content": xml}}}
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            report = Path(tmp) / "report.md"
            report.write_text(markdown, encoding="utf-8")
            with mock.patch.object(sender, "run_lark", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "Source 来源表"):
                    sender.verify_report_content_readback("doc-token", report)

    def test_content_readback_compares_visible_markdown_title_text(self):
        sender = load_sender()
        markdown = """### 1）Qwen\\-Image\\-2.1 上线

正文内容。（IT之家）

Source 来源：

|来源|本期采用条数|
|---|---:|
|IT之家|1|
"""
        xml = """<doc><h3>1）Qwen-Image-2.1 上线</h3><p>正文内容。（IT之家）</p>
<table><tr><th>来源</th><th>本期采用条数</th></tr>
<tr><td>IT之家</td><td>1</td></tr></table></doc>"""
        response = {"ok": True, "data": {"document": {"content": xml}}}
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            report = Path(tmp) / "report.md"
            report.write_text(markdown, encoding="utf-8")
            with mock.patch.object(sender, "run_lark", return_value=response):
                self.assertTrue(sender.verify_report_content_readback("doc-token", report))

    def test_staging_strips_internal_qa_skip_comments(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as staging_tmp:
            source = Path(source_tmp) / "report.md"
            source.write_text(
                "# report\n\n正文保留。\n\n"
                "<!-- QA_SKIP: https://example.com/a?x=1&y=2 | 主题准入剔除 -->\n"
                "<!--\nQA_SKIP: https://example.com/b\n| 编辑准入剔除\n-->\n",
                encoding="utf-8",
            )
            sender.STAGING_DIR = Path(staging_tmp)

            staged = Path(
                sender.stage_report_for_lark(source, "20260908", localize_images=False)
            )

            staged_text = staged.read_text(encoding="utf-8")
            self.assertIn("正文保留。", staged_text)
            self.assertNotIn("QA_SKIP", staged_text)
            self.assertNotIn("主题准入剔除", staged_text)
            self.assertNotIn("编辑准入剔除", staged_text)

    def test_first_publish_uses_docs_create_with_markdown_file(self):
        sender = load_sender()
        response = {
            "ok": True,
            "data": {
                "document": {
                    "document_id": "doc-token",
                    "url": "https://example.com/docx/doc-token",
                }
            },
        }
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp, mock.patch.object(
            sender, "run_lark", return_value=response
        ) as run_lark:
            report = Path(tmp) / "report.md"
            report.write_text("# report\n", encoding="utf-8")
            token, url = sender.import_report(report, "report", import_as="user")

        self.assertEqual(token, "doc-token")
        self.assertEqual(url, "https://example.com/docx/doc-token")
        args = run_lark.call_args.args[0]
        self.assertEqual(args[:2], ["docs", "+create"])
        self.assertIn("--content", args)
        self.assertTrue(args[args.index("--content") + 1].startswith("@./"))
        self.assertEqual(args[args.index("--doc-format") + 1], "markdown")
        self.assertNotIn("drive", args)

    def test_download_uses_real_jpeg_signature_when_server_declares_png(self):
        sender = load_sender()
        payload = b"\xff\xd8\xff\xe0JFIF" + b"image-bytes"

        class FakeResponse:
            def __init__(self):
                self.headers = Message()
                self.headers["Content-Type"] = "image/png"
                self.headers["Content-Length"] = str(len(payload))
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://cdn.example.com/image.png"

            def read(self, amount):
                chunk = payload[self.offset : self.offset + amount]
                self.offset += len(chunk)
                return chunk

        opener = mock.Mock()
        opener.open.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            sender, "_assert_public_image_url"
        ), mock.patch.object(sender.urllib.request, "build_opener", return_value=opener):
            target = sender._download_remote_image(
                "https://cdn.example.com/image.png", "", Path(tmp)
            )
            self.assertEqual(target.suffix, ".jpg")
            self.assertEqual(target.read_bytes(), payload)

    def test_remote_images_are_staged_as_local_uploads_with_article_referer(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as staging_tmp:
            source = Path(source_tmp) / "report.md"
            source.write_text(
                "### 1）产品\n\n![](https://cdn.example.com/product.webp)\n\n"
                "https://news.example.com/article\n",
                encoding="utf-8",
            )
            sender.STAGING_DIR = Path(staging_tmp)

            def fake_download(url, referer, assets_dir):
                self.assertEqual(url, "https://cdn.example.com/product.webp")
                self.assertEqual(referer, "https://news.example.com/article")
                target = assets_dir / "product.webp"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"webp")
                return target

            with mock.patch.object(sender, "_assert_public_image_url"), mock.patch.object(
                sender, "_download_remote_image", side_effect=fake_download
            ):
                staged = Path(sender.stage_report_for_lark(source, "20260902"))

            text = staged.read_text(encoding="utf-8")
            self.assertNotIn("![](https://cdn.example.com", text)
            self.assertIn("![](@./", text)
            self.assertIn("product.webp)", text)

    def test_download_failure_omits_image_but_preserves_article_text(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as staging_tmp:
            source = Path(source_tmp) / "report.md"
            source.write_text(
                "article facts\n\n![](https://cdn.example.com/missing.png)\n\n"
                "https://news.example.com/article\n",
                encoding="utf-8",
            )
            sender.STAGING_DIR = Path(staging_tmp)
            with mock.patch.object(sender, "_assert_public_image_url"), mock.patch.object(
                sender, "_download_remote_image", side_effect=RuntimeError("download failed")
            ):
                staged = Path(sender.stage_report_for_lark(source, "20260902"))
            staged_text = staged.read_text(encoding="utf-8")
            self.assertIn("article facts", staged_text)
            self.assertNotIn("missing.png", staged_text)
            warning = json.loads(
                (Path(staging_tmp) / "20260902" / "image-warnings.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(warning["warnings"][0]["action"], "image_omitted_text_preserved")
            self.assertNotIn("https://", json.dumps(warning))

    def test_baijing_legacy_host_retries_on_current_host(self):
        sender = load_sender()
        urls = []

        def fake_download(url, _referer, assets_dir):
            urls.append(url)
            if "baijingapp.com" in url:
                raise RuntimeError("legacy host failed")
            target = assets_dir / "image.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"image")
            return target

        markdown = (
            "![](https://www.baijingapp.com/ueditor/a.jpg)\n\n"
            "https://www.baijing.cn/a\n"
        )
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp, mock.patch.object(
            sender, "_download_remote_image", side_effect=fake_download
        ):
            localized, warnings = sender._localize_report_images(markdown, Path(tmp))
        self.assertEqual(warnings, [])
        self.assertIn("![](@./", localized)
        self.assertEqual(urls[1], "https://www.baijing.cn/ueditor/a.jpg")

    def test_private_image_url_is_rejected(self):
        sender = load_sender()
        for url in (
            "http://127.0.0.1/image.png",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/image.png",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(RuntimeError, "非公网地址"):
                    sender._assert_public_image_url(url)

    def test_supported_image_signatures_reject_html_payloads(self):
        sender = load_sender()
        samples = {
            "image/png": b"\x89PNG\r\n\x1a\nrest",
            "image/jpeg": b"\xff\xd8\xffrest",
            "image/gif": b"GIF89arest",
            "image/webp": b"RIFF\x04\x00\x00\x00WEBPrest",
            "image/bmp": b"BMrest",
        }
        for content_type, payload in samples.items():
            with self.subTest(content_type=content_type):
                self.assertTrue(sender.IMAGE_SIGNATURES[content_type](payload))
                self.assertFalse(sender.IMAGE_SIGNATURES[content_type](b"<html>blocked</html>"))

    def test_dry_run_keeps_remote_images_without_network(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory(
            dir=REPO_ROOT
        ) as staging_tmp:
            source = Path(source_tmp) / "report.md"
            remote = "https://cdn.example.com/product.png"
            source.write_text(f"![]({remote})\n", encoding="utf-8")
            sender.STAGING_DIR = Path(staging_tmp)
            with mock.patch.object(sender, "_download_remote_image") as download:
                staged = Path(
                    sender.stage_report_for_lark(
                        source,
                        "20260902",
                        localize_images=False,
                    )
                )
            download.assert_not_called()
            self.assertIn(remote, staged.read_text(encoding="utf-8"))

    def test_feishu_image_verification_previews_every_expected_block(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            report = Path(tmp) / "report.md"
            image_a = Path(tmp) / "image-a.png"
            image_b = Path(tmp) / "image-b.webp"
            image_a.write_bytes(b"image-a")
            image_b.write_bytes(b"image-b")
            ref_a = "@" + sender.project_relative(image_a)
            ref_b = "@" + sender.project_relative(image_b)
            report.write_text(
                f"![]({ref_a})\n![]({ref_b})\n",
                encoding="utf-8",
            )
            sender.STAGING_DIR = Path(tmp) / "staging"

            def preview_side_effect(token, output_stem, preview_as="user", dry_run=False):
                return image_a if token == "token-a" else image_b

            with mock.patch.object(
                sender,
                "_doc_image_tokens",
                return_value=["token-a", "token-b"],
            ), mock.patch.object(
                sender,
                "_preview_image_token",
                side_effect=preview_side_effect,
            ) as preview:
                self.assertTrue(
                    sender.verify_report_images(
                        "doc-token",
                        report,
                        "20260902",
                    )
                )
            self.assertEqual(preview.call_count, 2)

    def test_feishu_image_verification_rejects_missing_blocks(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            report = Path(tmp) / "report.md"
            report.write_text("![](@./image-a.png)\n", encoding="utf-8")
            with mock.patch.object(sender, "_doc_image_tokens", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "图片块数量不符"):
                    sender.verify_report_images("doc-token", report, "20260902")

    def test_feishu_image_verification_rejects_placeholder_content(self):
        sender = load_sender()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            source = Path(tmp) / "source.png"
            source.write_bytes(b"real-image")
            placeholder = Path(tmp) / "placeholder.png"
            placeholder.write_bytes(b"failed-import-placeholder")
            report = Path(tmp) / "report.md"
            report.write_text(
                f"![](@{sender.project_relative(source)})\n",
                encoding="utf-8",
            )
            with mock.patch.object(sender, "_doc_image_tokens", return_value=["token"]), mock.patch.object(
                sender,
                "_preview_image_token",
                return_value=placeholder,
            ):
                with self.assertRaisesRegex(RuntimeError, "内容与本地原图不一致"):
                    sender.verify_report_images("doc-token", report, "20260902")


if __name__ == "__main__":
    unittest.main()
