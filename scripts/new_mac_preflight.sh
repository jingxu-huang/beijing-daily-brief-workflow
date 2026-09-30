#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
FAILED=0

ok() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; FAILED=1; }
optional() { printf '[OPTIONAL] %s\n' "$1"; }

if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi
ENABLE_FEISHU="${RIBAO_ENABLE_FEISHU:-0}"
ENABLE_WEWE="${RIBAO_ENABLE_WEWE:-0}"
case "$ENABLE_FEISHU:$ENABLE_WEWE" in
  0:0|0:1|1:0|1:1) ;;
  *) fail "RIBAO_ENABLE_FEISHU 与 RIBAO_ENABLE_WEWE 必须为 0 或 1" ;;
esac

if [ "$(uname -s)" = "Darwin" ]; then
  ok "macOS"
else
  fail "定时守门器需要 macOS launchd"
fi

for command_name in python3 docker codex; do
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "$command_name 已安装"
  else
    fail "$command_name 未安装或不在 PATH"
  fi
done
if [ "$ENABLE_FEISHU" = "1" ]; then
  if ! command -v lark-cli >/dev/null 2>&1; then
    fail "已启用飞书，但 lark-cli 未安装或不在 PATH"
  elif lark-cli auth status 2>/dev/null | grep -q 'User identity: ready'; then
    ok "飞书能力已启用，lark-cli user 身份就绪"
  else
    fail "已启用飞书，但 lark-cli user 身份未就绪"
  fi
else
  optional "飞书能力未启用；本地生成与质检不受影响"
fi
if [ "$ENABLE_WEWE" = "1" ]; then
  if command -v cloudflared >/dev/null 2>&1; then
    ok "WeWe 能力已启用，cloudflared 已安装"
  else
    fail "已启用 WeWe，但 cloudflared 未安装或不在 PATH"
  fi
else
  optional "WeWe 能力未启用；守门器会明确排除公众号增强源"
fi

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
  if curl -fsS http://127.0.0.1:1200/healthz >/dev/null 2>&1; then
    ok "本地公开源适配器健康"
  else
    fail "本地公开源适配器不可用；运行 docker compose -f public-sources/docker-compose.yml up -d"
  fi
else
  fail "Docker daemon 不可用；请启动 Docker Desktop"
fi

if [ -f "$CONFIG_FILE" ]; then
  ok "config.env 已创建"
  for variable_name in RIBAO_DATA_ROOT RIBAO_PYTHON_BIN; do
    if [ -n "${!variable_name:-}" ]; then
      ok "$variable_name 已配置"
    else
      fail "$variable_name 未配置"
    fi
  done
  if [ "$ENABLE_WEWE" = "1" ] && [ -n "${WEWE_AUTH_CODE:-}" ]; then
    ok "WEWE_AUTH_CODE 已配置"
  elif [ "$ENABLE_WEWE" = "1" ]; then
    fail "已启用 WeWe，但 WEWE_AUTH_CODE 未配置"
  else
    optional "WEWE_AUTH_CODE 未要求；WeWe 能力未启用"
  fi
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

printf '\n新机预检通过。公开源本地日报可运行；飞书与 WeWe 可选能力按需配置。\n'
