#!/usr/bin/env bash
# Run all real-LLM benchmarks through the non-interactive CLI path.
#
# This is intended for VM automation, including opencode-driven runs where no
# browser approval UI is available. Each per-benchmark run.sh invokes:
#   kyagent ask --auto-approve-safe-remediation
# so safe remediation is decided by kyagent guardrails/preflight, not by Web UI.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
LOG_DIR="${KYBENCH_LOG_DIR:-/tmp}"
STAMP="${KYBENCH_RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
ENV_FILE="${KYAGENT_ENV_FILE:-/etc/kyagent/env}"
INSTALL_PREFIX="${KYAGENT_INSTALL_PREFIX:-/opt/kyagent}"
KYAGENT_USER="${KYAGENT_USER:-kyagent}"
SETUP_PERMISSIONS=0
TEARDOWN_EACH=0

BENCHES=(
  cleanup-v2
  secret-spill-v1
  port-conflict-v1
  open-deleted-v1
  runaway-cpu-v1
  demo-cleanup
)

log() { printf '[kybench:real-llm] %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: sudo bash benchmarks/run-real-llm.sh [options] [bench ...]

Options:
  --setup-permissions-prod  Run scripts/setup-sudoers-prod.sh --yes first.
  --teardown-each           Run each benchmark teardown after its post-verify.
  --log-dir DIR             Write kybench-rerun-*.log files under DIR.
  -h, --help                Show this help.

Environment:
  KYAGENT_INSTALL_PREFIX    Default: /opt/kyagent
  KYAGENT_ENV_FILE          Default: /etc/kyagent/env
  KYAGENT_USER              Default: kyagent
  KYBENCH_LOG_DIR           Default: /tmp
  KYBENCH_RUN_ID            Default: current timestamp
EOF
}

selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup-permissions-prod) SETUP_PERMISSIONS=1; shift ;;
    --teardown-each) TEARDOWN_EACH=1; shift ;;
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
  echo "must run as root: sudo bash benchmarks/run-real-llm.sh" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
[[ -f "$ENV_FILE" ]] || { echo "env not found: $ENV_FILE" >&2; exit 1; }
if [[ ! -x "$INSTALL_PREFIX/.venv/bin/kyagent" ]]; then
  INSTALL_PREFIX="$REPO_ROOT"
  log "fallback prefix: $INSTALL_PREFIX"
fi
[[ -x "$INSTALL_PREFIX/.venv/bin/kyagent" ]] || {
  echo "kyagent executable not found under $INSTALL_PREFIX/.venv/bin/kyagent" >&2
  exit 1
}

if [[ "$SETUP_PERMISSIONS" == "1" ]]; then
  log "setting permissions-prod sudoers"
  bash "$REPO_ROOT/scripts/setup-sudoers-prod.sh" --yes
  visudo -cf /etc/sudoers.d/kyagent
fi

failures=0
summary_file="$LOG_DIR/kybench-rerun-summary-$STAMP.log"
: > "$summary_file"

for bench in "${BENCHES[@]}"; do
  bench_dir="$ROOT/$bench"
  run_script="$bench_dir/run.sh"
  log_file="$LOG_DIR/kybench-rerun-$bench-$STAMP.log"
  if [[ ! -x "$run_script" && ! -f "$run_script" ]]; then
    log "missing benchmark: $bench"
    printf '%s\tMISSING\t%s\n' "$bench" "$log_file" >> "$summary_file"
    failures=$((failures + 1))
    continue
  fi

  log "running $bench -> $log_file"
  (
    set -o pipefail
    bash "$run_script" --ask 2>&1 | tee "$log_file"
  )
  rc=$?
  if [[ $rc -eq 0 ]]; then
    printf '%s\tPASS\t%s\n' "$bench" "$log_file" >> "$summary_file"
  else
    printf '%s\tFAIL(%s)\t%s\n' "$bench" "$rc" "$log_file" >> "$summary_file"
    failures=$((failures + 1))
  fi

  if [[ "$TEARDOWN_EACH" == "1" && -f "$bench_dir/teardown.sh" ]]; then
    log "teardown $bench"
    bash "$bench_dir/teardown.sh" >> "$log_file" 2>&1 || failures=$((failures + 1))
  fi
done

log "summary: $summary_file"
cat "$summary_file"
exit "$failures"
