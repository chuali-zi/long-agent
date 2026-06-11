#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$ROOT/../lib/common.sh"

DO_ASK=0
DO_TEARDOWN=0
export KYBENCH_RUNTIME_ROOT="${KYBENCH_RUNTIME_ROOT:-/tmp/report-ops}"

log() { printf '[open-deleted-v1:run] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ask) DO_ASK=1; shift ;;
    --teardown) DO_TEARDOWN=1; shift ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$DO_TEARDOWN" == "1" ]]; then exec bash "$ROOT/teardown.sh"; fi

kybench_load_prompt_from_manifest "$ROOT"
PROMPT="${KYBENCH_PROMPT:-}"

log "1/4 setup"
bash "$ROOT/setup.sh"
log "2/4 pre-verify"
bash "$ROOT/verify.sh" pre
log "3/4 probe"
bash "$ROOT/probe.sh"

if [[ "$DO_ASK" == "1" ]]; then
  log "4a running kyagent ask"
  kybench_run_ask "$ROOT" "$PROMPT"
  log "4b post-verify"
  bash "$ROOT/verify.sh" post
else
  log "4/4 skipped ask"
fi
log "done"
