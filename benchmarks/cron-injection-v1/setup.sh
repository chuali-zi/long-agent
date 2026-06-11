#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
RUNTIME_ROOT="${KYBENCH_RUNTIME_ROOT:-/tmp/secops-cron}"
CRON_DIR="${KYBENCH_CRON_DIR:-/etc/cron.d}"

log() { printf '[cron-injection-v1:setup] %s\n' "$*"; }
die() { printf '[cron-injection-v1:setup][ERROR] %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python is required"

if [[ -d "$RUNTIME_ROOT" && -n "$(ls -A "$RUNTIME_ROOT" 2>/dev/null || true)" && ! -f "$STATE_FILE" ]]; then
  die "$RUNTIME_ROOT is non-empty without bench-state.json; refusing to overwrite"
fi
for f in "$CRON_DIR/sys-stat-sync" "$CRON_DIR/nightly-ledger-backup"; do
  if [[ -e "$f" && ! -f "$STATE_FILE" ]]; then
    die "$f already exists and no bench-state.json is present; refusing to overwrite"
  fi
done

log "creating cron prompt-injection fixture"
"$PY" "$ROOT/gen_artifacts.py" --runtime-root "$RUNTIME_ROOT" --cron-dir "$CRON_DIR" --state "$STATE_FILE"
log "done"
