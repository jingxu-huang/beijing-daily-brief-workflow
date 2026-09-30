import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ribao_guard


class GuardContractTests(unittest.TestCase):
    def setUp(self):
        # Historical WeWe behavior is retained for isolated audit tests only;
        # production defaults to retired and never runs these probes.
        patcher = mock.patch.dict(
            os.environ,
            {"RIBAO_ENABLE_WEWE": "1", "RIBAO_WEWE_RETIRED": "0"},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fixed_business_times(self):
        self.assertEqual(ribao_guard.CHECK_1, dt.time(11, 0))
        self.assertEqual(ribao_guard.REFRESH_1, dt.time(12, 0))
        self.assertEqual(ribao_guard.CHECK_2, dt.time(14, 30))
        self.assertEqual(ribao_guard.WINDOW_END, dt.time(15, 0))
        self.assertEqual(ribao_guard.DELIVERY_DEADLINE, dt.time(15, 30))
        self.assertEqual(ribao_guard.DEADLINE_ALERT, dt.time(15, 30))

    def test_modified_reminder_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            with mock.patch.dict(os.environ, {"RIBAO_WEWE_CHECK_1": "11:01"}, clear=False):
                self.assertFalse(guard.fixed_config_ok())

    def test_default_fixed_reminder_times_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            with mock.patch.dict(
                os.environ,
                {
                    "RIBAO_WEWE_CHECK_1": "11:00",
                    "RIBAO_WEWE_REFRESH_1": "12:00",
                    "RIBAO_WEWE_CHECK_2": "14:30",
                },
                clear=False,
            ):
                self.assertTrue(guard.fixed_config_ok())

    def test_guard_probe_sets_target_window_and_forces_refresh_temporarily(self):
        fake_module = mock.Mock()

        def probe_health():
            self.assertEqual(os.environ.get("RIBAO_TARGET_YMD"), "20260820")
            self.assertEqual(os.environ.get("WEWE_REFRESH_FEEDS"), "1")
            self.assertEqual(os.environ.get("WEWE_REFRESH_MODE"), "all")
            return {"status": "healthy", "reason": "ok"}

        fake_module.probe_health.side_effect = probe_health
        with mock.patch.object(
            ribao_guard, "load_wewe_module", return_value=fake_module
        ), mock.patch.dict(
            os.environ,
            {"WEWE_REFRESH_FEEDS": "0", "RIBAO_TARGET_YMD": "legacy"},
            clear=False,
        ):
            result = ribao_guard.probe_wewe(Path("/tmp/skill"), target_ymd="20260820")
            self.assertEqual(os.environ.get("WEWE_REFRESH_FEEDS"), "0")
            self.assertEqual(os.environ.get("RIBAO_TARGET_YMD"), "legacy")
        self.assertEqual(result["status"], "healthy")

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

    def test_login_check_uses_a_single_live_source_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {"target_ymd": "20260821", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 21, 11, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(
                guard, "probe_wewe", return_value={"status": "healthy", "reason": "ok"}
            ) as probe:
                guard.process_wewe_checks(state, now_bj)
            probe.assert_called_once_with(state, refresh_mode="single")

    def test_unavailable_refresh_is_retried_instead_of_marked_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {"target_ymd": "20260821", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 21, 12, 0, tzinfo=ribao_guard.BEIJING)
            fake_module = mock.Mock()
            fake_module.send_fixed_guard_reminder.return_value = {
                "ok": True,
                "feishu_verified": True,
                "dialog_started": True,
            }
            with mock.patch.object(
                guard,
                "probe_wewe",
                side_effect=[
                    {"status": "service_down", "reason": "starting"},
                    {"status": "healthy", "reason": "ok"},
                ],
            ) as probe, mock.patch.object(
                ribao_guard, "load_wewe_module", return_value=fake_module
            ):
                guard.process_wewe_checks(state, now_bj)
                self.assertEqual(state["stages"]["wewe_refresh_1200"]["status"], "unavailable")
                guard.process_wewe_checks(state, now_bj + dt.timedelta(seconds=30))
            self.assertEqual(state["stages"]["wewe_refresh_1200"]["status"], "ok")
            self.assertEqual(probe.call_count, 2)

    def test_legacy_ok_with_an_unhealthy_probe_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {
                "target_ymd": "20260821",
                "last_now_bj": "",
                "stages": {
                    "wewe_refresh_1200": {
                        "status": "ok",
                        "probe": {"status": "service_down", "reason": "old startup error"},
                    }
                },
            }
            now_bj = dt.datetime(2026, 8, 21, 12, 1, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(
                guard, "probe_wewe", return_value={"status": "healthy", "reason": "ok"}
            ) as probe:
                guard.process_wewe_checks(state, now_bj)
            probe.assert_called_once_with(state, refresh_mode="all")
            self.assertEqual(state["stages"]["wewe_refresh_1200"]["status"], "ok")

    def test_final_refresh_is_reused_at_15_without_another_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {
                "target_ymd": "20260821",
                "last_now_bj": "",
                "stages": {"wewe_check_1430": {"probe": {"status": "healthy", "reason": "ok"}}},
            }
            now_bj = dt.datetime(2026, 8, 21, 15, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(guard, "probe_wewe") as probe:
                frozen = guard.freeze_wewe_at_window_close(state, now_bj)
            self.assertTrue(frozen["wewe_included"])
            probe.assert_not_called()

    def test_stale_wewe_still_requires_fixed_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            state = {"target_ymd": "20260820", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 20, 14, 30, tzinfo=ribao_guard.BEIJING)
            guard.reminder(
                state,
                "wewe_check_1430",
                {"status": "stale", "reason": "all.rss latest before target window"},
                now_bj,
            )
            entry = state["stages"]["wewe_check_1430"]
            self.assertTrue(entry["reminder_required"])
            self.assertEqual(entry["status"], "shadow")

    def test_stale_wewe_is_excluded_at_window_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=True)
            state = {"target_ymd": "20260820", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 20, 15, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(
                guard,
                "probe_wewe",
                return_value={"status": "stale", "reason": "old all.rss"},
            ):
                frozen = guard.freeze_wewe_at_window_close(state, now_bj)
            self.assertFalse(frozen["wewe_included"])

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

    def test_retry_reuses_only_coverage_verified_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            state_dir = data_root / "日报状态"
            draft_dir = data_root / "日报草稿"
            state_dir.mkdir()
            draft_dir.mkdir()
            ymd = "20260912"
            draft = draft_dir / f"资讯日报-草稿-{ymd}.md"
            draft.write_text("failed coverage draft\n", encoding="utf-8")
            manifest = state_dir / f"run-manifest-{ymd}.json"

            manifest.write_text(
                '{"steps":{"fetch":{"status":"fail"}}}\n', encoding="utf-8"
            )
            self.assertFalse(ribao_guard.coverage_verified_draft(data_root, ymd, draft))

            manifest.write_text(
                '{"steps":{"fetch":{"status":"ok"}}}\n', encoding="utf-8"
            )
            self.assertTrue(ribao_guard.coverage_verified_draft(data_root, ymd, draft))

            draft.write_text("", encoding="utf-8")
            self.assertFalse(ribao_guard.coverage_verified_draft(data_root, ymd, draft))

    def test_generated_manifest_is_adopted_only_with_nonblocking_qa_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workflow"
            data_root = Path(tmp) / "data"
            (root / "资讯日报-skill").mkdir(parents=True)
            (data_root / "日报状态").mkdir(parents=True)
            (data_root / "日报文档").mkdir()
            (data_root / "日报质检").mkdir()
            (data_root / "日报状态" / "run-manifest-20260901.json").write_text(
                '{"final":"generated"}\n', encoding="utf-8"
            )
            (data_root / "日报文档" / "资讯类简报-9.1日.md").write_text(
                "report\n", encoding="utf-8"
            )
            check = data_root / "日报质检" / "资讯日报-质检-20260901.md"
            check.write_text("- 结论：WARN\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"RIBAO_DATA_ROOT": str(data_root)}, clear=False):
                guard = ribao_guard.Guard(root, Path(tmp) / "runtime")
                self.assertEqual(guard.existing_sent("20260901").get("final"), "generated")
                check.write_text("- 结论：FAIL\n", encoding="utf-8")
                self.assertEqual(guard.existing_sent("20260901"), {})

    def test_generated_manifest_without_artifacts_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workflow"
            data_root = Path(tmp) / "data"
            (root / "资讯日报-skill").mkdir(parents=True)
            (data_root / "日报状态").mkdir(parents=True)
            (data_root / "日报状态" / "run-manifest-20260901.json").write_text(
                '{"final":"generated"}\n', encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"RIBAO_DATA_ROOT": str(data_root)}, clear=False):
                guard = ribao_guard.Guard(root, Path(tmp) / "runtime")
                self.assertEqual(guard.existing_sent("20260901"), {})

    def test_adoption_preserves_prior_failure_audit_and_sets_current_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workflow"
            data_root = Path(tmp) / "data"
            (root / "资讯日报-skill").mkdir(parents=True)
            (data_root / "日报状态").mkdir(parents=True)
            (data_root / "日报文档").mkdir()
            (data_root / "日报质检").mkdir()
            (data_root / "日报状态" / "run-manifest-20260901.json").write_text(
                '{"final":"generated"}\n', encoding="utf-8"
            )
            (data_root / "日报文档" / "资讯类简报-9.1日.md").write_text(
                "report\n", encoding="utf-8"
            )
            (data_root / "日报质检" / "资讯日报-质检-20260901.md").write_text(
                "- 结论：WARN\n", encoding="utf-8"
            )
            state = {
                "target_ymd": "20260901",
                "last_now_bj": "2026-09-01T15:18:36+08:00",
                "stages": {
                    "daily": {
                        "status": "fail",
                        "at_bj": "2026-09-01T15:08:20+08:00",
                        "exit_code": 1,
                        "final": "failed",
                        "next_retry_at_bj": "2026-09-01T15:18:20+08:00",
                    }
                },
            }
            now_bj = dt.datetime(2026, 9, 1, 15, 18, 36, tzinfo=ribao_guard.BEIJING)
            with mock.patch.dict(os.environ, {"RIBAO_DATA_ROOT": str(data_root)}, clear=False):
                guard = ribao_guard.Guard(root, Path(tmp) / "runtime")
                guard.run_daily(state, now_bj.astimezone(dt.timezone.utc), now_bj)
            daily = state["stages"]["daily"]
            self.assertEqual(daily["status"], "sent")
            self.assertEqual(daily["final"], "generated")
            self.assertEqual(daily["exit_code"], 0)
            self.assertEqual(daily["next_retry_at_bj"], "")
            self.assertEqual(daily["prior_failure"]["final"], "failed")
            self.assertEqual(daily["prior_failure"]["exit_code"], 1)

    def test_guard_uses_external_codex_runtime_without_local_staging(self):
        source = Path(ribao_guard.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"RIBAO_CODEX_STAGING_BASE":', source)
        self.assertIn('"RIBAO_CODEX_WORK_BASE":', source)
        self.assertIn('"RIBAO_CODEX_CONTROL_HOME":', source)

    def test_disabled_wewe_never_probes_or_reminds_and_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"RIBAO_ENABLE_WEWE": "0"}, clear=False
        ):
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {"target_ymd": "20260821", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 8, 21, 15, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(guard, "probe_wewe") as probe:
                guard.process_wewe_checks(state, now_bj)
                frozen = guard.freeze_wewe_at_window_close(state, now_bj)
            probe.assert_not_called()
            self.assertEqual(state["stages"]["wewe_disabled"]["status"], "skipped")
            self.assertEqual(frozen["status"], "disabled")
            self.assertFalse(frozen["wewe_included"])

    def test_retired_wewe_never_probes_or_reminds_and_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"RIBAO_ENABLE_WEWE": "1", "RIBAO_WEWE_RETIRED": "1"},
            clear=False,
        ):
            guard = ribao_guard.Guard(Path(tmp), Path(tmp) / "runtime", shadow=False)
            state = {"target_ymd": "20260917", "last_now_bj": ""}
            now_bj = dt.datetime(2026, 9, 17, 15, 0, tzinfo=ribao_guard.BEIJING)
            with mock.patch.object(guard, "probe_wewe") as probe:
                guard.process_wewe_checks(state, now_bj)
                frozen = guard.freeze_wewe_at_window_close(state, now_bj)
            probe.assert_not_called()
            self.assertEqual(state["stages"]["wewe_retired"]["status"], "skipped")
            self.assertEqual(frozen["status"], "retired")
            self.assertFalse(frozen["wewe_included"])


if __name__ == "__main__":
    unittest.main()
