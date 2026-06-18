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
Usage: bash benchmarks/verify-all.sh [options] [bench ...]

Verify all selected benchmark scenes. Use --pre immediately after setup-all, and
use the default --post after Web/Agent remediation.

Options:
  --pre           Run verify.sh pre.
  --post          Run verify.sh post (default).
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
exit "$failures"
