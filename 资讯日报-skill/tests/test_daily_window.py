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


if __name__ == "__main__":
    unittest.main()
