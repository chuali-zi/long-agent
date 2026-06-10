#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

log() { printf '[secret-spill-v1:teardown] %s\n' "$*"; }
die() { printf '[secret-spill-v1:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

"$PY" - "$STATE_FILE" <<'PY'
import json, shutil, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    p = Path(art["path"])
    if p.exists():
        p.unlink()
        print(f"  - removed {p}")
for root in sorted(data["roots"].values(), key=len, reverse=True):
    rp = Path(root)
    if rp.exists():
        shutil.rmtree(rp, ignore_errors=True)
        print(f"  - removed tree {rp}")
PY
rm -f "$STATE_FILE"
log "done"
