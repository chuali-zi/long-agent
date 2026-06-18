#!/usr/bin/env bash
# Set up every RealOps benchmark scene for manual Web testing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

DO_PRE_VERIFY=1
DO_PROBE=0

log() { printf '[kybench:setup-all] %s\n' "$*"; }
die() { printf '[kybench:setup-all][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo bash benchmarks/setup-all.sh [options] [bench ...]

Set up benchmark scenes without running kyagent ask. This is intended for Web
manual testing: run this once, ask the Web Agent to fix each ticket, then run
benchmarks/verify-all.sh.

Options:
  --no-pre-verify   Skip verify.sh pre after each setup.
  --probe           Run probe.sh after setup/pre-verify.
  -h, --help        Show help.

Notes:
  Defaults use real-looking roots under /var and /tmp, so run as root.
  The wrapper gives each scene isolated roots to avoid collisions.
EOF
}

selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pre-verify) DO_PRE_VERIFY=0; shift ;;
    --probe) DO_PROBE=1; shift ;;
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
  die "must run as root: sudo bash benchmarks/setup-all.sh"
fi

run_bench_script() {
  local bench="$1"
  local script="$2"
  shift 2
  local bench_dir="$ROOT/$bench"
  [[ -f "$bench_dir/$script" ]] || die "missing $bench/$script"

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

for bench in "${BENCHES[@]}"; do
  log "setup $bench"
  run_bench_script "$bench" setup.sh
  if [[ "$DO_PRE_VERIFY" == "1" ]]; then
    log "pre-verify $bench"
    run_bench_script "$bench" verify.sh pre
  fi
  if [[ "$DO_PROBE" == "1" ]]; then
    log "probe $bench"
    run_bench_script "$bench" probe.sh
  fi
done

log "all selected benchmark scenes are ready"
