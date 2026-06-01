#!/usr/bin/env bash
# Backend-only launcher for the kyagent FastAPI browser console.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${KYAGENT_WEB_HOST:-127.0.0.1}"
PORT="${KYAGENT_WEB_PORT:-8000}"
CONFIG="${KYAGENT_CONFIG:-}"
ENV_FILE="${KYAGENT_ENV_FILE:-}"
INSTALL_WEB=0
USE_MOCK=0

usage() {
  cat <<'EOF'
Usage: bash scripts/start-web-backend.sh [options]

Options:
  --host HOST          Listen address. Defaults to 127.0.0.1.
  --port PORT          Listen port. Defaults to 8000.
  --config PATH        kyagent YAML config path.
  --env-file PATH      Load environment variables before launch.
  --install-web        Install the optional FastAPI/uvicorn extra if missing.
  --mock               Force KYAGENT_LLM_BACKEND=mock for an offline demo.
  --help               Show this help.

Manual equivalent:
  python -m pip install -e .[web]
  kyagent web serve --host 127.0.0.1 --port 8000
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:?missing value for --host}"
      shift 2
      ;;
    --port)
      PORT="${2:?missing value for --port}"
      shift 2
      ;;
    --config)
      CONFIG="${2:?missing value for --config}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?missing value for --env-file}"
      shift 2
      ;;
    --install-web)
      INSTALL_WEB=1
      shift
      ;;
    --mock)
      USE_MOCK=1
      shift
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

cd "$ROOT"

if [[ -z "$ENV_FILE" && -r /etc/kyagent/env ]]; then
  ENV_FILE=/etc/kyagent/env
fi
if [[ -n "$ENV_FILE" ]]; then
  if [[ ! -r "$ENV_FILE" ]]; then
    printf '[kyagent-web][ERROR] env file is not readable: %s\n' "$ENV_FILE" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

case "$HOST" in
  127.0.0.1|localhost|::1)
    ;;
  *)
    if [[ "${KYAGENT_WEB_ALLOW_NON_LOOPBACK:-}" != "1" \
       || "${KYAGENT_WEB_REQUIRE_AUTH:-}" != "1" \
       || -z "${KYAGENT_WEB_OPERATOR_TOKEN:-}" \
       || -z "${KYAGENT_WEB_REVIEWER_TOKEN:-}" \
       || -z "${KYAGENT_WEB_AUDITOR_TOKEN:-}" \
       || -z "${KYAGENT_WEB_ADMIN_TOKEN:-}" ]]; then
      printf '%s\n' \
        '[kyagent-web][ERROR] non-loopback listen requires KYAGENT_WEB_ALLOW_NON_LOOPBACK=1, KYAGENT_WEB_REQUIRE_AUTH=1, and all role tokens.' >&2
      exit 1
    fi
    ;;
esac

if [[ -n "${KYAGENT_WEB_PYTHON:-}" ]]; then
  PYTHON="$KYAGENT_WEB_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

if [[ "$INSTALL_WEB" == "1" ]]; then
  printf '[kyagent-web] installing optional Web dependencies: pip install -e .[web]\n'
  "$PYTHON" -m pip install -e ".[web]"
fi

if ! "$PYTHON" - <<'PY'
import fastapi  # noqa: F401
import uvicorn  # noqa: F401
PY
then
  printf '[kyagent-web][ERROR] FastAPI/uvicorn missing. Re-run with --install-web.\n' >&2
  exit 1
fi

if [[ "$USE_MOCK" == "1" ]]; then
  export KYAGENT_LLM_BACKEND=mock
fi
if [[ -n "$CONFIG" ]]; then
  export KYAGENT_CONFIG="$CONFIG"
fi

printf '[kyagent-web] backend listening on http://%s:%s\n' "$HOST" "$PORT"
ARGS=(web serve --host "$HOST" --port "$PORT")
if [[ -n "$CONFIG" ]]; then
  ARGS+=(--config "$CONFIG")
fi
exec "$PYTHON" -m kyagent "${ARGS[@]}"
