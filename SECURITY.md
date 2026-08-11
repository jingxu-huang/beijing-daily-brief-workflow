# Security policy

Do not commit:

- `config.env`, Feishu environment files, application secrets or webhook credentials;
- Feishu chat/user/message/document identifiers or private document URLs;
- WeWe dashboard passwords, relogin tokens, cookies, account data or QR codes;
- real drafts, reports, QC output, manifests, logs, Codex sessions or caches;
- absolute paths containing a real local username or external-volume label.

Run `bash scripts/verify.sh` before every push and run the audit again against release archives. If sensitive material was ever committed, removing it in a later commit is insufficient: rotate the credential/identifier where applicable and rewrite or replace the repository history before publishing.

Report a vulnerability through a private maintainer channel rather than a public issue when the report contains secret material.
