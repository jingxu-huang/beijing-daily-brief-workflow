#!/bin/bash
# Install one bounded, test-only weread-omni list probe. No Codex task and no Feishu delivery.

set -euo pipefail

AUTOMATION_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$AUTOMATION_DIR/.." && pwd)"
PROBE_SCRIPT="$PROJECT_ROOT/资讯日报-skill/weread-omni/overnight_probe.mjs"
NODE_BIN="$(command -v node)"
DATA_ROOT="${RIBAO_WEREAD_OMNI_DATA_ROOT:-$HOME/Library/Application Support/beijing-daily-brief/weread-omni-test}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PROBE_PLIST="$LAUNCH_AGENTS_DIR/com.mico.ribao-weread-overnight-probe.plist"
AWAKE_PLIST="$LAUNCH_AGENTS_DIR/com.mico.ribao-weread-overnight-awake.plist"
LOG_DIR="$DATA_ROOT/overnight-probes/logs"
PROBE_TEMPLATE="$AUTOMATION_DIR/com.mico.ribao-weread-overnight-probe.plist"
AWAKE_TEMPLATE="$AUTOMATION_DIR/com.mico.ribao-weread-overnight-awake.plist"

if [ -z "$NODE_BIN" ] || [ ! -x "$NODE_BIN" ]; then
  echo "node is required" >&2
  exit 2
fi
if [ ! -f "$PROBE_SCRIPT" ]; then
  echo "probe script is missing" >&2
  exit 2
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"
INIT_JSON="$($NODE_BIN "$PROBE_SCRIPT" init)"
DURATION_SECONDS="$($NODE_BIN -e 'const x=JSON.parse(process.argv[1]); const d=Math.max(60,Math.ceil((Date.parse(x.stop_at)-Date.now())/1000)); process.stdout.write(String(d));' "$INIT_JSON")"
PROBE_ARGS_JSON="$($NODE_BIN -e 'process.stdout.write(JSON.stringify(process.argv.slice(1)))' "$NODE_BIN" "$PROBE_SCRIPT" step)"
AWAKE_ARGS_JSON="$($NODE_BIN -e 'process.stdout.write(JSON.stringify(process.argv.slice(1)))' /usr/bin/caffeinate -s -t "$DURATION_SECONDS")"

PROBE_TMP="$(mktemp /private/tmp/ribao-overnight-probe.XXXXXX.plist)"
AWAKE_TMP="$(mktemp /private/tmp/ribao-overnight-awake.XXXXXX.plist)"
trap 'rm -f "$PROBE_TMP" "$AWAKE_TMP"' EXIT
cp "$PROBE_TEMPLATE" "$PROBE_TMP"
cp "$AWAKE_TEMPLATE" "$AWAKE_TMP"

/usr/bin/plutil -replace ProgramArguments -json "$PROBE_ARGS_JSON" "$PROBE_TMP"
/usr/bin/plutil -replace EnvironmentVariables.RIBAO_WEREAD_OMNI_DATA_ROOT -string "$DATA_ROOT" "$PROBE_TMP"
/usr/bin/plutil -replace EnvironmentVariables.RIBAO_OVERNIGHT_PROBE_PLIST -string "$PROBE_PLIST" "$PROBE_TMP"
/usr/bin/plutil -replace EnvironmentVariables.RIBAO_OVERNIGHT_AWAKE_PLIST -string "$AWAKE_PLIST" "$PROBE_TMP"
/usr/bin/plutil -replace StandardOutPath -string "$LOG_DIR/probe.stdout.log" "$PROBE_TMP"
/usr/bin/plutil -replace StandardErrorPath -string "$LOG_DIR/probe.stderr.log" "$PROBE_TMP"
/usr/bin/plutil -replace ProgramArguments -json "$AWAKE_ARGS_JSON" "$AWAKE_TMP"
/usr/bin/plutil -replace StandardOutPath -string "$LOG_DIR/awake.stdout.log" "$AWAKE_TMP"
/usr/bin/plutil -replace StandardErrorPath -string "$LOG_DIR/awake.stderr.log" "$AWAKE_TMP"
/usr/bin/plutil -lint "$PROBE_TMP" "$AWAKE_TMP"

/bin/launchctl bootout "gui/$UID" "$PROBE_PLIST" >/dev/null 2>&1 || true
/bin/launchctl bootout "gui/$UID" "$AWAKE_PLIST" >/dev/null 2>&1 || true
/usr/bin/install -m 600 "$PROBE_TMP" "$PROBE_PLIST"
/usr/bin/install -m 600 "$AWAKE_TMP" "$AWAKE_PLIST"
/bin/launchctl bootstrap "gui/$UID" "$AWAKE_PLIST"
/bin/launchctl bootstrap "gui/$UID" "$PROBE_PLIST"

echo "$INIT_JSON"
echo "installed_label=com.mico.ribao-weread-overnight-probe"
echo "awake_until_report=ac_power_only"
