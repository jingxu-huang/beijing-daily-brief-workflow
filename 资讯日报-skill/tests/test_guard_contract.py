import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ribao_guard


class GuardContractTests(unittest.TestCase):
    def test_fixed_business_times(self):
        self.assertEqual(ribao_guard.CHECK_1, dt.time(13, 30))
        self.assertEqual(ribao_guard.CHECK_2, dt.time(14, 30))
        self.assertEqual(ribao_guard.WINDOW_END, dt.time(15, 0))
        self.assertEqual(ribao_guard.DELIVERY_DEADLINE, dt.time(15, 30))
        self.assertEqual(ribao_guard.DEADLINE_ALERT, dt.time(15, 30))

    def test_modified_reminder_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            with mock.patch.dict(os.environ, {"RIBAO_WEWE_CHECK_1": "13:31"}, clear=False):
                self.assertFalse(guard.fixed_config_ok())

    def test_default_fixed_reminder_times_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            with mock.patch.dict(
                os.environ,
                {"RIBAO_WEWE_CHECK_1": "13:30", "RIBAO_WEWE_CHECK_2": "14:30"},
                clear=False,
            ):
                self.assertTrue(guard.fixed_config_ok())

    def test_wewe_cutoff_decision_is_immutable_for_late_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            state = {"target_ymd": "20260805", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 5, 15, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(
                guard,
                "probe_wewe",
                side_effect=[
                    {"status": "auth_required", "reason": "logged out"},
                    {"status": "healthy", "reason": "late recovery"},
                ],
            ) as probe:
                first = guard.freeze_wewe_at_window_close(state, now_bj)
                second = guard.freeze_wewe_at_window_close(
                    state, now_bj + dt.timedelta(hours=1)
                )
            self.assertFalse(first["wewe_included"])
            self.assertEqual(second, first)
            self.assertEqual(probe.call_count, 1)

    def test_service_down_still_requires_fixed_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            state = {"target_ymd": "20260806", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 6, 14, 30, tzinfo=ribao_guard.BEIJING)
            guard.reminder(
                state,
                "wewe_check_1430",
                {"status": "service_down", "reason": "connection closed"},
                now_bj,
            )
            entry = state["stages"]["wewe_check_1430"]
            self.assertTrue(entry["reminder_required"])
            self.assertEqual(entry["status"], "shadow")

    def test_daily_failure_waits_for_retry_backoff(self):
        now_bj = dt.datetime(2026, 8, 6, 15, 40, tzinfo=ribao_guard.BEIJING)
        daily = {
            "status": "fail",
            "next_retry_at_bj": (now_bj + dt.timedelta(minutes=10)).isoformat(),
        }
        self.assertFalse(ribao_guard.Guard.daily_retry_due(daily, now_bj))
        self.assertTrue(
            ribao_guard.Guard.daily_retry_due(daily, now_bj + dt.timedelta(minutes=10))
        )

    def test_guard_uses_external_codex_runtime_without_local_staging(self):
        source = Path(ribao_guard.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"RIBAO_CODEX_STAGING_BASE":', source)
        self.assertIn('"RIBAO_CODEX_WORK_BASE":', source)
        self.assertIn('"RIBAO_CODEX_CONTROL_HOME":', source)


if __name__ == "__main__":
    unittest.main()
