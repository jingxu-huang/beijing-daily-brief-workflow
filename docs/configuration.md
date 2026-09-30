# Configuration reference

Copy `config.env.example` to the gitignored `config.env`.

| Variable | Required | Purpose |
|---|---:|---|
| `RIBAO_DATA_ROOT` | yes | Existing directory on a dedicated external/non-root mount. |
| `FEISHU_REPORT_CHAT_ID` | for delivery | Target Feishu group. Blank means generate + QC and skip delivery. |
| `RIBAO_ENABLE_FEISHU` | for Feishu | `1` enables document and index writes; `0` keeps only local output. |
| `FEISHU_DOC_ONLY` | for index delivery | `1` imports or updates documents without sending an IM. |
| `FEISHU_DAILY_INDEX` | for production | `1` updates the existing fixed production index after QA and document permission checks. |
| `RIBAO_GROUP_MESSAGES` | yes | Keep `0` for both current workflows; manual errors also must not send group alerts. |
| `FEISHU_CLI_IMPORT_AS` / `FEISHU_CLI_PERMISSION_AS` | for Feishu | Use the authorized `user` identity for current index-only delivery. |
| `FEISHU_TEST_INDEX_CHAT_ID` | optional | Test-index/edit-permission audience; defaults to `FEISHU_REPORT_CHAT_ID`. This is not a message destination. |
| `RIBAO_TEST_DATA_ROOT` | optional | Test-only data directory; default is `$RIBAO_DATA_ROOT/测试版`. It must not equal the production data root. |
| `RIBAO_TEST_CODEX_AUDIT_MODE` | optional | `auto` by default; `off` disables only the shared test candidate audit. Audit failures fall back to native rules. |
| `RIBAO_CODEX_MODEL` | no | Codex model used for composition and repair. |
| `RIBAO_EXISTING_CODEX_HOME` | no | Existing authenticated Codex home; defaults to `$HOME/.codex`. |
| `RIBAO_ENABLE_REPORT_IMAGES` | no | Defaults to `1`; prefetch and visually audit candidate images without blocking text delivery. |
| `RIBAO_IMAGE_AUDIT_TIMEOUT_SECONDS` | no | Visual audit timeout, clamped to 10–120 seconds; timeout records a media warning only. |
| `WEWE_AUTH_CODE` | historical only | Retained local WeWe RSS credential; ignored by the formal workflow. |
| `WEWE_ALL_FEED_URL` | historical only | Retained aggregate-feed address; ignored by the formal workflow. |
| `RELOGIN_TOKEN` | historical only | Retained old QR-page token; ignored by formal production. |
| `RELOGIN_ADMIN_TOKEN` | historical only | Retained old QR admin token; ignored by formal production. |
| `RIBAO_WEWE_RETIRED` | `1` | Formal guard hard-disables all WeWe RSS probes, refreshes and login reminders. |
| `RIBAO_ENABLE_WERSS` | no | Keep unset/`0`; old WeRSS code is retained but not an approved production source. |
| `RIBAO_WEWE_CHECK_*` | historical only | Old 11:00/12:00/14:30 timings, retained for audit and never executed by formal production. |

Do not put secrets in launchd plist templates or source files. The guard installer copies the local config into a mode-600 runtime env file.
