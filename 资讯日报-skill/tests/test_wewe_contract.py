import unittest
from unittest import mock

import wewe_preflight


class WeweContractTests(unittest.TestCase):
    def test_account_failure_requires_login(self):
        with mock.patch(
            "wewe_preflight.check_once",
            side_effect=wewe_preflight.WeweError("暂无可用微信读书账号：账号失效"),
        ):
            self.assertEqual(wewe_preflight.probe_health()["status"], "auth_required")

    def test_service_failure_is_non_blocking_source_outage(self):
        with mock.patch(
            "wewe_preflight.check_once",
            side_effect=wewe_preflight.WeweError("无法连接 WeWe RSS：connection refused"),
        ):
            self.assertEqual(wewe_preflight.probe_health()["status"], "service_down")

    def test_missing_dedicated_relogin_page_falls_back_to_add_account_dashboard(self):
        with mock.patch.object(wewe_preflight, "RELOGIN_TOKEN", ""), mock.patch.object(
            wewe_preflight, "DASH_URL", "http://127.0.0.1:4000/dash/accounts"
        ):
            self.assertEqual(
                wewe_preflight.relogin_local_url(),
                "http://127.0.0.1:4000/dash/accounts",
            )

    def test_transient_source_error_does_not_open_relogin_page(self):
        with mock.patch(
            "wewe_preflight.check_once",
            side_effect=wewe_preflight.WeweError("HTTP 500: upstream 502"),
        ), mock.patch("wewe_preflight.alert_source_unavailable") as unavailable, mock.patch(
            "wewe_preflight.open_relogin_page"
        ) as relogin:
            self.assertEqual(wewe_preflight.precheck_once(), 4)
        unavailable.assert_called_once()
        relogin.assert_not_called()

    def test_fixed_reminder_redacts_local_qr_token_and_verbose_502_details(self):
        token = "private-local-token"
        reason = "源甲: HTTP 500: Request failed with status code 502; 源乙: HTTP 500: Request failed with status code 502"
        with mock.patch.object(wewe_preflight, "RELOGIN_TOKEN", token), mock.patch(
            "wewe_preflight.start_dialog_async", return_value={"started": True, "pid": 7}
        ) as dialog:
            result = wewe_preflight.send_fixed_guard_reminder(
                target_ymd="20260805", stage="1200", reason=reason, show_dialog=True
            )
        self.assertTrue(result["ok"])
        message = dialog.call_args.args[1]
        self.assertNotIn(token, message)
        self.assertNotIn("源甲", message)
        self.assertIn("上游请求返回 502", message)

    def test_target_ymd_window_does_not_roll_back_before_15(self):
        with mock.patch.dict(
            "os.environ",
            {
                "RIBAO_TARGET_YMD": "20260820",
                "RIBAO_TRUSTED_UTC": "2026-08-20T06:30:00+00:00",
            },
            clear=False,
        ):
            start_bj, end_bj = wewe_preflight.current_bj_window()
        self.assertEqual(start_bj.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-19 15:00:00")
        self.assertEqual(end_bj.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-20 15:00:00")

    def test_fixed_reminder_mentions_15_cutoff_and_both_channels(self):
        # Local production can explicitly disable group messages.  This test
        # verifies the enabled-channel contract, so isolate it from that
        # machine-level switch.
        with mock.patch.object(wewe_preflight, "GROUP_MESSAGES_ENABLED", True), mock.patch.object(
            wewe_preflight, "FEISHU_CHAT_ID", "oc_mock"
        ), mock.patch(
            "wewe_preflight.request_remote_scan_url", return_value="https://scan.example/x"
        ), mock.patch(
            "wewe_preflight.open_relogin_page"
        ) as relogin, mock.patch(
            "wewe_preflight.open_dashboard"
        ) as dashboard, mock.patch(
            "wewe_preflight.feishu_push_verified", return_value={"verified": True, "message_id": "m1"}
        ) as feishu, mock.patch(
            "wewe_preflight.start_dialog_async", return_value={"started": True, "pid": 7}
        ) as dialog:
            result = wewe_preflight.send_fixed_guard_reminder(
                target_ymd="20260805", stage="1100", reason="账号失效"
            )
        self.assertTrue(result["ok"])
        relogin.assert_called_once()
        dashboard.assert_not_called()
        self.assertIn("新增账号二维码", feishu.call_args.args[1])
        self.assertIn("15:00", feishu.call_args.args[1])
        self.assertIn("11:00", feishu.call_args.args[0])
        self.assertIn("新增账号二维码", dialog.call_args.args[1])
        self.assertIn("15:00", dialog.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
