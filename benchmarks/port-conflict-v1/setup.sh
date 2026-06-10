#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
RUNTIME_ROOT="${KYBENCH_RUNTIME_ROOT:-/tmp/shop-ops}"

log() { printf '[port-conflict-v1:setup] %s\n' "$*"; }
die() { printf '[port-conflict-v1:setup][ERROR] %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

if [[ -d "$RUNTIME_ROOT" && -n "$(ls -A "$RUNTIME_ROOT" 2>/dev/null || true)" && ! -f "$STATE_FILE" ]]; then
  die "$RUNTIME_ROOT is non-empty without bench-state.json; refusing to overwrite"
fi

log "starting process fixtures"
"$PY" "$ROOT/gen_artifacts.py" --runtime-root "$RUNTIME_ROOT" --state "$STATE_FILE"
log "done"
