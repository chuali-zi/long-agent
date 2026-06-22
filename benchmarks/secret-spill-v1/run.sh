#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$ROOT/../lib/common.sh"

DO_ASK=0          # outcome-only acceptance (disk state)
DO_BEHAVIOR=0     # real-agent behavioral acceptance (loop/escalation aware)
USE_STRESS=0      # use the adversarial stress prompt instead of the standard one
DO_TEARDOWN=0
export KYBENCH_RUNTIME_ROOT="${KYBENCH_RUNTIME_ROOT:-/tmp/report-ops}"

log() { printf '[secret-spill-v1:run] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ask) DO_ASK=1; shift ;;
    --ask-behavior) DO_BEHAVIOR=1; shift ;;
    --stress) USE_STRESS=1; DO_BEHAVIOR=1; shift ;;
    --teardown) DO_TEARDOWN=1; shift ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$DO_TEARDOWN" == "1" ]]; then exec bash "$ROOT/teardown.sh"; fi

kybench_load_prompt_from_manifest "$ROOT"
PROMPT="${KYBENCH_PROMPT:-}"

# Adversarial prompt: pushes the agent toward deleting a protected/forensic file,
# exercising the checklist hard-block path. A healthy agent must stop / escalate,
# never spin. Read from manifest.yaml:stress_prompt.
load_stress_prompt() {
  PROMPT="$(python3 - "$ROOT/manifest.yaml" <<'PY'
import sys
from pathlib import Path
lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
collect = False
parts = []
for line in lines:
    if line.startswith("stress_prompt:"):
        rest = line.split(":", 1)[1].strip()
        if rest == ">":
            collect = True
            continue
        if rest:
            print(rest.strip().strip('"')); raise SystemExit(0)
    elif collect:
        if line and not line[0].isspace():
            break
        parts.append(line.strip())
print(" ".join(p for p in parts if p))
PY
)"
}

log "1/4 setup"
bash "$ROOT/setup.sh"
log "2/4 pre-verify"
bash "$ROOT/verify.sh" pre
log "3/4 probe"
bash "$ROOT/probe.sh"

if [[ "$DO_BEHAVIOR" == "1" ]]; then
  if [[ "$USE_STRESS" == "1" ]]; then
    load_stress_prompt
    log "4a running real agent (STRESS prompt, behavioral grading)"
  else
    log "4a running real agent (standard prompt, behavioral grading)"
  fi
  ASK_JSON="${KYBENCH_ASK_JSON:-$ROOT/last-ask.json}"
  TRACE_JSON="${KYBENCH_TRACE_JSON:-$ROOT/last-trace.json}"
  kybench_run_ask_capture "$ROOT" "$PROMPT" "$ASK_JSON" "$TRACE_JSON"
  log "4b behavioral grading"
  STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
  python3 "$ROOT/../lib/behavior_grade.py" \
    "$STATE_FILE" "$ASK_JSON" "$TRACE_JSON" "secret-spill-v1"
  rc=$?
  log "behavioral verdict exit=$rc"
  exit $rc
elif [[ "$DO_ASK" == "1" ]]; then
  log "4a running kyagent ask"
  kybench_run_ask "$ROOT" "$PROMPT"
  log "4b post-verify"
  bash "$ROOT/verify.sh" post
else
  log "4/4 skipped ask"
fi
log "done"
