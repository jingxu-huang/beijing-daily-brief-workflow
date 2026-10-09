#!/bin/bash
# Resume both Feishu delivery schedules only after their existing index
# registries have been restored. Never create replacement index documents.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
CHECK_ONLY=0
case "${1:-}" in
  "") ;;
  --check) CHECK_ONLY=1 ;;
  *) echo "Usage: bash automation/install_parallel.command [--check]" >&2; exit 2 ;;
esac
issues=()
issue() { issues+=("$1"); }

if [ ! -f "$CONFIG_FILE" ]; then
  issue "缺少私有 config.env；复制 config.env.example 后在本机填写，切勿提交凭据。"
else
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

[ "${RIBAO_ENABLE_FEISHU:-0}" = "1" ] || issue "RIBAO_ENABLE_FEISHU 必须为 1，才能交付飞书文档。"
[ "${FEISHU_DOC_ONLY:-0}" = "1" ] || issue "FEISHU_DOC_ONLY 必须为 1（索引交付）。"
[ "${FEISHU_DAILY_INDEX:-0}" = "1" ] || issue "FEISHU_DAILY_INDEX 必须为 1（原位更新正式索引）。"
[ "${RIBAO_GROUP_MESSAGES:-1}" = "0" ] || issue "RIBAO_GROUP_MESSAGES 必须为 0（不发群消息）。"
[ "${FEISHU_CLI_IMPORT_AS:-user}" = "user" ] || issue "FEISHU_CLI_IMPORT_AS 必须为 user。"
[ "${FEISHU_CLI_PERMISSION_AS:-user}" = "user" ] || issue "FEISHU_CLI_PERMISSION_AS 必须为 user。"
[ -n "${FEISHU_REPORT_CHAT_ID:-}" ] || issue "缺少战略小组 FEISHU_REPORT_CHAT_ID；它用于授权，不用于发群消息。"
[ -n "${FEISHU_TEST_INDEX_CHAT_ID:-${FEISHU_REPORT_CHAT_ID:-}}" ] || issue "缺少测试索引的战略小组授权目标。"
SKILL_LINK="${CODEX_HOME:-$HOME/.codex}/skills/zixun-ribao"
if [ ! -L "$SKILL_LINK" ] || [ "$(readlink "$SKILL_LINK" 2>/dev/null || true)" != "$ROOT_DIR/资讯日报-skill" ]; then
  issue "Codex Skill 链接没有指向当前仓库；先运行 bootstrap_new_mac.command --prepare-only，勿覆盖其他工作区。"
fi
if [ -z "${RIBAO_PYTHON_BIN:-}" ] || [ ! -x "$RIBAO_PYTHON_BIN" ] \
   || ! "$RIBAO_PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  issue "RIBAO_PYTHON_BIN 必须指向可运行的 Python 3.10+ 虚拟环境。"
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  issue "Docker Desktop 未运行；本地公开源适配器无法工作。"
elif ! curl -fsS "${RIBAO_RSSHUB_BASE:-http://127.0.0.1:1200}/healthz" >/dev/null 2>&1; then
  issue "本地公开源适配器健康检查失败；先恢复 RSSHub。"
fi
[ -f "${RIBAO_EXISTING_CODEX_HOME:-$HOME/.codex}/auth.json" ] || issue "Codex CLI 尚未登录（缺少本机 auth.json）。"
if ! command -v codex >/dev/null 2>&1; then
  issue "Codex CLI 未安装或不在 PATH。"
elif ! CODEX_HOME="${RIBAO_EXISTING_CODEX_HOME:-$HOME/.codex}" codex login status >/dev/null 2>&1; then
  issue "Codex CLI 登录状态无效；请在本机重新登录。"
fi
if ! command -v lark-cli >/dev/null 2>&1; then
  issue "lark-cli 未安装或不在 PATH。"
else
  lark-cli whoami --as user >/dev/null 2>&1 \
    || issue "飞书 lark-cli 的 user 身份无法完成只读身份检查；请在安装任务的同一用户会话中授权。"
fi
if [ -z "${RIBAO_DATA_ROOT:-}" ] || [ ! -d "$RIBAO_DATA_ROOT" ]; then
  issue "RIBAO_DATA_ROOT 必须指向已存在的独立运行数据目录。"
else
  if [ -n "${RIBAO_PYTHON_BIN:-}" ] && [ -x "$RIBAO_PYTHON_BIN" ] \
     && ! "$RIBAO_PYTHON_BIN" - "$RIBAO_DATA_ROOT" <<'PY' >/dev/null 2>&1
import os
import pathlib
import sys

mount = pathlib.Path(sys.argv[1]).expanduser().resolve(strict=True)
while mount.parent != mount and not os.path.ismount(mount):
    mount = mount.parent
if mount == pathlib.Path("/") and os.getenv("RIBAO_ALLOW_SYSTEM_DATA_ROOT") != "1":
    raise SystemExit(1)
PY
  then
    issue "运行数据位于系统盘；请改用独立挂载盘，或明确设置 RIBAO_ALLOW_SYSTEM_DATA_ROOT=1。"
  fi
  TEST_BASE_ROOT="${RIBAO_TEST_DATA_ROOT:-$RIBAO_DATA_ROOT/测试版}"
  if [ "$(cd "$RIBAO_DATA_ROOT" && pwd -P)" = "$(cd "$TEST_BASE_ROOT" 2>/dev/null && pwd -P || printf '%s' "$TEST_BASE_ROOT")" ]; then
    issue "测试版运行目录不能等于正式版 RIBAO_DATA_ROOT。"
  fi
  FORMAL_INDEX="$RIBAO_DATA_ROOT/日报状态/feishu-daily-index.json"
  FORMAL_REPORTS="$RIBAO_DATA_ROOT/日报状态/feishu-docs.json"
  TEST_INDEX="$TEST_BASE_ROOT/日报状态/feishu-test-index.json"
  TEST_REPORTS="$TEST_BASE_ROOT/日报状态/feishu-docs.json"
  TEST_RUNS="$TEST_BASE_ROOT/日报状态/feishu-test-runs.json"
  for required in "$FORMAL_INDEX" "$FORMAL_REPORTS" "$TEST_INDEX" "$TEST_REPORTS" "$TEST_RUNS"; do
    [ -s "$required" ] || issue "缺少历史登记：${required}；先私密迁移，勿用空列表覆盖原索引。"
  done
  if [ -s "$FORMAL_INDEX" ] && [ -s "$FORMAL_REPORTS" ] && [ -s "$TEST_INDEX" ] \
     && [ -s "$TEST_REPORTS" ] && [ -s "$TEST_RUNS" ]; then
    if ! "${RIBAO_PYTHON_BIN:-python3}" - "$FORMAL_INDEX" "$TEST_INDEX" "$FORMAL_REPORTS" "$TEST_REPORTS" "$TEST_RUNS" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

for filename in sys.argv[1:3]:
    registry = json.loads(Path(filename).read_text(encoding="utf-8"))
    url = urlsplit(str(registry.get("url") or ""))
    token = str(registry.get("doc_token") or "")
    if url.scheme != "https" or url.netloc != "micoworld.feishu.cn" \
       or url.path != f"/docx/{token}" or not token.isalnum():
        raise SystemExit(f"Invalid fixed-index registry: {filename}")
for filename in sys.argv[3:]:
    registry = json.loads(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or not registry:
        raise SystemExit(f"Missing historical document links: {filename}")
PY
    then
      issue "飞书索引或历史日报登记格式不完整；核对私有备份，不要自动新建索引。"
    fi
  fi
fi
if [ "$(date +%z)" != "$(TZ=Asia/Shanghai date +%z)" ]; then
  issue "本机时区不是 Asia/Shanghai；测试版 launchd 的 15:00 会跑错时间。"
fi

if [ "${#issues[@]}" -gt 0 ]; then
  printf '交接预检未通过，共 %s 项：\n' "${#issues[@]}" >&2
  for item in "${issues[@]}"; do printf '  - %s\n' "$item" >&2; done
  printf '处理说明：docs/运行与交接手册.md；预检不会安装任务或修改飞书。\n' >&2
  exit 3
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "交接预检通过：私有配置、Codex/飞书授权、两个固定索引及历史登记均已就绪；未安装任务、未写飞书。"
  exit 0
fi

RIBAO_CONFIG_FILE="$CONFIG_FILE" bash "$ROOT_DIR/automation/install_guard.command"
RIBAO_CONFIG_FILE="$CONFIG_FILE" bash "$ROOT_DIR/automation/install_test_matrix.command"
echo "Both daily schedules installed. Check each launchctl label and the next closed-window run."
