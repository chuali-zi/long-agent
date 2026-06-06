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
  install       Install the local development environment.
  permissions   Create the restricted runtime account, sudoers policy, and audit dirs.
  permissions-prod  Same as permissions, but pre-enables a production-realistic
                    write-operation allowlist (log clean, pkg mgmt, proc kill, common services).
  prod-env      Write the minimal production runtime env file.
  chat          Start the interactive chat shell.
  tui           Start the streaming terminal UI.
  web           Start the Web backend and open the browser UI.
  web-backend   Start only the Web backend.
  web-open      Open an already running Web UI.
  tools         List the enabled tools.

Common examples:
  bash scripts/kyagent.sh install
  sudo bash scripts/kyagent.sh permissions
  sudo bash scripts/kyagent.sh permissions-prod --yes
  sudo bash scripts/kyagent.sh prod-env
  bash scripts/kyagent.sh chat
  bash scripts/kyagent.sh tui
  bash scripts/kyagent.sh web --mock
  bash scripts/kyagent.sh web-backend --mock
  bash scripts/kyagent.sh web-open

LoongArch/Kylin deployment:
  sudo install -d -m 0755 /opt/kyagent
  sudo rsync -a --delete ./ /opt/kyagent/
  cd /opt/kyagent
  sudo bash scripts/install-loongarch.sh --yes --with-web
  sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
EOF
}

require_kyagent() {
  if [[ ! -x "$KYAGENT_BIN" ]]; then
    printf '[kyagent][ERROR] missing %s; run: bash scripts/kyagent.sh install\n' "$KYAGENT_BIN" >&2
    exit 1
  fi
}

require_script_readable() {
  local script_path="$1"
  local current_user
  current_user="$(id -un 2>/dev/null || printf unknown)"
  if [[ ! -r "$script_path" ]]; then
    printf '[kyagent][ERROR] cannot read delegated script: %s\n' "$script_path" >&2
    printf '[kyagent][ERROR] current user: %s\n' "$current_user" >&2
    printf '[kyagent][ERROR] Production installs should run from /opt/kyagent; if the repo is under another user home, copy it there or fix every parent directory execute bit plus file read permissions.\n' >&2
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
    require_script_readable "$SCRIPT_DIR/install.sh"
    exec bash "$SCRIPT_DIR/install.sh" "$@"
    ;;
  permissions)
    require_script_readable "$SCRIPT_DIR/setup-sudoers.sh"
    exec bash "$SCRIPT_DIR/setup-sudoers.sh" "$@"
    ;;
  permissions-prod)
    require_script_readable "$SCRIPT_DIR/setup-sudoers-prod.sh"
    require_script_readable "$SCRIPT_DIR/setup-sudoers.sh"
    exec bash "$SCRIPT_DIR/setup-sudoers-prod.sh" "$@"
    ;;
  prod-env)
    require_script_readable "$SCRIPT_DIR/write-prod-env.sh"
    exec bash "$SCRIPT_DIR/write-prod-env.sh" "$@"
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
    require_script_readable "$SCRIPT_DIR/start-web.sh"
    exec bash "$SCRIPT_DIR/start-web.sh" "$@"
    ;;
  web-backend)
    require_script_readable "$SCRIPT_DIR/start-web-backend.sh"
    exec bash "$SCRIPT_DIR/start-web-backend.sh" "$@"
    ;;
  web-open)
    require_script_readable "$SCRIPT_DIR/open-web.sh"
    exec bash "$SCRIPT_DIR/open-web.sh" "$@"
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
