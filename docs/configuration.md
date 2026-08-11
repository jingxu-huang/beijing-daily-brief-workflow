# Configuration reference

Copy `config.env.example` to the gitignored `config.env`.

| Variable | Required | Purpose |
|---|---:|---|
| `RIBAO_DATA_ROOT` | yes | Existing directory on a dedicated external/non-root mount. |
| `FEISHU_REPORT_CHAT_ID` | for delivery | Target Feishu group. Blank means generate + QC and skip delivery. |
| `RIBAO_CODEX_MODEL` | no | Codex model used for composition and repair. |
| `RIBAO_EXISTING_CODEX_HOME` | no | Existing authenticated Codex home; defaults to `$HOME/.codex`. |
| `WEWE_AUTH_CODE` | for WeWe | Local WeWe RSS dashboard/API credential. |
| `WEWE_ALL_FEED_URL` | no | Aggregate feed; defaults to the localhost sample. |
| `RELOGIN_TOKEN` | for remote login | Long public-page token; installer generates it when blank. |
| `RELOGIN_ADMIN_TOKEN` | for remote login | Admin API token; installer generates it when blank. |
| `RIBAO_WEWE_CHECK_1` | fixed | Must remain `13:30`. |
| `RIBAO_WEWE_CHECK_2` | fixed | Must remain `14:30`. |

Do not put secrets in launchd plist templates or source files. The guard installer copies the local config into a mode-600 runtime env file.
