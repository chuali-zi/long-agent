#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

log() { printf '[logrotate-perms-v1:teardown] %s\n' "$*"; }
die() { printf '[logrotate-perms-v1:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

"$PY" - "$STATE_FILE" <<'PY'
import json, shutil, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(data["roots"]["log"])
allowed = str(root).startswith("/var/log/payroll-api") or str(root).startswith("/tmp/")
if allowed and root.exists():
    shutil.rmtree(root, ignore_errors=True)
    print(f"  - removed tree {root}")
else:
    print(f"  ! skip removing unexpected root {root}")
PY
rm -f "$STATE_FILE"
log "done"
