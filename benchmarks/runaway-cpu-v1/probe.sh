#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

section() { printf '\n=== %s ===\n' "$*"; }

section "state summary"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for proc in data["processes"]:
    print(proc)
PY
fi

section "top cpu processes"
ps -eo user,pid,pcpu,pmem,etime,stat,comm,args --sort=-pcpu | head -20 || true

section "protected port"
if command -v ss >/dev/null 2>&1; then
  ss -tlnp | grep -E '18281|28281' || true
fi
