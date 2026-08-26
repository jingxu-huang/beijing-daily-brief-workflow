#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
FAILED=0

ok() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; FAILED=1; }

if [ "$(uname -s)" = "Darwin" ]; then
  ok "macOS"
else
  fail "定时守门器需要 macOS launchd"
fi

for command_name in python3 docker codex lark-cli cloudflared; do
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "$command_name 已安装"
  else
    fail "$command_name 未安装或不在 PATH"
  fi
done

PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}"
if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    ok "$PYTHON_BIN 版本满足 Python 3.10+"
  else
    fail "$PYTHON_BIN 版本低于 Python 3.10"
  fi
fi

if docker info >/dev/null 2>&1; then
  ok "Docker Desktop 正在运行"
else
  fail "Docker daemon 不可用；请启动 Docker Desktop"
fi

if [ -f "$CONFIG_FILE" ]; then
  ok "config.env 已创建"
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
  for variable_name in RIBAO_DATA_ROOT RIBAO_PYTHON_BIN WEWE_AUTH_CODE; do
    if [ -n "${!variable_name:-}" ]; then
      ok "$variable_name 已配置"
    else
      fail "$variable_name 未配置"
    fi
  done
  if [ -n "${RIBAO_DATA_ROOT:-}" ] && [ -d "$RIBAO_DATA_ROOT" ]; then
    ok "RIBAO_DATA_ROOT 目录存在"
  else
    fail "RIBAO_DATA_ROOT 目录不存在"
  fi
else
  fail "缺少 config.env；请从 config.env.example 复制并填写"
fi

if [ -f "${RIBAO_EXISTING_CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
  ok "Codex 登录状态存在"
else
  fail "未找到 Codex auth.json；请先登录 Codex CLI"
fi

if RIBAO_PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}" bash "$ROOT_DIR/scripts/verify.sh" >/dev/null; then
  ok "源码、测试和隐私审计通过"
else
  fail "scripts/verify.sh 未通过"
fi

if [ "$FAILED" -ne 0 ]; then
  printf '\n新机预检未通过，请先处理 FAIL 项。\n' >&2
  exit 1
fi

printf '\n新机预检通过。下一步可启动 WeWe、安装重登服务与日报守门器。\n'
