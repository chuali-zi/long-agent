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

if [[ $EUID -ne 0 ]]; then
  die "此脚本需要 root"
fi

if ! command -v visudo >/dev/null 2>&1; then
  die "找不到 visudo，请先安装 sudo 包"
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
    "$SUDOERS_SRC" >"$TMP_SUDOERS"
fi
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
chmod 0750 /var/log/sudo-io
mkdir -p "/var/lib/kyagent" /var/log/kyagent
chown -R "$USER_NAME":"$USER_NAME" "/var/lib/kyagent" /var/log/kyagent

echo "[OK] kyagent 部署完成。"
echo "    账户:        $USER_NAME"
echo "    sudoers:     $SUDOERS_DST"
echo "    数据目录:    /var/lib/kyagent"
echo "    日志目录:    /var/log/kyagent"
