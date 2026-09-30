# Feishu delivery

`send_to_feishu_cli.py` imports the QC-approved Markdown report as Docx with `lark-cli`. In the current index-only production mode it runs with `--doc-only` and user identity: it does not send IM, but it must grant the configured group `full_access` on the daily Docx. `update_daily_index.py` then updates the pinned index Docx and grants the same group `full_access`. Weaker `view` or `edit` roles are rejected for both documents.

Copy `feishu_bot.env.example` to the gitignored `feishu_bot.env` only if you prefer a separate Feishu env file. The root `config.env` is sufficient for scheduled runs.

There is no built-in chat ID. Set `FEISHU_REPORT_CHAT_ID` explicitly. If it is absent, the root wrapper keeps the generated report and records that publishing was skipped.

Dry run:

```bash
python3 local-only/feishu-bot/send_to_feishu_cli.py \
  --report examples/资讯类简报-1.2日.example.md \
  --check examples/资讯日报-质检-20300102.example.md \
  --date 20300102 \
  --chat-id '<configured-chat-id>' \
  --dry-run
```
