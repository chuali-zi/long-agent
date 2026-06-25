#!/usr/bin/env bash
# Run the RealOps benchmark suite with strict grading (PERFECT-only pass).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
# shellcheck source=lib/common.sh
source "$ROOT/lib/common.sh"

LOG_DIR="${KYBENCH_LOG_DIR:-/tmp/kybench-runs}"
STAMP="${KYBENCH_RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
SETUP_PERMISSIONS=0
TEARDOWN_EACH=0
OUTCOME_ONLY=0

BENCHES=(
  cleanup-v2
  secret-spill-v1
  port-conflict-v1
  open-deleted-v1
  runaway-cpu-v1
  stale-lock-v1
  unix-socket-stale-v1
  logrotate-perms-v1
  cron-injection-v1
)

log() { printf '[kybench:suite] %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: sudo bash benchmarks/run-suite.sh [options] [bench ...]

Runs each benchmark: setup → pre-verify → kyagent ask → post-verify (strict).

Options:
  --setup-permissions-prod  Install production sudoers before running.
  --teardown-each             Teardown each bench after its run.
  --outcome-only              Skip trace/behavior grading (faster compatibility mode).
  --log-dir DIR             Store logs and score copies under DIR.
  -h, --help                Show help.

Exit codes (suite):
  0  All selected benches scored PERFECT
  N  Number of benches that did not score PERFECT

Environment:
  KYAGENT_INSTALL_PREFIX, KYAGENT_ENV_FILE, KYAGENT_USER
  KYBENCH_PROMPT              Override ticket prompt for all benches
  KYBENCH_LOG_DIR, KYBENCH_RUN_ID
EOF
}

selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup-permissions-prod) SETUP_PERMISSIONS=1; shift ;;
    --teardown-each) TEARDOWN_EACH=1; shift ;;
    --outcome-only) OUTCOME_ONLY=1; shift ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) selected+=("$1"); shift ;;
  esac
done
if [[ ${#selected[@]} -gt 0 ]]; then
  BENCHES=("${selected[@]}")
fi

if [[ $EUID -ne 0 ]]; then
  echo "must run as root: sudo bash benchmarks/run-suite.sh" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
summary="$LOG_DIR/kybench-summary-$STAMP.tsv"
results_dir="$LOG_DIR/kybench-results-$STAMP"
mkdir -p "$results_dir"
: > "$summary"
printf 'bench\tmode\tgrade\tverdict\tbehavior\texit_code\tlog\tscore\tartifacts\n' >> "$summary"

if [[ "$SETUP_PERMISSIONS" == "1" ]]; then
  log "installing permissions-prod sudoers"
  bash "$REPO_ROOT/scripts/setup-sudoers-prod.sh" --yes
  visudo -cf /etc/sudoers.d/kyagent
fi

failures=0
for bench in "${BENCHES[@]}"; do
  bench_dir="$ROOT/$bench"
  run_script="$bench_dir/run.sh"
  log_file="$LOG_DIR/kybench-$bench-$STAMP.log"
  score_copy="$results_dir/$bench.score.json"
  artifact_dir="$results_dir/$bench.behavior"
  run_flag="--ask-behavior"
  mode="behavior"
  if [[ "$OUTCOME_ONLY" == "1" ]]; then
    run_flag="--ask"
    mode="outcome-only"
  else
    mkdir -p "$artifact_dir"
    chmod 0700 "$artifact_dir"
  fi

  if [[ ! -f "$run_script" ]]; then
    log "missing benchmark: $bench"
    printf '%s\t%s\tMISSING\t-\tFAIL\t10\t%s\t-\t%s\n' \
      "$bench" "$mode" "$log_file" "$artifact_dir" >> "$summary"
    failures=$((failures + 1))
    continue
  fi

  log "running $bench -> $log_file"
  if KYBENCH_ARTIFACT_DIR="$artifact_dir" bash "$run_script" "$run_flag" 2>&1 | tee "$log_file"; then
    rc=0
  else
    rc=$?
  fi

  verdict="-"
  exit_code="$rc"
  grade="FAIL"
  behavior_status="NOT_RUN"
  score_src="${KYBENCH_STATE:-$bench_dir/bench-state.json}"
  score_src="$(dirname "$score_src")/score.json"
  if [[ -f "$score_src" ]]; then
    cp -f "$score_src" "$score_copy"
    py="$(command -v python3 || command -v python)"
    verdict="$("$py" - "$score_copy" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("verdict", "?"))
PY
)"
    behavior_status="$("$py" - "$score_copy" "$mode" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if sys.argv[2] == "outcome-only":
    print("NOT_RUN")
else:
    required = {"no_max_iteration_spin", "no_stuck_tool_loop", "real_backend", "behavior_evidence_available"}
    checks = {c.get("name"): bool(c.get("pass")) for c in d.get("checks", [])}
    print("PASS" if required <= checks.keys() and all(checks[n] for n in required) else "FAIL")
PY
)"
    if "$py" "$ROOT/lib/grade.py" exit "$score_copy"; then
      exit_code=0
    else
      exit_code=$?
    fi
  fi

  if [[ "$rc" -eq 0 && "$verdict" == "PERFECT" ]]; then
    grade="PERFECT"
  else
    case "$verdict" in
      PARTIAL*) grade="PARTIAL" ;;
      INCONCLUSIVE*) grade="INCONCLUSIVE" ;;
      FAIL*) grade="FAIL" ;;
      *) grade="FAIL" ;;
    esac
  fi

  if [[ "$grade" != "PERFECT" ]]; then
    failures=$((failures + 1))
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$bench" "$mode" "$grade" "$verdict" "$behavior_status" "$exit_code" \
    "$log_file" "$score_copy" "$artifact_dir" >> "$summary"

  if [[ "$TEARDOWN_EACH" == "1" && -f "$bench_dir/teardown.sh" ]]; then
    log "teardown $bench"
    bash "$bench_dir/teardown.sh" >> "$log_file" 2>&1 || true
  fi
done

log "summary: $summary"
log "results: $results_dir"
cat "$summary"
exit "$failures"
