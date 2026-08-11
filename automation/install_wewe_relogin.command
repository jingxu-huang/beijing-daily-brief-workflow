#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
LABEL="com.example.beijing-daily-brief-wewe-relogin"
APP_DIR="$HOME/Library/Application Support/beijing-daily-brief/wewe-relogin"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Missing $CONFIG_FILE. Copy config.env.example to config.env first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

if [ -z "${WEWE_AUTH_CODE:-}" ]; then
  echo "Set WEWE_AUTH_CODE in config.env before installing the relogin service." >&2
  exit 1
fi
command -v cloudflared >/dev/null 2>&1 || {
  echo "cloudflared is required. Install it, then rerun this installer." >&2
  exit 1
}
/usr/bin/python3 -c 'import qrcode, qrcode.image.svg' >/dev/null 2>&1 || {
  echo "Python package qrcode is required: python3 -m pip install --user qrcode" >&2
  exit 1
}

RELOGIN_TOKEN="${RELOGIN_TOKEN:-$(openssl rand -hex 20)}"
RELOGIN_ADMIN_TOKEN="${RELOGIN_ADMIN_TOKEN:-$(openssl rand -hex 20)}"
if ! grep -q '^RELOGIN_TOKEN=.' "$CONFIG_FILE"; then
  printf '\nRELOGIN_TOKEN=%s\n' "$RELOGIN_TOKEN" >> "$CONFIG_FILE"
fi
if ! grep -q '^RELOGIN_ADMIN_TOKEN=.' "$CONFIG_FILE"; then
  printf 'RELOGIN_ADMIN_TOKEN=%s\n' "$RELOGIN_ADMIN_TOKEN" >> "$CONFIG_FILE"
fi

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"
cp "$ROOT_DIR/资讯日报-skill/wewe_relogin_server.py" "$APP_DIR/wewe_relogin_server.py"
chmod 755 "$APP_DIR/wewe_relogin_server.py"
{
  printf 'RELOGIN_TOKEN=%q\n' "$RELOGIN_TOKEN"
  printf 'RELOGIN_ADMIN_TOKEN=%q\n' "$RELOGIN_ADMIN_TOKEN"
  printf 'RELOGIN_PORT=%q\n' "${RELOGIN_PORT:-4001}"
  printf 'WEWE_RSS_URL=%q\n' "${WEWE_RSS_URL:-http://127.0.0.1:4000}"
  printf 'WEWE_AUTH_CODE=%q\n' "$WEWE_AUTH_CODE"
  printf 'TUNNEL_TTL_MINUTES=%q\n' "${TUNNEL_TTL_MINUTES:-360}"
} > "$APP_DIR/relogin.env"
chmod 600 "$APP_DIR/relogin.env"

/usr/bin/python3 - "$PLIST_DST" "$LABEL" "$APP_DIR" "$PATH" <<'PY'
import plistlib, sys
output, label, app_dir, path = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": ["/usr/bin/env", "python3", f"{app_dir}/wewe_relogin_server.py"],
    "EnvironmentVariables": {
        "PATH": path,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "RELOGIN_ENV_FILE": f"{app_dir}/relogin.env",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 15,
    "StandardOutPath": "/dev/null",
    "StandardErrorPath": "/dev/null",
}
with open(output, "wb") as handle:
    plistlib.dump(payload, handle)
PY
/usr/bin/plutil -lint "$PLIST_DST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"
echo "Installed $LABEL. Re-run automation/install_guard.command so the guard receives the generated admin token."
