import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
ASSESSOR_PATH = SKILL_DIR / "weread-omni" / "assess_sync.py"
SYNC_PATH = SKILL_DIR / "weread-omni" / "sync_window.mjs"
RUN_SYNC_PATH = SKILL_DIR / "weread-omni" / "run_sync_test.sh"
TEST_WRAPPER_PATH = SKILL_DIR.parent / "run_daily_test.sh"

spec = importlib.util.spec_from_file_location("assess_sync", ASSESSOR_PATH)
assess_sync = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(assess_sync)


def rss_for(target, item_count):
    items = "".join(
        f"<item><title>item-{index}</title></item>" for index in range(item_count)
    )
    return (
        "<rss><channel>"
        f"<description>weread-omni isolated test feed for {target}</description>"
        f"{items}</channel></rss>"
    )


class WereadOmniBoundaryTests(unittest.TestCase):
    def test_healthy_observation_can_be_complete_without_selected_items(self):
        result = assess_sync.assess(
            {
                "status": "healthy",
                "targetDate": "20260914",
                "windowItemCount": 0,
                "subscriptionCount": 27,
                "freshSources": 27,
                "staleSources": 0,
            },
            rss_for("20260914", 0),
            "20260914",
        )
        self.assertTrue(result["coverage_complete"])
        self.assertFalse(result["usable"])
        self.assertEqual(result["unassessed_source_count"], 0)

    def test_human_verification_cache_is_not_coverage_complete(self):
        result = assess_sync.assess(
            {
                "status": "human_verification_required",
                "targetDate": "20260914",
                "windowItemCount": 0,
                "subscriptionCount": 27,
                "freshSources": 0,
                "staleSources": 27,
                "failureClasses": {"human_verification": 27},
            },
            rss_for("20260914", 0),
            "20260914",
        )
        self.assertFalse(result["coverage_complete"])
        self.assertFalse(result["usable"])
        self.assertEqual(result["failure_class"], "human_verification")
        self.assertEqual(result["unassessed_source_count"], 0)

    def test_feed_and_observation_item_counts_must_match(self):
        result = assess_sync.assess(
            {
                "status": "healthy",
                "targetDate": "20260914",
                "windowItemCount": 2,
                "freshSources": 27,
                "staleSources": 0,
            },
            rss_for("20260914", 1),
            "20260914",
        )
        self.assertFalse(result["observation_valid"])
        self.assertFalse(result["coverage_complete"])

    def test_adapter_preserves_human_verification_and_target_scoped_health(self):
        sync = SYNC_PATH.read_text(encoding="utf-8")
        self.assertIn('code === -2041', sync)
        self.assertIn('kind: "human_verification"', sync)
        self.assertIn('targetDate: ymd', sync)
        self.assertIn('unknownExpectedSources', sync)
        self.assertIn('requestDelayMs', sync)
        self.assertIn('else if (synckey !== undefined', sync)
        self.assertNotIn('sameTargetDate &&\n\t\t\t\tsynckey !== undefined', sync)
        self.assertNotIn('const expected = health?.expectedWindowItems ?? 0', sync)

    def test_human_verification_stops_after_first_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            dist = package / "dist"
            state_dir = root / "sync-state"
            snapshots = state_dir / "accounts"
            dist.mkdir(parents=True)
            snapshots.mkdir(parents=True)
            (package / "package.json").write_text('{"type":"module"}', encoding="utf-8")
            trace = root / "trace.txt"
            accounts = {f"account-{index}": {"synckey": 1} for index in range(27)}
            for account_id in accounts:
                snapshot = snapshots / f"{account_id}.json"
                snapshot.write_text("[]", encoding="utf-8")
                accounts[account_id]["snapshot"] = str(snapshot)
            (state_dir / "state.json").write_text(
                json.dumps({"targetDate": "20260913", "accounts": accounts}),
                encoding="utf-8",
            )
            (dist / "accounts.js").write_text(
                textwrap.dedent(
                    """
                    import { appendFileSync } from "node:fs";
                    export class AccountManager {
                      async open() {
                        const accounts = Array.from({length: 27}, (_, i) => ({accountId: `account-${i}`}));
                        return {identity: {vid: "fixture"}, canonical: {publicAccounts: {
                          subscriptions: async () => ({accounts}),
                          articles: async () => {
                            appendFileSync(process.env.TEST_TRACE, "request\\n");
                            const error = new Error("official client verification required");
                            error.status = 499;
                            error.errCode = -2041;
                            throw error;
                          }
                        }}};
                      }
                    }
                    """
                ),
                encoding="utf-8",
            )
            (dist / "index.js").write_text(
                textwrap.dedent(
                    """
                    export const buildPublicAccountFeed = async () => { throw new Error("must not run"); };
                    export const ContentLibrary = {open: async () => ({
                      verify: async () => ({ok: true}), close: () => undefined
                    })};
                    """
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "RIBAO_WEREAD_OMNI_DATA_ROOT": str(root),
                    "WEREAD_OMNI_PACKAGE_ROOT": str(package),
                    "RIBAO_WEREAD_OMNI_REQUEST_DELAY_MS": "0",
                    "TEST_TRACE": str(trace),
                }
            )
            result = subprocess.run(
                ["node", str(SYNC_PATH), "20260914"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "human_verification_required")
            self.assertEqual(payload["failureClass"], "human_verification")
            self.assertEqual(payload["subscriptionCount"], 27)
            self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), ["request"])

    def test_test_wrapper_records_channel_without_blocking_public_sources(self):
        run_sync = RUN_SYNC_PATH.read_text(encoding="utf-8")
        wrapper = TEST_WRAPPER_PATH.read_text(encoding="utf-8")
        checker = (SKILL_DIR / "check_report.py").read_text(encoding="utf-8")
        self.assertIn('latest-${YMD}.json', run_sync)
        self.assertIn('assess_sync.py', wrapper)
        self.assertIn('RIBAO_WEREAD_OMNI_CHANNEL_STATUS', wrapper)
        self.assertIn('RIBAO_ENABLE_WEREAD_OMNI_TEST=0', wrapper)
        self.assertIn("不能宣称公众号全链路通过", checker)


if __name__ == "__main__":
    unittest.main()
