#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-v1.0.0}"
NAME="beijing-daily-brief-workflow-${VERSION}"
ARCHIVE="$ROOT_DIR/release/$NAME.zip"

cd "$ROOT_DIR"
test -d .git || { echo "Initialize and commit the clean repository first." >&2; exit 1; }
mkdir -p release
git archive --format=zip --prefix="$NAME/" --output="$ARCHIVE" HEAD
python3 - "$ARCHIVE" "$ARCHIVE.sha256" "$NAME.zip" <<'PY'
import hashlib
import sys
from pathlib import Path

archive, checksum, public_name = map(Path, sys.argv[1:])
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
checksum.write_text(f"{digest}  {public_name.name}\n", encoding="utf-8")
PY
python3 scripts/audit_public_package.py --root "$ROOT_DIR" --archive "$ARCHIVE" --asset "$ARCHIVE.sha256"
echo "$ARCHIVE"
echo "$ARCHIVE.sha256"
