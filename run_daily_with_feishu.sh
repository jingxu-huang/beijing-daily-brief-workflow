#!/bin/bash
# 本地私有包装层：先运行日报 skill，再把质检通过的成稿导入/更新飞书文档并投递到日报接收群。
# 阶段1加固(2026-07-08)：
#   - 目标日期由北京时间守门器显式传入并贯穿全链路（此前用 ls -t 取最新草稿：fetch 失败时会拿到
#     昨天的草稿 → 昨天的成稿+PASS质检 → 错日重发。该路径已删除）
#   - 质检 FAIL / 产物缺失 / 投递失败 均发飞书告警+系统通知（此前只写日志，静默丢一天）
#   - 系统时钟与网络时间对表（偏差只告警不拦截；连续告警=需人工校准时钟/时区）
#   - 每步状态写 日报状态/run-manifest-YYYYMMDD.json（供 Cowork 看门狗与复盘）
#   - caffeinate 防睡眠
#   - WeWe 缺源提醒收敛到发送脚本内（此前 wrapper 与发送脚本各发一条，重复）
# 时钟校正补丁(2026-07-13)：
#   - 实测本机系统 UTC 快约 15 小时（时区配错+墙钟手动拨对），manifest 时间戳"来自未来"、
#     目标日期纯靠运气才正确。现网络对表移到入口，偏差经 RIBAO_CLOCK_SKEW_S 传给
#     fetch_news.py / manifest.py 自动扣除；|skew|>300s 时 clock_check 记 warn（此前恒 ok）

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${RIBAO_CONFIG_FILE:-$ROOT_DIR/config.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi
SKILL_DIR="$ROOT_DIR/资讯日报-skill"
DATA_ROOT="${RIBAO_DATA_ROOT:-$ROOT_DIR}"
export RIBAO_PROJECT_ROOT="${RIBAO_PROJECT_ROOT:-$ROOT_DIR}"
export RIBAO_DATA_ROOT="$DATA_ROOT"
REPORT_DIR="${RIBAO_REPORT_DIR:-$DATA_ROOT/日报文档}"
DRAFT_DIR="${RIBAO_DRAFT_DIR:-$DATA_ROOT/日报草稿}"
CHECK_DIR="${RIBAO_CHECK_DIR:-$DATA_ROOT/日报质检}"
STATE_DIR="${RIBAO_STATE_DIR:-$DATA_ROOT/日报状态}"
FEISHU_CLI_SCRIPT="$ROOT_DIR/local-only/feishu-bot/send_to_feishu_cli.py"
REPORT_CHAT_ID="${FEISHU_REPORT_CHAT_ID:-}"
mkdir -p "$REPORT_DIR" "$DRAFT_DIR" "$CHECK_DIR" "$STATE_DIR"

# launchd 环境 PATH 精简；补上 nvm 下的 lark-cli / codex。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
NVM_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$NVM_BIN" ] && export PATH="$NVM_BIN:$PATH"

# 时钟对表（2026-07-13 起先于目标日期计算：目标日期与 manifest 时间戳都由系统 UTC 推导，
# 而系统 UTC 本身可能配错——实测本机时区留在国外、墙钟手动拨到北京时间，系统 UTC 快约 15 小时）。
# SKEW = 系统UTC − 网络UTC（秒），正=本机偏快；经 RIBAO_CLOCK_SKEW_S 传给
# fetch_news.py / manifest.py，两者在 |skew|>300s 时先扣除偏差再换算北京时间。
SKEW=""
if [ -z "${RIBAO_TRUSTED_UTC:-}" ]; then
SKEW="$(python3 - <<'PY' 2>/dev/null
import urllib.request, email.utils, datetime as dt, random
try:
    # 防缓存：唯一查询参数 + no-cache 头，避免中间代理返回旧的 Date 头造成假偏差
    url = f"https://www.apple.com/?nc={random.randrange(10**12)}"
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    with urllib.request.urlopen(req, timeout=6) as r:
        net = email.utils.parsedate_to_datetime(r.headers["Date"])
    print(int((dt.datetime.now(dt.timezone.utc) - net).total_seconds()))
except Exception:
    pass
PY
)" || SKEW=""
fi
if [[ "$SKEW" =~ ^-?[0-9]+$ ]]; then
  export RIBAO_CLOCK_SKEW_S="$SKEW"
fi

# 目标日期：守门器传入；手动执行时取最近一个已闭合的北京15:00窗口。
YMD="${RIBAO_TARGET_YMD:-$(python3 "$SKILL_DIR/fetch_news.py" --print-date)}"
export RIBAO_TARGET_YMD="$YMD"
MD="$(python3 -c 'import sys; y=sys.argv[1]; print(f"{int(y[4:6])}.{int(y[6:8])}")' "$YMD")"
DRAFT="$DRAFT_DIR/资讯日报-草稿-${YMD}.md"
REPORT="$REPORT_DIR/资讯类简报-${MD}日.md"
CHECK="$CHECK_DIR/资讯日报-质检-${YMD}.md"
LOG_HINT="状态见 日报状态/run-manifest-${YMD}.json"

MANIFEST() { python3 "$SKILL_DIR/manifest.py" --date "$YMD" --state-dir "$STATE_DIR" "$@" || true; }

# alert <tag> <正文>：飞书 + macOS 通知双通道；告警本身失败绝不中断流程
alert() {
  local tag="$1" text="$2"
  echo "[$(date '+%F %T')] ALERT(${tag}): ${text}"
  # 守门器托管时，外部告警统一由它按 15:30 硬截止去重发送；wrapper 只落日志和 manifest。
  if [ "${RIBAO_GUARD_MANAGED:-0}" = "1" ]; then
    return 0
  fi
  if [ -z "$REPORT_CHAT_ID" ]; then
    echo "[$(date '+%F %T')] WARN: FEISHU_REPORT_CHAT_ID 未配置，飞书告警已跳过（${tag}）"
  elif command -v lark-cli >/dev/null 2>&1; then
    lark-cli im +messages-send \
      --chat-id "$REPORT_CHAT_ID" \
      --as "${FEISHU_CLI_SEND_AS:-bot}" \
      --text "【日报告警】${YMD}：${text}（${LOG_HINT}）" \
      --idempotency-key "ribao-${YMD}-alert-${tag}" \
      --format json >/dev/null 2>&1 || echo "[$(date '+%F %T')] WARN: 飞书告警发送失败（${tag}）"
  else
    echo "[$(date '+%F %T')] WARN: 找不到 lark-cli，飞书告警未发出（${tag}）"
  fi
  osascript -e "display notification \"${text}\" with title \"资讯日报告警\"" >/dev/null 2>&1 || true
}

echo "[$(date '+%F %T')] 目标日期 ${YMD}"
MANIFEST --step wrapper_start --status ok

# 时钟对表结果落 manifest（SKEW 已在入口计算并导出）。偏差大时状态记 warn 而非 ok，
# 且告警指明最可能根因（时区配错），并说明本次已自动校正、不影响交付。
if [ -n "${RIBAO_TRUSTED_UTC:-}" ]; then
  MANIFEST --step clock_check --status ok --detail "trusted_utc=${RIBAO_TRUSTED_UTC}，由北京时间守门器网络仲裁"
elif [[ "$SKEW" =~ ^-?[0-9]+$ ]]; then
  if [ "${SKEW#-}" -gt 300 ]; then
    MANIFEST --step clock_check --status warn --detail "skew=${SKEW}s，已按偏差校正目标日期与时间戳"
    alert clock "本机系统时钟与网络时间偏差 ${SKEW} 秒（约 $(( (${SKEW#-} + 1800) / 3600 )) 小时）。偏差接近整小时通常是时区配错（如时区仍是国外、墙钟被手动拨到北京时间）：请在 系统设置→通用→日期与时间 开启自动设置并选上海时区。本次运行已按偏差自动校正，不影响交付"
  else
    MANIFEST --step clock_check --status ok --detail "skew=${SKEW}s"
  fi
else
  MANIFEST --step clock_check --status warn --detail "网络对表失败，未校正（若本机时钟本身配错，目标日期可能漂移）"
fi

# 主流程（caffeinate 防睡眠；质检 FAIL 会以非零退出，这里不让它中断包装层，以便统一告警）
CAFF=""
command -v caffeinate >/dev/null 2>&1 && CAFF="caffeinate -i"
RUN_RC=0
RIBAO_REPORT_DIR="$REPORT_DIR" \
RIBAO_DRAFT_DIR="$DRAFT_DIR" \
RIBAO_CHECK_DIR="$CHECK_DIR" \
RIBAO_STATE_DIR="$STATE_DIR" \
$CAFF /bin/bash "$SKILL_DIR/run_daily.sh" || RUN_RC=$?

# 出口门槛：一律按显式 YMD 检查产物与质检结论；缺什么、拦什么，都有告警。
if [ "$RUN_RC" -ne 0 ]; then
  MANIFEST --final failed --kv "reason=日报生成流程失败" --kv "run_exit=${RUN_RC}"
  alert generation "日报生成或质检失败（run_daily 退出码 ${RUN_RC}），未发送任何旧成稿或半成品；后台将继续重试"
  exit 1
fi
if [ ! -f "$DRAFT" ]; then
  MANIFEST --final failed --kv "reason=无草稿"
  alert nodraft "生成失败：没有今天(${YMD})的草稿（run_daily 退出码 ${RUN_RC}），本期日报未产出，需人工排查后重跑"
  exit 1
fi
if [ ! -f "$REPORT" ]; then
  MANIFEST --final failed --kv "reason=无成稿"
  alert noreport "生成失败：草稿已有但成稿缺失（Codex 浓缩步骤疑似失败，run_daily 退出码 ${RUN_RC}），本期日报未产出"
  exit 1
fi

VERDICT="$(grep -m1 '结论：' "$CHECK" 2>/dev/null | sed -E 's/.*结论：[[:space:]]*//' | tr -d '[:space:]')"
echo "[$(date '+%F %T')] 质检结论：${VERDICT:-未知}"
if [ -z "$VERDICT" ]; then
  MANIFEST --final blocked --kv "reason=质检无结论"
  alert noverdict "质检文件缺失或无结论，已拦截不发送。请检查 日报质检/资讯日报-质检-${YMD}.md 后重跑"
  exit 1
fi
if [ "$VERDICT" = "FAIL" ]; then
  MANIFEST --final blocked --kv "reason=质检FAIL"
  alert qcfail "质检 FAIL，已拦截不发送。请查看 日报质检/资讯日报-质检-${YMD}.md，修复成稿后重跑"
  exit 1
fi
# PASS 直接发；WARN 属"需人工复核"的软提示（如数字待核对）仍会发送。
# 若想更严格"仅 PASS 才发"，把上面判断改为：[ "$VERDICT" != "PASS" ] && 拦截。

if [ ! -f "$FEISHU_CLI_SCRIPT" ]; then
  MANIFEST --final failed --kv "reason=缺发送脚本"
  alert nosender "找不到发送脚本 ${FEISHU_CLI_SCRIPT}，成稿已生成但未投递"
  exit 1
fi

if [ -z "$REPORT_CHAT_ID" ]; then
  MANIFEST --step publish --status skipped --detail "FEISHU_REPORT_CHAT_ID not configured"
  MANIFEST --final generated --kv "reason=飞书群未配置"
  echo "[$(date '+%F %T')] 已生成并通过质检；FEISHU_REPORT_CHAT_ID 未配置，明确跳过飞书发送。"
  exit 0
fi

echo "[$(date '+%F %T')] 4/4 投递飞书（结论：${VERDICT}；同日已有文档则原地更新，不新建链接）..."
if python3 "$FEISHU_CLI_SCRIPT" --report "$REPORT" --check "$CHECK" --date "$YMD" --chat-id "$REPORT_CHAT_ID" --perm edit --send-as "${FEISHU_CLI_SEND_AS:-bot}"; then
  MANIFEST --step publish --status ok
  MANIFEST --final sent
  if REMOVED="$(python3 "$SKILL_DIR/codex_external.py" --root "$ROOT_DIR" cleanup-success --date "$YMD")"; then
    MANIFEST --step codex_cleanup --status ok --detail "removed_external_workdirs=${REMOVED}"
  else
    CLEANUP_RC=$?
    MANIFEST --step codex_cleanup --status warn --detail "external cleanup failed exit=${CLEANUP_RC}"
  fi
else
  MANIFEST --step publish --status fail
  MANIFEST --final failed --kv "reason=飞书投递失败"
  alert sendfail "质检 ${VERDICT}，但飞书投递失败。成稿在 日报文档/资讯类简报-${MD}日.md，排查后重跑本脚本即可（已发过的当天文档会原地更新，不会重复建链接）"
  exit 1
fi

echo "[$(date '+%F %T')] 完成。"
