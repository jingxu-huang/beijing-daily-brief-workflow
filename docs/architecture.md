# Architecture

## Time and delivery state machine

```mermaid
flowchart LR
    T["Trusted UTC quorum"] --> B["Asia/Shanghai business clock"]
    B --> W1["11:00 WeWe live-login validation + two-channel reminder"]
    W1 --> W2["12:00 full refresh"]
    W2 --> W3["14:30 final full refresh"]
    W3 --> C["15:00 freeze source set and close window"]
    C --> F["Fetch other sources; WeWe is optional"]
    F --> G["Codex stdin-only composition"]
    G --> Q["Independent QC; up to 2 repair rounds"]
    Q -->|"PASS or WARN"| P["Idempotent Feishu Docx delivery"]
    Q -->|"FAIL"| R["10-minute background retry"]
    P --> D{"Delivered by 15:30?"}
    D -->|"No"| A["One Feishu alert + one local popup"]
    A --> R
    R --> G
```

## Storage boundaries

- `RIBAO_PROJECT_ROOT` contains trusted source code and templates.
- `RIBAO_DATA_ROOT` contains drafts, reports, QC, manifests, full text, Codex work and caches. Production validation rejects the system root mount and common system temporary/home roots.
- The Codex model receives the skill, format rules, draft and limited style references through stdin only.
- Model file, shell, browser, plugin, app and multi-agent tools are disabled; the run is `--ephemeral` and read-only.
- A trusted wrapper validates schema/QC and performs final filesystem writes on the data volume.
- The small local launchd control directory holds only static config/schema and a symlink to existing Codex authentication; daily content never enters it.

## Scheduler ownership

`com.example.beijing-daily-brief-guard` is the only daily scheduler template in this repository. The WeWe relogin service is a keep-alive HTTP helper, not a second time-based report task. The guard uses a non-blocking file lock, per-day state and idempotency keys to suppress duplicate work and duplicate messages.
