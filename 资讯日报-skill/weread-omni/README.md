# weread-omni 隔离测试链路

该目录只提供公众号增强源的测试适配，不会默认进入正式日报。正式启用前必须完成多源容量、登录持久化、正文完整率和同窗口质量对照。

## 身份与版本

- 源标识：`weread_omni`
- 上游：`https://github.com/teng-lin/weread-omni.git`
- 已验证版本：`0.1.1`
- 已验证提交：`3a7fee58995ef502f2044d93f11af7b76fdacd0e`

旧源必须使用不同标识：WeWe RSS 为 `wewe_rss`，旧 WeRSS / we-mp-rss 为 `werss`。日志、状态和日报 Source 表不得把三者统称为“RSS”。

## 同步策略

`sync_window.mjs` 不使用上游的聚合订阅 Feed，因为该入口会对每个公众号分别处理 `limit` 篇，公众号增多时首次同步会放大请求。适配器改为：

1. 逐公众号读取文章列表，只回溯北京时间目标窗口所需篇数，单源上限100篇；
2. 保存每个公众号的 `synckey` 和本地快照；跨日也先使用 `synckey` 增量查询，只对确有更新的源回补文章与正文，无新增时直接复用快照；
3. 默认60分钟内不重复探测文章端点，只复用快照并明确返回 `cached`；可用 `RIBAO_WEREAD_OMNI_MIN_SYNC_INTERVAL_MINUTES` 在10至1440分钟间调整；
4. 登录失效硬失败；`-2041` 人工验证或限流在首次出现时立即停止后续源请求，避免扩大风控；其他非认证类的单源刷新失败，在已有快照时降级为 `partial`，并报告 `SOURCE_REFRESH_FAILED_USING_CACHE`，不得冒充健康；
5. 正文优先使用 weread-omni 内容库，避免重复访问 `mp.weixin.qq.com`；
6. 只输出 `[前日15:00, 当日15:00)` 内、具有真实发布时间和完整正文的条目；
7. 文章ID或规范化链接任一重复即在第一层去重，事件级跨源、跨日去重仍由日报抓取器执行；
8. 全程设置 `WEREAD_READONLY=1`，不会自动订阅、退订或修改书架；公众号之间默认间隔750毫秒，可用 `RIBAO_WEREAD_OMNI_REQUEST_DELAY_MS` 在0至5000毫秒内调整。

微信读书的 `mobile /mp/chapters` 会触发官方客户端人机校验。实测12源全量同步后立即逐源复查，12个源均返回 `-2041`。因此测试链路不得按10分钟×12源高频访问；当前只验证“受控低频同步 + 本地快照”，尚未证明可长期无人值守。

`weread-omni` 是微信读书非官方接口的 CLI/SDK 封装，不是带 SLA 的 RSS 服务。它能分页读取订阅号和文章、用 `synckey` 做增量查询，并将已取得正文写入本地库；但不负责定时调度、不保证新文发现时延，也不绕过微信读书人机校验或 `mp.weixin.qq.com` 验证码。`-2041` 表示需要在官方客户端完成人工验证，不是登录令牌失效，无界面任务不得尝试绕过。

适配层必须将“订阅列表可读”、“文章列表已刷新”和“正文已取得”分开记录。空 RSS 外壳不等于渠道可用；复用旧快照时，只能重算目标窗口实际存在的条目，不得沿用前一日的 `expectedWindowItems`。

默认数据根目录位于 `~/Library/Application Support/beijing-daily-brief/weread-omni-test/`，凭据、正文库、游标、快照和Feed均位于Git之外。

## 显式测试

先把上游仓库放到默认数据根目录的 `source/`，或设置 `WEREAD_OMNI_PACKAGE_ROOT`。然后运行：

```bash
node 资讯日报-skill/weread-omni/sync_window.mjs YYYYMMDD
```

把生成的Feed加入测试版候选池时必须同时设置：

```bash
RIBAO_ENABLE_WEREAD_OMNI_TEST=1
RIBAO_WEREAD_OMNI_FEED_URL=file:///绝对路径/weread-omni-test.rss
RIBAO_EXCLUDE_WERSS=1
python 资讯日报-skill/fetch_news.py --date YYYYMMDD --editorial-profile business_v1_test
```

未显式设置 `RIBAO_ENABLE_WEREAD_OMNI_TEST=1` 时，新源不会运行。正式 `run_daily.sh` 也不会自动生成或接入该Feed。

## 每日稳定性测试

测试版采用低频快照，不做10分钟轮询：北京时间12:00和14:30分别运行一次 `run_sync_test.sh`；15:00 由仓库根目录的 `run_daily_test_matrix.sh` 与正式日报同步启动。协调器只补一次缺失同步、只抓取一次公开源和公众号源并冻结原始候选，再让严格/平衡/高召回三版并行处理该同一快照。测试版产物使用独立数据目录并写入独立飞书测试文档；不更新正式索引、不授予小组可管理权限、不发群消息，也不修改正式版成稿。

每次同步会同时写入带时间戳的原始观测文件和 `latest-YYYYMMDD.json`。测试日报会把通道状态脱敏写入 manifest 和质检：通道异常不阻断公开源成稿，但该轮不得标记为“公众号全链路通过”。只有观测日期、RSS目标窗口和条目数一致，且无陈旧源，才算覆盖验收完整。

```bash
bash 资讯日报-skill/weread-omni/run_sync_test.sh
bash run_daily_test.sh
```

## 状态码

- `0`：本轮远端同步健康，或在最小间隔内明确返回 `cached`；
- `3`：认证失效，需要重新扫码；
- `4`：上游或网络失败，不应误报成账号掉线；
- `5`：正文不完整，或非认证故障后使用旧快照，状态为 `partial`；
- `6`：已有同步正在运行，拒绝双跑。
- `7`：微信读书返回 `-2041`，需在官方客户端完成人工验证；不得当作登录失效或普通网络错误。
