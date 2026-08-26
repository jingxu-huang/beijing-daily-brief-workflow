#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
fi

say() { printf '\n==> %s\n' "$1"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This one-command installer supports macOS only." >&2
  exit 1
fi

say "安装系统依赖"
if ! command -v brew >/dev/null 2>&1; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] install Homebrew from https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
  else
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
fi

if [ -x /opt/homebrew/bin/brew ]; then
  BREW_BIN=/opt/homebrew/bin/brew
elif [ -x /usr/local/bin/brew ]; then
  BREW_BIN=/usr/local/bin/brew
else
  BREW_BIN="$(command -v brew || true)"
fi
if [ -z "$BREW_BIN" ] && [ "$DRY_RUN" -ne 1 ]; then
  echo "Homebrew installation did not produce a usable brew command." >&2
  exit 1
fi
BREW_BIN="${BREW_BIN:-brew}"
export PATH="$(dirname "$BREW_BIN"):$PATH"

for formula in python@3.12 node cloudflared; do
  if ! "$BREW_BIN" list --versions "$formula" >/dev/null 2>&1; then
    run "$BREW_BIN" install "$formula"
  fi
done

if [ ! -d /Applications/Docker.app ]; then
  run "$BREW_BIN" install --cask docker
fi
if ! command -v lark-cli >/dev/null 2>&1; then
  run npm install -g @larksuite/cli
fi
if ! command -v codex >/dev/null 2>&1; then
  run npm install -g @openai/codex
fi

PYTHON_BOOTSTRAP="$(command -v python3.12 || true)"
if [ -z "$PYTHON_BOOTSTRAP" ]; then
  PYTHON_BOOTSTRAP="$($BREW_BIN --prefix python@3.12)/bin/python3.12"
fi

say "创建 Python 环境与本地数据目录"
if [ ! -x "$ROOT_DIR/.venv/bin/python3" ]; then
  run "$PYTHON_BOOTSTRAP" -m venv "$ROOT_DIR/.venv"
fi
run "$ROOT_DIR/.venv/bin/python3" -m pip install --upgrade pip
run "$ROOT_DIR/.venv/bin/python3" -m pip install -r "$ROOT_DIR/资讯日报-skill/requirements.txt"
run "$ROOT_DIR/.venv/bin/python3" -m pip install qrcode

DATA_ROOT="${RIBAO_DATA_ROOT:-$HOME/Library/Application Support/beijing-daily-brief-data}"
CONFIG_FILE="$ROOT_DIR/config.env"
run mkdir -p "$DATA_ROOT"
if [ ! -f "$CONFIG_FILE" ]; then
  run cp "$ROOT_DIR/config.env.example" "$CONFIG_FILE"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] configure $CONFIG_FILE with local data, Python and a generated WeWe AuthCode"
else
  WEWE_CODE="$(openssl rand -hex 24)"
  "$ROOT_DIR/.venv/bin/python3" - "$CONFIG_FILE" "$DATA_ROOT" "$ROOT_DIR/.venv/bin/python3" "$WEWE_CODE" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
values = {
    "RIBAO_DATA_ROOT": sys.argv[2],
    "RIBAO_PYTHON_BIN": sys.argv[3],
    "RIBAO_EXISTING_CODEX_HOME": str(Path.home() / ".codex"),
}
text = path.read_text(encoding="utf-8")
for key, value in values.items():
    line = f'{key}="{value}"'
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    text = re.sub(pattern, line, text) if re.search(pattern, text) else text + "\n" + line + "\n"
if not re.search(r"(?m)^WEWE_AUTH_CODE=.+$", text):
    text = re.sub(r"(?m)^WEWE_AUTH_CODE=.*$", f"WEWE_AUTH_CODE={sys.argv[4]}", text)
if not re.search(r"(?m)^RIBAO_ALLOW_SYSTEM_DATA_ROOT=", text):
    text += "\n# User selected local storage for this installation.\nRIBAO_ALLOW_SYSTEM_DATA_ROOT=1\n"
path.write_text(text, encoding="utf-8")
PY
  chmod 600 "$CONFIG_FILE"
fi

say "把 skill 安装到 Codex"
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
SKILL_LINK="$CODEX_SKILLS_DIR/zixun-ribao"
run mkdir -p "$CODEX_SKILLS_DIR"
if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
  echo "$SKILL_LINK already exists and is not a symlink; leaving it untouched." >&2
  echo "Move that directory aside, then run this same command again." >&2
  exit 1
fi
run ln -sfn "$ROOT_DIR/资讯日报-skill" "$SKILL_LINK"

say "启动固定版本 WeWe RSS 镜像"
if [ ! -f "$ROOT_DIR/资讯日报-skill/wewe-rss/.env" ] && [ -f "$CONFIG_FILE" ]; then
  run cp "$CONFIG_FILE" "$ROOT_DIR/资讯日报-skill/wewe-rss/.env"
fi
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] start Docker Desktop and pull WeWe RSS 2.6.1"
else
  open -gja Docker
  for _ in $(seq 1 40); do
    docker info >/dev/null 2>&1 && break
    sleep 3
  done
  docker info >/dev/null 2>&1 || {
    echo "Docker Desktop is not ready. Start it, then run this same command again." >&2
    exit 1
  }
  docker compose --env-file "$ROOT_DIR/资讯日报-skill/wewe-rss/.env" \
    -f "$ROOT_DIR/资讯日报-skill/wewe-rss/docker-compose.yml" pull
  docker compose --env-file "$ROOT_DIR/资讯日报-skill/wewe-rss/.env" \
    -f "$ROOT_DIR/资讯日报-skill/wewe-rss/docker-compose.yml" up -d
fi

say "检查必须的人工授权"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] check Codex auth and lark-cli bot identity"
else
  if [ ! -f "$HOME/.codex/auth.json" ]; then
    echo "Codex 尚未登录。请先在 Codex App/CLI 登录，然后重跑本命令。" >&2
    exit 2
  fi
  if ! lark-cli auth status 2>/dev/null | grep -q 'Bot identity: ready'; then
    echo "飞书 bot 身份尚未就绪。完成 lark-cli 配置后重跑本命令。" >&2
    lark-cli auth status || true
    exit 2
  fi
fi

say "安装扫码提醒和每日日报定时任务"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] install WeWe relogin service and daily guard"
else
  RIBAO_CONFIG_FILE="$CONFIG_FILE" bash "$ROOT_DIR/automation/install_wewe_relogin.command"
  RIBAO_CONFIG_FILE="$CONFIG_FILE" bash "$ROOT_DIR/automation/install_guard.command"
fi

say "最终自检"
if [ "$DRY_RUN" -eq 1 ]; then
  bash -n "$ROOT_DIR/bootstrap_new_mac.command"
  echo "Dry run passed."
  exit 0
fi
bash "$ROOT_DIR/scripts/new_mac_preflight.sh"

open "http://127.0.0.1:4000/dash/accounts"
printf '\n安装完成。\n'
printf '1. 当前打开的是 WeWe 账号页；首次必须新增账号并扫码。\n'
printf '2. 如 MIGRATION/wewe-share-links.txt 存在，在公众号源页逐行导入。\n'
printf '3. 完成扫码/导入后，再运行同一条命令做幂等复检。\n'
printf '4. Codex 重新启动后可直接使用 $zixun-ribao。\n'
