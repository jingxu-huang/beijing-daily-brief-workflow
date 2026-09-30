# English quick start

This repository generates a daily news brief for the half-open Beijing-time window `[previous day 15:00, current day 15:00)`. Host timezone and manually adjusted wall-clock time are not trusted.

```bash
python3 -m pip install -r 资讯日报-skill/requirements.txt
cp config.env.example config.env
```

Set `RIBAO_DATA_ROOT` to an existing directory on a dedicated external/non-root-mounted volume. `FEISHU_REPORT_CHAT_ID` is optional: when blank, generation and quality control continue, while delivery is explicitly skipped.

Run once:

```bash
bash run_daily_with_feishu.sh
```

Install the production macOS guard:

```bash
bash automation/install_guard.command
```

WeWe RSS has been retired from production. The independent test matrix has its own 15:00 macOS scheduler and uses one frozen source snapshot for three isolated profiles. Once Feishu credentials and both existing index registries are restored, `bash automation/install_parallel.command` installs both schedules. See [the current handoff](运行与交接手册.md). In index-only delivery mode, neither schedule sends group messages; production continues background retries after the 15:30 deadline and only publishes a complete QC-approved report.

Run all local checks before publishing:

```bash
bash scripts/verify.sh
```
