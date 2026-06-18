#!/usr/bin/env bash
# Tear down every RealOps benchmark scene created by setup-all.sh.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BENCHES=(
  cron-injection-v1
  logrotate-perms-v1
  unix-socket-stale-v1
  stale-lock-v1
  runaway-cpu-v1
  open-deleted-v1
  port-conflict-v1
  secret-spill-v1
  cleanup-v2
)

log() { printf '[kybench:teardown-all] %s\n' "$*"; }
die() { printf '[kybench:teardown-all][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo bash benchmarks/teardown-all.sh [bench ...]

Tear down benchmark scenes created by benchmarks/setup-all.sh. Missing state
files are skipped so the command is safe to rerun.
EOF
}

selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
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
  die "must run as root: sudo bash benchmarks/teardown-all.sh"
fi

run_teardown() {
  local bench="$1"
  local bench_dir="$ROOT/$bench"
  [[ -f "$bench_dir/teardown.sh" ]] || die "missing $bench/teardown.sh"
  if [[ ! -f "$bench_dir/bench-state.json" ]]; then
    log "skip $bench: no bench-state.json"
    return 0
  fi

  case "$bench" in
    cleanup-v2)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/web-app01 \
        KYBENCH_CACHE_ROOT=/var/cache/web-app01 \
        KYBENCH_TMP_ROOT=/var/tmp/web-app01 \
        bash "$bench_dir/teardown.sh"
      ;;
    secret-spill-v1)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/auth-api01 \
        KYBENCH_CACHE_ROOT=/var/cache/auth-api01 \
        KYBENCH_TMP_ROOT=/var/tmp/auth-api01 \
        bash "$bench_dir/teardown.sh"
      ;;
    logrotate-perms-v1)
      env -u KYBENCH_STATE -u KYBENCH_RUNTIME_ROOT \
        KYBENCH_LOG_ROOT=/var/log/payroll-api \
        bash "$bench_dir/teardown.sh"
      ;;
    cron-injection-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/secops-cron \
        KYBENCH_CRON_DIR=/etc/cron.d \
        bash "$bench_dir/teardown.sh"
      ;;
    open-deleted-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/report-ops-open-deleted \
        bash "$bench_dir/teardown.sh"
      ;;
    port-conflict-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/shop-ops \
        bash "$bench_dir/teardown.sh"
      ;;
    runaway-cpu-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/loadtest-ops \
        bash "$bench_dir/teardown.sh"
      ;;
    stale-lock-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/deploy-ops \
        bash "$bench_dir/teardown.sh"
      ;;
    unix-socket-stale-v1)
      env -u KYBENCH_STATE -u KYBENCH_LOG_ROOT -u KYBENCH_CACHE_ROOT -u KYBENCH_TMP_ROOT \
        KYBENCH_RUNTIME_ROOT=/tmp/socket-ops \
        bash "$bench_dir/teardown.sh"
      ;;
    *)
      env -u KYBENCH_STATE bash "$bench_dir/teardown.sh"
      ;;
  esac
}

failures=0
for bench in "${BENCHES[@]}"; do
  log "teardown $bench"
  if ! run_teardown "$bench"; then
    failures=$((failures + 1))
  fi
done

if [[ "$failures" -gt 0 ]]; then
  die "$failures teardown(s) failed"
fi
log "all selected benchmark scenes are removed"
