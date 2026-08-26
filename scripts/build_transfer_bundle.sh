#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(date '+%Y%m%d')}"
NAME="beijing-daily-brief-workflow-transfer-${VERSION}"
ARCHIVE="$ROOT_DIR/release/$NAME.zip"
CHECKSUM="$ARCHIVE.sha256"
PYTHON_BIN="${RIBAO_PYTHON_BIN:-python3}"
WEWE_URL="${WEWE_RSS_URL:-http://127.0.0.1:4000}"
FALLBACK_INVENTORY_FILE="${WEWE_MIGRATION_INVENTORY_FILE:-}"

cd "$ROOT_DIR"
git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "This transfer builder must run from a Git working tree." >&2
  exit 1
}
mkdir -p release

"$PYTHON_BIN" - "$ROOT_DIR" "$ARCHIVE" "$NAME" "$WEWE_URL" "$FALLBACK_INVENTORY_FILE" <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve()
archive_path = Path(sys.argv[2]).resolve()
name = sys.argv[3]
wewe_url = sys.argv[4].rstrip("/")
fallback_inventory_file = Path(sys.argv[5]).expanduser() if sys.argv[5] else None

listed = subprocess.run(
    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    cwd=root,
    check=True,
    capture_output=True,
).stdout.split(b"\0")
files = []
for raw in listed:
    if not raw:
        continue
    relative = Path(os.fsdecode(raw))
    source = root / relative
    if not source.is_file() or source.is_symlink():
        continue
    if relative.parts and relative.parts[0] == "release":
        continue
    files.append((relative, source))


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "daily-brief-transfer/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


inventory = []
inventory_error = ""
try:
    feeds = fetch_json(f"{wewe_url}/feeds")
    for feed in feeds:
        feed_id = str(feed.get("id") or "").strip()
        if not feed_id:
            continue
        payload = fetch_json(
            f"{wewe_url}/feeds/{urllib.parse.quote(feed_id, safe='')}.json?limit=1"
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        example_url = str(items[0].get("url") or "") if items else ""
        inventory.append(
            {
                "name": str(feed.get("name") or feed_id),
                "feed_id": feed_id,
                "example_article_url": example_url,
            }
        )
except Exception as exc:  # optional migration inventory
    inventory_error = f"WeWe inventory export unavailable: {type(exc).__name__}"

if not inventory and fallback_inventory_file and fallback_inventory_file.is_file():
    fallback = json.loads(fallback_inventory_file.read_text(encoding="utf-8"))
    if not isinstance(fallback, list):
        raise SystemExit("WEWE_MIGRATION_INVENTORY_FILE must contain a JSON list")
    for item in fallback:
        if not isinstance(item, dict):
            continue
        feed_id = str(item.get("feed_id") or "").strip()
        example_url = str(item.get("example_article_url") or "").strip()
        if feed_id and example_url.startswith("https://mp.weixin.qq.com/"):
            inventory.append(
                {
                    "name": str(item.get("name") or feed_id),
                    "feed_id": feed_id,
                    "example_article_url": example_url,
                }
            )

manifest = {
    "package": name,
    "built_at_utc": datetime.now(timezone.utc).isoformat(),
    "git_head": subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip(),
    "contains_working_tree_changes": bool(
        subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True).stdout
    ),
    "contains_secrets": False,
    "wewe_feed_inventory_count": len(inventory),
}

archive_path.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
    for relative, source in sorted(files):
        bundle.write(source, PurePosixPath(name, relative.as_posix()))
    bundle.writestr(
        str(PurePosixPath(name, "MIGRATION", "bundle-manifest.json")),
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    if inventory:
        bundle.writestr(
            str(PurePosixPath(name, "MIGRATION", "wewe-feed-inventory.json")),
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        )
        links = "\n".join(item["example_article_url"] for item in inventory if item["example_article_url"])
        bundle.writestr(
            str(PurePosixPath(name, "MIGRATION", "wewe-share-links.txt")),
            links + ("\n" if links else ""),
        )
    elif inventory_error:
        bundle.writestr(
            str(PurePosixPath(name, "MIGRATION", "wewe-inventory-status.txt")),
            inventory_error + "\n",
        )
PY

"$PYTHON_BIN" - "$ARCHIVE" "$CHECKSUM" <<'PY'
import hashlib
import sys
from pathlib import Path

archive, checksum = map(Path, sys.argv[1:])
checksum.write_text(
    f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
    encoding="utf-8",
)
PY

"$PYTHON_BIN" scripts/audit_public_package.py \
  --root "$ROOT_DIR" \
  --archive "$ARCHIVE" \
  --asset "$CHECKSUM" \
  --deny-local-context

echo "$ARCHIVE"
echo "$CHECKSUM"
