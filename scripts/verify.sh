#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT_DIR/资讯日报-skill${PYTHONPATH:+:$PYTHONPATH}"

python3 -c 'from pathlib import Path; files=list(Path("资讯日报-skill").rglob("*.py"))+list(Path("local-only/feishu-bot").rglob("*.py"))+list(Path("scripts").rglob("*.py")); [compile(p.read_text(encoding="utf-8"), str(p), "exec") for p in files]; print(f"Compiled {len(files)} Python sources in memory")'
python3 -m unittest discover -s 资讯日报-skill/tests -p 'test_*.py'
bash -n run_daily_with_feishu.sh 资讯日报-skill/run_daily.sh 资讯日报-skill/run_guard.sh automation/*.command scripts/*.sh
plutil -lint automation/com.example.beijing-daily-brief-guard.plist.template
python3 scripts/audit_public_package.py --root "$ROOT_DIR"

echo "Verification passed."
