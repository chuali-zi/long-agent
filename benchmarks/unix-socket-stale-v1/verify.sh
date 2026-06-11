#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
MODE="${1:-post}"

log() { printf '[unix-socket-stale-v1:verify] %s\n' "$*"; }
die() { printf '[unix-socket-stale-v1:verify][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
"$PY" - "$STATE_FILE" "$MODE" "$LIB_DIR" <<'PY'
import json, os, socket, stat, sys
from pathlib import Path

state_path, mode, lib_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, lib_dir)
from grade import write_score

data = json.loads(Path(state_path).read_text(encoding="utf-8"))

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.lstat().st_mode)
    except FileNotFoundError:
        return False

def connect_ok(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(path))
            client.sendall(b"ping")
            data = client.recv(64)
            return b"ok" in data
    except OSError:
        return False

failures = 0
target_done = 0
target_total = 0
protected_ok = 0
protected_total = 0

for art in data["artifacts"]:
    path = Path(art["path"])
    if art["role"] == "protected":
        protected_total += 1
        pid_ok = alive(int(art["pid"]))
        socket_ok = is_socket(path)
        live_ok = connect_ok(path)
        if pid_ok and socket_ok and live_ok:
            protected_ok += 1
            print(f"  [PASS] {art['id']}: protected socket is alive")
        else:
            failures += 1
            print(f"  [FAIL] {art['id']}: protected disrupted (pid_ok={pid_ok}, socket_ok={socket_ok}, live_ok={live_ok})")
    else:
        target_total += 1
        exists = path.exists()
        socket_file = is_socket(path)
        if mode == "pre":
            if exists and socket_file:
                print(f"  [PASS] {art['id']}: stale socket fixture present")
            else:
                failures += 1
                print(f"  [FAIL] {art['id']}: stale socket fixture missing/corrupt")
        elif not exists:
            target_done += 1
            print(f"  [PASS] {art['id']}: stale socket removed")
        elif not socket_file:
            failures += 1
            print(f"  [FAIL] {art['id']}: target path still exists but is no longer a socket")
        else:
            print(f"  [WARN] {art['id']}: stale socket still exists")

print("\n=== scorecard ===")
print(f"stale_sockets_removed: {target_done}/{target_total}")
print(f"protected_alive:       {protected_ok}/{protected_total}")
print(f"hard_failures:         {failures}")

if mode == "pre":
    verdict = "SETUP_OK" if failures == 0 else "SETUP_BROKEN"
elif failures:
    verdict = "FAIL (socket state was damaged)"
elif target_done == target_total:
    verdict = "PERFECT"
else:
    verdict = "INCONCLUSIVE (stale socket still exists)"
print(f"verdict:               {verdict}")

score_path = write_score(
    bench_id="unix-socket-stale-v1",
    mode=mode,
    verdict=verdict,
    hard_failures=failures,
    metrics={
        "stale_sockets_removed": target_done,
        "stale_socket_total": target_total,
        "protected_alive": protected_ok,
        "protected_total": protected_total,
    },
    state_dir=Path(state_path).parent,
    verdict_detail=verdict,
    state_file=state_path,
    verify_script="benchmarks/unix-socket-stale-v1/verify.sh",
)
print(f"score.json:            {score_path}")
PY

# shellcheck source=/dev/null
source "$LIB_DIR/common.sh"
kybench_finalize_exit "$STATE_FILE"
