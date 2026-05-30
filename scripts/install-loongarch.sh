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
  --allow-non-loongarch         Allow running checks on non-loongarch64 hosts.
  --prefix PATH                 Project directory. Defaults to current repo root.
  --user USER                   Runtime account. Defaults to kyagent.
  --python PATH                 Python 3.10-3.13 interpreter to use.
  --skip-system-packages        Do not install OS packages.
  --skip-sudoers                Do not create account/sudoers/audit dirs.
  --deepseek-key KEY            Write DEEPSEEK_API_KEY to /etc/kyagent/env.
  --run-deepseek-check          Run a real DeepSeek request after install.
  --with-web                    Install the optional FastAPI/uvicorn Web extra.
  --help                        Show this help.

Default LoongArch path:
  pip install --no-binary PyYAML -r requirements-loongarch.txt
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
    --deepseek-key)
      DEEPSEEK_KEY="${2:?missing value for --deepseek-key}"
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
  local arch kernel glibc_line glibc_version
  arch="$(uname -m)"
  kernel="$(uname -r)"
  glibc_line="$(ldd --version 2>/dev/null | head -1 || true)"
  glibc_version="$(printf '%s\n' "$glibc_line" | grep -Eo '[0-9]+\.[0-9]+' | head -1 || true)"

  log "architecture: $arch"
  log "kernel:       $kernel"
  log "glibc:        ${glibc_line:-unknown}"

  if [[ "$arch" != "loongarch64" && "$ALLOW_NON_LOONGARCH" != "1" ]]; then
    die "this installer targets loongarch64; pass --allow-non-loongarch only for dry-run/testing"
  fi

  if [[ "$arch" == "loongarch64" ]]; then
    if [[ "$kernel" == 4.19* || "$glibc_version" == 2.28* ]]; then
      log "world:        likely Old World (Kylin/UOS/Loongnix style)"
    elif [[ -n "$glibc_version" ]] && version_ge "$glibc_version" "2.36"; then
      log "world:        likely New World"
    else
      log "world:        unknown; continuing with conservative Old World dependency path"
    fi
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
  if [[ ! -f "pyproject.toml" || ! -f "requirements-loongarch.txt" ]]; then
    die "prefix does not look like the kyagent repo: $INSTALL_PREFIX"
  fi

  if [[ ! -d ".venv" ]]; then
    log "creating virtualenv"
    run "$PYTHON_BIN" -m venv .venv
  fi

  local vpy="$INSTALL_PREFIX/.venv/bin/python"
  log "upgrading pip/setuptools/wheel"
  run "$vpy" -m pip install --upgrade "pip>=23" setuptools wheel

  log "installing LoongArch-audited default requirements"
  run "$vpy" -m pip install --no-binary PyYAML -r requirements-loongarch.txt

  log "installing kyagent editable package without optional SDK extras"
  run "$vpy" -m pip install -e .

  if [[ "$WITH_WEB" == "1" ]]; then
    log "installing optional FastAPI/uvicorn Web extra"
    run "$vpy" -m pip install -e ".[web]"
  fi

  log "verifying default dependency graph"
  if [[ "$DRY_RUN" != "1" ]]; then
    "$vpy" -m pip freeze | grep -Eiq '^(openai|anthropic|mcp|jiter|pydantic-core)==' \
      && die "forbidden optional SDK/Rust dependency found in default LoongArch venv" \
      || true
  fi
}

setup_sudoers_and_dirs() {
  if [[ "$SKIP_SUDOERS" == "1" ]]; then
    log "skipping sudoers/account setup"
    return 0
  fi
  need_root_for_system_changes

  log "installing runtime account, sudoers, and audit directories"
  run env KYAGENT_USER="$KYAGENT_USER" bash "$INSTALL_PREFIX/scripts/setup-sudoers.sh"
  run visudo -cf /etc/sudoers.d/kyagent
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
  tmp="$(mktemp)"
  chmod 0600 "$tmp"

  {
    printf 'KYAGENT_CONFIG=%s/configs/deepseek.yaml\n' "$INSTALL_PREFIX"
    printf 'KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx\n'
    printf 'KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db\n'
    printf 'KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl\n'
    if [[ -n "$DEEPSEEK_KEY" ]]; then
      printf 'DEEPSEEK_API_KEY=%s\n' "$DEEPSEEK_KEY"
    else
      printf '# DEEPSEEK_API_KEY=sk-...\n'
    fi
  } >"$tmp"

  run install -d -m 0750 -o "$KYAGENT_USER" -g "$KYAGENT_USER" "$env_dir"
  run install -m 0600 -o "$KYAGENT_USER" -g "$KYAGENT_USER" "$tmp" "$env_file"
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
      --preserve-env=PATH,KYAGENT_CONFIG,KYAGENT_DEEPSEEK_TRANSPORT,KYAGENT_AUDIT_DB,KYAGENT_AUDIT_JSONL \
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
  INSTALL_PREFIX="$(cd "$INSTALL_PREFIX" && pwd)"
  log "prefix:       $INSTALL_PREFIX"
  log "user:         $KYAGENT_USER"

  confirm "Install kyagent for LoongArch using prefix $INSTALL_PREFIX?" || die "aborted"
  detect_arch
  install_system_packages
  detect_python
  create_venv_and_install
  setup_sudoers_and_dirs
  write_env_file
  run_selfcheck

  log "done"
  log "activate: source $INSTALL_PREFIX/.venv/bin/activate"
  log "env:      sudo -u $KYAGENT_USER bash -c 'set -a; source /etc/kyagent/env; set +a; $INSTALL_PREFIX/.venv/bin/kyagent ask \"查 80 端口\"'"
  if [[ "$WITH_WEB" == "1" ]]; then
    log "web:      sudo -u $KYAGENT_USER bash $INSTALL_PREFIX/scripts/start-web.sh --env-file /etc/kyagent/env"
  fi
}

main "$@"
