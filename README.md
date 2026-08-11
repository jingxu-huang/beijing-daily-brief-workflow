# Beijing Daily Brief Workflow

一套按**标准北京时间**运行的每日资讯简报工作流，覆盖抓取、Codex 成稿、独立质检、WeWe RSS 增强、飞书 Docx 投递与 macOS `launchd` 守门调度。

核心合同：

- 信息窗口固定为北京时间 `[前一日 15:00，当日 15:00)`。
- 13:30 检查 WeWe；未恢复时通过飞书与本机弹窗各提醒一次。
- 14:30 仍未恢复时，再通过两个渠道各提醒一次。
- 15:00 冻结 WeWe 决策并启动正式日报；WeWe 仅是增强源，失效时排除它并继续其他渠道。
- 15:30 是硬截止：未送达则发失败告警，后台每 10 分钟继续处理，通过质检后补发**完整正式版**；不发送应急版或半成品。
- 所有业务时间来自多源网络 UTC 仲裁后换算为 `Asia/Shanghai`，不依赖主机时区或手动墙钟。

## 目录

- `资讯日报-skill/`：抓取、成稿、质检、可信时间、守门器、WeWe 接入和内容规则。
- `run_daily_with_feishu.sh`：生成与飞书投递的唯一正式管线。
- `automation/`：唯一日报 `launchd` 模板与安装器，以及独立的 WeWe 重登服务安装器。
- `local-only/feishu-bot/`：飞书 Docx 导入、授权、链接发送与同日幂等更新。
- `examples/`：完全虚构的草稿、成稿、质检与状态示例。
- `scripts/`：公开包泄密审计、验证与发布归档脚本。

## 前置条件

- macOS（定时守门器使用 `launchd`；核心抓取与质检可在其他系统手动运行）。
- Python 3.10+。
- 已安装并登录的 Codex CLI。
- 飞书投递可选：已安装、授权的 `lark-cli`，并能以配置身份导入文档、授权群聊和发送消息。
- WeWe 增强可选：Docker、WeWe RSS；远程扫码链接另需 `cloudflared` 与 Python `qrcode`。
- 一个专用外接盘/非根挂载目录用于运行数据。源码可放任意位置，运行产物不会回退到系统盘。

## 快速开始

```bash
python3 -m pip install -r 资讯日报-skill/requirements.txt
cp config.env.example config.env
```

编辑本地 `config.env`：

1. 把 `RIBAO_DATA_ROOT` 指向已经存在的外接盘目录。
2. 若需要飞书发送，填写 `FEISHU_REPORT_CHAT_ID`；留空时仍生成并质检，但明确跳过发送。
3. 若启用 WeWe，设置一个强 `WEWE_AUTH_CODE`，并让 `资讯日报-skill/wewe-rss/.env` 使用相同值。

手动运行完整管线：

```bash
bash run_daily_with_feishu.sh
```

只做抓取测试：

```bash
set -a
source config.env
set +a
python3 资讯日报-skill/fetch_news.py
```

## 安装定时守门器

先确认系统中不存在另一套日报 cron、`launchd` 或 Codex 定时任务。安装器故意不自动删除未知任务。

```bash
chmod +x automation/*.command
bash automation/install_guard.command
```

守门器每 30 秒唤醒一次，但只在固定北京时间节点行动；同一标签通过文件锁与状态机幂等。安装当日若已过 13:30，会从下一个北京时间自然日启用，避免补跑半套流程。

## WeWe RSS 与远程重登

```bash
cp config.env 资讯日报-skill/wewe-rss/.env
cd 资讯日报-skill/wewe-rss
docker compose up -d
```

默认聚合 feed 为 `http://localhost:4000/feeds/all.rss`。远程扫码服务：

```bash
bash automation/install_wewe_relogin.command
bash automation/install_guard.command
```

第二次执行守门器安装器会把新生成的重登管理 token 同步进守门运行环境。重登服务只提供页面与按需隧道，不创建另一套日报预检调度。

## 飞书行为

质检为 `PASS` 或 `WARN` 才允许投递；`FAIL` 或缺少结论时阻断。首发顺序是：导入 Docx → 先登记当天文档 → 授予目标群 `edit` 权限 → 发送唯一链接 → 回读验证。重跑会原地更新同一篇文档，不重复创建链接。

`FEISHU_REPORT_CHAT_ID` 没有内置默认值。未配置时，正式文件保留在 `RIBAO_DATA_ROOT`，manifest 终态为 `generated`。

## 验证与发布前审计

```bash
bash scripts/verify.sh
```

审计会检查源码、跟踪文件和可选 ZIP，阻断群聊/用户/消息 ID、绝对个人路径、凭据、真实运行目录、日志、二维码和归档误入。

## 内容规则

完整内容范围、赛道、主题准入、同事件合并、来源保留、写作格式与 QA 规则见：

- `资讯日报-skill/SKILL.md`
- `资讯日报-skill/format/格式规范.md`
- `资讯日报-skill/sources.yaml`

## Privacy

本仓库由白名单重新生成，不包含真实日报历史、飞书 ID、文档链接、二维码、认证文件、cookies、日志、Codex 会话、状态文件或原仓库 Git 历史。请只把 `config.env.example` 提交到版本库，永远不要提交本地 `config.env`。

English setup: [docs/quickstart.en.md](docs/quickstart.en.md)
