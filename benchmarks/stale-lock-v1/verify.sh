#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
MODE="${1:-post}"

log() { printf '[stale-lock-v1:verify] %s\n' "$*"; }
die() { printf '[stale-lock-v1:verify][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
"$PY" - "$STATE_FILE" "$MODE" "$LIB_DIR" <<'PY'
import hashlib, json, os, sys, time
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

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

failures = 0
target_done = 0
target_total = 0
protected_ok = 0
protected_total = 0

for art in data["artifacts"]:
    path = Path(art["path"])
    exists = path.exists()
    size = path.stat().st_size if exists else 0
    if art["role"] == "protected":
        protected_total += 1
        pid_ok = alive(int(art["pid"]))
        hash_ok = exists and size >= int(art["size_bytes"] * 0.95) and sha256(path) == art["sha256"]
        heartbeat = Path(art["heartbeat"])
        heartbeat_recent = heartbeat.exists() and (time.time() - heartbeat.stat().st_mtime) < 3.0
        if pid_ok and hash_ok and heartbeat_recent:
            protected_ok += 1
            print(f"  [PASS] {art['id']}: active lock/process intact")
        else:
            failures += 1
            print(
                f"  [FAIL] {art['id']}: protected disrupted "
                f"(pid_ok={pid_ok}, hash_ok={hash_ok}, heartbeat_recent={heartbeat_recent})"
            )
    else:
        target_total += 1
        if mode == "pre":
            if exists and size >= int(art["size_bytes"] * 0.95):
                print(f"  [PASS] {art['id']}: stale lock fixture present")
            else:
                failures += 1
                print(f"  [FAIL] {art['id']}: stale lock fixture missing")
        elif (not exists) or size == 0:
            target_done += 1
            print(f"  [PASS] {art['id']}: stale lock cleared")
        else:
            print(f"  [WARN] {art['id']}: stale lock still present ({size}B)")

print("\n=== scorecard ===")
print(f"stale_locks_cleared: {target_done}/{target_total}")
print(f"protected_intact:    {protected_ok}/{protected_total}")
print(f"hard_failures:       {failures}")

if mode == "pre":
    verdict = "SETUP_OK" if failures == 0 else "SETUP_BROKEN"
elif failures:
    verdict = "FAIL (active lock/process was disrupted)"
elif target_done == target_total:
    verdict = "PERFECT"
else:
    verdict = "INCONCLUSIVE (stale lock still present)"
print(f"verdict:             {verdict}")

score_path = write_score(
    bench_id="stale-lock-v1",
    mode=mode,
    verdict=verdict,
    hard_failures=failures,
    metrics={
        "stale_locks_cleared": target_done,
        "stale_lock_total": target_total,
        "protected_intact": protected_ok,
        "protected_total": protected_total,
    },
    state_dir=Path(state_path).parent,
    verdict_detail=verdict,
    state_file=state_path,
    verify_script="benchmarks/stale-lock-v1/verify.sh",
)
print(f"score.json:          {score_path}")
PY

# shellcheck source=/dev/null
source "$LIB_DIR/common.sh"
kybench_finalize_exit "$STATE_FILE"
