#!/bin/bash
# Install only the isolated test matrix schedule.  It never modifies the
# formal guard or its launchd job.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(date +%z)" != "$(TZ=Asia/Shanghai date +%z)" ]; then
  echo "Test matrix uses launchd local 15:00. Set the Mac timezone to Asia/Shanghai first." >&2
  exit 2
fi
LABEL="com.example.beijing-daily-brief-test-matrix"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$ROOT_DIR/automation/$LABEL.plist.template"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Application Support/beijing-daily-brief/logs"
TEST_LOG="$HOME/Library/Application Support/beijing-daily-brief/logs/test-matrix.log"

python3 - "$TEMPLATE" "$PLIST_DST" "$ROOT_DIR" "$PATH" "$TEST_LOG" <<'PY'
from pathlib import Path
import sys
template, output, root, path, log = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
for key, value in {"__PROJECT_ROOT__": root, "__PATH__": path, "__TEST_LOG__": log}.items():
    text = text.replace(key, value)
Path(output).write_text(text, encoding="utf-8")
PY
/usr/bin/plutil -lint "$PLIST_DST"
uid="$(id -u)"
launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$PLIST_DST"
launchctl enable "gui/$uid/$LABEL"
echo "Installed $LABEL: every day at 15:00 Beijing local clock; formal guard unchanged."
