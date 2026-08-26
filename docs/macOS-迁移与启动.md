# macOS 迁移与启动

## 最短路径：只记一条命令

把迁移 ZIP 与校验文件复制到新 Mac、校验并解压后，在解压目录运行：

```bash
bash bootstrap_new_mac.command
```

该命令会自动安装 Homebrew 依赖、Python 虚拟环境、`lark-cli`/Codex CLI，拉取并启动固定的 WeWe RSS `2.6.1` 镜像，把 `zixun-ribao` 链接进新机 Codex skills 目录，创建本地数据目录，并安装扫码提醒与日报守门器。它可以重复运行。

三类授权不能放进公开迁移包，也不能安全地“自动继承”：Codex 登录、飞书 bot 身份、微信读书扫码。脚本遇到前两项未就绪会停止并说明原因；WeWe 启动后会打开账号页供本人扫码。完成后仍执行同一条命令即可复检和继续。公众号订阅首次需从 `MIGRATION/wewe-share-links.txt` 恢复。

本文用于把资讯日报工作流迁移到一台新 Mac，并恢复以下固定行为：

- 11:00 验证 WeWe 微信读书账号；
- 12:00 首轮全量刷新，14:30 最后一轮全量刷新；
- 15:00 生成、质检并导入飞书 Docx；
- 更新固定的飞书日报链接索引，不发群消息、不授权群聊；
- 15:30 未完成时告警，后台继续重试完整正式版。

## 一、旧机器：生成安全迁移包

在独立工作流仓库执行：

```bash
bash scripts/verify.sh
bash scripts/build_transfer_bundle.sh
```

输出位于 `release/`：

- `beijing-daily-brief-workflow-transfer-YYYYMMDD.zip`
- 同名 `.sha256` 校验文件

ZIP 包含当前工作区的最新代码，包括尚未提交但未被 `.gitignore` 排除的改动。构建器会执行隐私审计，排除 `config.env`、账号 token、二维码、飞书私有链接、真实日报、日志和运行状态。

如果旧机器的 WeWe 正在运行，ZIP 还会生成：

- `MIGRATION/wewe-feed-inventory.json`：公众号名称、feed ID 和一篇公开文章链接；
- `MIGRATION/wewe-share-links.txt`：可在新 WeWe 的“添加公众号源”窗口中逐行粘贴的公开文章链接。

若构建时 WeWe 暂时未运行，可把上一次安全包中的 `wewe-feed-inventory.json` 作为离线回填输入：`WEWE_MIGRATION_INVENTORY_FILE=/path/to/wewe-feed-inventory.json bash scripts/build_transfer_bundle.sh`。

如需让新机器继续更新**原来的飞书日报索引文档**，还要通过 AirDrop、加密盘等私密方式单独复制旧 `RIBAO_DATA_ROOT/日报状态/` 下的：

- `feishu-docs.json`
- `feishu-daily-index.json`

这两个文件包含私有文档标识，不能放进 GitHub 或公开 ZIP。真实日报历史可按需单独迁移；不迁移不影响新日报生成。

## 二、新机器：安装依赖（已由一键命令自动完成）

建议使用 macOS 13 或更高版本，并安装：

- Docker Desktop；
- Python 3.10+，建议 Python 3.12；
- Codex CLI，并完成登录；
- Node.js 与 `lark-cli`；
- `cloudflared`，用于失效提醒中的远程扫码页。

常见 Homebrew/npm 安装方式：

```bash
brew install python@3.12 node cloudflared
npm install -g @larksuite/cli
```

Docker Desktop、Codex CLI 和 `lark-cli` 的 user/bot 身份需要在新机器重新登录或配置。不要直接复制旧机器的认证缓存。

## 三、解压并创建 Python 环境（手动备用路径）

校验并解压：

```bash
shasum -a 256 -c beijing-daily-brief-workflow-transfer-YYYYMMDD.zip.sha256
ditto -x -k beijing-daily-brief-workflow-transfer-YYYYMMDD.zip .
cd beijing-daily-brief-workflow-transfer-YYYYMMDD
```

创建独立虚拟环境：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r 资讯日报-skill/requirements.txt
.venv/bin/pip install qrcode
```

## 四、创建本机配置

```bash
cp config.env.example config.env
mkdir -p "/path/to/external-volume/beijing-daily-brief-data"
```

编辑 `config.env`，至少设置：

```bash
RIBAO_DATA_ROOT="/path/to/external-volume/beijing-daily-brief-data"
RIBAO_PYTHON_BIN="/absolute/path/to/workflow/.venv/bin/python3"
RIBAO_EXISTING_CODEX_HOME="$HOME/.codex"
WEWE_AUTH_CODE="在新机器生成的强随机管理码"
```

生成管理码时可使用：

```bash
openssl rand -hex 24
```

保持以下现用行为：

```bash
FEISHU_REPORT_CHAT_ID=
FEISHU_DOC_ONLY=1
FEISHU_DAILY_INDEX=1
FEISHU_CLI_IMPORT_AS=bot
FEISHU_CLI_SEND_AS=bot
```

如已私密迁移飞书状态文件，把它们放回新 `RIBAO_DATA_ROOT/日报状态/`。否则系统会从新日报开始创建新的日报索引文档。

## 五、启动并恢复 WeWe RSS

```bash
cp config.env 资讯日报-skill/wewe-rss/.env
cd 资讯日报-skill/wewe-rss
docker compose up -d
cd ../..
```

打开：

- 管理登录：`http://127.0.0.1:4000/dash/login`
- 账号管理：`http://127.0.0.1:4000/dash/accounts`
- 公众号源：`http://127.0.0.1:4000/dash/feeds`

使用 `WEWE_AUTH_CODE` 登录后台，新增微信读书账号并扫码。不要只启用旧的失效账号。

若迁移包中有 `MIGRATION/wewe-share-links.txt`，在“添加公众号源”窗口中整文件复制、逐行粘贴即可恢复公众号列表。考虑微信读书风控，建议将 12 个源分给两个微信读书账号，每个约 6 个源。

验证聚合 RSS：

```bash
curl -f http://127.0.0.1:4000/feeds/all.rss >/dev/null
```

## 六、安装扫码服务与日报守门器

先运行新机预检：

```bash
bash scripts/new_mac_preflight.sh
```

通过后安装：

```bash
bash automation/install_wewe_relogin.command
bash automation/install_guard.command
```

重登安装器会生成本机专用 token；第二次命令把它同步给日报守门器。14:30 前安装会当天启用，14:30 后安装则从次日启用。

检查服务：

```bash
launchctl print "gui/$(id -u)/com.example.beijing-daily-brief-wewe-relogin"
launchctl print "gui/$(id -u)/com.example.beijing-daily-brief-guard"
docker ps --filter name=wewe-rss
```

## 七、首次验收

先做只抓取测试，不写飞书：

```bash
set -a
source config.env
set +a
"$RIBAO_PYTHON_BIN" 资讯日报-skill/fetch_news.py
```

确认抓取正常后，可手动执行一次完整管线：

```bash
bash run_daily_with_feishu.sh
```

完整管线会真实创建或更新飞书文档与日报索引。确认新机器连续正常运行后，再停用旧机器的守门器和 WeWe 容器，避免两台机器重复生成同一天日报。

## 八、常见问题

- `Codex auth.json not found`：先在新机器登录 Codex CLI。
- `找不到 lark-cli`：确认全局 npm bin 在 `PATH`，并重新配置 user/bot 身份。
- WeWe 扫码后又失效：这通常是微信读书服务端风控，不是 SQLite 丢失；减少单账号源数并重新扫码。
- 公众号列表为空：使用迁移包内的 `MIGRATION/wewe-share-links.txt` 重新添加。
- 新建了第二份飞书索引：说明旧的 `feishu-daily-index.json` 未私密迁移；可继续使用新索引，或恢复旧状态文件后重跑。
