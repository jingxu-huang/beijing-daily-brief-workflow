#!/bin/bash
# 每日运行：抓取 → 让 Codex 浓缩成稿 → 质检 → 写回项目根目录的产物文件夹
# 由北京时间守门器在每天 15:00 窗口闭合后调用（也可手动 bash run_daily.sh 测试）
# 阶段1加固(2026-07-08)：目标日期显式贯穿（不再 ls -t 猜最新文件），每步写 run-manifest。
# UTF-8：确保中文路径与输出正常
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# cron 环境 PATH 很精简，显式补全 homebrew/python 的常见位置
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
# 关键：nvm 安装的新版 codex 放最前，避免用到 Homebrew 的旧版（取最高 node 版本）
NVM_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$NVM_BIN" ] && export PATH="$NVM_BIN:$PATH"

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
YMD="${RIBAO_TARGET_YMD:-$(python3 "$SKILL_DIR/fetch_news.py" --print-date)}"
MD="$(python3 -c 'import sys; y=sys.argv[1]; print(f"{int(y[4:6])}.{int(y[6:8])}")' "$YMD")"
DRAFT="$DRAFT_DIR/资讯日报-草稿-${YMD}.md"
REPORT="$REPORT_DIR/资讯类简报-${MD}日.md"
CHECK="$CHECK_DIR/资讯日报-质检-${YMD}.md"
MANIFEST() { python3 "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$STATE_DIR" "$@" || true; }

echo "[$(date '+%F %T')] 目标日期 ${YMD}（窗口：北京时间 前一日15:00—当日15:00，左闭右开）"
MANIFEST --step plan --status ok --detail "window=[前日15:00,当日15:00) draft=$(basename "$DRAFT") report=$(basename "$REPORT")"

# WeWe 只是增强源，不再是日报硬门槛。守门器会在 15:00 前完成最终判定并显式传入
# RIBAO_EXCLUDE_WEWE；只有手动运行没有该变量时，才做一次非阻塞探测并自动降级。
if [ -z "${RIBAO_EXCLUDE_WEWE+x}" ]; then
  echo "[$(date '+%F %T')] 0/3 WeWe RSS 单次探测（失败不阻断日报）..."
  set +e
  WEWE_PROBE_JSON="$(python3 -c 'import json,sys; sys.path.insert(0, sys.argv[1]); import wewe_preflight as w; r=w.probe_health(); print(json.dumps(r, ensure_ascii=False)); raise SystemExit(0 if r.get("status") in {"healthy","stale"} else 1)' "$SKILL_DIR")"
  WEWE_PROBE_RC=$?
  set -e
  if [ "$WEWE_PROBE_RC" -eq 0 ]; then
    export RIBAO_EXCLUDE_WEWE=0
    MANIFEST --step wewe_preflight --status ok --detail "$WEWE_PROBE_JSON"
  else
    export RIBAO_EXCLUDE_WEWE=1
    MANIFEST --step wewe_preflight --status warn --detail "WeWe 已降级排除：$WEWE_PROBE_JSON"
  fi
else
  MANIFEST --step wewe_preflight --status "$([ "$RIBAO_EXCLUDE_WEWE" = "1" ] && echo warn || echo ok)" \
    --detail "守门器判定：wewe_status=${RIBAO_WEWE_STATUS:-unknown} excluded=${RIBAO_EXCLUDE_WEWE}"
fi

echo "[$(date '+%F %T')] 1/3 抓取..."
if [ "${RIBAO_REUSE_DRAFT:-0}" = "1" ] && [ -s "$DRAFT" ]; then
  echo "[$(date '+%F %T')] 复用已冻结的目标窗口草稿：$(basename "$DRAFT")"
  MANIFEST --step fetch --status ok --detail "reused frozen draft"
else
  if RIBAO_REPORT_DIR="$REPORT_DIR" RIBAO_DRAFT_DIR="$DRAFT_DIR" RIBAO_STATE_DIR="$STATE_DIR" python3 fetch_news.py --date "$YMD"; then
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
CODEX_WORK_DIR="$(python3 "$CODEX_RUNTIME" --root "$ROOT_DIR" prepare --date "$YMD")" || {
  rc=$?
  MANIFEST --step compose --status fail --detail "external runtime preparation failed exit=$rc"
  exit "$rc"
}
CODEX_RESULT="$CODEX_WORK_DIR/result.json"
CODEX_EVENTS="$CODEX_WORK_DIR/codex.events.jsonl"
CODEX_STDERR="$CODEX_WORK_DIR/codex.stderr.log"
CANDIDATE="$CODEX_WORK_DIR/report.md"
CANDIDATE_CHECK="$CODEX_WORK_DIR/check.md"
echo "[$(date '+%F %T')] Codex 外接盘专用工作区：$CODEX_WORK_DIR"

if CANDIDATE="$(python3 "$CODEX_RUNTIME" --root "$ROOT_DIR" compose \
  --date "$YMD" --work-dir "$CODEX_WORK_DIR" --draft "$DRAFT" \
  --codex "$CODEX_BIN" --control-home "$CODEX_CONTROL_HOME" --model "$CODEX_DAILY_MODEL")"; then
  :
else
  rc=$?
  MANIFEST --step compose --status fail --detail "prompt-only codex exit=$rc external_work_dir=$CODEX_WORK_DIR"
  exit "$rc"
fi
MANIFEST --step compose --status ok --detail "external_work_dir=$CODEX_WORK_DIR ephemeral=true stdin_only=true model_tools=disabled"

echo "[$(date '+%F %T')] 3/3 质检..."
QC_RC=1
REPAIR_ROUND=0
while [ "$REPAIR_ROUND" -le 2 ]; do
  QC_RC=0
  python3 "$SKILL_DIR/check_report.py" --draft "$DRAFT" --report "$CANDIDATE" --write "$CANDIDATE_CHECK" || QC_RC=$?
  if [ "$QC_RC" -eq 0 ]; then
    break
  fi
  if [ "$REPAIR_ROUND" -ge 2 ]; then
    break
  fi
  REPAIR_ROUND=$((REPAIR_ROUND + 1))
  echo "[$(date '+%F %T')] 质检未通过，将必须修复项通过内存管道回传 Codex（${REPAIR_ROUND}/2）..."
  if CANDIDATE="$(python3 "$CODEX_RUNTIME" --root "$ROOT_DIR" repair \
    --date "$YMD" --work-dir "$CODEX_WORK_DIR" --draft "$DRAFT" \
    --candidate "$CANDIDATE" --check "$CANDIDATE_CHECK" \
    --codex "$CODEX_BIN" --control-home "$CODEX_CONTROL_HOME" --model "$CODEX_DAILY_MODEL")"; then
    :
  else
    rc=$?
    MANIFEST --step compose_repair --status fail --detail "round=$REPAIR_ROUND exit=$rc external_work_dir=$CODEX_WORK_DIR"
    exit "$rc"
  fi
done
VERDICT="$(grep -m1 '结论：' "$CANDIDATE_CHECK" 2>/dev/null | sed -E 's/.*结论：[[:space:]]*//' | tr -d '[:space:]')"
MANIFEST --step qc --status "$([ "$QC_RC" -eq 0 ] && echo ok || echo fail)" --detail "verdict=${VERDICT:-无} exit=$QC_RC repair_rounds=$REPAIR_ROUND" --kv "verdict=${VERDICT:-}"
if [ "$QC_RC" -ne 0 ]; then
  exit "$QC_RC"
fi

# 候选稿与正式稿在同一外接 APFS 卷；质检通过后才由可信包装层原子替换。
python3 "$CODEX_RUNTIME" --root "$ROOT_DIR" validate-publish \
  --work-dir "$CODEX_WORK_DIR" --report "$REPORT" --check "$CHECK"
mv -f "$CANDIDATE" "$REPORT"
mv -f "$CANDIDATE_CHECK" "$CHECK"

osascript -e "display notification \"已生成：$(basename "$REPORT")\" with title \"资讯日报完成\"" >/dev/null 2>&1 || true

echo "[$(date '+%F %T')] 完成。"
