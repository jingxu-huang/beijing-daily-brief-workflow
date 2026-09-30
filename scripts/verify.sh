#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT_DIR/资讯日报-skill${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${RIBAO_PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$RIBAO_PYTHON_BIN"
elif [ -x "$ROOT_DIR/.venv/bin/python3" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python3"
else
  PYTHON_BIN=python3
fi

"$PYTHON_BIN" -c 'from pathlib import Path; files=list(Path("资讯日报-skill").rglob("*.py"))+list(Path("local-only/feishu-bot").rglob("*.py"))+list(Path("scripts").rglob("*.py")); [compile(p.read_text(encoding="utf-8"), str(p), "exec") for p in files]; print(f"Compiled {len(files)} Python sources in memory")'
"$PYTHON_BIN" -m unittest discover -s 资讯日报-skill/tests -p 'test_*.py'
"$PYTHON_BIN" -m unittest discover -s local-only/feishu-bot/tests -p 'test_*.py'
bash -n bootstrap_new_mac.command run_daily_test_matrix.sh run_daily_test.sh run_daily_with_feishu.sh 资讯日报-skill/run_daily.sh 资讯日报-skill/run_guard.sh automation/*.command scripts/*.sh
[ -x run_daily_test.sh ] || { echo "run_daily_test.sh must be executable" >&2; exit 1; }
plutil -lint automation/com.example.beijing-daily-brief-guard.plist.template
plutil -lint automation/com.example.beijing-daily-brief-test-matrix.plist.template
"$PYTHON_BIN" scripts/audit_public_package.py --root "$ROOT_DIR"

echo "Verification passed."
