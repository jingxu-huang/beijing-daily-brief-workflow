# WeRSS migration service

This is the maintained, direct-WeRead replacement staged beside legacy WeWe RSS. It is bound to `127.0.0.1:8001`, uses its own named Docker volume, and must not read, copy, or overwrite the legacy WeWe RSS data volume.

The `weread_mp` collector directly calls `weread.qq.com` rather than the unavailable legacy relay.

## ARM64 image provenance

On 2026-09-03, both vendor-published ARM64 image variants (Docker Hub and
GHCR) failed with `exec format error` before their first shell command on this
otherwise verified ARM64 Docker host. The compose file therefore uses the
local image `local/we-mp-rss:v1.5.3`, rebuilt from upstream source tag `v1.5.3`
(base commit `bb29a7a45e516d26cf434c6f349404ff18413727`) plus local source
commits `523ddd8` (article-link subscription error handling) and `2fbfd02`
(secure macOS WeRead-session refresh), with
`Dockerfile.local-arm64`. It is a local build, so it must not be described as
an immutable registry digest.

Rebuilds are deliberate: obtain the selected upstream source revision in a
separate temporary directory, run Docker build with that directory as context
and `Dockerfile.local-arm64`, verify the resulting image reports `linux/arm64`
and can start, then run the workflow verification. Do not auto-pull a mutable
image tag, and do not remove either legacy service or its volume during this
migration.

The local ARM64 build deliberately includes the WebKit Playwright runtime.
WeRSS's supported “add by public article URL” screen needs it to resolve a
public-account identity before it can create a feed. A lookup failure must be
shown as an error and must not be treated as a successfully added subscription.

## One-time local authorization

The direct collector requires a fresh WeRead browser session. Log in locally at `https://weread.qq.com`, open any public-account page, then save the browser session cookie in WeRSS's local management page. Do not paste that cookie into chat, Git, source files, or a report.

After authorization, validate one feed refresh and its RSS output before changing `sources.yaml` or the daily guard. The legacy WeWe service remains read-only evidence and fallback until this validation passes.

## Optional host login refresh agent

The collector can invoke a local macOS refresh agent before each scheduled
incremental run. The agent validates the existing WeRead session first; only
when it is no longer valid does it attempt a headless Chrome refresh, and then
opens a visible scan window if a human re-login is genuinely required.

The agent must listen only on the host path reachable from the local Docker VM
and must require a per-machine token. Keep its token and URL in this
directory's ignored `.env`; do not put them in Git, a report, or chat. The
container must receive both `WEREAD_REFRESH_AGENT_URL` and
`WEREAD_REFRESH_AGENT_TOKEN`. The macOS agent uses a private state directory;
production compose bind-mounts only that one `wx.lic` file, while the SQLite
database and every other service file stay in the independent named volume.
It never reads or alters the legacy WeWe RSS volume.
