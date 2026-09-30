import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class BootstrapModeTests(unittest.TestCase):
    def _fake_command(self, directory, name, body="exit 0\n"):
        path = Path(directory) / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_preflight(self, enable_feishu="0", enable_wewe="0"):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            data_root = tmp_path / "data"
            auth_home = tmp_path / "codex-home"
            bin_dir.mkdir()
            data_root.mkdir()
            auth_home.mkdir()
            (auth_home / "auth.json").write_text("{}\n", encoding="utf-8")
            self._fake_command(bin_dir, "docker")
            self._fake_command(bin_dir, "codex")
            self._fake_command(bin_dir, "curl")
            # The behavior under test is dependency/capability gating. Avoid
            # recursively invoking verify.sh, which discovers this test again.
            self._fake_command(bin_dir, "bash")
            config = tmp_path / "config.env"
            config.write_text(
                "\n".join(
                    [
                        f'RIBAO_DATA_ROOT="{data_root}"',
                        f'RIBAO_PYTHON_BIN="{sys.executable}"',
                        f'RIBAO_EXISTING_CODEX_HOME="{auth_home}"',
                        f"RIBAO_ENABLE_FEISHU={enable_feishu}",
                        f"RIBAO_ENABLE_WEWE={enable_wewe}",
                        "WEWE_AUTH_CODE=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin"
            env["RIBAO_CONFIG_FILE"] = str(config)
            return subprocess.run(
                ["/bin/bash", str(REPO_ROOT / "scripts/new_mac_preflight.sh")],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_public_only_mode_passes_without_lark_or_wewe_credentials(self):
        result = self._run_preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("公开源本地日报可运行", result.stdout)

    def test_enabled_feishu_requires_lark_identity(self):
        result = self._run_preflight(enable_feishu="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已启用飞书", result.stdout + result.stderr)

    def test_enabled_wewe_requires_optional_dependencies_and_auth(self):
        result = self._run_preflight(enable_wewe="1")
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("已启用 WeWe", output)
        self.assertIn("WEWE_AUTH_CODE", output)

    def test_bootstrap_gates_optional_install_and_browser_actions(self):
        script = (REPO_ROOT / "bootstrap_new_mac.command").read_text(encoding="utf-8")
        self.assertIn('if [ "$ENABLE_FEISHU" = "1" ]', script)
        self.assertIn('if [ "$ENABLE_WEWE" = "1" ]', script)
        self.assertIn('RIBAO_ENABLE_FEISHU', script)
        self.assertIn('RIBAO_ENABLE_WEWE', script)
        self.assertIn('open "http://127.0.0.1:4000/dash/accounts"', script)
        self.assertLess(script.index('if [ "$PREPARE_ONLY" -eq 1 ]'), script.index('say "安装每日日报定时任务"'))
        self.assertNotIn('text += "\\n# User selected local storage', script)

    def test_local_relogin_installer_does_not_require_remote_tunnel(self):
        installer = (REPO_ROOT / "automation/install_wewe_relogin.command").read_text(encoding="utf-8")
        self.assertIn('ENABLE_REMOTE_RELOGIN="${RIBAO_ENABLE_REMOTE_RELOGIN:-0}"', installer)
        self.assertIn('if [ "$ENABLE_REMOTE_RELOGIN" = "1" ]', installer)
        self.assertIn("Installing local-only WeWe relogin page", installer)

    def test_rsshub_compose_uses_repo_digest_and_loopback_only(self):
        compose = (REPO_ROOT / "public-sources/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("diygod/rsshub@sha256:", compose)
        self.assertNotIn("diygod/rsshub:", compose)
        self.assertIn('127.0.0.1:1200:1200', compose)

    def test_wrapper_requires_explicit_feishu_switch(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        self.assertIn('FEISHU_ENABLED="${RIBAO_ENABLE_FEISHU:-0}"', wrapper)
        self.assertIn('RIBAO_ENABLE_FEISHU=0，明确跳过全部飞书写入', wrapper)


if __name__ == "__main__":
    unittest.main()
