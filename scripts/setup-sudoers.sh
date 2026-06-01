#!/usr/bin/env bash
# 在 Kylin / Linux 上把 kyagent 部署成最小权限账户。
# 用法（必须 root）：
#   sudo bash scripts/setup-sudoers.sh
set -euo pipefail

USER_NAME=${KYAGENT_USER:-kyagent}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDOERS_SRC="$SCRIPT_DIR/../configs/sudoers.kyagent"
SUDOERS_DST="/etc/sudoers.d/kyagent"
BACKUP=""
TMP_SUDOERS=""

cleanup() {
  [[ -n "$TMP_SUDOERS" && -f "$TMP_SUDOERS" ]] && rm -f "$TMP_SUDOERS"
  return 0
}
trap cleanup EXIT

die() {
  echo "[!] $*" >&2
  exit 1
}

detect_sudo_version() {
  SUDO_VERSION_LINE="$(LC_ALL=C sudo -V 2>&1 | sed -n '1p')"
  SUDO_VERSION="$(printf '%s\n' "$SUDO_VERSION_LINE" | grep -Eo '[0-9]+([.][0-9]+)+([[:alnum:]._-]+)?' | head -1)"
  printf '%s\n' "$SUDO_VERSION"
}

render_service_allowlist() {
  local user_name="$1"
  local allowlist="${2:-}"
  local -a units commands
  local unit command separator

  [[ -z "$allowlist" ]] && return 0
  IFS=',' read -r -a units <<<"$allowlist"
  for unit in "${units[@]}"; do
    unit="${unit#"${unit%%[![:space:]]*}"}"
    unit="${unit%"${unit##*[![:space:]]}"}"
    if [[ ! "$unit" =~ ^[A-Za-z0-9@_+:][-A-Za-z0-9@._+:]*$ ]]; then
      die "非法服务 allowlist unit：$unit"
    fi
    if [[ "$unit" == *.* && "$unit" != *.service ]]; then
      die "非法服务 allowlist unit：$unit；仅允许 service unit"
    fi
    case "$unit" in
      systemd-*|dbus|dbus.service|polkit|polkit.service)
        die "非法服务 allowlist unit：$unit；核心服务禁止授权"
        ;;
    esac
    commands+=("/usr/bin/systemctl restart $unit")
    commands+=("/usr/bin/systemctl reload $unit")
  done

  printf '\n# Explicit service mutations generated from KYAGENT_SERVICE_ALLOWLIST.\n'
  printf 'Cmnd_Alias KY_SVC_MUTATE = '
  separator=""
  for command in "${commands[@]}"; do
    printf '%s%s' "$separator" "$command"
    separator=", "
  done
  printf '\n%s  ALL=(root)  NOPASSWD: KY_SVC_MUTATE\n' "$user_name"
}

main() {
  if [[ $EUID -ne 0 ]]; then
    die "此脚本需要 root；请执行 sudo bash scripts/kyagent.sh permissions"
  fi

  if ! command -v visudo >/dev/null 2>&1; then
    die "找不到 visudo，请先安装 sudo 包"
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    die "找不到 sudo，请先安装 sudo >= 1.9.10"
  fi

  detect_sudo_version >/dev/null || true
  local sudo_version="$SUDO_VERSION"
  if [[ -z "$sudo_version" ]] || ! printf '%s\n' "1.9.10" "$sudo_version" | sort -V -C; then
    die "sudo 版本过旧或无法识别：${sudo_version:-unknown}；原始输出：${SUDO_VERSION_LINE:-empty}；参数正则要求 sudo >= 1.9.10"
  fi

  if [[ ! "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    die "非法运行账户名：$USER_NAME"
  fi

  if [[ ! -f "$SUDOERS_SRC" ]]; then
    die "找不到 sudoers 模板：$SUDOERS_SRC"
  fi

  if ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "[+] 创建系统账户 $USER_NAME"
    NOLOGIN="/usr/sbin/nologin"
    [[ -x "$NOLOGIN" ]] || NOLOGIN="/sbin/nologin"
    useradd -r -s "$NOLOGIN" -m -d "/var/lib/$USER_NAME" "$USER_NAME"
  fi

  # 让运行账户能读 journalctl。最小化系统没有该组时跳过。
  if getent group systemd-journal >/dev/null; then
    usermod -aG systemd-journal "$USER_NAME"
  fi

  TMP_SUDOERS="$(mktemp)"
  if [[ "$USER_NAME" == "kyagent" ]]; then
    cp "$SUDOERS_SRC" "$TMP_SUDOERS"
  else
    # 模板默认写 kyagent；允许通过 KYAGENT_USER 部署到自定义账户。
    sed \
      -e "s/^Defaults:kyagent/Defaults:${USER_NAME}/" \
      -e "s/^kyagent[[:space:]]/${USER_NAME} /" \
      -e "s#/usr/bin/sudo -l -U kyagent#/usr/bin/sudo -l -U ${USER_NAME}#" \
      "$SUDOERS_SRC" >"$TMP_SUDOERS"
  fi
  render_service_allowlist "$USER_NAME" "${KYAGENT_SERVICE_ALLOWLIST:-}" >>"$TMP_SUDOERS"
  chmod 0440 "$TMP_SUDOERS"

  if ! visudo -cf "$TMP_SUDOERS"; then
    die "sudoers 临时文件语法校验失败，未写入 $SUDOERS_DST"
  fi

  if [[ -f "$SUDOERS_DST" ]]; then
    BACKUP="$(mktemp)"
    cp -p "$SUDOERS_DST" "$BACKUP"
  fi

  install -m 0440 "$TMP_SUDOERS" "$SUDOERS_DST"
  if ! visudo -cf "$SUDOERS_DST"; then
    echo "[!] sudoers 安装后校验失败，开始回滚" >&2
    if [[ -n "$BACKUP" && -f "$BACKUP" ]]; then
      install -m 0440 "$BACKUP" "$SUDOERS_DST"
    else
      rm -f "$SUDOERS_DST"
    fi
    [[ -n "$BACKUP" ]] && rm -f "$BACKUP"
    exit 2
  fi
  [[ -n "$BACKUP" ]] && rm -f "$BACKUP"

  mkdir -p /var/log/sudo-io
  chmod 0700 /var/log/sudo-io
  install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "/var/lib/kyagent"
  install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" /var/log/kyagent

  echo "[OK] kyagent 最小权限账户部署完成。"
  echo "    账户:        $USER_NAME"
  echo "    sudoers:     $SUDOERS_DST"
  echo "    数据目录:    /var/lib/kyagent"
  echo "    日志目录:    /var/log/kyagent"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
