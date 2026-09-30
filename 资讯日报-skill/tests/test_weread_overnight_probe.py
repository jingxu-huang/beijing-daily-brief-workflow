import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "weread-omni" / "overnight_probe.mjs"
PROBE_PLIST = ROOT.parent / "automation" / "com.mico.ribao-weread-overnight-probe.plist"
AWAKE_PLIST = ROOT.parent / "automation" / "com.mico.ribao-weread-overnight-awake.plist"


class OvernightProbeTests(unittest.TestCase):
    def test_init_writes_private_bounded_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                ["node", str(SCRIPT), "init"],
                env={**os.environ, "RIBAO_WEREAD_OMNI_DATA_ROOT": temporary},
                text=True,
                capture_output=True,
                check=True,
            )
            result = json.loads(completed.stdout)
            pointer = json.loads((Path(temporary) / "overnight-probes" / "current.json").read_text())
            run_dir = Path(pointer["runDir"])
            plan = json.loads((run_dir / "plan.json").read_text())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(result["status"], "initialized")
            self.assertEqual(plan["intervalMinutes"], 15)
            self.assertEqual(plan["mode"], "article_list_only")
            self.assertEqual(plan["articleRequestCount"], 2)
            self.assertEqual(plan["requestTimeoutMs"], 15000)
            self.assertEqual(plan["initialCooldownMinutes"], 0)
            self.assertFalse(plan["baselineHumanVerification"])
            self.assertEqual(state["backoffIndex"], 0)
            self.assertFalse(state["loginRequired"])
            self.assertLessEqual(run_dir.stat().st_mode & 0o777, 0o700)

    def test_launchd_is_quarter_hour_and_does_not_run_codex_or_feishu(self):
        text = PROBE_PLIST.read_text()
        self.assertEqual(text.count("<key>Minute</key>"), 4)
        for minute in (0, 15, 30, 45):
            self.assertIn(f"<integer>{minute}</integer>", text)
        self.assertNotIn("codex", text.lower())
        self.assertNotIn("feishu", text.lower())
        self.assertIn("/usr/bin/caffeinate", AWAKE_PLIST.read_text())
        self.assertIn("<string>-s</string>", AWAKE_PLIST.read_text())

    def test_installer_replaces_whole_argument_arrays(self):
        installer = (ROOT.parent / "automation" / "install_weread_overnight_probe.command").read_text()
        self.assertIn("-replace ProgramArguments -json", installer)
        self.assertNotIn("-replace ProgramArguments.0", installer)
        self.assertNotIn("-replace ProgramArguments.3", installer)

    def test_probe_separates_list_only_from_full_text(self):
        text = SCRIPT.read_text()
        self.assertIn('kind: "articles_success"', text)
        self.assertIn('mode: "article_list_only"', text)
        self.assertNotIn("buildPublicAccountFeed", text)
        self.assertNotIn("ContentLibrary", text)
        self.assertIn("code === -2041", text)
        self.assertIn("code === -2012", text)
        self.assertIn("AbortSignal.timeout(plan.requestTimeoutMs)", text)
        self.assertIn("count: plan.articleRequestCount", text)
        self.assertNotIn("count: 50", text)

    def test_probe_bounds_request_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                ["node", str(SCRIPT), "init"],
                env={
                    **os.environ,
                    "RIBAO_WEREAD_OMNI_DATA_ROOT": temporary,
                    "RIBAO_OVERNIGHT_ARTICLE_COUNT": "999",
                    "RIBAO_OVERNIGHT_REQUEST_TIMEOUT_MS": "1",
                    "RIBAO_OVERNIGHT_INITIAL_COOLDOWN_MINUTES": "9999",
                },
                text=True,
                capture_output=True,
                check=True,
            )
            pointer = json.loads((Path(temporary) / "overnight-probes" / "current.json").read_text())
            plan = json.loads((Path(pointer["runDir"]) / "plan.json").read_text())
            self.assertEqual(plan["articleRequestCount"], 10)
            self.assertEqual(plan["requestTimeoutMs"], 5000)
            self.assertEqual(plan["initialCooldownMinutes"], 1440)


if __name__ == "__main__":
    unittest.main()
