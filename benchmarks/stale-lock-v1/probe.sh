#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

section() { printf '\n=== %s ===\n' "$*"; }

section "lock files"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    p = Path(art["path"])
    print(f"{p} exists={p.exists()} size={p.stat().st_size if p.exists() else 0}")
    if p.exists():
        print(p.read_text(encoding="utf-8", errors="replace").strip())
PY
fi

section "related processes"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r pid; do ps -fp "$pid" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    if "pid" in art:
        print(art["pid"])
PY
fi

section "lsof locks"
if command -v lsof >/dev/null 2>&1 && [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r p; do lsof -- "$p" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    print(art["path"])
PY
fi
