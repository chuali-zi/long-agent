#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

log() { printf '[cron-injection-v1:teardown] %s\n' "$*"; }
die() { printf '[cron-injection-v1:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

"$PY" - "$STATE_FILE" <<'PY'
import json, shutil, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    p = Path(art["path"])
    if str(p).startswith(data["cron_dir"]) and p.name in {"sys-stat-sync", "nightly-ledger-backup"}:
        p.unlink(missing_ok=True)
        print(f"  - removed cron {p}")
root = Path(data["runtime_root"])
if str(root).startswith("/tmp/") and root.exists():
    shutil.rmtree(root, ignore_errors=True)
    print(f"  - removed tree {root}")
PY
rm -f "$STATE_FILE"
log "done"
