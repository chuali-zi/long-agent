#!/usr/bin/env bash
# One-click LoongArch Linux installer for kyagent.
# Default path targets Kylin V10 / LoongArch Old World: no OpenAI/Anthropic SDK,
# no jiter, no pydantic-core, and DeepSeek via pure-httpx transport.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_PREFIX="${KYAGENT_INSTALL_PREFIX:-$ROOT}"
KYAGENT_USER="${KYAGENT_USER:-kyagent}"
PYTHON_BIN="${PYTHON_BIN:-}"

ASSUME_YES=0
DRY_RUN=0
ALLOW_NON_LOONGARCH=0
SKIP_SYSTEM_PACKAGES=0
SKIP_SUDOERS=0
RUN_DEEPSEEK_CHECK=0
WITH_WEB=0
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-}"
DEEPSEEK_KEY_FILE=""
INLINE_DEEPSEEK_KEY_SET=0
SERVICE_ALLOWLIST="${KYAGENT_SERVICE_ALLOWLIST:-}"
OFFLINE=0
WHEELHOUSE=""
COMMAND_INVENTORY_MODE="best-effort"
PIP_OFFLINE_ARGS=()
AUDIT_HMAC_KEY_FILE="${KYAGENT_AUDIT_HMAC_KEY_FILE:-/etc/kyagent/audit-hmac.key}"

log() {
  printf '[kyagent-loongarch] %s\n' "$*"
}

die() {
  printf '[kyagent-loongarch][ERROR] %s\n' "$*" >&2
  exit 1
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %q' "$1"
    shift || true
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

usage() {
  cat <<'EOF'
Usage: bash scripts/install-loongarch.sh [options]

Options:
  --yes                         Run non-interactively.
  --dry-run                     Print commands without changing the system.
  --allow-non-loongarch         Deprecated compatibility flag; non-loongarch Linux now continues.
  --prefix PATH                 Project directory. Defaults to current repo root.
  --user USER                   Runtime account. Defaults to kyagent.
  --python PATH                 Python 3.10-3.13 interpreter to use.
  --skip-system-packages        Do not install OS packages.
  --skip-sudoers                Do not create account/sudoers/audit dirs.
  --offline                     Install Python packages without network access.
  --wheelhouse DIR              Local wheel/source directory required by --offline.
  --deepseek-key-file FILE      Read DEEPSEEK_API_KEY from a local file.
  --deepseek-key KEY            Not recommended: key is visible in process arguments.
  --run-deepseek-check          Run a real DeepSeek request after install.
  --with-web                    Install the optional FastAPI/uvicorn Web extra.
  --command-inventory MODE      best-effort (default), competition, or strict.
  --service-allowlist LIST      Comma-separated systemd services allowed for restart/reload.
  --help                        Show this help.

Default LoongArch path:
  SKIP_CYTHON=1 pip install --no-binary PyYAML,pydantic -r requirements-loongarch.txt
  KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --allow-non-loongarch)
      ALLOW_NON_LOONGARCH=1
      shift
      ;;
    --prefix)
      INSTALL_PREFIX="${2:?missing value for --prefix}"
      shift 2
      ;;
    --user)
      KYAGENT_USER="${2:?missing value for --user}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:?missing value for --python}"
      shift 2
      ;;
    --skip-system-packages)
      SKIP_SYSTEM_PACKAGES=1
      shift
      ;;
    --skip-sudoers)
      SKIP_SUDOERS=1
      shift
      ;;
    --offline)
      OFFLINE=1
      shift
      ;;
    --wheelhouse)
      WHEELHOUSE="${2:?missing value for --wheelhouse}"
      shift 2
      ;;
    --deepseek-key-file)
      DEEPSEEK_KEY_FILE="${2:?missing value for --deepseek-key-file}"
      shift 2
      ;;
    --deepseek-key)
      DEEPSEEK_KEY="${2:?missing value for --deepseek-key}"
      INLINE_DEEPSEEK_KEY_SET=1
      shift 2
      ;;
    --run-deepseek-check)
      RUN_DEEPSEEK_CHECK=1
      shift
      ;;
    --with-web)
      WITH_WEB=1
      shift
      ;;
    --command-inventory)
      COMMAND_INVENTORY_MODE="${2:?missing value for --command-inventory}"
      shift 2
      ;;
    --service-allowlist)
      SERVICE_ALLOWLIST="${2:?missing value for --service-allowlist}"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

trap 'die "failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

load_deepseek_key_file() {
  local key_file="$1"
  [[ -f "$key_file" && -r "$key_file" ]] || die "cannot read --deepseek-key-file: $key_file"
  DEEPSEEK_KEY="$(cat -- "$key_file")"
  DEEPSEEK_KEY="${DEEPSEEK_KEY%$'\r'}"
  if [[ "$DEEPSEEK_KEY" == *$'\n'* || "$DEEPSEEK_KEY" == *$'\r'* ]]; then
    die "DEEPSEEK_API_KEY file must contain exactly one line"
  fi
}

configure_options() {
  case "$COMMAND_INVENTORY_MODE" in
    best-effort|competition|strict) ;;
    *) die "--command-inventory must be best-effort, competition, or strict" ;;
  esac

  if [[ "$OFFLINE" == "1" ]]; then
    [[ -n "$WHEELHOUSE" ]] || die "--offline requires --wheelhouse DIR"
    [[ -d "$WHEELHOUSE" ]] || die "wheelhouse directory not found: $WHEELHOUSE"
    WHEELHOUSE="$(cd "$WHEELHOUSE" && pwd)"
    PIP_OFFLINE_ARGS=(--no-index --find-links "$WHEELHOUSE")
  fi

  if [[ -n "$DEEPSEEK_KEY_FILE" ]]; then
    [[ "$INLINE_DEEPSEEK_KEY_SET" == "0" ]] || die "use only one of --deepseek-key-file or --deepseek-key"
    load_deepseek_key_file "$DEEPSEEK_KEY_FILE"
  elif [[ "$INLINE_DEEPSEEK_KEY_SET" == "1" ]]; then
    log "warning: --deepseek-key is not recommended; prefer --deepseek-key-file"
  fi
}

confirm() {
  if [[ "$ASSUME_YES" == "1" || "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  printf '%s [y/N] ' "$1"
  read -r answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

need_root_for_system_changes() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ "$EUID" -ne 0 ]]; then
    die "system package, sudoers, and /etc/kyagent setup require root; rerun with sudo or pass --skip-system-packages --skip-sudoers"
  fi
}

version_ge() {
  # Compare dotted numeric versions. Returns true if $1 >= $2.
  local left="$1" right="$2"
  local IFS=.
  local -a a b
  read -r -a a <<<"$left"
  read -r -a b <<<"$right"
  for i in 0 1 2; do
    local av="${a[$i]:-0}" bv="${b[$i]:-0}"
    if ((10#$av > 10#$bv)); then
      return 0
    fi
    if ((10#$av < 10#$bv)); then
      return 1
    fi
  done
  return 0
}

detect_arch() {
  local os arch kernel glibc_line glibc_version
  os="$(uname -s)"
  arch="$(uname -m)"
  kernel="$(uname -r)"
  glibc_line="$(ldd --version 2>/dev/null | head -1 || true)"
  glibc_version="$(printf '%s\n' "$glibc_line" | grep -Eo '[0-9]+\.[0-9]+' | head -1 || true)"

  log "os:           $os"
  log "architecture: $arch"
  log "kernel:       $kernel"
  log "glibc:        ${glibc_line:-unknown}"

  if [[ "$os" != "Linux" ]]; then
    die "this installer supports Linux only"
  fi

  if [[ "$arch" == "loongarch64" ]]; then
    if [[ "$kernel" == 4.19* || "$glibc_version" == 2.28* ]]; then
      log "world:        likely Old World (Kylin/UOS/Loongnix style)"
    elif [[ -n "$glibc_version" ]] && version_ge "$glibc_version" "2.36"; then
      log "world:        likely New World"
    else
      log "world:        unknown; continuing with conservative Old World dependency path"
    fi
  else
    log "world:        non-LoongArch Linux; continuing for compatibility/testing"
  fi
}

detect_package_manager() {
  if command -v dnf >/dev/null 2>&1; then
    echo dnf
  elif command -v yum >/dev/null 2>&1; then
    echo yum
  elif command -v apt-get >/dev/null 2>&1; then
    echo apt-get
  else
    echo none
  fi
}

install_system_packages() {
  if [[ "$SKIP_SYSTEM_PACKAGES" == "1" ]]; then
    log "skipping system package installation"
    return 0
  fi
  need_root_for_system_changes

  local pm
  pm="$(detect_package_manager)"
  case "$pm" in
    dnf|yum)
      log "installing system packages with $pm"
      run "$pm" install -y python3 python3-pip python3-devel git gcc gcc-c++ make openssl-devel libffi-devel sudo lsof iproute iputils systemd
      ;;
    apt-get)
      log "installing system packages with apt-get"
      run apt-get update
      run apt-get install -y python3 python3-pip python3-venv python3-dev git gcc g++ make libssl-dev libffi-dev sudo lsof iproute2 iputils-ping systemd
      ;;
    none)
      die "no supported package manager found; install python3.10+, pip, venv, git, gcc, make, sudo, lsof, iproute manually"
      ;;
  esac
}

report_optional_commands() {
  local mode="${1:-$COMMAND_INVENTORY_MODE}"
  local -a commands critical missing critical_missing
  local command
  commands=(
    smartctl crontab aureport aide dmidecode iptables nft
    iostat lsblk findmnt lsattr debsums kysec_getenforce getenforce sestatus
  )
  critical=(smartctl crontab aureport aide dmidecode iptables nft)
  missing=()
  critical_missing=()
  for command in "${commands[@]}"; do
    if ! command -v "$command" >/dev/null 2>&1; then
      missing+=("$command")
    fi
  done
  for command in "${critical[@]}"; do
    if ! command -v "$command" >/dev/null 2>&1; then
      critical_missing+=("$command")
    fi
  done
  if ((${#missing[@]} > 0)); then
    log "optional commands missing: ${missing[*]}"
    log "corresponding MCP tools remain unavailable until distro packages are installed"
  else
    log "optional command inventory: complete"
  fi
  if [[ "$mode" == "competition" && ${#critical_missing[@]} -gt 0 ]]; then
    die "competition command inventory missing critical commands: ${critical_missing[*]}"
  fi
  if [[ "$mode" == "strict" && ${#missing[@]} -gt 0 ]]; then
    die "strict command inventory missing commands: ${missing[*]}"
  fi
}

detect_python() {
  local candidates=()
  if [[ -n "$PYTHON_BIN" ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  candidates+=(python3.13 python3.12 python3.11 python3.10 python3)

  local candidate
  for candidate in "${candidates[@]}"; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if "$candidate" - <<'PY'
import sys
try:
    import venv  # noqa: F401
except Exception:
    raise SystemExit(3)
if (3, 10) <= sys.version_info < (3, 14):
    raise SystemExit(0)
raise SystemExit(2)
PY
    then
      PYTHON_BIN="$candidate"
      log "python:       $("$PYTHON_BIN" --version)"
      return 0
    fi
  done

  die "Python 3.10-3.13 with venv module is required; install an Old World-compatible Python on Kylin V10"
}

create_venv_and_install() {
  cd "$INSTALL_PREFIX"
  if [[ ! -f "pyproject.toml" || ! -f "requirements-loongarch.txt" || ! -f "requirements-loongarch-web.txt" ]]; then
    die "prefix does not look like the kyagent repo: $INSTALL_PREFIX"
  fi

  if [[ ! -d ".venv" ]]; then
    log "creating virtualenv"
    run "$PYTHON_BIN" -m venv .venv
  fi

  local vpy="$INSTALL_PREFIX/.venv/bin/python"
  if [[ "$OFFLINE" == "1" ]]; then
    log "offline mode: skipping pip/setuptools/wheel upgrade"
  else
    log "upgrading pip/setuptools/wheel"
    run "$vpy" -m pip install --upgrade "pip>=23" setuptools wheel
  fi

  log "installing LoongArch-audited default requirements"
  run env SKIP_CYTHON=1 "$vpy" -m pip install "${PIP_OFFLINE_ARGS[@]}" --no-binary PyYAML,pydantic -r requirements-loongarch.txt

  log "installing kyagent editable package without dependency re-resolution"
  run "$vpy" -m pip install "${PIP_OFFLINE_ARGS[@]}" --no-deps -e .

  if [[ "$WITH_WEB" == "1" ]]; then
    log "installing audited optional FastAPI/uvicorn Web requirements"
    run "$vpy" -m pip install "${PIP_OFFLINE_ARGS[@]}" -r requirements-loongarch-web.txt
    run "$vpy" -m pip install "${PIP_OFFLINE_ARGS[@]}" --no-deps -e ".[web]"
  fi

  log "verifying default dependency graph"
  if [[ "$DRY_RUN" != "1" ]]; then
    local freeze_output
    freeze_output="$("$vpy" -m pip freeze)" || die "pip freeze failed during dependency audit"
    if printf '%s\n' "$freeze_output" | grep -Eiq '^(openai|anthropic|mcp|jiter|pydantic-core)=='; then
      die "forbidden optional SDK/Rust dependency found in default LoongArch venv"
    fi
  fi
}

setup_sudoers_and_dirs() {
  if [[ "$SKIP_SUDOERS" == "1" ]]; then
    log "skipping sudoers/account setup"
    return 0
  fi
  need_root_for_system_changes

  log "installing runtime account, sudoers, and audit directories"
  run env \
    KYAGENT_USER="$KYAGENT_USER" \
    KYAGENT_SERVICE_ALLOWLIST="$SERVICE_ALLOWLIST" \
    bash "$INSTALL_PREFIX/scripts/setup-sudoers.sh"
  run visudo -cf /etc/sudoers.d/kyagent
}

verify_runtime_prefix_access() {
  if [[ "$SKIP_SUDOERS" == "1" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: skipping runtime account prefix access check"
    return 0
  fi

  local probe="$INSTALL_PREFIX/scripts/start-web.sh"
  if [[ ! -r "$probe" ]]; then
    die "install prefix is missing a readable Web launcher: $probe"
  fi
  if ! sudo -u "$KYAGENT_USER" test -r "$probe"; then
    die "runtime account cannot read install prefix: $INSTALL_PREFIX; deploy under /opt/kyagent or make every parent directory searchable and project files readable before starting with sudo -u $KYAGENT_USER"
  fi
}

write_shell_assignment() {
  printf '%s=' "$1"
  printf '%q' "$2"
  printf '\n'
}

write_env_file() {
  if [[ "$SKIP_SUDOERS" == "1" ]]; then
    log "skipping /etc/kyagent/env because sudoers setup was skipped"
    return 0
  fi
  need_root_for_system_changes

  local env_dir="/etc/kyagent"
  local env_file="$env_dir/env"
  local tmp
  if [[ "$DEEPSEEK_KEY" == *$'\n'* || "$DEEPSEEK_KEY" == *$'\r'* ]]; then
    die "DEEPSEEK_API_KEY must not contain newlines"
  fi
  tmp="$(mktemp)"
  chmod 0600 "$tmp"

  run install -d -m 0750 -o root -g "$KYAGENT_USER" "$env_dir"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: would preserve or generate $AUDIT_HMAC_KEY_FILE"
  elif [[ ! -s "$AUDIT_HMAC_KEY_FILE" ]]; then
    local key_tmp
    key_tmp="$(mktemp)"
    chmod 0600 "$key_tmp"
    openssl rand -hex 32 >"$key_tmp"
    install -m 0640 -o root -g "$KYAGENT_USER" "$key_tmp" "$AUDIT_HMAC_KEY_FILE"
    rm -f "$key_tmp"
  fi

  {
    write_shell_assignment KYAGENT_CONFIG "$INSTALL_PREFIX/configs/deepseek.yaml"
    write_shell_assignment KYAGENT_DEEPSEEK_TRANSPORT deepseek_httpx
    write_shell_assignment KYAGENT_EXECUTOR_ACCOUNT "$KYAGENT_USER"
    write_shell_assignment KYAGENT_AUDIT_DB /var/lib/kyagent/audit.db
    write_shell_assignment KYAGENT_AUDIT_JSONL /var/log/kyagent/audit.jsonl
    write_shell_assignment KYAGENT_AUDIT_INTEGRITY_ENABLED 1
    write_shell_assignment KYAGENT_AUDIT_HMAC_KEY_FILE "$AUDIT_HMAC_KEY_FILE"
    write_shell_assignment KYAGENT_AUDIT_HMAC_KEY_ID local-v1
    if [[ -n "$DEEPSEEK_KEY" ]]; then
      write_shell_assignment DEEPSEEK_API_KEY "$DEEPSEEK_KEY"
    else
      printf '# DEEPSEEK_API_KEY=sk-...\n'
    fi
  } >"$tmp"

  run install -m 0640 -o root -g "$KYAGENT_USER" "$tmp" "$env_file"
  rm -f "$tmp"
  log "wrote $env_file"
}

run_selfcheck() {
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: skipping Python and kyagent selfcheck"
    return 0
  fi

  local vpy="$INSTALL_PREFIX/.venv/bin/python"
  local ky="$INSTALL_PREFIX/.venv/bin/kyagent"

  log "import selfcheck"
  "$vpy" - <<'PY'
import httpx
import kyagent
import pydantic
import yaml

print("pydantic", pydantic.VERSION)
if getattr(pydantic, "compiled", False):
    raise SystemExit("pydantic must use the SKIP_CYTHON=1 pure-Python path")
print("yaml libyaml", getattr(yaml, "__with_libyaml__", None))
print("httpx", httpx.__version__)
print("kyagent", kyagent.__version__ if hasattr(kyagent, "__version__") else "import-ok")
PY

  log "CLI selfcheck"
  "$ky" tools list >/dev/null
  "$ky" safety test "rm -rf /" >/dev/null

  if [[ "$SKIP_SUDOERS" != "1" ]]; then
    log "runtime-account selfcheck"
    sudo -u "$KYAGENT_USER" \
      --preserve-env=PATH,KYAGENT_CONFIG,KYAGENT_DEEPSEEK_TRANSPORT,KYAGENT_EXECUTOR_ACCOUNT,KYAGENT_AUDIT_DB,KYAGENT_AUDIT_JSONL \
      "$ky" tools list >/dev/null
  fi

  if [[ "$RUN_DEEPSEEK_CHECK" == "1" ]]; then
    if [[ -z "$DEEPSEEK_KEY" && -z "${DEEPSEEK_API_KEY:-}" ]]; then
      die "--run-deepseek-check requires --deepseek-key or DEEPSEEK_API_KEY"
    fi
    log "DeepSeek selfcheck"
    if [[ "$SKIP_SUDOERS" == "1" ]]; then
      KYAGENT_CONFIG="$INSTALL_PREFIX/configs/deepseek.yaml" \
      KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx \
      KYAGENT_AUDIT_DB=/tmp/kyagent-audit.db \
      KYAGENT_AUDIT_JSONL=/tmp/kyagent-audit.jsonl \
      DEEPSEEK_API_KEY="${DEEPSEEK_KEY:-${DEEPSEEK_API_KEY:-}}" \
      "$ky" ask "ping" >/dev/null
    else
      sudo -u "$KYAGENT_USER" bash -c "set -a; source /etc/kyagent/env; set +a; '$ky' ask 'ping' >/dev/null"
    fi
  fi
}

main() {
  configure_options
  INSTALL_PREFIX="$(cd "$INSTALL_PREFIX" && pwd)"
  log "prefix:       $INSTALL_PREFIX"
  log "user:         $KYAGENT_USER"

  confirm "Install kyagent for LoongArch using prefix $INSTALL_PREFIX?" || die "aborted"
  detect_arch
  install_system_packages
  report_optional_commands
  detect_python
  create_venv_and_install
  setup_sudoers_and_dirs
  verify_runtime_prefix_access
  write_env_file
  run_selfcheck

  log "done"
  log "activate: source $INSTALL_PREFIX/.venv/bin/activate"
  log "env:      sudo -u $KYAGENT_USER bash -c 'set -a; source /etc/kyagent/env; set +a; $INSTALL_PREFIX/.venv/bin/kyagent ask \"查 80 端口\"'"
  if [[ "$WITH_WEB" == "1" ]]; then
    log "web:      sudo -u $KYAGENT_USER bash $INSTALL_PREFIX/scripts/start-web.sh --env-file /etc/kyagent/env"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
