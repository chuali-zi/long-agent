#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

section() { printf '\n=== %s ===\n' "$*"; }

section "directory permissions"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r path; do ls -ld "$path" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for target in data["targets"]:
    print(target["path"])
PY
fi

section "protected logs"
if [[ -f "$STATE_FILE" ]]; then
  PY="$(command -v python3 || command -v python || true)"
  "$PY" - "$STATE_FILE" <<'PY' | while read -r path; do ls -l "$path" || true; done
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    print(art["path"])
PY
fi

section "synthetic logrotate error"
cat <<'EOF'
logrotate[24177]: error: skipping "/var/log/payroll-api/app/current.log" because parent directory has insecure permissions
logrotate[24177]: Set "su" directive in config file to tell logrotate which user/group should be used for rotation.
EOF
