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

    def test_fixed_reminder_mentions_15_cutoff_and_both_channels(self):
        with mock.patch("wewe_preflight.request_remote_scan_url", return_value="https://scan.example/x"), mock.patch(
            "wewe_preflight.feishu_push_verified", return_value={"verified": True, "message_id": "m1"}
        ) as feishu, mock.patch(
            "wewe_preflight.start_dialog_async", return_value={"started": True, "pid": 7}
        ) as dialog:
            result = wewe_preflight.send_fixed_guard_reminder(
                target_ymd="20260805", stage="1330", reason="账号失效"
            )
        self.assertTrue(result["ok"])
        self.assertIn("15:00", feishu.call_args.args[1])
        self.assertIn("13:30", feishu.call_args.args[0])
        self.assertIn("15:00", dialog.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
