#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

section() { printf '\n=== %s ===\n' "$*"; }

section "cron entries"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r path; do
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    if art["path"].startswith(data["cron_dir"]):
        print(art["path"])
PY
    printf '\n--- %s ---\n' "$path"
    ls -l "$path" || true
    sed -n '1,20p' "$path" || true
  done
fi

section "script metadata"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r path; do ls -l "$path" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    if art["path"].startswith(data["runtime_root"]):
        print(art["path"])
PY
fi

section "payload marker"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
p = Path(data["marker"])
print(f"{p} exists={p.exists()}")
PY
fi
