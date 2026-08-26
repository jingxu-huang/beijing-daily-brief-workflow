# WeWe RSS setup

1. Copy the repository `config.env` to this directory as `.env`, or create `.env` containing the same strong `WEWE_AUTH_CODE`.
2. Run `docker compose up -d`.
3. Open `http://localhost:4000`, sign in, add a WeRead account and subscribe to the desired public accounts.
4. Keep the aggregate URL `http://localhost:4000/feeds/all.rss`, or update `sources.yaml` with your endpoint.

The compose sample refreshes at 12:00 BJT. The daily guard validates the live login at 11:00, then performs the final full refresh at 14:30. At 15:00 it freezes that source decision: an unavailable WeWe feed is excluded, while all other channels continue.

`AUTH_CODE` is never committed. Do not publish the Docker data volume, account tokens, QR codes or relogin URLs.
