#!/bin/bash
# 低频同步 weread-omni 测试源；只写 Git 外的本地快照、正文库、状态和 Feed。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_ROOT="${RIBAO_WEREAD_OMNI_DATA_ROOT:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test}"
STATE_DIR="${RIBAO_WEREAD_OMNI_OBSERVATION_DIR:-$DATA_ROOT/observations}"
PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}"
YMD="${1:-$("$PYTHON_BIN" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"))
PY
)}"

case "$YMD" in
  ????????) ;;
  *) echo "目标日期必须是 YYYYMMDD" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
STAMP="$(date '+%Y%m%d-%H%M%S')"
LOG="$STATE_DIR/sync-${YMD}-${STAMP}.json"
LATEST="$STATE_DIR/latest-${YMD}.json"

set +e
node "$SCRIPT_DIR/sync_window.mjs" "$YMD" >"$LOG" 2>&1
RC=$?
set -e

# A deterministic pointer lets the daily wrapper assess exactly this target
# window without guessing which timestamped observation is newest.
LATEST_TMP="$STATE_DIR/.latest-${YMD}.$$"
cp "$LOG" "$LATEST_TMP"
mv "$LATEST_TMP" "$LATEST"

"$PYTHON_BIN" - "$LOG" "$RC" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
rc = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {}
summary = {
    "status": payload.get("status", "invalid_output"),
    "source": payload.get("source", "weread_omni"),
    "exit_code": rc,
    "account_count": payload.get("subscriptionCount"),
    "item_count": payload.get("windowItemCount"),
    "fresh_source_count": payload.get("freshSources"),
    "stale_source_count": payload.get("staleSources"),
    "failure_class": payload.get("failureClass") or next(
        iter((payload.get("failureClasses") or {}).keys()), None
    ),
    "target_date": payload.get("targetDate"),
}
print(json.dumps(summary, ensure_ascii=False))
PY

exit "$RC"
