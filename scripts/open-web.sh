#!/usr/bin/env bash
# Browser-only helper for an already running kyagent Web backend.
set -euo pipefail

URL="${KYAGENT_WEB_BROWSER_URL:-http://127.0.0.1:8000}"
HEALTH_URL=""
BACKEND_PID=""
WAIT_SECONDS="${KYAGENT_WEB_WAIT_SECONDS:-10}"
OPENERS="${KYAGENT_WEB_OPENERS:-xdg-open gio sensible-browser}"

usage() {
  cat <<'EOF'
Usage: bash scripts/open-web.sh [options]

Options:
  --url URL             Browser URL. Defaults to http://127.0.0.1:8000.
  --health-url URL      Health endpoint. Defaults to URL/api/health.
  --pid PID             Stop waiting if this backend process exits.
  --wait-seconds N      Health-check timeout. Defaults to 10.
  --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      URL="${2:?missing value for --url}"
      shift 2
      ;;
    --health-url)
      HEALTH_URL="${2:?missing value for --health-url}"
      shift 2
      ;;
    --pid)
      BACKEND_PID="${2:?missing value for --pid}"
      shift 2
      ;;
    --wait-seconds)
      WAIT_SECONDS="${2:?missing value for --wait-seconds}"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      printf '[kyagent-web][ERROR] unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$HEALTH_URL" ]]; then
  HEALTH_URL="${URL%/}/api/health"
fi

if [[ -n "${KYAGENT_WEB_PYTHON:-}" ]]; then
  PYTHON="$KYAGENT_WEB_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

ready=0
for ((i = 0; i < WAIT_SECONDS * 5; i++)); do
  if [[ -n "$BACKEND_PID" ]] && ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    printf '[kyagent-web][ERROR] backend exited before becoming ready\n' >&2
    exit 1
  fi
  if "$PYTHON" - "$HEALTH_URL" <<'PY'
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=0.5) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
  then
    ready=1
    break
  fi
  sleep 0.2
done

if [[ "$ready" != "1" ]]; then
  printf '[kyagent-web][ERROR] backend health check timed out: %s\n' "$HEALTH_URL" >&2
  exit 1
fi

for opener in $OPENERS; do
  if ! command -v "$opener" >/dev/null 2>&1; then
    continue
  fi
  if [[ "$opener" == "gio" ]]; then
    "$opener" open "$URL" >/dev/null 2>&1 &
  else
    "$opener" "$URL" >/dev/null 2>&1 &
  fi
  printf '[kyagent-web] opened %s with %s\n' "$URL" "$opener"
  exit 0
done

printf '[kyagent-web][WARN] browser opener unavailable; open manually: %s\n' "$URL" >&2
