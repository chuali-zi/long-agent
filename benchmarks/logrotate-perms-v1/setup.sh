#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
LOG_ROOT="${KYBENCH_LOG_ROOT:-/var/log/payroll-api}"

log() { printf '[logrotate-perms-v1:setup] %s\n' "$*"; }
die() { printf '[logrotate-perms-v1:setup][ERROR] %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

if [[ -d "$LOG_ROOT" && -n "$(ls -A "$LOG_ROOT" 2>/dev/null || true)" && ! -f "$STATE_FILE" ]]; then
  die "$LOG_ROOT is non-empty without bench-state.json; refusing to overwrite"
fi

log "creating insecure logrotate permission fixture"
"$PY" "$ROOT/gen_artifacts.py" --log-root "$LOG_ROOT" --state "$STATE_FILE"
log "done"
