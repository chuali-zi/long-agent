#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

section() { printf '\n=== %s ===\n' "$*"; }

section "socket paths"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY'
import json, stat, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    p = Path(art["path"])
    kind = "missing"
    if p.exists():
        mode = p.lstat().st_mode
        kind = "socket" if stat.S_ISSOCK(mode) else oct(mode)
    print(f"{p} exists={p.exists()} type={kind}")
PY
fi

section "unix sockets"
if command -v ss >/dev/null 2>&1; then
  ss -xlpn | grep -F '/tmp/socket-ops' || true
fi

section "lsof unix"
if command -v lsof >/dev/null 2>&1; then
  lsof -nP -U | grep -F '/tmp/socket-ops' || true
fi

section "protected process"
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
