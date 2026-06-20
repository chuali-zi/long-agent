#!/usr/bin/env bash
# One-command full validation: install deps, run pytest, then run strict benchmarks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${KYAGENT_FULL_TEST_LOG_DIR:-/tmp/kyagent-full-test}"
RUN_INSTALL=1
RUN_PYTEST=1
RUN_BENCHMARKS=1
SETUP_PERMISSIONS_PROD=0
TEARDOWN_EACH=0

log() { printf '[kyagent-full-test] %s\n' "$*"; }
die() { printf '[kyagent-full-test][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/full-test.sh [options] [-- pytest_arg ...]

Runs the full validation set in one command:
  1. bash scripts/kyagent.sh install
  2. bash scripts/kyagent.sh test [pytest_arg ...]
  3. sudo bash benchmarks/run-suite.sh --log-dir DIR

Benchmark pass criteria are strict: every selected benchmark must score PERFECT.

Options:
  --log-dir DIR                 Store benchmark logs/results under DIR.
  --skip-install                Do not run scripts/kyagent.sh install first.
  --skip-pytest                 Skip the normal pytest suite.
  --skip-benchmarks             Skip RealOps benchmarks.
  --setup-permissions-prod      Install production sudoers before benchmarks.
  --teardown-each               Teardown each benchmark after its run.
  -h, --help                    Show help.

Environment used by benchmarks:
  KYAGENT_INSTALL_PREFIX        Default: /opt/kyagent, fallback to repo root.
  KYAGENT_ENV_FILE              Default: /etc/kyagent/env.
  KYAGENT_USER                  Default: kyagent.
  KYBENCH_LOG_DIR               Alternative default for --log-dir.
EOF
}

PYTEST_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --log-dir)
      [[ $# -ge 2 ]] || die "--log-dir requires a value"
      LOG_DIR="$2"
      shift 2
      ;;
    --skip-install)
      RUN_INSTALL=0
      shift
      ;;
    --skip-pytest)
      RUN_PYTEST=0
      shift
      ;;
    --skip-benchmarks)
      RUN_BENCHMARKS=0
      shift
      ;;
    --setup-permissions-prod)
      SETUP_PERMISSIONS_PROD=1
      shift
      ;;
    --teardown-each)
      TEARDOWN_EACH=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      PYTEST_ARGS+=("$@")
      break
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      PYTEST_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "$RUN_BENCHMARKS" == "1" && ${EUID:-$(id -u)} -ne 0 ]]; then
  die "benchmarks require root; run: sudo bash scripts/full-test.sh"
fi

if [[ "$RUN_INSTALL" == "1" ]]; then
  log "installing/updating development dependencies"
  bash "$ROOT/scripts/kyagent.sh" install
fi

if [[ "$RUN_PYTEST" == "1" ]]; then
  log "running full pytest suite"
  bash "$ROOT/scripts/kyagent.sh" test "${PYTEST_ARGS[@]}"
fi

if [[ "$RUN_BENCHMARKS" == "1" ]]; then
  bench_args=(--log-dir "$LOG_DIR")
  if [[ "$SETUP_PERMISSIONS_PROD" == "1" ]]; then
    bench_args+=(--setup-permissions-prod)
  fi
  if [[ "$TEARDOWN_EACH" == "1" ]]; then
    bench_args+=(--teardown-each)
  fi

  log "running strict RealOps benchmark suite"
  bash "$ROOT/benchmarks/run-suite.sh" "${bench_args[@]}"
fi

log "full validation passed"
