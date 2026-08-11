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
shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"
python3 scripts/audit_public_package.py --root "$ROOT_DIR" --archive "$ARCHIVE"
echo "$ARCHIVE"
echo "$ARCHIVE.sha256"
