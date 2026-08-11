import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import beijing_time


class NetworkQuorumTests(unittest.TestCase):
    def test_two_agreeing_samples_ignore_outlier(self):
        values = {
            "a": dt.datetime(2026, 8, 5, 7, 0, 0, tzinfo=dt.timezone.utc),
            "b": dt.datetime(2026, 8, 5, 7, 0, 2, tzinfo=dt.timezone.utc),
            "bad": dt.datetime(2026, 8, 5, 22, 0, 0, tzinfo=dt.timezone.utc),
        }

        chosen, observations = beijing_time.network_utc(
            endpoints=("a", "b", "bad"), fetcher=lambda url, _timeout: values[url]
        )

        self.assertEqual(chosen, dt.datetime(2026, 8, 5, 7, 0, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(sum(item["ok"] for item in observations), 3)

    def test_quorum_requires_two_agreeing_sources(self):
        values = {
            "a": dt.datetime(2026, 8, 5, 7, 0, tzinfo=dt.timezone.utc),
            "b": dt.datetime(2026, 8, 5, 8, 0, tzinfo=dt.timezone.utc),
        }
        with self.assertRaises(beijing_time.TimeAuthorityError):
            beijing_time.network_utc(
                endpoints=("a", "b"), fetcher=lambda url, _timeout: values[url]
            )


class TrustedClockTests(unittest.TestCase):
    def test_same_boot_monotonic_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / "anchor.json"
            anchor.write_text(
                json.dumps({"utc_epoch": 1_786_000_000, "monotonic": 100.0}),
                encoding="utf-8",
            )
            clock = beijing_time.TrustedClock(anchor, max_fallback_seconds=60, network_refresh_seconds=0)
            with mock.patch("beijing_time.network_utc", side_effect=RuntimeError("offline")), mock.patch(
                "beijing_time.monotonic_seconds", return_value=112.5
            ):
                value, meta = clock.now()
            self.assertEqual(value.timestamp(), 1_786_000_012.5)
            self.assertEqual(meta["mode"], "monotonic_fallback")

    def test_recent_anchor_avoids_repeated_network_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / "anchor.json"
            anchor.write_text(
                json.dumps({"utc_epoch": 1_786_000_000, "monotonic": 100.0}),
                encoding="utf-8",
            )
            clock = beijing_time.TrustedClock(anchor, network_refresh_seconds=300)
            with mock.patch("beijing_time.network_utc") as network, mock.patch(
                "beijing_time.monotonic_seconds", return_value=112.5
            ):
                value, meta = clock.now()
            network.assert_not_called()
            self.assertEqual(value.timestamp(), 1_786_000_012.5)
            self.assertEqual(meta["mode"], "monotonic_cached")

    def test_anchor_from_earlier_boot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / "anchor.json"
            anchor.write_text(
                json.dumps({"utc_epoch": 1_786_000_000, "monotonic": 100.0, "boot_session": "old"}),
                encoding="utf-8",
            )
            clock = beijing_time.TrustedClock(anchor)
            with mock.patch("beijing_time.boot_session_id", return_value="new"):
                with self.assertRaises(beijing_time.TimeAuthorityError):
                    clock._fallback()


if __name__ == "__main__":
    unittest.main()
