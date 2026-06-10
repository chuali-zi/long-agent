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

section "open deleted files"
if command -v lsof >/dev/null 2>&1; then
  lsof +L1 | grep -E 'report-worker|deleted|report-ops' || true
else
  echo "lsof not available"
fi

section "du runtime root"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  RUNTIME_ROOT="$("$PY" - "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["runtime_root"])
PY
)"
  du -sh "$RUNTIME_ROOT" 2>/dev/null || true
fi
