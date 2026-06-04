#!/usr/bin/env bash
# Write the minimal production environment file without reinstalling kyagent.
set -euo pipefail

INSTALL_PREFIX="${KYAGENT_INSTALL_PREFIX:-/opt/kyagent}"
KYAGENT_USER="${KYAGENT_USER:-kyagent}"
ENV_FILE="${KYAGENT_ENV_FILE:-/etc/kyagent/env}"
AUDIT_HMAC_KEY_FILE="${KYAGENT_AUDIT_HMAC_KEY_FILE:-/etc/kyagent/audit-hmac.key}"
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-}"
DEEPSEEK_KEY_FILE=""
GENERATE_HMAC_KEY=1

log() {
  printf '[kyagent-prod-env] %s\n' "$*"
}

die() {
  printf '[kyagent-prod-env][ERROR] %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/kyagent.sh prod-env [options]

Writes the minimal production runtime config:
  /etc/kyagent/env
  /etc/kyagent/audit-hmac.key
  /var/lib/kyagent
  /var/log/kyagent

This script does not install dependencies, create the runtime account, or change
tool command permissions. Run "sudo bash scripts/kyagent.sh permissions" first
if the kyagent account does not exist yet.

Options:
  --prefix DIR                Production install prefix. Default: /opt/kyagent
  --user USER                 Runtime account. Default: kyagent
  --env-file FILE             Env file path. Default: /etc/kyagent/env
  --deepseek-key-file FILE    Read DEEPSEEK_API_KEY from a one-line local file.
  --no-hmac-key               Do not create or reference an audit HMAC key.
  -h, --help                  Show this help.
EOF
}

read_secret_file() {
  local key_file="$1"
  [[ -r "$key_file" ]] || die "cannot read --deepseek-key-file: $key_file"
  DEEPSEEK_KEY="$(cat -- "$key_file")"
  DEEPSEEK_KEY="${DEEPSEEK_KEY%$'\r'}"
  if [[ "$DEEPSEEK_KEY" == *$'\n'* || "$DEEPSEEK_KEY" == *$'\r'* ]]; then
    die "DEEPSEEK_API_KEY file must contain exactly one line"
  fi
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
    --no-hmac-key)
      GENERATE_HMAC_KEY=0
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

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "root is required; run: sudo bash scripts/kyagent.sh prod-env"
[[ "$INSTALL_PREFIX" == /* ]] || die "--prefix must be an absolute path"
[[ "$ENV_FILE" == /* ]] || die "--env-file must be an absolute path"
[[ "$KYAGENT_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--user is not a safe Linux account name"
id "$KYAGENT_USER" >/dev/null 2>&1 || die "runtime account '$KYAGENT_USER' does not exist; run permissions first"
[[ -f "$INSTALL_PREFIX/configs/deepseek.yaml" ]] || die "missing $INSTALL_PREFIX/configs/deepseek.yaml"

if [[ -n "$DEEPSEEK_KEY_FILE" ]]; then
  read_secret_file "$DEEPSEEK_KEY_FILE"
fi
if [[ "$DEEPSEEK_KEY" == *$'\n'* || "$DEEPSEEK_KEY" == *$'\r'* ]]; then
  die "DEEPSEEK_API_KEY must not contain newlines"
fi

write_shell_assignment() {
  printf '%s=' "$1"
  printf '%q' "$2"
  printf '\n'
}

make_hmac_key() {
  local key_tmp
  key_tmp="$(mktemp)"
  chmod 0600 "$key_tmp"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32 >"$key_tmp"
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))' >"$key_tmp"
  fi
  install -m 0640 -o root -g "$KYAGENT_USER" "$key_tmp" "$AUDIT_HMAC_KEY_FILE"
  rm -f "$key_tmp"
}

ENV_DIR="$(dirname -- "$ENV_FILE")"
HMAC_DIR="$(dirname -- "$AUDIT_HMAC_KEY_FILE")"

install -d -m 0750 -o root -g "$KYAGENT_USER" "$ENV_DIR"
install -d -m 0750 -o root -g "$KYAGENT_USER" "$HMAC_DIR"
install -d -m 0750 -o "$KYAGENT_USER" -g "$KYAGENT_USER" /var/lib/kyagent
install -d -m 0750 -o "$KYAGENT_USER" -g "$KYAGENT_USER" /var/log/kyagent

if [[ "$GENERATE_HMAC_KEY" == "1" && ! -s "$AUDIT_HMAC_KEY_FILE" ]]; then
  make_hmac_key
fi

tmp="$(mktemp)"
chmod 0600 "$tmp"
{
  write_shell_assignment KYAGENT_CONFIG "$INSTALL_PREFIX/configs/deepseek.yaml"
  write_shell_assignment KYAGENT_DEEPSEEK_TRANSPORT deepseek_httpx
  write_shell_assignment KYAGENT_EXECUTOR_ACCOUNT "$KYAGENT_USER"
  write_shell_assignment KYAGENT_AUDIT_DB /var/lib/kyagent/audit.db
  write_shell_assignment KYAGENT_AUDIT_JSONL /var/log/kyagent/audit.jsonl
  write_shell_assignment KYAGENT_AUDIT_INTEGRITY_ENABLED 1
  if [[ "$GENERATE_HMAC_KEY" == "1" ]]; then
    write_shell_assignment KYAGENT_AUDIT_HMAC_KEY_FILE "$AUDIT_HMAC_KEY_FILE"
    write_shell_assignment KYAGENT_AUDIT_HMAC_KEY_ID local-v1
  fi
  if [[ -n "$DEEPSEEK_KEY" ]]; then
    write_shell_assignment DEEPSEEK_API_KEY "$DEEPSEEK_KEY"
  else
    printf '# DEEPSEEK_API_KEY=sk-...\n'
  fi
} >"$tmp"

install -m 0640 -o root -g "$KYAGENT_USER" "$tmp" "$ENV_FILE"
rm -f "$tmp"

log "wrote $ENV_FILE"
log "verify: sudo -u $KYAGENT_USER test -r $ENV_FILE"
