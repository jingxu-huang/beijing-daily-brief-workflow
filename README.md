# Beijing Daily Brief Workflow

> **交接前先看：克隆仓库不等于已能生产日报。** 本仓库只提交代码、规则和配置模板，**不包含** `config.env`、Codex/飞书登录授权、战略小组会话 ID、固定飞书索引地址、历史日报及飞书索引登记。仅下载 `资讯日报-skill/` 子目录也不能运行完整链路；需要克隆整个工作流。

接手人先确认以下事项，再安装正式版和测试版调度：

1. 从本仓库 `main` 取得**完整工作流**，核对实际提交；不要使用旧 ZIP 或仅复制 Skill 子目录。
2. 在本机建立 Git 忽略的 `config.env` 和独立运行数据目录；完成 Codex CLI、飞书 `lark-cli` 的 **user** 身份授权，并私下配置战略小组 ID。账号、令牌、Cookie 不得写进 Git。
3. 私密迁移正式/测试索引登记及历史日报链接登记；仅有飞书网页链接不等于恢复了历史。缺登记时先按[交接手册](docs/运行与交接手册.md)续接，**不得自动新建同名索引或用空列表覆盖旧索引**。
4. 运行 `bash automation/install_parallel.command --check` 获取逐项、只读的缺失提示；全部就绪后再运行不带 `--check` 的安装命令。安装调度不等于日报已经跑通，首次实际交付仍需在接手人的环境验收。

> 本独立仓库是交接与后续 Skill 更新的目标，保留最初 fork 的 Git 祖先。2026-09-30 的现行工作流从 Mico 快照受控同步而来；Mico 的并行开发历史不是本仓库的 Git 祖先，证据见 [`UPSTREAM.md`](UPSTREAM.md)。

接手、并行运行和飞书原位续接请先读 [运行与交接手册](docs/运行与交接手册.md)。仓库内的 `资讯日报-skill/SKILL.md` 是 Codex 调用入口；一句话调用示例及两条定时任务的安装命令均在手册中。

想复盘初始仓库、fork、Mico 快照阶段，以及 WeWe RSS → WeRSS → weread-omni → 下载 API 等公众号替代尝试，请读 [Skill 历史与公众号源迭代](docs/Skill历史与公众号源迭代.md)。Mico 阶段的 Git 提交保留为引用，不伪装成本仓库的祖先。

一套按**标准北京时间**运行的每日资讯简报工作流，覆盖公开源抓取、Codex 成稿、独立质检、飞书 Docx 投递与 macOS `launchd` 守门调度。

核心合同：

- 信息窗口固定为北京时间 `[前一日 15:00，当日 15:00)`。
- WeWe RSS 已从**正式**路径退役：守门器和手动正式运行都不会探测、刷新、登录或弹出二维码。
- 14:30 可预抓公开源；15:00 启动正式日报。
- 15:30 是正式版硬截止：未送达则记录失败状态和本机告警，后台每 10 分钟继续处理，通过质检后补发**完整正式版**；当前无群消息模式不发送群告警、应急版或半成品。
- 所有业务时间来自多源网络 UTC 仲裁后换算为 `Asia/Shanghai`，不依赖主机时区或手动墙钟。

## 目录

- `资讯日报-skill/`：抓取、成稿、质检、可信时间、守门器和内容规则；旧 WeWe 文件仅作历史留档。
- `public-sources/`：固定版本的本地 RSSHub 公开源适配器，仅监听本机回环地址。
- `run_daily_with_feishu.sh`：生成与飞书投递的唯一正式管线。
- `automation/`：正式守门器与独立测试矩阵各一份 `launchd` 模板和安装器；旧 WeWe 重登安装器仅作历史留档。
- `local-only/feishu-bot/`：飞书 Docx 导入、授权、链接发送与同日幂等更新。
- `examples/`：完全虚构的草稿、成稿、质检与状态示例。
- `scripts/`：公开包泄密审计、验证与发布归档脚本。

## 前置条件

- macOS（定时守门器使用 `launchd`；核心抓取与质检可在其他系统手动运行）。
- Python 3.10+。
- 已安装并登录的 Codex CLI。
- 飞书投递可选：已安装、授权的 `lark-cli`，并能以配置身份导入文档、授权群聊和发送消息。
- 正式日报不需要 WeWe RSS、Docker 或扫码登录；旧项目保留但不由正式流程调用。
- 一个专用外接盘/非根挂载目录用于运行数据。源码可放任意位置，运行产物不会回退到系统盘。

## 快速开始

新 Mac 克隆本仓库后，可用一条命令准备公开源基线、虚拟环境和 Codex skill 链接。默认只安装本地正式守门器；启用飞书时，必须先通过私有状态预检，才会安装正式与测试两条 `launchd` 定时任务。WeWe 已退役，不再由此流程安装或验证：

```bash
bash bootstrap_new_mac.command
```

能力模式：

```bash
# 默认：公开源本地生成 + 质检，不写飞书
bash bootstrap_new_mac.command

# 按需启用；启用后检查账号授权并安装可选依赖（不恢复旧微信扫码服务）
RIBAO_ENABLE_FEISHU=1 bash bootstrap_new_mac.command
```

脚本会在必须由本人授权或恢复历史登记的环节停下并给出提示；完成后可重复运行同一条命令。安装成功也**不代表当天日报已经生成并写入飞书**；首次实际交付仍须按[运行与交接手册](docs/运行与交接手册.md)验收。

```bash
python3 -m pip install -r 资讯日报-skill/requirements.txt
cp config.env.example config.env
docker compose -f public-sources/docker-compose.yml up -d
```

编辑本地 `config.env`：

1. 把 `RIBAO_DATA_ROOT` 指向已经存在的外接盘目录。
2. 只有需要飞书写入时才设 `RIBAO_ENABLE_FEISHU=1` 并配置目标；为 `0` 时无论其他飞书字段为何值都跳过全部飞书调用。
3. 不设置 `RIBAO_ENABLE_WEWE`；正式守门器会强制排除已退役的 WeWe RSS。

手动运行完整管线：

```bash
bash run_daily_with_feishu.sh
```

换到一台新 Mac 时，先阅读[运行与交接手册](docs/运行与交接手册.md)。旧版 [macOS 迁移与启动](docs/macOS-迁移与启动.md)仅用于查阅已退役的 WeWe 历史，不得照其扫码/重登步骤执行。

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

守门器每 30 秒唤醒一次，但只在固定北京时间节点行动；同一标签通过文件锁与状态机幂等。安装当日若在 14:30 前，会在当天启用并补齐已错过的检查；14:30 后安装则从下一个北京时间自然日启用。

## 历史 WeWe RSS 记录（不执行）

`wewe-rss/`、重登页和其 Docker 数据保留为历史可追溯记录。正式日报不执行其中的命令、不连接其服务，也不会发起扫码提醒；不得运行旧重登安装器。

## 飞书行为

日报 Markdown 可以继续保存经视觉核验的源站图片 URL；正式投递前，发送脚本会将远程图下载到 Git 忽略的 `.staging/`，按条目原文补 Referer，并改用飞书本地图片上传。无法下载、返回非图片或不支持的图片会从飞书发布副本剔除并记媒体告警，保留已通过质检的正文；已上传的图片必须回读素材并与 staging 原图做 SHA-256 比对，不能把防盗链产生的占位图误判成投递成功。

质检为 `PASS` 或 `WARN` 才允许投递；`FAIL` 或缺少结论时阻断。首发顺序是：导入 Docx → 先登记当天文档 → 授予目标群 `full_access`（可管理）权限 → 发送唯一链接 → 回读验证 → 幂等更新固定日报索引并同样授予目标群 `full_access` → 标记整次投递成功。重跑会原地更新同一篇文档，不重复创建链接；同日更新也会重新核验并授予目标群可管理权限。生产投递拒绝 `view` 或 `edit` 等较弱角色。

`FEISHU_REPORT_CHAT_ID` 没有内置默认值。未配置时：`FEISHU_DOC_ONLY=1` 会创建/更新飞书文档及可选索引；`FEISHU_DOC_ONLY=0` 才会完全跳过飞书写入。两种情况下正式本地文件都保留在 `RIBAO_DATA_ROOT`。

索引交付模式使用 `FEISHU_DOC_ONLY=1`、`FEISHU_DAILY_INDEX=1` 和 `RIBAO_GROUP_MESSAGES=0`：以 `user` 身份创建/更新日报与固定索引，不向群发送日报、WeWe 或截止告警，不给单篇日报授群权限；只在更新固定索引后向配置群授予 `full_access`。该模式不要求机器人留在群内。

`FEISHU_DAILY_INDEX=1` 只应在私密恢复并核验 `日报状态/feishu-daily-index.json` 后开启。自动模式缺少该登记时会硬失败，防止误建同名索引；可用手册记录的原索引地址执行 `--register-target` 续接。只有明确需要创建一份全新索引时，才可人工运行 `update_daily_index.py --mode new`。

测试版使用另一份固定索引 `资讯日报测试版本`。每日北京时间 15:00，独立的 `run_daily_test_matrix.sh` 与正式守门器同时启动：它先抓取并冻结一次**共同的原始候选快照**，随后执行最多 3 批并行、每批 4 条且最多 60 秒的共享 Codex 候选审核，再让 `测试版v2-严格`、`测试版v2-平衡`、`测试版v2-高召回` 在各自隔离目录并行筛选、成稿、质检和写入独立飞书文档；最后只由协调器更新一次测试索引。因此三版的差异只来自筛选策略，不来自不同时间的网络抓取。审核仅能剔除已通过候选或提权对应版本的完整灰区候选；审核失败、超时或结果校验不通过会记录 `fallback`，并立即按原生筛选继续，绝不影响正式日报。每个日期只占一行，固定列为日期、三种测试版、公开源、公众号源、质检结果、生成时间；三种策略只显示各自最新日报链接。公开源和公众号源分别展示抓到、精选、候补、最终采用文章数，公众号另显示当天是否启用及覆盖 `x/27`。运行标签和内部策略名不进入业务索引。正式日报的数据目录、manifest、飞书文档和正式索引与测试矩阵完全隔离；测试日报单篇文档与测试索引都向战略小组授予 `edit`（可编辑），不授予可管理权限、不写正式索引、不发群消息。任一文档授权失败时测试流程必须失败。失败或被质检拦截的测试也会登记状态，但没有可发布文档链接。已有索引可用 `--register-target` 续接；只有明确需要新建时才人工执行 `update_test_index.py --mode new`。使用 `bash automation/install_parallel.command` 同时安装两条调度；测试安装器不会改动正式守门器。

## 公开源覆盖门禁

所有阈值只在 `资讯日报-skill/sources.yaml` 的 `config.quality_gate` 中定义，代码不保留隐藏默认值：

|门禁|阈值|
|---|---:|
|精选公开源候选|至少 8 条|
|精选公开源覆盖|至少 3 个来源|
|公开源抓取失败|最多 2 个来源|
|最终成稿正文|至少 10 条（正式与三测试版硬下限；平衡/高召回目标 25 条）|
|最终 Source 来源|至少 3 个来源|

任一硬门禁失败都会返回非零状态，不能把“抓到候选”“生成草稿”或“一条成稿”误判为日报合格，更不能替代飞书投递成功。

公众号链路使用独立源标识：`wewe_rss`（历史 WeWe RSS）、`werss`（旧 we-mp-rss）和 `weread_omni`（新隔离测试）。三者都只是增强源，不能替代公开源覆盖。旧 `werss` 默认停用并保留恢复路径；`weread_omni` 只有显式测试开关开启时才参与候选，正式日报默认不接入。所有公众号条目都必须带上游真实发布时间，不能把轮询时间当作资讯发布时间。版本证据和退役边界见 `docs/公众号源版本与退役记录.md`。

## 测试版误杀保护

`business_v1_test` 与 `business_v2_*` 只影响显式启用的测试日报，正式 `legacy` 策略不变。其判定分为三层：先由 `sources.yaml` 做源和时间窗召回，再由 `资讯日报-skill/editorial_policy.yaml` 按多标签、正反证据与集中阈值给出 `accept / review / reject`，最后由 `check_report.py` 独立复核。短英文信号采用单词边界，网页导航/推广样板不参与业务判断；宽泛排除词只记录为证据。正文少于完整度门槛的候选进入灰区，不会自动写入成稿，也不会被无声删除。

策略配置边界：

- `editorial_policy.yaml`：业务信号、权重、`accept_score=65`、`review_score=40` 与硬拒绝置信度；这些值只作用于测试策略。
- `sources.yaml`：成稿数量、来源覆盖、正文长度等交付门禁。`business_v2_strict` 保持“不少于 10 条、至少 3 个来源、不得用不相关内容补数”；`business_v2_balanced` 和 `business_v2_recall` 是目标 25 条的对照实验，不得替代严格版验收。
- `日报状态/资讯日报-筛选审计-YYYYMMDD.json`：Git 外的逐条筛选账本，记录窗口内候选摘要、结论、证据和同次去重合并，可供三版使用同一快照离线回放；不得提交该文件，也不得把审计话术写进日报正文。

测试版开启示例：

```bash
python3 资讯日报-skill/fetch_news.py --editorial-profile business_v1_test

# 完整隔离流程；默认 business_v2_strict
RIBAO_TEST_PROFILE=business_v2_strict ./run_daily_test.sh
RIBAO_TEST_PROFILE=business_v2_balanced RIBAO_TEST_RUN_LABEL=balanced ./run_daily_test.sh
RIBAO_TEST_PROFILE=business_v2_recall RIBAO_TEST_RUN_LABEL=recall ./run_daily_test.sh
```

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

本公开仓库不包含真实日报历史、群聊/用户 ID、固定飞书文档地址、二维码、认证文件、cookies、日志、Codex 会话或状态文件。初始仓库的 Git 祖先保留在本仓库；Mico 快照阶段的提交仅作交叉引用。固定索引地址和历史登记必须由拥有者私下交接。请只把 `config.env.example` 提交到版本库，永远不要提交本地 `config.env`。

English setup: [docs/quickstart.en.md](docs/quickstart.en.md)
