#!/usr/bin/env bash
# Developer quick test: run the README production flow with maximal test sudoers.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_PREFIX="${KYAGENT_INSTALL_PREFIX:-/opt/kyagent}"
KYAGENT_USER="${KYAGENT_USER:-kyagent}"
ENV_FILE="${KYAGENT_ENV_FILE:-/etc/kyagent/env}"
ROOT_KEY_FILE="${KYAGENT_DEEPSEEK_KEY_FILE:-/root/deepseek.key}"
ASK_PROMPT="${KYAGENT_DEVELOPER_TEST_PROMPT:-which process used the most cpu}"
DEEPSEEK_KEY_FILE=""
START_WEB=1
RUN_ASK=1

log() {
  printf '[kyagent-dev-quick-test] %s\n' "$*"
}

die() {
  printf '[kyagent-dev-quick-test][ERROR] %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/developer-quick-test.sh [options]

Runs the README path end to end for developer verification:
  1. copy the repo to /opt/kyagent
  2. run install-loongarch.sh --yes --with-web
  3. switch sudoers to the maximal test policy
  4. write /etc/kyagent/env with DEEPSEEK_API_KEY and KYAGENT_WEB_ADMIN_TOKEN=admin123
  5. run README validation commands and the real CLI prompt
  6. start the Web UI

By default the only interactive input is the DeepSeek API key.

Options:
  --prefix DIR              Install prefix. Default: /opt/kyagent
  --user USER               Runtime account. Default: kyagent
  --env-file FILE           Runtime env file. Default: /etc/kyagent/env
  --deepseek-key-file FILE  Read the DeepSeek API key from FILE instead of prompting.
  --no-ask                  Skip the real "which process used the most cpu" CLI call.
  --no-web                  Do not start the Web UI at the end.
  -h, --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      [[ $# -ge 2 ]] || die "--prefix requires a value"
      INSTALL_PREFIX="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || die "--user requires a value"
      KYAGENT_USER="$2"
      shift 2
      ;;
    --env-file)
      [[ $# -ge 2 ]] || die "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --deepseek-key-file)
      [[ $# -ge 2 ]] || die "--deepseek-key-file requires a value"
      DEEPSEEK_KEY_FILE="$2"
      shift 2
      ;;
    --no-ask)
      RUN_ASK=0
      shift
      ;;
    --no-web)
      START_WEB=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "root is required; run: sudo bash scripts/developer-quick-test.sh"
[[ "$INSTALL_PREFIX" == /* ]] || die "--prefix must be an absolute path"
[[ "$ENV_FILE" == /* ]] || die "--env-file must be an absolute path"
[[ "$KYAGENT_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--user is not a safe Linux account name"

read_secret_file() {
  local key_file="$1"
  [[ -r "$key_file" ]] || die "cannot read DeepSeek key file: $key_file"
  DEEPSEEK_API_KEY="$(cat -- "$key_file")"
  DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY%$'\r'}"
}

write_root_key_file() {
  local key="$1"
  [[ "$key" != *$'\n'* && "$key" != *$'\r'* ]] || die "DEEPSEEK_API_KEY must contain exactly one line"
  [[ -n "$key" ]] || die "DEEPSEEK_API_KEY must not be empty"
  (umask 077 && printf '%s\n' "$key" >"$ROOT_KEY_FILE")
  chmod 0600 "$ROOT_KEY_FILE"
  DEEPSEEK_KEY_FILE="$ROOT_KEY_FILE"
}

ensure_deepseek_key() {
  if [[ -n "$DEEPSEEK_KEY_FILE" ]]; then
    read_secret_file "$DEEPSEEK_KEY_FILE"
    write_root_key_file "$DEEPSEEK_API_KEY"
    return 0
  fi

  if [[ -n "${DEEPSEEK_API_KEY:-}" ]]; then
    write_root_key_file "$DEEPSEEK_API_KEY"
    return 0
  fi

  [[ -t 0 ]] || die "non-interactive mode requires DEEPSEEK_API_KEY or --deepseek-key-file"
  local key
  read -r -s -p "DeepSeek API Key: " key
  printf '\n'
  write_root_key_file "$key"
}

run_step() {
  log "$1"
  shift
  "$@"
}

copy_to_prefix() {
  if [[ "$(cd "$SOURCE_ROOT" && pwd)" == "$(mkdir -p "$INSTALL_PREFIX" && cd "$INSTALL_PREFIX" && pwd)" ]]; then
    log "already running from $INSTALL_PREFIX"
    return 0
  fi

  command -v rsync >/dev/null 2>&1 || die "rsync is required to copy the repo to $INSTALL_PREFIX"
  run_step "copying repo to $INSTALL_PREFIX" install -d -m 0755 "$INSTALL_PREFIX"
  run_step "syncing source tree" rsync -a --delete --exclude '.venv/' "$SOURCE_ROOT/" "$INSTALL_PREFIX/"
}

write_admin_token_to_env() {
  [[ -r "$ENV_FILE" ]] || die "env file is not readable after prod-env: $ENV_FILE"
  local tmp
  tmp="$(mktemp)"
  chmod 0600 "$tmp"
  awk '
    BEGIN { wrote = 0 }
    /^KYAGENT_WEB_ADMIN_TOKEN=/ { next }
    {
      print
      if ($0 ~ /^DEEPSEEK_API_KEY=/ && wrote == 0) {
        print "KYAGENT_WEB_ADMIN_TOKEN=admin123"
        wrote = 1
      }
    }
    END {
      if (wrote == 0) {
        print "KYAGENT_WEB_ADMIN_TOKEN=admin123"
      }
    }
  ' "$ENV_FILE" >"$tmp"
  install -m 0640 -o root -g "$KYAGENT_USER" "$tmp" "$ENV_FILE"
  rm -f "$tmp"
  log "wrote KYAGENT_WEB_ADMIN_TOKEN=admin123 to $ENV_FILE"
}

runtime_kyagent() {
  local env_q arg arg_q cmd_q
  printf -v env_q '%q' "$ENV_FILE"
  cmd_q=""
  for arg in "$INSTALL_PREFIX/.venv/bin/kyagent" "$@"; do
    printf -v arg_q '%q' "$arg"
    cmd_q+=" $arg_q"
  done
  sudo -u "$KYAGENT_USER" bash -c "set -a; source $env_q; set +a;$cmd_q"
}

main() {
  ensure_deepseek_key
  copy_to_prefix

  cd "$INSTALL_PREFIX"
  run_step "installing LoongArch/Web runtime" \
    bash "$INSTALL_PREFIX/scripts/install-loongarch.sh" --yes --with-web --deepseek-key-file "$DEEPSEEK_KEY_FILE"
  run_step "switching to maximal test sudoers" \
    bash "$INSTALL_PREFIX/scripts/setup-sudoers-max-test.sh" --yes
  run_step "rewriting production env" \
    bash "$INSTALL_PREFIX/scripts/kyagent.sh" prod-env --deepseek-key-file "$DEEPSEEK_KEY_FILE"
  write_admin_token_to_env

  run_step "validating sudoers syntax" visudo -cf /etc/sudoers.d/kyagent
  run_step "showing runtime sudo policy" sudo -l -U "$KYAGENT_USER"
  run_step "checking launcher readability" sudo -u "$KYAGENT_USER" test -r "$INSTALL_PREFIX/scripts/kyagent.sh"
  run_step "checking audit db directory write access" sudo -u "$KYAGENT_USER" test -w /var/lib/kyagent
  run_step "checking tool registry" runtime_kyagent tools list

  if [[ "$RUN_ASK" == "1" ]]; then
    run_step "asking real DeepSeek-backed prompt: $ASK_PROMPT" runtime_kyagent ask "$ASK_PROMPT"
  fi

  if [[ "$START_WEB" == "1" ]]; then
    log "starting Web UI; stop with Ctrl-C when finished"
    exec sudo -u "$KYAGENT_USER" bash "$INSTALL_PREFIX/scripts/kyagent.sh" web --env-file "$ENV_FILE"
  fi

  log "done"
  log "max-test sudoers is still active; restore with: sudo bash $INSTALL_PREFIX/scripts/setup-sudoers.sh"
}

main "$@"
