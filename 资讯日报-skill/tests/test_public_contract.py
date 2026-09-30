import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class PublicPackageContractTests(unittest.TestCase):
    def test_feishu_chat_has_no_default(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        sender = (REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py").read_text(encoding="utf-8")
        self.assertIn('REPORT_CHAT_ID="${FEISHU_REPORT_CHAT_ID:-}"', wrapper)
        self.assertNotIn("DEFAULT_CHAT_ID", sender)

    def test_feishu_group_permission_requires_full_access(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        sender = (REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py").read_text(encoding="utf-8")
        config_example = (REPO_ROOT / "config.env.example").read_text(encoding="utf-8")
        self.assertIn('FEISHU_REPORT_CHAT_PERM:-full_access', wrapper)
        self.assertIn('if perm == "full_access":', sender)
        self.assertIn('RIBAO_TEST_MODE', sender)
        self.assertIn('perm == "edit"', sender)
        self.assertIn('FEISHU_REPORT_CHAT_PERM=full_access', config_example)

    def test_normal_feishu_delivery_updates_index_before_success(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        publish_start = wrapper.index('echo "[$(date \'+%F %T\')] 4/4 投递飞书')
        index_start = wrapper.index('if [ "${FEISHU_DAILY_INDEX:-0}" = "1" ]; then', publish_start)
        final_sent = wrapper.index("MANIFEST --final sent", index_start)
        self.assertLess(publish_start, index_start)
        self.assertLess(index_start, final_sent)

    def test_index_auto_mode_refuses_unregistered_target(self):
        updater = (REPO_ROOT / "local-only/feishu-bot/update_daily_index.py").read_text(encoding="utf-8")
        self.assertIn("缺少已确认的索引文档注册表", updater)
        self.assertIn("确需新建时须显式使用 --mode new", updater)

    def test_index_grants_group_full_access(self):
        updater = (REPO_ROOT / "local-only/feishu-bot/update_daily_index.py").read_text(encoding="utf-8")
        self.assertIn("feishu.grant_chat_permission", updater)
        self.assertIn('args.perm != "full_access"', updater)
        self.assertIn('"group_permission": "full_access"', updater)

    def test_index_only_mode_precedes_chat_delivery(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        index_only = wrapper.index('if [ "${FEISHU_DOC_ONLY:-0}" = "1" ]; then')
        chat_delivery = wrapper.index('4/4 投递飞书', index_only)
        self.assertLess(index_only, chat_delivery)
        self.assertIn('--import-as "${FEISHU_CLI_IMPORT_AS:-user}" --doc-only', wrapper)
        self.assertIn("不发送群消息", wrapper)

    def test_isolated_test_flow_waits_for_closed_window_and_writes_feishu(self):
        wrapper = (REPO_ROOT / "run_daily_test.sh").read_text(encoding="utf-8")
        self.assertIn('RIBAO_ALLOW_OPEN_WINDOW:-0', wrapper)
        self.assertIn('RIBAO_ENABLE_FEISHU=1', wrapper)
        self.assertIn('FEISHU_DOC_ONLY=1', wrapper)
        self.assertIn('RIBAO_TEST_MODE=1', wrapper)
        self.assertIn('FEISHU_TEST_SKIP_INDEX=1', wrapper)
        self.assertIn('FEISHU_TEST_SKIP_PERMISSION=0', wrapper)
        self.assertIn('FEISHU_REPORT_CHAT_ID="$TEST_INDEX_CHAT_ID"', wrapper)
        self.assertIn('FEISHU_REPORT_CHAT_PERM=edit', wrapper)
        self.assertIn('FEISHU_REGISTRY_KEY', wrapper)
        self.assertIn('unset FEISHU_CHAT_ID', wrapper)
        self.assertIn('版本归档', wrapper)
        self.assertIn('RIBAO_GROUP_MESSAGES=0', wrapper)
        self.assertIn('run_daily_with_feishu.sh', wrapper)
        self.assertIn('FEISHU_REPORT_TITLE="测试版v2-严格"', wrapper)
        self.assertIn('FEISHU_REPORT_TITLE="测试版v2-平衡"', wrapper)
        self.assertIn('FEISHU_REPORT_TITLE="测试版v2-高召回"', wrapper)
        self.assertIn('RIBAO_TEST_PROFILE:-business_v2_strict', wrapper)
        self.assertIn('business_v2_balanced)', wrapper)
        self.assertIn('business_v2_recall)', wrapper)
        self.assertIn('export RIBAO_EDITORIAL_POLICY="$TEST_PROFILE"', wrapper)
        self.assertIn('update_test_index.py', wrapper)
        self.assertIn('FEISHU_TEST_INDEX_CHAT_ID', wrapper)
        self.assertIn('--run-status generated', wrapper)
        self.assertIn('--run-status "$RUN_STATUS"', wrapper)
        self.assertIn('--step test_index --status ok', wrapper)
        self.assertIn('资讯日报-草稿-${YMD}.md', wrapper)
        self.assertIn('RIBAO_TEST_PROFILE_DATA_ROOT', wrapper)
        self.assertIn('RIBAO_TEST_SHARED_SNAPSHOT', wrapper)
        self.assertIn('RIBAO_TEST_DEFER_INDEX', wrapper)

    def test_test_matrix_freezes_one_source_snapshot_then_runs_profiles_in_parallel(self):
        matrix = (REPO_ROOT / "run_daily_test_matrix.sh").read_text(encoding="utf-8")
        template = (
            REPO_ROOT / "automation" / "com.example.beijing-daily-brief-test-matrix.plist.template"
        ).read_text(encoding="utf-8")
        self.assertIn('--snapshot-out "$SNAPSHOT"', matrix)
        self.assertIn('RIBAO_TEST_SHARED_SNAPSHOT="$SNAPSHOT"', matrix)
        self.assertIn('RIBAO_TEST_DEFER_INDEX=1', matrix)
        self.assertIn('PIDS+=', matrix)
        self.assertIn('wait "${PIDS[$index]}"', matrix)
        self.assertIn('--record-only', matrix)
        self.assertIn('StartCalendarInterval', template)
        self.assertIn('<integer>15</integer>', template)
        self.assertIn('<integer>0</integer>', template)
        self.assertNotIn('run_daily_with_feishu.sh', matrix)

    def test_candidate_audit_is_shared_test_only_and_has_a_native_fallback(self):
        matrix = (REPO_ROOT / "run_daily_test_matrix.sh").read_text(encoding="utf-8")
        test_wrapper = (REPO_ROOT / "run_daily_test.sh").read_text(encoding="utf-8")
        formal = (REPO_ROOT / "资讯日报-skill" / "run_daily.sh").read_text(encoding="utf-8")
        self.assertIn('codex_candidate_audit.py', matrix)
        self.assertIn('RIBAO_TEST_CODEX_AUDIT_MODE:-auto', matrix)
        self.assertIn('共享候选审核不可用', matrix)
        self.assertIn('RIBAO_TEST_CODEX_AUDIT="$WORKER_AUDIT"', matrix)
        self.assertIn('[ -s "$CANDIDATE_AUDIT" ]', matrix)
        self.assertNotIn('AUDIO_AUDIT', matrix)
        self.assertIn('--codex-audit "$SHARED_CANDIDATE_AUDIT"', test_wrapper)
        self.assertNotIn('codex_candidate_audit', formal)

    def test_test_index_is_separate_grants_edit_and_never_sends_messages(self):
        updater = (REPO_ROOT / "local-only/feishu-bot/update_test_index.py").read_text(encoding="utf-8")
        self.assertIn('feishu-test-index.json', updater)
        self.assertIn('RIBAO_TEST_MODE', updater)
        self.assertIn('args.import_as != "user" or args.permission_as != "user"', updater)
        self.assertIn('grant_test_index_edit_permission', updater)
        self.assertIn('"perm": "edit"', updater)
        self.assertIn('"messages_sent": False', updater)
        self.assertNotIn('send_chat_message(', updater)

    def test_production_index_cannot_be_skipped_without_explicit_test_mode(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        self.assertIn('"${RIBAO_TEST_MODE:-0}" = "1"', wrapper)
        self.assertIn('"${FEISHU_TEST_SKIP_INDEX:-0}" = "1"', wrapper)
        self.assertIn("正式索引保持不变", wrapper)

    def test_index_only_mode_still_grants_daily_group_full_access(self):
        sender = (REPO_ROOT / "local-only/feishu-bot/send_to_feishu_cli.py").read_text(encoding="utf-8")
        self.assertIn('if not chat_id:', sender)
        self.assertIn('if perm == "full_access":', sender)
        self.assertIn('RIBAO_TEST_MODE', sender)
        self.assertIn('perm == "edit"', sender)
        self.assertGreaterEqual(sender.count("grant_chat_permission(doc_token, chat_id, perm"), 2)

    def test_manual_wrapper_honors_no_group_messages_for_failure_alerts(self):
        wrapper = (REPO_ROOT / "run_daily_with_feishu.sh").read_text(encoding="utf-8")
        alert = wrapper[wrapper.index("alert() {"):wrapper.index("echo \"[$(date '+%F %T')] 目标日期")]
        self.assertIn('elif [ "${RIBAO_GROUP_MESSAGES:-1}" = "0" ]; then', alert)
        self.assertLess(alert.index('RIBAO_GROUP_MESSAGES:-1'), alert.index('lark-cli im +messages-send'))

    def test_group_messages_have_global_off_switch(self):
        guard = (REPO_ROOT / "资讯日报-skill/ribao_guard.py").read_text(encoding="utf-8")
        wewe = (REPO_ROOT / "资讯日报-skill/wewe_preflight.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("RIBAO_GROUP_MESSAGES", "1") != "0"', guard)
        self.assertIn('os.getenv("RIBAO_GROUP_MESSAGES", "1") != "0"', wewe)

    def test_test_matrix_reuses_a_frozen_snapshot_for_same_day_repair(self):
        matrix = (REPO_ROOT / "run_daily_test_matrix.sh").read_text(encoding="utf-8")
        self.assertIn('if [ -s "$SNAPSHOT" ]; then', matrix)
        self.assertIn("复用已冻结的共同原始候选", matrix)

    def test_only_one_daily_scheduler_template(self):
        templates = list((REPO_ROOT / "automation").glob("*guard*.plist.template"))
        self.assertEqual([path.name for path in templates], ["com.example.beijing-daily-brief-guard.plist.template"])

    def test_audit_accepts_repository(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/audit_public_package.py"), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_internal_index_urls_remain_blocked_from_public_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "public-note.md"
            asset.write_text(
                "https://micoworld.feishu.cn" + "/docx/ExampleToken123\n", encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts/audit_public_package.py"),
                    "--root",
                    str(REPO_ROOT),
                    "--asset",
                    str(asset),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private Feishu document URL", result.stderr)

    def test_audit_rejects_absolute_path_in_checksum_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            checksum = Path(tmp) / "release.zip.sha256"
            private_path = Path("/") / "Volumes" / "PrivateDisk" / "release.zip"
            checksum.write_text(f"{'0' * 64}  {private_path}\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts/audit_public_package.py"),
                    "--root",
                    str(REPO_ROOT),
                    "--asset",
                    str(checksum),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
