#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
LABEL="com.example.beijing-daily-brief-guard"
RUNTIME_DIR="${RIBAO_GUARD_RUNTIME_DIR:-$HOME/Library/Application Support/beijing-daily-brief}"
CONTROL_HOME="$RUNTIME_DIR/codex-control"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$ROOT_DIR/automation/$LABEL.plist.template"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Missing $CONFIG_FILE. Copy config.env.example to config.env and configure it." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

if [ -z "${RIBAO_DATA_ROOT:-}" ] || [ ! -d "$RIBAO_DATA_ROOT" ]; then
  echo "RIBAO_DATA_ROOT must be an existing directory on an external volume." >&2
  exit 1
fi

/usr/bin/python3 - "$RIBAO_DATA_ROOT" <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1]).expanduser().resolve(strict=True)
mount = path
while mount.parent != mount and not os.path.ismount(mount):
    mount = mount.parent
if mount == pathlib.Path("/"):
    raise SystemExit("RIBAO_DATA_ROOT is on the system root mount; refusing installation")
PY

mkdir -p "$RUNTIME_DIR" "$CONTROL_HOME" "$HOME/Library/LaunchAgents"
cp "$ROOT_DIR/资讯日报-skill/ribao_guard.py" "$RUNTIME_DIR/ribao_guard.py"
cp "$ROOT_DIR/资讯日报-skill/beijing_time.py" "$RUNTIME_DIR/beijing_time.py"
cp "$ROOT_DIR/资讯日报-skill/run_guard.sh" "$RUNTIME_DIR/run_guard.sh"
chmod 755 "$RUNTIME_DIR/ribao_guard.py" "$RUNTIME_DIR/beijing_time.py" "$RUNTIME_DIR/run_guard.sh"

cp "$ROOT_DIR/资讯日报-skill/config/codex-ribao-prompt-only.config.toml" "$CONTROL_HOME/config.toml"
cp "$ROOT_DIR/资讯日报-skill/config/report-output.schema.json" "$CONTROL_HOME/report-output.schema.json"
if [ ! -f "${RIBAO_EXISTING_CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
  echo "Codex auth.json not found. Log in with Codex CLI first." >&2
  exit 1
fi
if [ -e "$CONTROL_HOME/auth.json" ] && [ ! -L "$CONTROL_HOME/auth.json" ]; then
  echo "$CONTROL_HOME/auth.json exists and is not a symlink; refusing overwrite." >&2
  exit 1
fi
ln -sfn "${RIBAO_EXISTING_CODEX_HOME:-$HOME/.codex}/auth.json" "$CONTROL_HOME/auth.json"
chmod 444 "$CONTROL_HOME/config.toml" "$CONTROL_HOME/report-output.schema.json"

cp "$CONFIG_FILE" "$RUNTIME_DIR/guard.env"
{
  printf '\nRIBAO_PROJECT_ROOT=%q\n' "$ROOT_DIR"
  printf 'RIBAO_DATA_ROOT=%q\n' "$RIBAO_DATA_ROOT"
  printf 'RIBAO_CODEX_CONTROL_HOME=%q\n' "$CONTROL_HOME"
} >> "$RUNTIME_DIR/guard.env"
chmod 600 "$RUNTIME_DIR/guard.env"

ACTIVATE_YMD="$(RIBAO_INSTALL_RUNTIME="$RUNTIME_DIR" PYTHONPATH="$RUNTIME_DIR" /usr/bin/python3 -c 'import datetime as d, os; from pathlib import Path; from beijing_time import TrustedClock,to_beijing; n,_=TrustedClock(Path(os.environ["RIBAO_INSTALL_RUNTIME"])/"trusted-time.json").now(); b=to_beijing(n); day=b.date() if b.time()<d.time(13,30) else b.date()+d.timedelta(days=1); print(day.strftime("%Y%m%d"))')"
printf 'RIBAO_GUARD_ACTIVATE_YMD=%s\n' "$ACTIVATE_YMD" >> "$RUNTIME_DIR/guard.env"

/usr/bin/python3 - "$TEMPLATE" "$PLIST_DST" "$ROOT_DIR" "$RUNTIME_DIR" "$PATH" <<'PY'
from pathlib import Path
import sys
template, output, root, runtime, path = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
for key, value in {
    "__PROJECT_ROOT__": root,
    "__RUNTIME_DIR__": runtime,
    "__PATH__": path,
}.items():
    text = text.replace(key, value)
Path(output).write_text(text, encoding="utf-8")
PY
/usr/bin/plutil -lint "$PLIST_DST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "Installed $LABEL. Beijing activation date: $ACTIVATE_YMD"
echo "This installer does not remove other cron/launchd jobs; disable any older daily-brief scheduler before using it."
