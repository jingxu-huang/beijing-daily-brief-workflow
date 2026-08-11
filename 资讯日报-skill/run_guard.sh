#!/bin/bash
# launchd/TCC shim: /bin/bash already owns the project's approved automation access.
set -e
RUNTIME_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARD_ENV="${RIBAO_GUARD_ENV:-$RUNTIME_DIR/guard.env}"
if [ -f "$GUARD_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$GUARD_ENV"
  set +a
fi
exec /usr/bin/python3 "$RUNTIME_DIR/ribao_guard.py" "$@"
