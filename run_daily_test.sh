#!/bin/bash
# 隔离测试版：weread-omni + 公开源 → 显式测试策略 → 成稿、质检、独立飞书文档。
# 不覆盖正式版产物、不发群消息、不写正式索引；测试单篇文档与测试索引仅向战略小组授予编辑权限；只允许在北京时间 15:00 闭窗后运行。

set -euo pipefail

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$ROOT_DIR/资讯日报-skill"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}"
TEST_INDEX_CHAT_ID="${FEISHU_TEST_INDEX_CHAT_ID:-${FEISHU_REPORT_CHAT_ID:-${FEISHU_CHAT_ID:-}}}"
PRODUCTION_DATA_ROOT="${RIBAO_DATA_ROOT:-$ROOT_DIR}"
TEST_BASE_ROOT="${RIBAO_TEST_DATA_ROOT:-$PRODUCTION_DATA_ROOT/测试版}"
PRODUCTION_REAL="$(cd "$PRODUCTION_DATA_ROOT" && pwd -P)"
mkdir -p "$TEST_BASE_ROOT"

CLOSED_YMD="$("$PYTHON_BIN" "$SKILL_DIR/fetch_news.py" --print-date)"
YMD="${RIBAO_TARGET_YMD:-$CLOSED_YMD}"
if [ "${RIBAO_ALLOW_OPEN_WINDOW:-0}" = "1" ] || [ "$YMD" -gt "$CLOSED_YMD" ]; then
  echo "测试版只允许使用北京时间 15:00 已闭合窗口；拒绝开放窗口生成" >&2
  exit 4
fi
TEST_RUN_LABEL="${RIBAO_TEST_RUN_LABEL:-$(date '+%H%M')}"
if ! [[ "$TEST_RUN_LABEL" =~ ^[A-Za-z0-9._-]{1,23}$ ]]; then
  echo "测试版本标识只允许 1-23 位字母、数字、点、下划线或连字符" >&2
  exit 5
fi
TEST_PROFILE="${RIBAO_TEST_PROFILE:-business_v2_strict}"
case "$TEST_PROFILE" in
  business_v2_strict) TEST_PROFILE_SLUG="strict" ;;
  business_v2_balanced) TEST_PROFILE_SLUG="balanced" ;;
  business_v2_recall) TEST_PROFILE_SLUG="recall" ;;
  *)
    echo "不允许的测试版筛选策略：$TEST_PROFILE" >&2
    exit 6
    ;;
esac
# 单版本手动运行仍默认使用原测试根目录；矩阵协调器为每个版本显式传入
# 独立目录，避免成稿、质检、manifest 和 Codex 工作区相互覆盖。
TEST_DATA_ROOT="${RIBAO_TEST_PROFILE_DATA_ROOT:-$TEST_BASE_ROOT}"
mkdir -p "$TEST_DATA_ROOT"
TEST_REAL="$(cd "$TEST_DATA_ROOT" && pwd -P)"
if [ "$PRODUCTION_REAL" = "$TEST_REAL" ]; then
  echo "测试版数据目录不得与正式版相同" >&2
  exit 2
fi
FEED_PATH="${RIBAO_WEREAD_OMNI_FEED_PATH:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test/weread-omni-test.rss}"
SYNC_SCRIPT="$SKILL_DIR/weread-omni/run_sync_test.sh"
SYNC_ASSESSOR="$SKILL_DIR/weread-omni/assess_sync.py"
SYNC_OBSERVATION_DIR="${RIBAO_WEREAD_OMNI_OBSERVATION_DIR:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test/observations}"
SYNC_LATEST="$SYNC_OBSERVATION_DIR/latest-${YMD}.json"
TEST_INDEX_SCRIPT="$ROOT_DIR/local-only/feishu-bot/update_test_index.py"
SHARED_SNAPSHOT="${RIBAO_TEST_SHARED_SNAPSHOT:-}"
if [ -n "$SHARED_SNAPSHOT" ] && [ ! -s "$SHARED_SNAPSHOT" ]; then
  echo "测试共享候选快照不存在或为空：$SHARED_SNAPSHOT" >&2
  exit 7
fi
SHARED_CANDIDATE_AUDIT="${RIBAO_TEST_CODEX_AUDIT:-}"
SHARED_FINANCE_RESEARCH="${RIBAO_TEST_FINANCE_RESEARCH:-}"
if [ -n "$SHARED_CANDIDATE_AUDIT" ] && [ ! -s "$SHARED_CANDIDATE_AUDIT" ]; then
  echo "测试共享候选审核不存在或为空：$SHARED_CANDIDATE_AUDIT" >&2
  exit 8
fi
if [ -n "$SHARED_FINANCE_RESEARCH" ] && [ ! -s "$SHARED_FINANCE_RESEARCH" ]; then
  echo "测试共享融资资料不存在或为空：$SHARED_FINANCE_RESEARCH" >&2
  exit 9
fi

# 14:00 后产生的同日快照可直接冻结；若 14:30 调度因休眠错过，生成前补同步一次。
FEED_READY=0
WEREAD_CHANNEL_STATUS="${RIBAO_WEREAD_OMNI_CHANNEL_STATUS:-unavailable}"
WEREAD_CHANNEL_COMPLETE="${RIBAO_WEREAD_OMNI_CHANNEL_COMPLETE:-0}"
WEREAD_CHANNEL_ITEMS="${RIBAO_WEREAD_OMNI_CHANNEL_ITEMS:-0}"
WEREAD_CHANNEL_FRESH_SOURCES="${RIBAO_WEREAD_OMNI_FRESH_SOURCES:-0}"
WEREAD_CHANNEL_STALE_SOURCES="${RIBAO_WEREAD_OMNI_STALE_SOURCES:-0}"
WEREAD_CHANNEL_ACCOUNTS="${RIBAO_WEREAD_OMNI_ACCOUNTS:-0}"
WEREAD_CHANNEL_UNASSESSED_SOURCES="${RIBAO_WEREAD_OMNI_UNASSESSED_SOURCES:-0}"
WEREAD_CHANNEL_FAILURE_CLASS="${RIBAO_WEREAD_OMNI_FAILURE_CLASS:-}"

assess_latest() {
  [ -s "$SYNC_LATEST" ] && [ -f "$SYNC_ASSESSOR" ] || return 1
  set +e
  IFS=$'\t' read -r WEREAD_CHANNEL_STATUS FEED_READY WEREAD_CHANNEL_COMPLETE \
    WEREAD_CHANNEL_ITEMS WEREAD_CHANNEL_FRESH_SOURCES \
    WEREAD_CHANNEL_STALE_SOURCES WEREAD_CHANNEL_ACCOUNTS \
    WEREAD_CHANNEL_UNASSESSED_SOURCES WEREAD_CHANNEL_FAILURE_CLASS < <(
      "$PYTHON_BIN" "$SYNC_ASSESSOR" \
        --observation "$SYNC_LATEST" --feed "$FEED_PATH" --date "$YMD" --format tsv
    )
  set -e
}

if [ "${RIBAO_TEST_WEREAD_PREPARED:-0}" != "1" ]; then
  assess_latest || true
  LATEST_RECENT=0
  if [ -s "$SYNC_LATEST" ] && "$PYTHON_BIN" - "$SYNC_LATEST" "$YMD" <<'PY'
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

path = Path(sys.argv[1])
ymd = sys.argv[2]
mtime = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo("Asia/Shanghai"))
fresh = mtime.strftime("%Y%m%d") == ymd and (mtime.hour, mtime.minute) >= (14, 0)
raise SystemExit(0 if fresh else 1)
PY
  then
    LATEST_RECENT=1
  fi

  if ! { [ "$LATEST_RECENT" -eq 1 ] && { [ "$WEREAD_CHANNEL_COMPLETE" -eq 1 ] \
    || [ "$WEREAD_CHANNEL_STATUS" = "human_verification_required" ] \
    || [ "$WEREAD_CHANNEL_STATUS" = "rate_limited" ]; }; }; then
    set +e
    "$SYNC_SCRIPT" "$YMD"
    SYNC_RC=$?
    set -e
  fi

  assess_latest || true
fi
if [ "$WEREAD_CHANNEL_STATUS" = "invalid" ]; then
  FEED_READY=0
fi

export RIBAO_PROJECT_ROOT="$ROOT_DIR"
export RIBAO_DATA_ROOT="$TEST_DATA_ROOT"
export RIBAO_TARGET_YMD="$YMD"
export RIBAO_EDITORIAL_POLICY="$TEST_PROFILE"
export RIBAO_OLD_NEWS_GATE=hard
export RIBAO_ENABLE_FEISHU=1
export FEISHU_DOC_ONLY=1
export FEISHU_DAILY_INDEX=0
export RIBAO_TEST_MODE=1
export FEISHU_TEST_SKIP_INDEX=1
export FEISHU_TEST_SKIP_PERMISSION=0
export FEISHU_REPORT_CHAT_ID="$TEST_INDEX_CHAT_ID"
export FEISHU_REPORT_CHAT_PERM=edit
export RIBAO_GROUP_MESSAGES=0
export FEISHU_STAGING_NAMESPACE="${YMD}-test-${TEST_PROFILE_SLUG}"
export RIBAO_WEREAD_OMNI_CHANNEL_STATUS="$WEREAD_CHANNEL_STATUS"
export RIBAO_WEREAD_OMNI_CHANNEL_COMPLETE="$WEREAD_CHANNEL_COMPLETE"
export RIBAO_WEREAD_OMNI_CHANNEL_ITEMS="$WEREAD_CHANNEL_ITEMS"
export RIBAO_WEREAD_OMNI_FRESH_SOURCES="$WEREAD_CHANNEL_FRESH_SOURCES"
export RIBAO_WEREAD_OMNI_STALE_SOURCES="$WEREAD_CHANNEL_STALE_SOURCES"
export RIBAO_WEREAD_OMNI_ACCOUNTS="$WEREAD_CHANNEL_ACCOUNTS"
export RIBAO_WEREAD_OMNI_UNASSESSED_SOURCES="$WEREAD_CHANNEL_UNASSESSED_SOURCES"
export RIBAO_WEREAD_OMNI_FAILURE_CLASS="$WEREAD_CHANNEL_FAILURE_CLASS"
case "$TEST_PROFILE" in
  business_v2_strict) FEISHU_REPORT_TITLE="测试版v2-严格" ;;
  business_v2_balanced) FEISHU_REPORT_TITLE="测试版v2-平衡" ;;
  business_v2_recall) FEISHU_REPORT_TITLE="测试版v2-高召回" ;;
esac
export FEISHU_REPORT_TITLE
unset FEISHU_REPORT_TITLE_SUFFIX
export FEISHU_REGISTRY_KEY="${YMD}-test-${TEST_PROFILE_SLUG}-${TEST_RUN_LABEL}"
export FEISHU_IDEMPOTENCY_SUFFIX="${FEISHU_IDEMPOTENCY_SUFFIX:-${TEST_PROFILE_SLUG}-${TEST_RUN_LABEL}}"
export FEISHU_TEST_INDEX_CHAT_ID="$TEST_INDEX_CHAT_ID"
unset FEISHU_CHAT_ID
export RIBAO_ENABLE_WEWE=0
export RIBAO_EXCLUDE_WEWE=1
export RIBAO_ENABLE_WERSS=0
export RIBAO_EXCLUDE_WERSS=1
unset RIBAO_REPORT_DIR RIBAO_OUT_DIR RIBAO_DRAFT_DIR RIBAO_CHECK_DIR RIBAO_STATE_DIR
unset RIBAO_CODEX_HOME RIBAO_CODEX_WORK_BASE RIBAO_CODEX_STAGING_BASE
# 私有配置已在本包装器入口导出；阻止生产包装器再次 source 后覆盖测试数据根目录。
export RIBAO_CONFIG_FILE="$ROOT_DIR/.test-config-already-loaded"

if [ "$FEED_READY" -eq 1 ] && [ -s "$FEED_PATH" ]; then
  export RIBAO_ENABLE_WEREAD_OMNI_TEST=1
  export RIBAO_EXCLUDE_WEREAD_OMNI=0
  export RIBAO_WEREAD_OMNI_FEED_URL="file://$FEED_PATH"
else
  export RIBAO_ENABLE_WEREAD_OMNI_TEST=0
  export RIBAO_EXCLUDE_WEREAD_OMNI=1
  echo "WARN: weread-omni 当前窗口无可用条目，测试日报将只使用公开源；通道状态已脱敏写入 manifest 与质检。" >&2
fi

MD="$("$PYTHON_BIN" -c 'import sys; y=sys.argv[1]; print(f"{int(y[4:6])}.{int(y[6:8])}")' "$YMD")"
REPORT="$TEST_DATA_ROOT/日报文档/资讯类简报-${MD}日.md"
CHECK="$TEST_DATA_ROOT/日报质检/资讯日报-质检-${YMD}.md"
MANIFEST="$TEST_DATA_ROOT/日报状态/run-manifest-${YMD}.json"
QA_SKIP_STATE="$TEST_DATA_ROOT/日报状态/qa-skip-${YMD}.json"
TEST_INDEX_COMMON_ARGS=(
  --report-registry "$TEST_DATA_ROOT/日报状态/feishu-docs.json"
  --status-data-root "$TEST_DATA_ROOT"
)
if [ -n "$SHARED_SNAPSHOT" ]; then
  TEST_INDEX_COMMON_ARGS+=(--source-snapshot "$SHARED_SNAPSHOT")
fi

update_shared_test_index() {
  # Per-profile data roots keep reports isolated.  The single test index and
  # its registry deliberately live at TEST_BASE_ROOT, so a direct one-profile
  # repair updates the same index as the matrix coordinator.
  RIBAO_DATA_ROOT="$TEST_BASE_ROOT" RIBAO_STATE_DIR="$TEST_BASE_ROOT/日报状态" \
    "$PYTHON_BIN" "$TEST_INDEX_SCRIPT" "$@" "${TEST_INDEX_COMMON_ARGS[@]}"
}

if [ -n "$SHARED_SNAPSHOT" ]; then
  mkdir -p "$TEST_DATA_ROOT/日报草稿" "$TEST_DATA_ROOT/日报状态"
  FETCH_SNAPSHOT_ARGS=(
    --date "$YMD" --editorial-profile "$TEST_PROFILE" --old-news-gate hard \
    --snapshot-in "$SHARED_SNAPSHOT" --out "$TEST_DATA_ROOT/日报草稿/资讯日报-草稿-${YMD}.md"
  )
  if [ -n "$SHARED_CANDIDATE_AUDIT" ]; then
    FETCH_SNAPSHOT_ARGS+=(--codex-audit "$SHARED_CANDIDATE_AUDIT")
  fi
  "$PYTHON_BIN" "$SKILL_DIR/fetch_news.py" "${FETCH_SNAPSHOT_ARGS[@]}"
  if [ -n "$SHARED_FINANCE_RESEARCH" ]; then
    cp "$SHARED_FINANCE_RESEARCH" "$TEST_DATA_ROOT/日报状态/测试版-融资企业资料-${YMD}.json"
  fi
  "$PYTHON_BIN" "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$TEST_DATA_ROOT/日报状态" \
    --step fetch --status ok --detail "replayed shared frozen source snapshot=$(basename "$SHARED_SNAPSHOT") audit=$([ -n "$SHARED_CANDIDATE_AUDIT" ] && echo applied || echo fallback)"
  export RIBAO_REUSE_DRAFT=1
fi

RUN_RC=0
/bin/bash "$ROOT_DIR/run_daily_with_feishu.sh" || RUN_RC=$?
if [ "$RUN_RC" -ne 0 ]; then
  if [ "${RIBAO_TEST_DEFER_INDEX:-0}" = "1" ]; then
    exit "$RUN_RC"
  fi
  QA_STATUS="$("$PYTHON_BIN" - "$MANIFEST" <<'PYQA'
import json, sys
try:
    value = str(json.load(open(sys.argv[1], encoding="utf-8")).get("verdict") or "")
except Exception:
    value = ""
print(value if value in {"PASS", "WARN", "FAIL"} else "")
PYQA
)"
  RUN_STATUS="failed"
  [ "$QA_STATUS" != "FAIL" ] || RUN_STATUS="blocked"
  INDEX_ARGS=(
    --date "$YMD" --import-as user --permission-as user --mode auto
    --profile "$TEST_PROFILE" --run-label "$TEST_RUN_LABEL" --run-status "$RUN_STATUS"
  )
  [ -z "$QA_STATUS" ] || INDEX_ARGS+=(--qa "$QA_STATUS")
  update_shared_test_index "${INDEX_ARGS[@]}" || \
    echo "WARN: 测试运行失败状态未能写入测试索引" >&2
  exit "$RUN_RC"
fi

if [ ! -s "$REPORT" ] || [ ! -s "$CHECK" ]; then
  echo "测试版成稿或质检文件缺失" >&2
  exit 3
fi
if [ "${RIBAO_TEST_DEFER_INDEX:-0}" = "1" ]; then
  "$PYTHON_BIN" "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$TEST_DATA_ROOT/日报状态" \
    --step test_index --status skipped --detail "matrix coordinator will update the isolated test index once all profiles finish"
elif update_shared_test_index \
  --date "$YMD" --import-as user --permission-as user --mode auto \
  --profile "$TEST_PROFILE" --run-label "$TEST_RUN_LABEL" --run-status generated; then
  "$PYTHON_BIN" "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$TEST_DATA_ROOT/日报状态" \
    --step test_index --status ok --detail "isolated index; user identity; group edit permission; no messages" \
    --final generated --kv "reason=独立测试日报、测试索引、编辑权限与回读质检完成"
else
  INDEX_RC=$?
  "$PYTHON_BIN" "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$TEST_DATA_ROOT/日报状态" \
    --step test_index --status fail --detail "isolated test index update failed exit=${INDEX_RC}" \
    --final failed --kv "reason=独立测试索引更新失败"
  echo "测试日报已生成，但独立测试索引更新失败" >&2
  exit "$INDEX_RC"
fi
ARCHIVE_DIR="$TEST_DATA_ROOT/版本归档/$YMD/$TEST_PROFILE/$TEST_RUN_LABEL"
mkdir -p "$ARCHIVE_DIR"
cp "$TEST_DATA_ROOT/日报草稿/资讯日报-草稿-${YMD}.md" "$ARCHIVE_DIR/"
cp "$REPORT" "$ARCHIVE_DIR/"
cp "$CHECK" "$ARCHIVE_DIR/"
[ ! -s "$MANIFEST" ] || cp "$MANIFEST" "$ARCHIVE_DIR/"
[ ! -s "$QA_SKIP_STATE" ] || cp "$QA_SKIP_STATE" "$ARCHIVE_DIR/"

echo "测试版完整流程完成：$REPORT"
echo "筛选策略：$TEST_PROFILE"
echo "质检文件：$CHECK"
echo "本地版本归档：$ARCHIVE_DIR"
echo "飞书：独立测试文档与测试索引已写入；测试索引已授予战略小组编辑权限；未发群消息、未写正式索引；文档地址见飞书写入步骤输出"
