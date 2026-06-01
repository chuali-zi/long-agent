#!/usr/bin/env bash
# One-click launcher: start the kyagent Web backend and open its browser UI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${KYAGENT_WEB_HOST:-127.0.0.1}"
PORT="${KYAGENT_WEB_PORT:-8000}"
BROWSER_URL="${KYAGENT_WEB_BROWSER_URL:-}"
OPEN_BROWSER=1
BACKEND_ARGS=()

usage() {
  cat <<'EOF'
Usage: bash scripts/start-web.sh [options]

Starts the FastAPI backend, waits for /api/health, and opens the browser UI.
On headless Linux the backend keeps running and the script prints the URL.

Options:
  --host HOST          Listen address. Defaults to 127.0.0.1.
  --port PORT          Listen port. Defaults to 8000.
  --browser-url URL    URL to open. Defaults to the local listen URL.
  --no-open-browser    Start the backend without trying to open a browser.
  --config PATH        kyagent YAML config path.
  --env-file PATH      Load environment variables before launch.
  --install-web        Install the optional FastAPI/uvicorn extra if missing.
  --mock               Force KYAGENT_LLM_BACKEND=mock for an offline demo.
  --help               Show this help.

Separate scripts:
  bash scripts/start-web-backend.sh --mock
  bash scripts/open-web.sh --url http://127.0.0.1:8000
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:?missing value for --host}"
      BACKEND_ARGS+=("$1" "$2")
      shift 2
      ;;
    --port)
      PORT="${2:?missing value for --port}"
      BACKEND_ARGS+=("$1" "$2")
      shift 2
      ;;
    --browser-url)
      BROWSER_URL="${2:?missing value for --browser-url}"
      shift 2
      ;;
    --no-open-browser)
      OPEN_BROWSER=0
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      BACKEND_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$BROWSER_URL" ]]; then
  case "$HOST" in
    0.0.0.0|::|\[::\])
      BROWSER_URL="http://127.0.0.1:$PORT"
      ;;
    *)
      BROWSER_URL="http://$HOST:$PORT"
      ;;
  esac
fi

bash "$SCRIPT_DIR/start-web-backend.sh" "${BACKEND_ARGS[@]}" &
BACKEND_PID=$!

cleanup() {
  if kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup INT TERM EXIT

if [[ "$OPEN_BROWSER" == "1" ]]; then
  bash "$SCRIPT_DIR/open-web.sh" \
    --url "$BROWSER_URL" \
    --health-url "$BROWSER_URL/api/health" \
    --pid "$BACKEND_PID"
else
  printf '[kyagent-web] browser open disabled; open manually: %s\n' "$BROWSER_URL"
fi

wait "$BACKEND_PID"
STATUS=$?
trap - INT TERM EXIT
exit "$STATUS"
