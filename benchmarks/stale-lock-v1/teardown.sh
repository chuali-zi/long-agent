#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

log() { printf '[stale-lock-v1:teardown] %s\n' "$*"; }
die() { printf '[stale-lock-v1:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "missing state file: $STATE_FILE"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

"$PY" - "$STATE_FILE" <<'PY'
import json, os, shutil, signal, sys, time
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(data["runtime_root"])

def pid_matches(pid: int) -> bool:
    proc = Path("/proc") / str(pid)
    if not proc.exists():
        return True
    for item in ("cwd",):
        try:
            if os.readlink(proc / item).startswith(str(root)):
                return True
        except OSError:
            pass
    try:
        cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        return str(root) in cmd or "backup_sync_lock_holder.py" in cmd
    except OSError:
        return False

for art in data["artifacts"]:
    if "pid" not in art:
        continue
    pid = int(art["pid"])
    if not pid_matches(pid):
        print(f"  ! skip pid {pid}: no longer matches runtime root")
        continue
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"  - terminated pid {pid}")
    except ProcessLookupError:
        pass
time.sleep(0.3)
for art in data["artifacts"]:
    if "pid" not in art:
        continue
    pid = int(art["pid"])
    if not pid_matches(pid):
        continue
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"  - killed pid {pid}")
    except ProcessLookupError:
        pass
if str(root).startswith("/tmp/") and root.exists():
    shutil.rmtree(root, ignore_errors=True)
    print(f"  - removed tree {root}")
PY
rm -f "$STATE_FILE"
log "done"
