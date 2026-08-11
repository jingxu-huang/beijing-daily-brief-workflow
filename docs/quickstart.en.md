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

Install the single macOS scheduler:

```bash
bash automation/install_guard.command
```

WeWe RSS is optional and never blocks the other sources after 15:00 BJT. Fixed login reminders occur at 13:30 and, if still unavailable, 14:30. At the 15:30 hard deadline the workflow alerts once, continues in the background, and sends only the complete QC-approved report later.

Run all local checks before publishing:

```bash
bash scripts/verify.sh
```
