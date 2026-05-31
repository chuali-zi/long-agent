#!/usr/bin/env bash
# Thin operator entrypoint. Detailed logic stays in the focused scripts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$ROOT/scripts"
KYAGENT_BIN="${KYAGENT_BIN:-$ROOT/.venv/bin/kyagent}"

usage() {
  cat <<'EOF'
Usage: bash scripts/kyagent.sh <command> [options]

Commands:
  install       Install the development environment.
  permissions   Create the restricted runtime account and sudoers policy.
  chat          Start the interactive chat shell.
  tui           Start the streaming terminal UI.
  web           Start the browser console.
  tools         List the enabled tools.

Common examples:
  bash scripts/kyagent.sh install
  sudo bash scripts/kyagent.sh permissions
  bash scripts/kyagent.sh chat
  bash scripts/kyagent.sh tui
  bash scripts/kyagent.sh web --mock

LoongArch/Kylin deployment:
  sudo bash scripts/install-loongarch.sh --yes
EOF
}

require_kyagent() {
  if [[ ! -x "$KYAGENT_BIN" ]]; then
    printf '[kyagent][ERROR] missing %s; run: bash scripts/kyagent.sh install\n' "$KYAGENT_BIN" >&2
    exit 1
  fi
}

load_runtime_env() {
  local env_file="${KYAGENT_ENV_FILE:-}"
  if [[ -z "$env_file" && -r /etc/kyagent/env ]]; then
    env_file=/etc/kyagent/env
  fi
  if [[ -z "$env_file" ]]; then
    return 0
  fi
  if [[ ! -r "$env_file" ]]; then
    printf '[kyagent][ERROR] env file is not readable: %s\n' "$env_file" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
}

COMMAND="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$COMMAND" in
  install)
    exec bash "$SCRIPT_DIR/install.sh" "$@"
    ;;
  permissions)
    exec bash "$SCRIPT_DIR/setup-sudoers.sh" "$@"
    ;;
  chat)
    require_kyagent
    load_runtime_env
    exec "$KYAGENT_BIN" chat "$@"
    ;;
  tui)
    require_kyagent
    load_runtime_env
    exec "$KYAGENT_BIN" tui "$@"
    ;;
  web)
    exec bash "$SCRIPT_DIR/start-web.sh" "$@"
    ;;
  tools)
    require_kyagent
    load_runtime_env
    exec "$KYAGENT_BIN" tools list "$@"
    ;;
  help|--help|-h)
    usage
    ;;
  *)
    printf '[kyagent][ERROR] unknown command: %s\n' "$COMMAND" >&2
    usage >&2
    exit 1
    ;;
esac
