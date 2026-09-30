#!/bin/bash
# 每日运行：抓取 → 让 Codex 浓缩成稿 → 质检 → 写回项目根目录的产物文件夹
# 由北京时间守门器调用（也可手动 bash run_daily.sh 测试）
# 阶段1加固(2026-07-08)：目标日期显式贯穿（不再 ls -t 猜最新文件），每步写 run-manifest。
# UTF-8：确保中文路径与输出正常
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# cron 环境 PATH 很精简，显式补全 homebrew/python 的常见位置
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
# 关键：nvm 安装的新版 codex 放最前，避免用到 Homebrew 的旧版（取最高 node 版本）
NVM_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$NVM_BIN" ] && export PATH="$NVM_BIN:$PATH"
PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}"

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SKILL_DIR/.." && pwd)"
DATA_ROOT="${RIBAO_DATA_ROOT:-$ROOT_DIR}"
export RIBAO_PROJECT_ROOT="${RIBAO_PROJECT_ROOT:-$ROOT_DIR}"
export RIBAO_DATA_ROOT="$DATA_ROOT"
REPORT_DIR="${RIBAO_REPORT_DIR:-${RIBAO_OUT_DIR:-$DATA_ROOT/日报文档}}"
DRAFT_DIR="${RIBAO_DRAFT_DIR:-$DATA_ROOT/日报草稿}"
CHECK_DIR="${RIBAO_CHECK_DIR:-$DATA_ROOT/日报质检}"
STATE_DIR="${RIBAO_STATE_DIR:-$DATA_ROOT/日报状态}"
mkdir -p "$REPORT_DIR" "$DRAFT_DIR" "$CHECK_DIR" "$STATE_DIR"

cd "$SKILL_DIR"

# 目标日期：wrapper 传入 RIBAO_TARGET_YMD；手动运行时自己算（最近一个已闭合的北京15:00窗口）
YMD="${RIBAO_TARGET_YMD:-$("$PYTHON_BIN" "$SKILL_DIR/fetch_news.py" --print-date)}"
MD="$("$PYTHON_BIN" -c 'import sys; y=sys.argv[1]; print(f"{int(y[4:6])}.{int(y[6:8])}")' "$YMD")"
DRAFT="$DRAFT_DIR/资讯日报-草稿-${YMD}.md"
REPORT="$REPORT_DIR/资讯类简报-${MD}日.md"
CHECK="$CHECK_DIR/资讯日报-质检-${YMD}.md"
MANIFEST() { "$PYTHON_BIN" "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$STATE_DIR" "$@" || true; }

WINDOW_HINT="北京时间 前一日15:00—当日15:00，左闭右开"
FETCH_OPEN_ARGS=()
if [ "${RIBAO_ALLOW_OPEN_WINDOW:-0}" = "1" ]; then
  WINDOW_HINT="北京时间 前一日15:00—当前可信时间（14:00前交付开放窗口）"
  FETCH_OPEN_ARGS=(--allow-open-window)
fi
echo "[$(date '+%F %T')] 目标日期 ${YMD}（窗口：${WINDOW_HINT}）"
MANIFEST --step plan --status ok --detail "window=${WINDOW_HINT} draft=$(basename "$DRAFT") report=$(basename "$REPORT")"

# WeWe RSS is retired from the formal production workflow.  Do not probe,
# refresh, open a QR page, or allow a manually-set environment value to bring
# it back into a formal report.  Its historical project and records are kept
# outside this execution path for traceability only.
export RIBAO_EXCLUDE_WEWE=1
export RIBAO_WEWE_STATUS=retired
MANIFEST --step wewe_preflight --status skipped --detail "WeWe RSS 已退役：正式日报不检测、不刷新、不使用"

# WeRSS is an independent local enhancement feed.  It uses the same graceful
# degradation rule as WeWe: absence or a transient failure never blocks the
# public-source pipeline or turns a complete report into a failed report.
if [ "${RIBAO_ENABLE_WERSS:-0}" != "1" ]; then
  export RIBAO_EXCLUDE_WERSS=1
  export RIBAO_WERSS_STATUS=disabled
  MANIFEST --step werss_preflight --status skipped --detail "RIBAO_ENABLE_WERSS=0，WeRSS 增强源明确排除"
elif [ -z "${RIBAO_EXCLUDE_WERSS+x}" ]; then
  set +e
  WERSS_PROBE_JSON="$("$PYTHON_BIN" -c 'import json,sys; sys.path.insert(0, sys.argv[1]); import werss_preflight as w; r=w.probe_health(); print(json.dumps(r, ensure_ascii=False)); raise SystemExit(0 if r.get("status") == "healthy" else 1)' "$SKILL_DIR")"
  WERSS_PROBE_RC=$?
  set -e
  if [ "$WERSS_PROBE_RC" -eq 0 ]; then
    export RIBAO_EXCLUDE_WERSS=0
    export RIBAO_WERSS_STATUS=healthy
    MANIFEST --step werss_preflight --status ok --detail "$WERSS_PROBE_JSON"
  else
    export RIBAO_EXCLUDE_WERSS=1
    export RIBAO_WERSS_STATUS=unavailable
    MANIFEST --step werss_preflight --status warn --detail "WeRSS 已降级排除：$WERSS_PROBE_JSON"
  fi
else
  MANIFEST --step werss_preflight --status "$([ "$RIBAO_EXCLUDE_WERSS" = "1" ] && echo warn || echo ok)" \
    --detail "守门器判定：werss_status=${RIBAO_WERSS_STATUS:-unknown} excluded=${RIBAO_EXCLUDE_WERSS}"
fi

echo "[$(date '+%F %T')] 1/3 抓取..."
PRIOR_FETCH_STATUS="$($PYTHON_BIN "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$STATE_DIR" --read-step-status fetch 2>/dev/null || true)"
if [ "${RIBAO_REUSE_DRAFT:-0}" = "1" ] && [ -s "$DRAFT" ] && [ "$PRIOR_FETCH_STATUS" = "ok" ]; then
  echo "[$(date '+%F %T')] 复用已冻结的目标窗口草稿：$(basename "$DRAFT")"
  MANIFEST --step fetch --status ok --detail "reused coverage-verified frozen draft"
else
  if [ "${RIBAO_REUSE_DRAFT:-0}" = "1" ] && [ -s "$DRAFT" ]; then
    echo "[$(date '+%F %T')] 已有草稿未通过上次抓取门禁（fetch=${PRIOR_FETCH_STATUS:-unknown}），本轮重新抓取"
  fi
  if RIBAO_REPORT_DIR="$REPORT_DIR" RIBAO_DRAFT_DIR="$DRAFT_DIR" RIBAO_STATE_DIR="$STATE_DIR" "$PYTHON_BIN" fetch_news.py --date "$YMD" "${FETCH_OPEN_ARGS[@]}"; then
    MANIFEST --step fetch --status ok
  else
    rc=$?
    MANIFEST --step fetch --status fail --detail "exit=$rc"
    exit "$rc"
  fi
fi
if [ ! -f "$DRAFT" ]; then
  MANIFEST --step fetch --status fail --detail "抓取正常退出但未生成目标草稿 $(basename "$DRAFT")（fetch 计算的窗口日期与目标不一致？）"
  echo "[$(date '+%F %T')] ❌ 未找到今日草稿：$DRAFT"
  exit 4
fi

echo "[$(date '+%F %T')] 2/3 Codex 浓缩成稿..."
CODEX_DAILY_MODEL="${RIBAO_CODEX_MODEL:-gpt-5.6-sol}"
CODEX_BIN="$(command -v codex || true)"
CODEX_RUNTIME="$SKILL_DIR/codex_external.py"
CODEX_CONTROL_HOME="${RIBAO_CODEX_CONTROL_HOME:-$HOME/Library/Application Support/beijing-daily-brief/codex-control}"
if [ -z "$CODEX_BIN" ]; then
  MANIFEST --step compose --status fail --detail "codex executable not found"
  exit 6
fi

# 硬拒绝旧的系统盘暂存机制。准备器会再次核验卷标、realpath、剩余空间和
# 全部工作流目录，任一路径落到 /Users 或系统临时目录都会失败关闭。
if [ -n "${RIBAO_CODEX_STAGING_BASE:-}" ]; then
  MANIFEST --step compose --status fail --detail "legacy local Codex staging is forbidden"
  exit 7
fi
export RIBAO_CODEX_HOME="${RIBAO_CODEX_HOME:-$STATE_DIR/codex-home}"
export RIBAO_CODEX_WORK_BASE="${RIBAO_CODEX_WORK_BASE:-$STATE_DIR/codex-work}"
CODEX_WORK_DIR="$("$PYTHON_BIN" "$CODEX_RUNTIME" --root "$ROOT_DIR" prepare --date "$YMD")" || {
  rc=$?
  MANIFEST --step compose --status fail --detail "external runtime preparation failed exit=$rc"
  exit "$rc"
}
CODEX_RESULT="$CODEX_WORK_DIR/result.json"
CODEX_EVENTS="$CODEX_WORK_DIR/codex.events.jsonl"
CODEX_STDERR="$CODEX_WORK_DIR/codex.stderr.log"
CANDIDATE="$CODEX_WORK_DIR/report.md"
CANDIDATE_CHECK="$CODEX_WORK_DIR/check.md"
REPAIR_PLAN="$CODEX_WORK_DIR/repair-plan.json"
QA_SKIP_STATE="$STATE_DIR/qa-skip-${YMD}.json"
IMAGE_RUNTIME="$SKILL_DIR/report_images.py"
IMAGE_PREFETCH_PID=""
IMAGE_PREFETCH_RC=0

cleanup_image_prefetch() {
  if [ -n "$IMAGE_PREFETCH_PID" ] && kill -0 "$IMAGE_PREFETCH_PID" 2>/dev/null; then
    kill "$IMAGE_PREFETCH_PID" 2>/dev/null || true
    wait "$IMAGE_PREFETCH_PID" 2>/dev/null || true
  fi
}
trap cleanup_image_prefetch EXIT

echo "[$(date '+%F %T')] Codex 外接盘专用工作区：$CODEX_WORK_DIR"

# 配图候选下载与正文写作并行进行。这里只写入外接工作区缓存；任何下载失败
# 都不得改变正文生成或质检结果。视觉相关性判断在正文质检通过后单独执行。
if [ "${RIBAO_ENABLE_REPORT_IMAGES:-1}" = "1" ]; then
  "$PYTHON_BIN" "$IMAGE_RUNTIME" --root "$ROOT_DIR" prefetch \
    --draft "$DRAFT" --work-dir "$CODEX_WORK_DIR" \
    >"$CODEX_WORK_DIR/image-prefetch.stdout.log" \
    2>"$CODEX_WORK_DIR/image-prefetch.stderr.log" &
  IMAGE_PREFETCH_PID=$!
else
  MANIFEST --step images --status skipped --detail "RIBAO_ENABLE_REPORT_IMAGES=0"
fi

if CANDIDATE="$("$PYTHON_BIN" "$CODEX_RUNTIME" --root "$ROOT_DIR" compose \
  --date "$YMD" --work-dir "$CODEX_WORK_DIR" --draft "$DRAFT" \
  --codex "$CODEX_BIN" --control-home "$CODEX_CONTROL_HOME" --model "$CODEX_DAILY_MODEL")"; then
  :
else
  rc=$?
  MANIFEST --step compose --status fail --detail "prompt-only codex exit=$rc external_work_dir=$CODEX_WORK_DIR"
  exit "$rc"
fi
COMPOSE_MODE="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("mode", "unknown"))' "$CODEX_WORK_DIR/compose-mode.json" 2>/dev/null || echo unknown)"
MANIFEST --step compose --status ok \
  --detail "external_work_dir=$CODEX_WORK_DIR mode=$COMPOSE_MODE same_run=true ephemeral=true stdin_only=true model_tools=disabled" \
  --kv "compose_mode=$COMPOSE_MODE"

echo "[$(date '+%F %T')] 3/3 质检..."
QC_RC=1
REPAIR_ROUND=0
PLAN_ACTION=0
QC_TEST_ARGS=()
REPAIR_TEST_ARGS=()
if [ "${RIBAO_TEST_MODE:-0}" = "1" ]; then
  # Only explicit v2 test runs receive deterministic source-bound replacement
  # plans.  The formal legacy path keeps its existing QA/repair contract.
  QC_TEST_ARGS=(--repair-plan "$REPAIR_PLAN")
  REPAIR_TEST_ARGS=(--repair-plan "$REPAIR_PLAN")
fi
while [ "$REPAIR_ROUND" -le 2 ]; do
  QC_RC=0
  "$PYTHON_BIN" "$SKILL_DIR/check_report.py" --draft "$DRAFT" --report "$CANDIDATE" --write "$CANDIDATE_CHECK" \
    --qa-skip-state "$QA_SKIP_STATE" "${QC_TEST_ARGS[@]}" || QC_RC=$?
  PLAN_ACTION=0
  if [ "${RIBAO_TEST_MODE:-0}" = "1" ] && [ -s "$REPAIR_PLAN" ]; then
    PLAN_ACTION="$({ "$PYTHON_BIN" -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(1 if p.get("schema_version") == 2 and p.get("requires_repair") else 0)' "$REPAIR_PLAN"; } 2>/dev/null || echo 1)"
  fi
  if [ "$QC_RC" -eq 0 ] && [ "$PLAN_ACTION" -eq 0 ]; then
    break
  fi
  if [ "$REPAIR_ROUND" -ge 2 ]; then
    if [ "$PLAN_ACTION" -eq 1 ]; then
      QC_RC=1
      echo "测试版候选交付合同在 2 轮修复后仍未完成" >>"$CANDIDATE_CHECK"
    fi
    break
  fi
  REPAIR_ROUND=$((REPAIR_ROUND + 1))
  echo "[$(date '+%F %T')] 质检或候选交付合同未完成，将确定性修复计划通过内存管道回传 Codex（${REPAIR_ROUND}/2）..."
  PRE_REPAIR_CANDIDATE="$CODEX_WORK_DIR/report.before-repair-${REPAIR_ROUND}.md"
  cp "$CANDIDATE" "$PRE_REPAIR_CANDIDATE"
  PRE_REPAIR_ITEMS="$(grep -c '^### ' "$PRE_REPAIR_CANDIDATE" 2>/dev/null || true)"
  if CANDIDATE="$("$PYTHON_BIN" "$CODEX_RUNTIME" --root "$ROOT_DIR" repair \
    --date "$YMD" --work-dir "$CODEX_WORK_DIR" --draft "$DRAFT" \
    --candidate "$CANDIDATE" --check "$CANDIDATE_CHECK" \
    "${REPAIR_TEST_ARGS[@]}" \
    --codex "$CODEX_BIN" --control-home "$CODEX_CONTROL_HOME" --model "$CODEX_DAILY_MODEL")"; then
    :
  else
    rc=$?
    MANIFEST --step compose_repair --status fail --detail "round=$REPAIR_ROUND exit=$rc external_work_dir=$CODEX_WORK_DIR"
    exit "$rc"
  fi
  POST_REPAIR_ITEMS="$(grep -c '^### ' "$CANDIDATE" 2>/dev/null || true)"
  if [ "${RIBAO_TEST_MODE:-0}" = "1" ] \
    && [ "${PRE_REPAIR_ITEMS:-0}" -ge 10 ] \
    && [ "${POST_REPAIR_ITEMS:-0}" -lt 10 ]; then
    echo "[$(date '+%F %T')] WARN: 修复稿从 ${PRE_REPAIR_ITEMS} 条异常缩减为 ${POST_REPAIR_ITEMS} 条，已回滚上一候选稿。" >&2
    cp "$PRE_REPAIR_CANDIDATE" "$CANDIDATE"
  fi
done
VERDICT="$(grep -m1 '结论：' "$CANDIDATE_CHECK" 2>/dev/null | sed -E 's/.*结论：[[:space:]]*//' | tr -d '[:space:]')"
MANIFEST --step qc --status "$([ "$QC_RC" -eq 0 ] && echo ok || echo fail)" --detail "verdict=${VERDICT:-无} exit=$QC_RC repair_rounds=$REPAIR_ROUND" --kv "verdict=${VERDICT:-}"
if [ "$QC_RC" -ne 0 ]; then
  exit "$QC_RC"
fi

# 文本 QA 已通过后才处理配图；不会为了图片重跑正文或把文本终态改成失败。
if [ "${RIBAO_ENABLE_REPORT_IMAGES:-1}" = "1" ]; then
  if [ -n "$IMAGE_PREFETCH_PID" ]; then
    set +e
    wait "$IMAGE_PREFETCH_PID"
    IMAGE_PREFETCH_RC=$?
    set -e
    IMAGE_PREFETCH_PID=""
  fi

  IMAGE_APPLY_RC=0
  "$PYTHON_BIN" "$IMAGE_RUNTIME" --root "$ROOT_DIR" apply \
    --draft "$DRAFT" --report "$CANDIDATE" --work-dir "$CODEX_WORK_DIR" \
    --codex "$CODEX_BIN" --control-home "$CODEX_CONTROL_HOME" --model "$CODEX_DAILY_MODEL" \
    >"$CODEX_WORK_DIR/image-apply.stdout.log" \
    2>"$CODEX_WORK_DIR/image-apply.stderr.log" || IMAGE_APPLY_RC=$?

  IMAGE_PLAN="$CODEX_WORK_DIR/image-plan.json"
  IMAGE_STATUS="$({ "$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("status", "warn"))' "$IMAGE_PLAN"; } 2>/dev/null || echo warn)"
  IMAGE_INSERTED="$({ "$PYTHON_BIN" -c 'import json,sys; print(int(json.load(open(sys.argv[1], encoding="utf-8")).get("inserted", 0)))' "$IMAGE_PLAN"; } 2>/dev/null || echo 0)"
  if [ "$IMAGE_PREFETCH_RC" -ne 0 ] || [ "$IMAGE_APPLY_RC" -ne 0 ] || [ "$IMAGE_STATUS" = "warn" ]; then
    MANIFEST --step images --status warn \
      --detail "media_only=true prefetch_exit=$IMAGE_PREFETCH_RC apply_exit=$IMAGE_APPLY_RC status=$IMAGE_STATUS inserted=$IMAGE_INSERTED text_unchanged_on_failure=true"
  elif [ "$IMAGE_STATUS" = "no_report_candidates" ] || [ "$IMAGE_STATUS" = "no_relevant_images" ]; then
    MANIFEST --step images --status skipped \
      --detail "media_only=true status=$IMAGE_STATUS inserted=0"
  else
    MANIFEST --step images --status ok \
      --detail "media_only=true status=$IMAGE_STATUS inserted=$IMAGE_INSERTED"
  fi
fi

# 候选稿与正式稿在同一外接 APFS 卷；质检通过后才由可信包装层原子替换。
"$PYTHON_BIN" "$CODEX_RUNTIME" --root "$ROOT_DIR" validate-publish \
  --work-dir "$CODEX_WORK_DIR" --report "$REPORT" --check "$CHECK"
mv -f "$CANDIDATE" "$REPORT"
mv -f "$CANDIDATE_CHECK" "$CHECK"

osascript -e "display notification \"已生成：$(basename "$REPORT")\" with title \"资讯日报完成\"" >/dev/null 2>&1 || true

echo "[$(date '+%F %T')] 完成。"
