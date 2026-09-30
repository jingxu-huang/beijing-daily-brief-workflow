#!/bin/bash
# 测试日报矩阵协调器：一次网络采集冻结同一窗口的原始候选，随后三种
# 筛选策略在各自隔离目录并行生成。它从不触碰正式日报目录或正式索引。

set -euo pipefail

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
# The formal 15:00 delivery is always the priority when both pipelines start
# together.  Children inherit this lower CPU scheduling priority.
renice 10 -p "$$" >/dev/null 2>&1 || true

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
PRODUCTION_DATA_ROOT="${RIBAO_DATA_ROOT:-$ROOT_DIR}"
TEST_BASE_ROOT="${RIBAO_TEST_DATA_ROOT:-$PRODUCTION_DATA_ROOT/测试版}"
mkdir -p "$TEST_BASE_ROOT/日报状态"

# The target is computed by the same closed-window helper as the formal
# workflow.  A launchd wake a fraction before 15:00 therefore exits cleanly
# instead of producing an open-window report.
CLOSED_YMD="$($PYTHON_BIN "$SKILL_DIR/fetch_news.py" --print-date)"
YMD="${RIBAO_TARGET_YMD:-$CLOSED_YMD}"
if [ "$YMD" -gt "$CLOSED_YMD" ]; then
  echo "测试矩阵只允许在北京时间 15:00 窗口闭合后启动" >&2
  exit 4
fi
if ! [[ "$YMD" =~ ^[0-9]{8}$ ]]; then
  echo "无效目标日期：$YMD" >&2
  exit 5
fi

MATRIX_STATE="$TEST_BASE_ROOT/日报状态/test-matrix-${YMD}.json"
LOCK_DIR="$TEST_BASE_ROOT/日报状态/.test-matrix-${YMD}.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "测试矩阵已有运行实例（pid=$old_pid）" >&2
    exit 0
  fi
  # Only a dead process's own, date-scoped lock is recovered.  No report,
  # snapshot or formal-workflow file is removed here.
  rm -f -- "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || {
    echo "测试矩阵遗留锁无法安全回收：$LOCK_DIR" >&2
    exit 1
  }
  mkdir "$LOCK_DIR"
fi
cleanup() { rm -f -- "$LOCK_DIR/pid"; rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
printf '%s\n' "$$" > "$LOCK_DIR/pid"

SNAPSHOT_DIR="$TEST_BASE_ROOT/共享快照/$YMD"
SNAPSHOT="$SNAPSHOT_DIR/原始候选.json"
CANDIDATE_AUDIT="$SNAPSHOT_DIR/候选审核.json"
CANDIDATE_AUDIT_BATCH_STATUS="${CANDIDATE_AUDIT%.json}-batches.json"
FINANCE_RESEARCH="$SNAPSHOT_DIR/融资企业资料.json"
AUDIT_STATUS="fallback"
AUDIT_DETAIL="未执行"
mkdir -p "$SNAPSHOT_DIR"

# One low-frequency weread-omni assessment/sync per matrix, never once per
# profile.  Workers inherit the exact result so their only difference is the
# editorial profile.
FEED_PATH="${RIBAO_WEREAD_OMNI_FEED_PATH:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test/weread-omni-test.rss}"
SYNC_SCRIPT="$SKILL_DIR/weread-omni/run_sync_test.sh"
SYNC_ASSESSOR="$SKILL_DIR/weread-omni/assess_sync.py"
OBS_DIR="${RIBAO_WEREAD_OMNI_OBSERVATION_DIR:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test/observations}"
OBSERVATION="$OBS_DIR/latest-${YMD}.json"
WEREAD_CHANNEL_STATUS="unavailable"
WEREAD_CHANNEL_COMPLETE=0
WEREAD_CHANNEL_ITEMS=0
WEREAD_CHANNEL_FRESH_SOURCES=0
WEREAD_CHANNEL_STALE_SOURCES=0
WEREAD_CHANNEL_ACCOUNTS=0
WEREAD_CHANNEL_UNASSESSED_SOURCES=0
WEREAD_CHANNEL_FAILURE_CLASS=""

assess_latest() {
  [ -s "$OBSERVATION" ] && [ -f "$SYNC_ASSESSOR" ] || return 1
  set +e
  IFS=$'\t' read -r WEREAD_CHANNEL_STATUS _feed_ready WEREAD_CHANNEL_COMPLETE \
    WEREAD_CHANNEL_ITEMS WEREAD_CHANNEL_FRESH_SOURCES WEREAD_CHANNEL_STALE_SOURCES \
    WEREAD_CHANNEL_ACCOUNTS WEREAD_CHANNEL_UNASSESSED_SOURCES WEREAD_CHANNEL_FAILURE_CLASS < <(
      "$PYTHON_BIN" "$SYNC_ASSESSOR" --observation "$OBSERVATION" --feed "$FEED_PATH" --date "$YMD" --format tsv
    )
  set -e
}
assess_latest || true
if [ "$WEREAD_CHANNEL_COMPLETE" != "1" ] && [ "$WEREAD_CHANNEL_STATUS" != "human_verification_required" ] && [ "$WEREAD_CHANNEL_STATUS" != "rate_limited" ]; then
  set +e
  "$SYNC_SCRIPT" "$YMD"
  set -e
  assess_latest || true
fi

if [ "$WEREAD_CHANNEL_STATUS" = "healthy" ] || [ "$WEREAD_CHANNEL_STATUS" = "cached" ]; then
  export RIBAO_ENABLE_WEREAD_OMNI_TEST=1
  export RIBAO_EXCLUDE_WEREAD_OMNI=0
  export RIBAO_WEREAD_OMNI_FEED_URL="file://$FEED_PATH"
else
  export RIBAO_ENABLE_WEREAD_OMNI_TEST=0
  export RIBAO_EXCLUDE_WEREAD_OMNI=1
fi
# WeWe RSS is retired.  Disable is not sufficient because the fetcher only
# skips the historical transport on the explicit exclusion contract.
export RIBAO_ENABLE_WEWE=0 RIBAO_EXCLUDE_WEWE=1 RIBAO_ENABLE_WERSS=0 RIBAO_EXCLUDE_WERSS=1
export RIBAO_OLD_NEWS_GATE=hard
export RIBAO_DATA_ROOT="$SNAPSHOT_DIR"
export RIBAO_DRAFT_DIR="$SNAPSHOT_DIR/日报草稿"
export RIBAO_REPORT_DIR="$SNAPSHOT_DIR/日报文档"
export RIBAO_STATE_DIR="$SNAPSHOT_DIR/日报状态"

if [ -s "$SNAPSHOT" ]; then
  # A same-day repair must compare the exact 15:00 candidate set already
  # frozen by the first matrix run; never recrawl and silently mix later news.
  echo "[$(date '+%F %T')] 测试矩阵：复用已冻结的共同原始候选…"
else
  echo "[$(date '+%F %T')] 测试矩阵：冻结共同原始候选…"
  "$PYTHON_BIN" "$SKILL_DIR/fetch_news.py" --date "$YMD" \
    --editorial-profile business_v2_recall --old-news-gate hard \
    --enrich-shortlist --enrich-limit 6 \
    --snapshot-out "$SNAPSHOT" --out "$SNAPSHOT_DIR/日报草稿/资讯日报-草稿-${YMD}.md"
  chmod a-w "$SNAPSHOT"
fi

# One small, bounded Codex audit is shared by all profiles.  It is deliberately
# best-effort: the formal workflow never invokes this script, and an auth,
# schema or timeout failure leaves the frozen snapshot and native selection
# untouched.  No profile waits on a second model call of its own.
if [ "${RIBAO_TEST_CODEX_AUDIT_MODE:-auto}" != "off" ]; then
  if [ "${RIBAO_TEST_REFRESH_CODEX_AUDIT:-0}" != "1" ] && [ -s "$CANDIDATE_AUDIT" ] && "$PYTHON_BIN" "$SKILL_DIR/codex_candidate_audit.py" \
    --root "$ROOT_DIR" --snapshot "$SNAPSHOT" --out "$CANDIDATE_AUDIT" --date "$YMD" --validate >/dev/null 2>&1; then
    AUDIT_STATUS="applied"
    AUDIT_DETAIL="复用已核验的共享候选审核"
  else
    set +e
    "$PYTHON_BIN" "$SKILL_DIR/codex_candidate_audit.py" \
      --root "$ROOT_DIR" --snapshot "$SNAPSHOT" --out "$CANDIDATE_AUDIT" --date "$YMD" \
      >"$SNAPSHOT_DIR/candidate-audit.stdout.log" 2>"$SNAPSHOT_DIR/candidate-audit.stderr.log"
    AUDIT_RC=$?
    set -e
    # A successfully exited auditor is usable only when it actually emitted
    # the exact shared result the workers will receive.  Keep this check
    # explicit: a missing file must fall back to native routing, never be
    # mistaken for a successful Codex review.
    if [ "$AUDIT_RC" -eq 0 ] && [ -s "$CANDIDATE_AUDIT" ]; then
      AUDIT_STATUS="applied"
      if [ -s "$CANDIDATE_AUDIT_BATCH_STATUS" ] && ! rg -q '"fallback_batches": 0' "$CANDIDATE_AUDIT_BATCH_STATUS"; then
        AUDIT_DETAIL="共享候选审核部分完成；失败批次已按原生规则继续"
      else
        AUDIT_DETAIL="共享候选审核已完成；三版复用同一结果"
      fi
    else
      AUDIT_STATUS="fallback"
      AUDIT_DETAIL="共享候选审核不可用(exit=$AUDIT_RC)，已按原生规则继续"
      echo "WARN: $AUDIT_DETAIL" >&2
    fi
  fi
else
  AUDIT_STATUS="disabled"
  AUDIT_DETAIL="RIBAO_TEST_CODEX_AUDIT_MODE=off"
fi

if [ "$AUDIT_STATUS" = "applied" ] && [ ! -s "$FINANCE_RESEARCH" ]; then
  # A valid audit always writes the sibling source-bound research list.  Treat
  # a missing list as audit fallback rather than allowing stale finance data.
  AUDIT_STATUS="fallback"
  AUDIT_DETAIL="候选审核缺少同批融资资料，已按原生规则继续"
fi
WORKER_AUDIT=""
WORKER_FINANCE_RESEARCH=""
if [ "$AUDIT_STATUS" = "applied" ]; then
  WORKER_AUDIT="$CANDIDATE_AUDIT"
  WORKER_FINANCE_RESEARCH="$FINANCE_RESEARCH"
fi

RUN_BASE="$TEST_BASE_ROOT/运行/$YMD"
mkdir -p "$RUN_BASE"
PROFILES=(business_v2_strict business_v2_balanced business_v2_recall)
PIDS=()
RESULTS=()
for profile in "${PROFILES[@]}"; do
  slug="${profile#business_v2_}"
  profile_root="$RUN_BASE/$slug"
  mkdir -p "$profile_root"
  log="$profile_root/matrix.log"
  (
    RIBAO_TEST_DATA_ROOT="$TEST_BASE_ROOT" \
    RIBAO_TEST_PROFILE_DATA_ROOT="$profile_root" \
    RIBAO_TEST_PROFILE="$profile" \
    RIBAO_TEST_RUN_LABEL="daily" \
    RIBAO_TEST_SHARED_SNAPSHOT="$SNAPSHOT" \
    RIBAO_TEST_CODEX_AUDIT="$WORKER_AUDIT" \
    RIBAO_TEST_FINANCE_RESEARCH="$WORKER_FINANCE_RESEARCH" \
    RIBAO_TEST_DEFER_INDEX=1 \
    RIBAO_TEST_WEREAD_PREPARED=1 \
    RIBAO_WEREAD_OMNI_CHANNEL_STATUS="$WEREAD_CHANNEL_STATUS" \
    RIBAO_WEREAD_OMNI_CHANNEL_COMPLETE="$WEREAD_CHANNEL_COMPLETE" \
    RIBAO_WEREAD_OMNI_CHANNEL_ITEMS="$WEREAD_CHANNEL_ITEMS" \
    RIBAO_WEREAD_OMNI_FRESH_SOURCES="$WEREAD_CHANNEL_FRESH_SOURCES" \
    RIBAO_WEREAD_OMNI_STALE_SOURCES="$WEREAD_CHANNEL_STALE_SOURCES" \
    RIBAO_WEREAD_OMNI_ACCOUNTS="$WEREAD_CHANNEL_ACCOUNTS" \
    RIBAO_WEREAD_OMNI_UNASSESSED_SOURCES="$WEREAD_CHANNEL_UNASSESSED_SOURCES" \
    RIBAO_WEREAD_OMNI_FAILURE_CLASS="$WEREAD_CHANNEL_FAILURE_CLASS" \
    "$ROOT_DIR/run_daily_test.sh"
  ) >"$log" 2>&1 &
  PIDS+=("$!")
  RESULTS+=("$profile:$profile_root")
done

overall=0
for index in "${!PIDS[@]}"; do
  if ! wait "${PIDS[$index]}"; then
    overall=1
  fi
done

# The docs are already independently published by their own workers.  Only
# this coordinator records their outcomes and rewrites the shared test index,
# avoiding concurrent Feishu index edits.
export RIBAO_TEST_MODE=1
export RIBAO_DATA_ROOT="$TEST_BASE_ROOT"
export RIBAO_STATE_DIR="$TEST_BASE_ROOT/日报状态"
export FEISHU_TEST_INDEX_CHAT_ID="${FEISHU_TEST_INDEX_CHAT_ID:-${FEISHU_REPORT_CHAT_ID:-${FEISHU_CHAT_ID:-}}}"
REPORT_REGISTRY_ARGS=()
for profile in "${PROFILES[@]}"; do
  slug="${profile#business_v2_}"
  REPORT_REGISTRY_ARGS+=(--report-registry "$RUN_BASE/$slug/日报状态/feishu-docs.json")
done
for item in "${RESULTS[@]}"; do
  profile="${item%%:*}"
  profile_root="${item#*:}"
  manifest="$profile_root/日报状态/run-manifest-${YMD}.json"
  qa="$($PYTHON_BIN - "$manifest" <<'PY'
import json, sys
try:
    value = str(json.load(open(sys.argv[1], encoding="utf-8")).get("verdict") or "")
except Exception:
    value = ""
print(value if value in {"PASS", "WARN", "FAIL"} else "")
PY
)"
  final="$($PYTHON_BIN - "$manifest" <<'PY'
import json, sys
try:
    value = str(json.load(open(sys.argv[1], encoding="utf-8")).get("final") or "")
except Exception:
    value = ""
print(value)
PY
)"
  status="generated"
  [ "$final" = "generated" ] || status="failed"
  [ "$qa" != "FAIL" ] || status="blocked"
  index_args=(
    --date "$YMD" --import-as user --permission-as user --mode auto
    --profile "$profile" --run-label daily --run-status "$status"
    --status-data-root "$profile_root" --source-snapshot "$SNAPSHOT" --record-only
  )
  [ -z "$qa" ] || index_args+=(--qa "$qa")
  FEISHU_REGISTRY_KEY="${YMD}-test-${profile#business_v2_}-daily" \
  RIBAO_EDITORIAL_POLICY="$profile" \
    "$PYTHON_BIN" "$ROOT_DIR/local-only/feishu-bot/update_test_index.py" \
      "${index_args[@]}" "${REPORT_REGISTRY_ARGS[@]}"
done
"$PYTHON_BIN" "$ROOT_DIR/local-only/feishu-bot/update_test_index.py" \
  --date "$YMD" --import-as user --permission-as user --mode auto \
  "${REPORT_REGISTRY_ARGS[@]}"

RIBAO_MATRIX_AUDIT_STATUS="$AUDIT_STATUS" RIBAO_MATRIX_AUDIT_DETAIL="$AUDIT_DETAIL" \
"$PYTHON_BIN" - "$MATRIX_STATE" "$YMD" "$SNAPSHOT" "$overall" "$RUN_BASE" <<'PY'
import json, os, sys, time
from pathlib import Path
path, ymd, snapshot, overall, run_base = map(str, sys.argv[1:])
profile_states = {}
for profile in ("strict", "balanced", "recall"):
    manifest_path = Path(run_base) / profile / "日报状态" / f"run-manifest-{ymd}.json"
    manifest = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    verdict = str(manifest.get("verdict") or "unknown")
    final = str(manifest.get("final") or "failed")
    if verdict == "FAIL":
        status = "blocked"
    elif final == "generated":
        status = "generated"
    else:
        status = "failed"
    profile_states[profile] = {
        "status": status,
        "qa": verdict,
        "manifest": str(manifest_path),
    }
payload = {
    "target_ymd": ymd,
    "snapshot": snapshot,
    "profiles": profile_states,
    "status": "generated" if overall == "0" else "partial_failure",
    "candidate_audit": {"status": os.environ.get("RIBAO_MATRIX_AUDIT_STATUS", "fallback"), "detail": os.environ.get("RIBAO_MATRIX_AUDIT_DETAIL", "")},
    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}
Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit "$overall"
