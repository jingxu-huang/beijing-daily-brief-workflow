import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "automation" / "install_parallel.command"


class HandoffPreflightTests(unittest.TestCase):
    def _run_check(self, config, home, bin_dir):
        env = os.environ.copy()
        env.update(
            {
                "RIBAO_CONFIG_FILE": str(config),
                "HOME": str(home),
                "PATH": f"{bin_dir}:{env.get('PATH', '')}",
                "TZ": "Asia/Shanghai",
            }
        )
        return subprocess.run(
            ["/bin/bash", str(INSTALLER), "--check"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_private_handoff_items_are_reported_together(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.env"
            data = root / "data"
            data.mkdir()
            config.write_text(
                f'RIBAO_DATA_ROOT="{data}"\nRIBAO_ENABLE_FEISHU=0\n'
                'FEISHU_DOC_ONLY=0\nFEISHU_DAILY_INDEX=0\nRIBAO_GROUP_MESSAGES=1\n',
                encoding="utf-8",
            )
            result = self._run_check(config, root, root / "bin")
            self.assertNotEqual(result.returncode, 0)
            for term in ("RIBAO_ENABLE_FEISHU", "FEISHU_DOC_ONLY", "FEISHU_DAILY_INDEX", "feishu-daily-index.json", "feishu-test-index.json"):
                self.assertIn(term, result.stderr)
            self.assertFalse((data / "测试版").exists(), "--check must not create runtime directories")

    def test_complete_private_handoff_passes_without_installing_schedules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("docker", "curl"):
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)
            cli = bin_dir / "lark-cli"
            cli.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = whoami ] && [ \"$2\" = --as ] && [ \"$3\" = user ]\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            codex = bin_dir / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex.chmod(0o755)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
            skill_dir = root / ".codex" / "skills"
            skill_dir.mkdir(parents=True)
            (skill_dir / "zixun-ribao").symlink_to(ROOT / "资讯日报-skill")
            data = root / "data"
            formal_state = data / "日报状态"
            test_state = data / "测试版" / "日报状态"
            formal_state.mkdir(parents=True)
            test_state.mkdir(parents=True)
            base = "https://micoworld.feishu.cn"
            for directory, filename, token in (
                (formal_state, "feishu-daily-index.json", "FormalExample123"),
                (test_state, "feishu-test-index.json", "TestExample123"),
            ):
                (directory / filename).write_text(
                    json.dumps({"doc_token": token, "url": base + "/docx/" + token}),
                    encoding="utf-8",
                )
            for directory, filename in (
                (formal_state, "feishu-docs.json"),
                (test_state, "feishu-docs.json"),
                (test_state, "feishu-test-runs.json"),
            ):
                (directory / filename).write_text('{"historical": {}}\n', encoding="utf-8")
            config = root / "config.env"
            config.write_text(
                f'RIBAO_DATA_ROOT="{data}"\nRIBAO_EXISTING_CODEX_HOME="{codex_home}"\n'
                f'RIBAO_PYTHON_BIN="{sys.executable}"\n'
                'RIBAO_ENABLE_FEISHU=1\nFEISHU_DOC_ONLY=1\nFEISHU_DAILY_INDEX=1\n'
                'RIBAO_GROUP_MESSAGES=0\nFEISHU_REPORT_CHAT_ID="example-group"\n'
                'RIBAO_ALLOW_SYSTEM_DATA_ROOT=1\n',
                encoding="utf-8",
            )
            result = self._run_check(config, root, bin_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("交接预检通过", result.stdout)
            self.assertFalse((root / "Library" / "LaunchAgents").exists())
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "RIBAO_ALLOW_SYSTEM_DATA_ROOT=1", "RIBAO_ALLOW_SYSTEM_DATA_ROOT=0"
                ),
                encoding="utf-8",
            )
            blocked = self._run_check(config, root, bin_dir)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("RIBAO_ALLOW_SYSTEM_DATA_ROOT=1", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
