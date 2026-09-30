# Repository lineage and synchronization boundary

This independent repository is the handoff and update target for Beijing Daily Brief Workflow. Its `main` branch preserves the initial public repository's three Git commits through `b32ec7ad80b39364c36ed27e7430d354d39a231f`. The initial `evalee101010/beijing-daily-brief-workflow` commits are its ancestors.

The workflow was copied into the private Mico repository at Mico commit `bbb515b8c8249bbc158a0a7f4dba2814131ed950`. Subsequent Mico development did not inherit this independent repository's Git objects. On 2026-09-30 the current portable source, tests and public-safe documentation were synchronized back here from Mico branch `codex/zixun-ribao-production-repair` at `d24b904b`. That synchronization is a new commit on this repository's own history, not a merge of Mico history.

Read [Skill history and official-account source evolution](docs/Skill历史与公众号源迭代.md) for the evidence trail. Historical commits explain decisions but must not override current code or the retirement boundaries for WeWe RSS, WeRSS and the download API.

This public repository deliberately omits private Feishu document URLs and IDs, credentials, `config.env`, login state, logs, source snapshots, generated reports and Feishu registry files. Those must be transferred privately and verified on the receiving machine; cloning this repository alone cannot restore production.

For future synchronization, review the exact source diff, copy only reusable source/tests/templates and sanitized documentation, run `bash scripts/verify.sh`, and perform an explicit public-package audit before pushing. Do not run the daily pipeline or change Feishu permissions as part of a code synchronization.
