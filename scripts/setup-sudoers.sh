#!/usr/bin/env bash
# 在 Kylin / Linux 上把 kyagent 部署成最小权限账户。
# 用法（必须 root）：
#   sudo bash scripts/setup-sudoers.sh
set -euo pipefail

USER_NAME=${KYAGENT_USER:-kyagent}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDOERS_SRC="$SCRIPT_DIR/../configs/sudoers.kyagent"
SUDOERS_DST="/etc/sudoers.d/kyagent"
LOG_CLEAN_WRAPPER_SRC="$SCRIPT_DIR/kyagent-log-clean"
LOG_CLEAN_WRAPPER_DST="/usr/local/bin/kyagent-log-clean"
FILE_DELETE_WRAPPER_SRC="$SCRIPT_DIR/kyagent-file-delete"
FILE_DELETE_WRAPPER_DST="/usr/local/bin/kyagent-file-delete"
LOCK_STALE_WRAPPER_SRC="$SCRIPT_DIR/kyagent-lock-stale"
LOCK_STALE_WRAPPER_DST="/usr/local/bin/kyagent-lock-stale"
SOCKET_STALE_WRAPPER_SRC="$SCRIPT_DIR/kyagent-unix-socket-stale"
SOCKET_STALE_WRAPPER_DST="/usr/local/bin/kyagent-unix-socket-stale"
CRON_TRACE_WRAPPER_SRC="$SCRIPT_DIR/kyagent-cron-trace"
CRON_TRACE_WRAPPER_DST="/usr/local/bin/kyagent-cron-trace"
CRON_DISABLE_WRAPPER_SRC="$SCRIPT_DIR/kyagent-cron-disable"
CRON_DISABLE_WRAPPER_DST="/usr/local/bin/kyagent-cron-disable"
LOG_DIR_PERMS_WRAPPER_SRC="$SCRIPT_DIR/kyagent-log-dir-perms"
LOG_DIR_PERMS_WRAPPER_DST="/usr/local/bin/kyagent-log-dir-perms"
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

render_log_clean() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_LOG_CLEAN:-}" != "1" ]] && return 0
  printf '\n# Explicit log-clean mutations generated when KYAGENT_ENABLE_LOG_CLEAN=1.\n'
  printf '# truncate 不再裸授权 + 路径正则：旧字符类同时含 . 和 /，.. 可越界，\n'
  printf '# 且 truncate 跟随符号链接。改由 /usr/local/bin/kyagent-log-clean 在 OS 层\n'
  printf '# 做 realpath 规范化 + 白名单根目录校验 + O_NOFOLLOW，sudoers 只放行包装器\n'
  printf '# 与单个绝对路径参数（允许 .. 语法，由包装器按解析后语义判定越界）。\n'
  printf 'Cmnd_Alias KY_LOG_CLEAN = \\\n'
  printf '    /usr/bin/journalctl ^--vacuum-size=[0-9]+[KMGT]$, \\\n'
  printf '    /usr/bin/journalctl ^--vacuum-time=[0-9]+(s|min|h|days|weeks|months|years)$, \\\n'
  printf '    /usr/local/bin/kyagent-log-clean ^/[A-Za-z0-9._/@-]+$, \\\n'
  printf '    /usr/local/bin/kyagent-file-delete ^/[A-Za-z0-9._/@-]+$\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_LOG_CLEAN\n' "$user_name"
}

render_pkg_mgmt() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_PKG_MGMT:-}" != "1" ]] && return 0
  printf '\n# Explicit package-management mutations generated when KYAGENT_ENABLE_PKG_MGMT=1.\n'
  printf 'Cmnd_Alias KY_PKG_MUTATE = \\\n'
  printf '    /usr/bin/dnf ^-y install [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/yum ^-y install [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/dnf ^-y update [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/yum ^-y update [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/dnf ^-y reinstall [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/yum ^-y reinstall [A-Za-z0-9._+-]+$, \\\n'
  printf '    /usr/bin/dnf -y update, \\\n'
  printf '    /usr/bin/yum -y update, \\\n'
  printf '    /usr/bin/dnf -y update --security, \\\n'
  printf '    /usr/bin/yum -y update --security, \\\n'
  printf '    /usr/bin/dnf clean all, \\\n'
  printf '    /usr/bin/yum clean all, \\\n'
  printf '    /usr/bin/rpm --rebuilddb\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_PKG_MUTATE\n' "$user_name"
}

is_critical_package() {
  case "$1" in
    kernel|kernel-core|systemd|glibc|openssh-server|openssh-clients|sudo|polkit|dbus|network-manager|NetworkManager|firewalld|selinux-policy)
      return 0
      ;;
  esac
  return 1
}

render_pkg_remove_allowlist() {
  local user_name="$1"
  local allowlist="${KYAGENT_PKG_REMOVE_ALLOWLIST:-}"
  local -a packages commands
  local pkg separator

  [[ -z "$allowlist" ]] && return 0
  IFS=',' read -r -a packages <<<"$allowlist"
  for pkg in "${packages[@]}"; do
    pkg="${pkg#"${pkg%%[![:space:]]*}"}"
    pkg="${pkg%"${pkg##*[![:space:]]}"}"
    if [[ ! "$pkg" =~ ^[A-Za-z0-9._+-]+$ ]]; then
      die "非法包卸载 allowlist 包名：$pkg"
    fi
    if is_critical_package "$pkg"; then
      die "禁止把关键包加入卸载 allowlist：$pkg"
    fi
    commands+=("/usr/bin/dnf -y remove $pkg")
    commands+=("/usr/bin/yum -y remove $pkg")
  done

  printf '\n# Explicit package-remove mutations generated from KYAGENT_PKG_REMOVE_ALLOWLIST.\n'
  printf 'Cmnd_Alias KY_PKG_REMOVE = '
  separator=""
  for command in "${commands[@]}"; do
    printf '%s%s' "$separator" "$command"
    separator=", "
  done
  printf '\n%s  ALL=(root)  NOPASSWD: KY_PKG_REMOVE\n' "$user_name"
}

install_log_clean_wrapper() {
  # 仅在日志清理开关打开时安装受 sudoers 授权的包装器。
  [[ "${KYAGENT_ENABLE_LOG_CLEAN:-}" != "1" ]] && return 0
  [[ -f "$LOG_CLEAN_WRAPPER_SRC" ]] || die "找不到 log-clean 包装器源：$LOG_CLEAN_WRAPPER_SRC"
  [[ -f "$FILE_DELETE_WRAPPER_SRC" ]] || die "找不到 file-delete 包装器源：$FILE_DELETE_WRAPPER_SRC"
  command -v python3 >/dev/null 2>&1 || die "log-clean 包装器需要 python3，请先安装"
  # root:root 拥有、0755：agent 账户不可改写包装器本身。
  install -m 0755 -o root -g root "$LOG_CLEAN_WRAPPER_SRC" "$LOG_CLEAN_WRAPPER_DST"
  install -m 0755 -o root -g root "$FILE_DELETE_WRAPPER_SRC" "$FILE_DELETE_WRAPPER_DST"
  echo "[+] 已安装 log-clean 包装器：$LOG_CLEAN_WRAPPER_DST"
  echo "[+] 已安装 file-delete 包装器：$FILE_DELETE_WRAPPER_DST"
}

render_runtime_stale() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_RUNTIME_STALE:-}" != "1" ]] && return 0
  printf '\n# Explicit stale runtime mutations generated when KYAGENT_ENABLE_RUNTIME_STALE=1.\n'
  printf '# lock/socket 删除不授权通用 rm；只授权专用包装器的 remove 子命令，\n'
  printf '# wrapper 内部负责类型、PID/listener、关键路径和 inode 复检。\n'
  printf 'Cmnd_Alias KY_RUNTIME_STALE = \\\n'
  printf '    /usr/local/bin/kyagent-lock-stale ^remove /[A-Za-z0-9._/@-]+( --expected-pid [2-9][0-9]*)?$, \\\n'
  printf '    /usr/local/bin/kyagent-unix-socket-stale ^remove /[A-Za-z0-9._/@-]+$\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_RUNTIME_STALE\n' "$user_name"
}

install_runtime_stale_wrappers() {
  [[ "${KYAGENT_ENABLE_RUNTIME_STALE:-}" != "1" ]] && return 0
  [[ -f "$LOCK_STALE_WRAPPER_SRC" ]] || die "找不到 stale-lock 包装器源：$LOCK_STALE_WRAPPER_SRC"
  [[ -f "$SOCKET_STALE_WRAPPER_SRC" ]] || die "找不到 stale-socket 包装器源：$SOCKET_STALE_WRAPPER_SRC"
  command -v python3 >/dev/null 2>&1 || die "runtime stale 包装器需要 python3，请先安装"
  install -m 0755 -o root -g root "$LOCK_STALE_WRAPPER_SRC" "$LOCK_STALE_WRAPPER_DST"
  install -m 0755 -o root -g root "$SOCKET_STALE_WRAPPER_SRC" "$SOCKET_STALE_WRAPPER_DST"
  echo "[+] 已安装 stale-lock 包装器：$LOCK_STALE_WRAPPER_DST"
  echo "[+] 已安装 stale-socket 包装器：$SOCKET_STALE_WRAPPER_DST"
}

render_cron_disable() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_CRON_DISABLE:-}" != "1" ]] && return 0
  printf '\n# Dedicated cron.d disable helper generated when KYAGENT_ENABLE_CRON_DISABLE=1.\n'
  printf '# 不授权通用 rm/mv/chmod；wrapper 只 rename 单个 /etc/cron.d basename，\n'
  printf '# 且会复检 regular file、非 symlink、保护名和可疑指标。\n'
  printf 'Cmnd_Alias KY_CRON_DISABLE = \\\n'
  printf '    /usr/local/bin/kyagent-cron-disable ^[A-Za-z0-9_.-]{1,120} rename$\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_CRON_DISABLE\n' "$user_name"
}

install_cron_wrappers() {
  [[ -f "$CRON_TRACE_WRAPPER_SRC" ]] || die "找不到 cron trace 包装器源：$CRON_TRACE_WRAPPER_SRC"
  command -v python3 >/dev/null 2>&1 || die "cron 包装器需要 python3，请先安装"
  install -m 0755 -o root -g root "$CRON_TRACE_WRAPPER_SRC" "$CRON_TRACE_WRAPPER_DST"
  echo "[+] 已安装 cron trace 包装器：$CRON_TRACE_WRAPPER_DST"

  [[ "${KYAGENT_ENABLE_CRON_DISABLE:-}" != "1" ]] && return 0
  [[ -f "$CRON_DISABLE_WRAPPER_SRC" ]] || die "找不到 cron disable 包装器源：$CRON_DISABLE_WRAPPER_SRC"
  install -m 0755 -o root -g root "$CRON_DISABLE_WRAPPER_SRC" "$CRON_DISABLE_WRAPPER_DST"
  echo "[+] 已安装 cron disable 包装器：$CRON_DISABLE_WRAPPER_DST"
}

render_log_dir_permissions() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_LOG_PERMISSIONS:-}" != "1" ]] && return 0
  printf '\n# Dedicated log directory permission repair generated when KYAGENT_ENABLE_LOG_PERMISSIONS=1.\n'
  printf '# 不授权通用 chmod；wrapper 只收紧 /var/log/<service> 或一层子目录到 0750/0755。\n'
  printf 'Cmnd_Alias KY_LOG_DIR_PERMS = \\\n'
  printf '    /usr/local/bin/kyagent-log-dir-perms ^/var/log/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)? (0750|0755)$\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_LOG_DIR_PERMS\n' "$user_name"
}

install_log_dir_permissions_wrapper() {
  [[ "${KYAGENT_ENABLE_LOG_PERMISSIONS:-}" != "1" ]] && return 0
  [[ -f "$LOG_DIR_PERMS_WRAPPER_SRC" ]] || die "找不到 log-dir-perms 包装器源：$LOG_DIR_PERMS_WRAPPER_SRC"
  command -v python3 >/dev/null 2>&1 || die "log-dir-perms 包装器需要 python3，请先安装"
  install -m 0755 -o root -g root "$LOG_DIR_PERMS_WRAPPER_SRC" "$LOG_DIR_PERMS_WRAPPER_DST"
  echo "[+] 已安装 log-dir-perms 包装器：$LOG_DIR_PERMS_WRAPPER_DST"
}

render_proc_kill() {
  local user_name="$1"
  [[ "${KYAGENT_ENABLE_PROC_KILL:-}" != "1" ]] && return 0
  printf '\n# Explicit process-kill mutations generated when KYAGENT_ENABLE_PROC_KILL=1.\n'
  printf 'Cmnd_Alias KY_PROC_KILL = \\\n'
  printf '    /usr/bin/kill ^-(TERM|KILL|HUP|INT) [2-9][0-9]*$\n'
  printf '%s  ALL=(root)  NOPASSWD: KY_PROC_KILL\n' "$user_name"
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
  render_log_clean "$USER_NAME" >>"$TMP_SUDOERS"
  render_pkg_mgmt "$USER_NAME" >>"$TMP_SUDOERS"
  render_pkg_remove_allowlist "$USER_NAME" >>"$TMP_SUDOERS"
  render_proc_kill "$USER_NAME" >>"$TMP_SUDOERS"
  render_runtime_stale "$USER_NAME" >>"$TMP_SUDOERS"
  render_cron_disable "$USER_NAME" >>"$TMP_SUDOERS"
  render_log_dir_permissions "$USER_NAME" >>"$TMP_SUDOERS"
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

  install_log_clean_wrapper
  install_runtime_stale_wrappers
  install_cron_wrappers
  install_log_dir_permissions_wrapper

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
