# WeWe RSS setup

1. Copy the repository `config.env` to this directory as `.env`, or create `.env` containing the same strong `WEWE_AUTH_CODE`.
2. Run `docker compose up -d`.
3. Open `http://localhost:4000`, sign in, add a WeRead account and subscribe to the desired public accounts.
4. Keep the aggregate URL `http://localhost:4000/feeds/all.rss`, or update `sources.yaml` with your endpoint.

The compose sample refreshes at 12:00 BJT. The daily guard validates the live login at 11:00, then performs the final full refresh at 14:30. At 15:00 it freezes that source decision: an unavailable WeWe feed is excluded, while all other channels continue.

## Platform failover

WeWe RSS uses a platform relay to reach WeRead rather than a direct public-account API. The historical primary remains the default and is retained for traceability. Its maintainer-documented mirror is `https://weread.965111.xyz`, selectable through the ignored local `WEWE_PLATFORM_URL` override. A live validation on 2026-09-03 found the primary returning HTTP 502 and the mirror application route returning HTTP 404, so the mirror is recorded but not promoted as a working automatic fallback. Do not delete the Docker data volume while switching.

The upstream never published a `wewe-rss-sqlite:2.6.1` tag. The compose file pins the locally verified v2.6.1 image by its repository digest instead, so a recreate cannot silently substitute a mutable `latest` image.

`AUTH_CODE` is never committed. Do not publish the Docker data volume, account tokens, QR codes or relogin URLs.
