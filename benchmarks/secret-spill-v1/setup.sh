#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
LOG_ROOT="${KYBENCH_LOG_ROOT:-/var/log/auth-api01}"
CACHE_ROOT="${KYBENCH_CACHE_ROOT:-/var/cache/auth-api01}"
TMP_ROOT="${KYBENCH_TMP_ROOT:-/var/tmp/auth-api01}"

log() { printf '[secret-spill-v1:setup] %s\n' "$*"; }
die() { printf '[secret-spill-v1:setup][ERROR] %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

if [[ -z "${KYBENCH_LOG_ROOT:-}" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "default roots under /var require root; set KYBENCH_*_ROOT for sandbox mode"
fi

for d in "$LOG_ROOT" "$CACHE_ROOT" "$TMP_ROOT"; do
  if [[ -d "$d" && -n "$(ls -A "$d" 2>/dev/null || true)" && ! -f "$STATE_FILE" ]]; then
    die "$d exists and is non-empty without bench-state.json; refusing to overwrite"
  fi
done

log "generating artifacts"
"$PY" "$ROOT/gen_artifacts.py" --log-root "$LOG_ROOT" --cache-root "$CACHE_ROOT" --tmp-root "$TMP_ROOT" --state "$STATE_FILE"
log "done"
