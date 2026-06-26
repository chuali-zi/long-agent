#!/usr/bin/env bash
# Verify every RealOps benchmark scene and produce a compact summary.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${KYBENCH_LOG_DIR:-/tmp/kybench-runs}"
STAMP="${KYBENCH_RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
MODE=post

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

log() { printf '[kybench:verify-all] %s\n' "$*"; }
die() { printf '[kybench:verify-all][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo bash benchmarks/verify-all.sh [options] [bench ...]

Verify all selected benchmark scenes. Use --pre immediately after setup-all, and
use the default --post after Web/Agent remediation.

Must run as root (same as setup-all.sh) so score.json and fixture paths are
readable/writable. See benchmarks/WEB_MANUAL_TEST.md for the full Web workflow.

Options:
  --pre           Run verify.sh pre (expect SETUP_OK on each bench).
  --post          Run verify.sh post (default; expect PERFECT to pass).
  --log-dir DIR   Store logs and summary under DIR.
  -h, --help      Show help.

Exit code is the number of selected benchmarks that did not pass automation.
EOF
}

selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pre) MODE=pre; shift ;;
    --post) MODE=post; shift ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) die "unknown option: $1" ;;
    *) selected+=("$1"); shift ;;
  esac
done
if [[ ${#selected[@]} -gt 0 ]]; then
  BENCHES=("${selected[@]}")
fi

if [[ $EUID -ne 0 ]]; then
  die "must run as root: sudo bash benchmarks/verify-all.sh"
fi

mkdir -p "$LOG_DIR"
summary="$LOG_DIR/kybench-verify-$MODE-$STAMP.tsv"
: > "$summary"
printf 'bench\tmode\tverdict\texit_code\tlog\tscore\n' >> "$summary"

run_bench_script() {
  local bench="$1"
  local script="$2"
  shift 2
  local bench_dir="$ROOT/$bench"
  [[ -f "$bench_dir/$script" ]] || return 10

  case "$bench" in
    cleanup-v2)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/web-app01 \
        KYBENCH_CACHE_ROOT=/var/cache/web-app01 \
        KYBENCH_TMP_ROOT=/var/tmp/web-app01 \
        bash "$bench_dir/$script" "$@"
      ;;
    secret-spill-v1)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/auth-api01 \
        KYBENCH_CACHE_ROOT=/var/cache/auth-api01 \
        KYBENCH_TMP_ROOT=/var/tmp/auth-api01 \
        bash "$bench_dir/$script" "$@"
      ;;
    logrotate-perms-v1)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/payroll-api \
        bash "$bench_dir/$script" "$@"
      ;;
    cron-injection-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/secops-cron \
        KYBENCH_CRON_DIR=/etc/cron.d \
        bash "$bench_dir/$script" "$@"
      ;;
    open-deleted-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/report-ops-open-deleted \
        bash "$bench_dir/$script" "$@"
      ;;
    port-conflict-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/shop-ops \
        bash "$bench_dir/$script" "$@"
      ;;
    runaway-cpu-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/loadtest-ops \
        bash "$bench_dir/$script" "$@"
      ;;
    stale-lock-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/deploy-ops \
        bash "$bench_dir/$script" "$@"
      ;;
    unix-socket-stale-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/socket-ops \
        bash "$bench_dir/$script" "$@"
      ;;
    *)
      env -u KYBENCH_STATE bash "$bench_dir/$script" "$@"
      ;;
  esac
}

read_score_field() {
  local score="$1"
  local field="$2"
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" && -f "$score" ]] || { printf '?'; return; }
  "$py" - "$score" "$field" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get(sys.argv[2], "?"))
PY
}

score_mode_mismatch() {
  local score="$1"
  local expected_mode="$2"
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" && -f "$score" ]] || return 1
  "$py" - "$score" "$expected_mode" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if data.get("mode") != sys.argv[2] else 1)
PY
}

log_has_permission_error() {
  local log_file="$1"
  [[ -f "$log_file" ]] || return 1
  grep -q 'PermissionError' "$log_file" 2>/dev/null
}

failures=0
for bench in "${BENCHES[@]}"; do
  bench_dir="$ROOT/$bench"
  log_file="$LOG_DIR/kybench-verify-$MODE-$bench-$STAMP.log"
  score="$bench_dir/score.json"

  log "verify $MODE $bench -> $log_file"
  if run_bench_script "$bench" verify.sh "$MODE" 2>&1 | tee "$log_file"; then
    rc=0
  else
    rc=$?
  fi

  verdict="$(read_score_field "$score" verdict)"
  exit_code="$(read_score_field "$score" exit_code)"
  automation_pass="$(read_score_field "$score" automation_pass)"
  if [[ "$automation_pass" != "True" && "$automation_pass" != "true" ]]; then
    failures=$((failures + 1))
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$bench" "$MODE" "$verdict" "$exit_code" "$log_file" "$score" >> "$summary"
done

log "summary: $summary"
cat "$summary"

pass_label="PERFECT"
if [[ "$MODE" == "pre" ]]; then
  pass_label="SETUP_OK"
fi

printf '\n=== kybench verify %s summary ===\n' "$MODE"

passed=0
not_passed=0
declare -a passed_lines=()
declare -a failed_lines=()

while IFS=$'\t' read -r bench _mode verdict exit_code log_file score_path; do
  [[ "$bench" == "bench" ]] && continue
  if [[ "$verdict" == "$pass_label" ]]; then
    passed=$((passed + 1))
    passed_lines+=("  $bench	$verdict")
  else
    not_passed=$((not_passed + 1))
    hint=""
    if score_mode_mismatch "$score_path" "$MODE" || log_has_permission_error "$log_file"; then
      hint=$'\n         hint: score.json may be stale — run with sudo (same user as setup-all)'
    fi
    failed_lines+=("  $bench	$verdict	exit=$exit_code"$'\n'"         log: $log_file$hint")
  fi
done < "$summary"

total=$((passed + not_passed))
if [[ "$passed" -gt 0 ]]; then
  printf 'PASSED (%s/%s):\n' "$passed" "$total"
  for line in "${passed_lines[@]}"; do
    bench_name="${line%%	*}"
    bench_name="${bench_name#  }"
    verdict_name="${line##*	}"
    printf '  %-24s %s\n' "$bench_name" "$verdict_name"
  done
fi
if [[ "$not_passed" -gt 0 ]]; then
  printf 'NOT PASSED (%s/%s):\n' "$not_passed" "$total"
  for entry in "${failed_lines[@]}"; do
    printf '%s\n' "$entry"
  done
fi

printf '\nPASSED:   %s/%s\n' "$passed" "$total"
printf 'NOT PASSED: %s/%s\n' "$not_passed" "$total"
if [[ "$failures" -eq 0 ]]; then
  printf 'Overall: ALL PASSED (%s) — READY\n' "$pass_label"
else
  printf 'Overall: %s/%s passed — NOT READY (only %s counts as pass)\n' "$passed" "$total" "$pass_label"
fi
printf 'Summary TSV: %s\n' "$summary"

exit "$failures"
