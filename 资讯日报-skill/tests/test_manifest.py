import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manifest


class ManifestTests(unittest.TestCase):
    def test_read_step_status_is_read_only_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-manifest-20260908.json"
            path.write_text(
                json.dumps({"steps": {"fetch": {"status": "fail"}}}),
                encoding="utf-8",
            )
            before = path.read_bytes()
            self.assertEqual(manifest.read_step_status(path, "fetch"), "fail")
            self.assertEqual(manifest.read_step_status(path, "missing"), "")
            self.assertEqual(path.read_bytes(), before)

            path.write_text("{not-json", encoding="utf-8")
            self.assertEqual(manifest.read_step_status(path, "fetch"), "")

    def test_read_step_status_cli_does_not_create_or_update_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "manifest.py",
                "--date",
                "20260908",
                "--state-dir",
                tmp,
                "--read-step-status",
                "fetch",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print") as printer:
                manifest.main()
            printer.assert_called_once_with("")
            self.assertFalse((Path(tmp) / "run-manifest-20260908.json").exists())

    def test_successful_terminal_state_clears_stale_run_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "manifest.py",
                "--date",
                "20260908",
                "--state-dir",
                tmp,
                "--final",
                "failed",
                "--kv",
                "run_exit=1",
            ]
            with mock.patch.object(sys, "argv", argv):
                manifest.main()

            argv = [
                "manifest.py",
                "--date",
                "20260908",
                "--state-dir",
                tmp,
                "--final",
                "generated",
                "--kv",
                "reason=complete",
            ]
            with mock.patch.object(sys, "argv", argv):
                manifest.main()

            state = json.loads(
                (Path(tmp) / "run-manifest-20260908.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["final"], "generated")
            self.assertEqual(state["reason"], "complete")
            self.assertNotIn("run_exit", state)


if __name__ == "__main__":
    unittest.main()
