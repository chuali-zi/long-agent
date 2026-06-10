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
    print(f"{proc['role']:9s} pid={proc['pid']} port={proc['port']} cwd={proc['cwd']}")
PY
fi

section "listening ports"
if command -v ss >/dev/null 2>&1; then
  ss -tlnp | grep -E '18080|18081|28080|28081' || true
elif command -v netstat >/dev/null 2>&1; then
  netstat -tlnp | grep -E '18080|18081|28080|28081' || true
fi

section "lsof by port"
if [[ -f "$STATE_FILE" ]] && command -v lsof >/dev/null 2>&1; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r port; do lsof -nP -i "TCP:$port" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for proc in data["processes"]:
    print(proc["port"])
PY
fi
