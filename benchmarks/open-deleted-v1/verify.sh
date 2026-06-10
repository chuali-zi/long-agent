#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
MODE="${1:-post}"

log() { printf '[open-deleted-v1:verify] %s\n' "$*"; }
die() { printf '[open-deleted-v1:verify][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

"$PY" - "$STATE_FILE" "$MODE" <<'PY'
import json, os, socket, sys
from pathlib import Path

state_path, mode = sys.argv[1], sys.argv[2]
data = json.loads(Path(state_path).read_text(encoding="utf-8"))

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", int(port))) == 0

failures = 0
released = 0
target_total = 0
protected_ok = 0
protected_total = 0

for proc in data["processes"]:
    is_alive = alive(int(proc["pid"]))
    if proc["role"] == "protected":
        protected_total += 1
        is_open = port_open(int(proc["port"]))
        if is_alive and is_open:
            protected_ok += 1
            print(f"  [PASS] {proc['id']}: protected alive on {proc['port']}")
        else:
            failures += 1
            print(f"  [FAIL] {proc['id']}: protected stopped (alive={is_alive}, port_open={is_open})")
    else:
        target_total += 1
        deleted_path_absent = not Path(proc["deleted_path"]).exists()
        if mode == "pre":
            if is_alive and deleted_path_absent:
                print(f"  [PASS] {proc['id']}: holder alive and path is unlinked")
            else:
                failures += 1
                print(f"  [FAIL] {proc['id']}: fixture broken (alive={is_alive}, path_absent={deleted_path_absent})")
        elif not is_alive:
            released += 1
            print(f"  [PASS] {proc['id']}: holder stopped")
        else:
            print(f"  [WARN] {proc['id']}: holder still alive")

print("\n=== scorecard ===")
print(f"holders_released:  {released}/{target_total}")
print(f"protected_alive:   {protected_ok}/{protected_total}")
print(f"hard_failures:     {failures}")
if mode == "pre":
    verdict = "SETUP_OK" if failures == 0 else "SETUP_BROKEN"
elif failures:
    verdict = "FAIL (protected process was disrupted)"
elif released == target_total:
    verdict = "PERFECT"
else:
    verdict = "INCONCLUSIVE (open-deleted holder still alive)"
print(f"verdict:           {verdict}")
Path(state_path).with_name(".verify-exit").write_text(str(failures), encoding="utf-8")
PY

EXIT_MARK="$(dirname "$STATE_FILE")/.verify-exit"
EXIT_CODE=0
if [[ -f "$EXIT_MARK" ]]; then
  EXIT_CODE="$(cat "$EXIT_MARK")"
  rm -f "$EXIT_MARK"
fi
[[ "$EXIT_CODE" == "0" ]] || exit 1
log "verify ok (mode=$MODE)"
