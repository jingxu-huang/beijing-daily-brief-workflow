import datetime as dt
import os
import unittest
from unittest import mock

import fetch_news


class DailyWindowTests(unittest.TestCase):
    def test_target_window_is_15_to_15(self):
        start_utc, end_utc, start_bj, end_bj = fetch_news.bj_window(target_ymd="20260805")
        self.assertEqual(start_bj, dt.datetime(2026, 8, 4, 15, 0))
        self.assertEqual(end_bj, dt.datetime(2026, 8, 5, 15, 0))
        self.assertEqual(start_utc, dt.datetime(2026, 8, 4, 7, 0))
        self.assertEqual(end_utc, dt.datetime(2026, 8, 5, 7, 0))

    def test_window_is_left_closed_right_open(self):
        start = dt.datetime(2026, 8, 4, 7, 0)
        end = dt.datetime(2026, 8, 5, 7, 0)
        at_start = {"published_parsed": start.timetuple()}
        before_end = {"published_parsed": (end - dt.timedelta(seconds=1)).timetuple()}
        at_end = {"published_parsed": end.timetuple()}
        self.assertTrue(fetch_news.in_window(at_start, start, end))
        self.assertTrue(fetch_news.in_window(before_end, start, end))
        self.assertFalse(fetch_news.in_window(at_end, start, end))

    def test_trusted_utc_ignores_host_wall_clock(self):
        with mock.patch.dict(os.environ, {"RIBAO_TRUSTED_UTC": "2026-08-05T07:00:00+00:00"}, clear=False):
            self.assertEqual(fetch_news.now_bj_real(), dt.datetime(2026, 8, 5, 15, 0))

    def test_wewe_source_detection(self):
        self.assertTrue(fetch_news.is_wewe_source({"type": "wechat", "name": "公众号"}))
        self.assertTrue(
            fetch_news.is_wewe_source(
                {"type": "rss", "name": "公众号(WeWe RSS)", "url": "http://localhost:4000/feeds/all.rss"}
            )
        )
        self.assertFalse(
            fetch_news.is_wewe_source({"type": "rss", "name": "IT之家", "url": "https://www.ithome.com/rss/"})
        )

    def test_werss_is_an_optional_enhancement_not_a_public_source(self):
        source = {"type": "rss", "name": "微信公众号(WeRSS)", "url": "http://127.0.0.1:8001/feed/all.xml"}
        self.assertTrue(fetch_news.is_werss_source(source))
        self.assertTrue(fetch_news.is_optional_enhancement_source(source))
        self.assertTrue(fetch_news.requires_verifiable_publish_time(source))

    def test_undated_werss_is_rejected_even_for_ad_hoc_hours_mode(self):
        werss = {"type": "rss", "name": "微信公众号(WeRSS)", "url": "http://127.0.0.1:8001/feed/all.xml"}
        public = {"type": "rss", "name": "IT之家", "url": "https://www.ithome.com/rss/"}
        self.assertTrue(fetch_news.should_skip_undated_rss_entry(werss, strict_window=False))
        self.assertFalse(fetch_news.should_skip_undated_rss_entry(public, strict_window=False))

    def test_weread_omni_is_test_only_enhancement_with_required_timestamp(self):
        source = {
            "type": "rss",
            "name": "微信公众号(weread-omni测试)",
            "enhancement_source": "weread_omni",
            "enabled": False,
            "enabled_env": "RIBAO_ENABLE_WEREAD_OMNI_TEST",
            "url_env": "RIBAO_WEREAD_OMNI_FEED_URL",
        }
        self.assertTrue(fetch_news.is_weread_omni_source(source))
        self.assertTrue(fetch_news.is_optional_enhancement_source(source))
        self.assertTrue(fetch_news.requires_verifiable_publish_time(source))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(fetch_news.source_enabled(source))
            self.assertIsNone(fetch_news.feed_url(source, ""))
        with mock.patch.dict(
            os.environ,
            {
                "RIBAO_ENABLE_WEREAD_OMNI_TEST": "1",
                "RIBAO_WEREAD_OMNI_FEED_URL": "file:///tmp/weread-omni-test.rss",
            },
            clear=True,
        ):
            self.assertTrue(fetch_news.source_enabled(source))
            self.assertEqual(fetch_news.feed_url(source, ""), "file:///tmp/weread-omni-test.rss")

    def test_weread_omni_uses_entry_author_as_reader_facing_publisher(self):
        self.assertEqual(
            fetch_news.feed_entry_publisher({"author": "DataEye短剧出海"}),
            "DataEye短剧出海",
        )
        self.assertEqual(fetch_news.feed_entry_publisher({}), "")

    def test_private_source_registry_keeps_old_and_new_paths_distinct(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            sources = fetch_news.yaml.safe_load(handle)["sources"]
        by_kind = {
            source.get("enhancement_source"): source
            for source in sources
            if source.get("enhancement_source") in {"werss", "weread_omni"}
        }
        self.assertEqual(set(by_kind), {"werss", "weread_omni"})
        self.assertFalse(by_kind["werss"]["enabled"])
        self.assertFalse(by_kind["weread_omni"]["enabled"])
        self.assertEqual(by_kind["weread_omni"]["enabled_env"], "RIBAO_ENABLE_WEREAD_OMNI_TEST")
        self.assertEqual(by_kind["weread_omni"]["url_env"], "RIBAO_WEREAD_OMNI_FEED_URL")

    def test_tnglobal_is_disabled_by_default(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            sources = fetch_news.yaml.safe_load(handle)["sources"]
        tnglobal = next(source for source in sources if source.get("name") == "TNGlobal")
        self.assertFalse(fetch_news.source_enabled(tnglobal))


if __name__ == "__main__":
    unittest.main()
