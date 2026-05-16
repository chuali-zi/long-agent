#!/usr/bin/env bash
# 在 Kylin / Linux 上把 kyagent 部署成最小权限账户。
# 用法（必须 root）：
#   sudo bash scripts/setup-sudoers.sh
set -euo pipefail

USER_NAME=${KYAGENT_USER:-kyagent}
SUDOERS_SRC="$(dirname "$0")/../configs/sudoers.kyagent"
SUDOERS_DST="/etc/sudoers.d/kyagent"

if [[ $EUID -ne 0 ]]; then
  echo "[!] 此脚本需要 root" >&2
  exit 1
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
  echo "[+] 创建系统账户 $USER_NAME"
  useradd -r -s /usr/sbin/nologin -m -d "/var/lib/$USER_NAME" "$USER_NAME"
fi

# 让 kyagent 能读 journalctl
if getent group systemd-journal >/dev/null; then
  usermod -aG systemd-journal "$USER_NAME"
fi

install -m 0440 "$SUDOERS_SRC" "$SUDOERS_DST"
if ! visudo -cf "$SUDOERS_DST"; then
  echo "[!] sudoers 语法校验失败，已回滚" >&2
  rm -f "$SUDOERS_DST"
  exit 2
fi

mkdir -p /var/log/sudo-io
chmod 0750 /var/log/sudo-io
mkdir -p "/var/lib/kyagent" /var/log/kyagent
chown -R "$USER_NAME":"$USER_NAME" "/var/lib/kyagent" /var/log/kyagent

echo "[OK] kyagent 部署完成。"
echo "    账户:        $USER_NAME"
echo "    sudoers:     $SUDOERS_DST"
echo "    数据目录:    /var/lib/kyagent"
echo "    日志目录:    /var/log/kyagent"
