#!/usr/bin/env bash
# 一键写入「生产预设」最小权限 sudoers。
#
# 这是 setup-sudoers.sh 的薄封装：它预先打开常用写操作开关，并给出一份
# 贴近真实运维的「常见可重启服务」白名单，然后把控制权交给被审计的核心脚本
# setup-sudoers.sh（由它负责 visudo 校验 / 安装后复检 / 失败回滚）。
#
# 用途：在麒麟 / LoongArch 生产节点上，运维不想手搓 sudoers 时，一条命令即可
# 得到一份「默认覆盖大多数日常运维写操作」的最小权限策略。
#
# 用法（必须 root）：
#   sudo bash scripts/setup-sudoers-prod.sh            # 交互确认后写入
#   sudo bash scripts/setup-sudoers-prod.sh --yes      # 跳过确认
#   sudo bash scripts/kyagent.sh permissions-prod      # 等价入口
#
# 自定义（任意一项都可用环境变量覆盖本脚本的默认值）：
#   sudo env KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
#            KYAGENT_ENABLE_PROC_KILL=0 \
#            bash scripts/setup-sudoers-prod.sh --yes
#
# 安全说明：
#   - 本脚本只是把「显式授权」一次性配好；最终 sudoers 仍是固定命令 + 锚定
#     参数正则（见 setup-sudoers.sh 的 render_* 函数），不是通配放行。
#   - 默认不动核心服务（systemd-* / dbus / polkit 由核心脚本拒绝）。
#   - 不想要某类写权限，把对应开关设为 0 即可。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 生产预设默认值（均可被已存在的环境变量覆盖） -------------------------
# 写操作开关：默认全开，覆盖最常见的运维动作。
export KYAGENT_ENABLE_LOG_CLEAN="${KYAGENT_ENABLE_LOG_CLEAN:-1}"
export KYAGENT_ENABLE_PKG_MGMT="${KYAGENT_ENABLE_PKG_MGMT:-1}"
export KYAGENT_ENABLE_PROC_KILL="${KYAGENT_ENABLE_PROC_KILL:-1}"
export KYAGENT_ENABLE_RUNTIME_STALE="${KYAGENT_ENABLE_RUNTIME_STALE:-1}"
export KYAGENT_ENABLE_CRON_DISABLE="${KYAGENT_ENABLE_CRON_DISABLE:-1}"
export KYAGENT_ENABLE_LOG_PERMISSIONS="${KYAGENT_ENABLE_LOG_PERMISSIONS:-1}"
export KYAGENT_PKG_REMOVE_ALLOWLIST="${KYAGENT_PKG_REMOVE_ALLOWLIST:-}"

# 常见可重启服务白名单（教育/医疗数据中心节点上最可能出现的一组）。
# 仅授权 systemctl restart/reload <unit>，不含 stop/disable/mask。
# 不存在的服务列在这里是无害的：sudo 永远不会匹配到未被调用的命令。
DEFAULT_SERVICE_ALLOWLIST="\
nginx.service,\
httpd.service,\
sshd.service,\
firewalld.service,\
chronyd.service,\
crond.service,\
rsyslog.service,\
mariadb.service,\
mysqld.service,\
postgresql.service,\
redis.service,\
docker.service,\
php-fpm.service"
export KYAGENT_SERVICE_ALLOWLIST="${KYAGENT_SERVICE_ALLOWLIST:-$DEFAULT_SERVICE_ALLOWLIST}"

USER_NAME="${KYAGENT_USER:-kyagent}"

# ---- 参数解析 --------------------------------------------------------------
ASSUME_YES=0
PASSTHRU=()
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) PASSTHRU+=("$arg") ;;
  esac
done

bullet() { printf '    - %s\n' "$1"; }

echo "[kyagent] 生产预设 sudoers —— 将为账户 '${USER_NAME}' 授权以下写操作："
echo "  运行账户:        ${USER_NAME}"
echo "  日志清理:        $([[ "$KYAGENT_ENABLE_LOG_CLEAN" == 1 ]] && echo '开 (journalctl --vacuum / truncate/delete /var/log,/var/cache,/var/tmp,/tmp)' || echo '关')"
echo "  包管理:          $([[ "$KYAGENT_ENABLE_PKG_MGMT" == 1 ]] && echo '开 (dnf/yum install/update/reinstall/security/clean-cache + rpm rebuilddb)' || echo '关')"
echo "  包卸载 allowlist: ${KYAGENT_PKG_REMOVE_ALLOWLIST:-<empty>}"
echo "  进程终止:        $([[ "$KYAGENT_ENABLE_PROC_KILL" == 1 ]] && echo '开 (kill -TERM|KILL|HUP|INT <pid>)' || echo '关')"
echo "  stale runtime:   $([[ "$KYAGENT_ENABLE_RUNTIME_STALE" == 1 ]] && echo '开 (stale lock/socket 专用 wrapper)' || echo '关')"
echo "  cron 禁用:       $([[ "$KYAGENT_ENABLE_CRON_DISABLE" == 1 ]] && echo '开 (/etc/cron.d 单入口 rename 禁用)' || echo '关')"
echo "  日志目录权限:    $([[ "$KYAGENT_ENABLE_LOG_PERMISSIONS" == 1 ]] && echo '开 (/var/log/<service> 目录权限收紧)' || echo '关')"
echo "  可重启服务白名单:"
IFS=',' read -r -a _units <<<"$KYAGENT_SERVICE_ALLOWLIST"
for u in "${_units[@]}"; do
  u="${u#"${u%%[![:space:]]*}"}"; u="${u%"${u##*[![:space:]]}"}"
  [[ -n "$u" ]] && bullet "systemctl restart/reload $u"
done
echo "  说明: 危险动作（删内核/systemd 等关键包、清空 /etc 等关键路径、kill pid<2）"
echo "        仍会被工具层 + 安全护栏拦截；最终 sudoers 由 setup-sudoers.sh 经 visudo 校验。"
echo

if [[ "$ASSUME_YES" -ne 1 ]]; then
  if [[ -t 0 ]]; then
    read -r -p "确认写入以上生产预设 sudoers？[y/N] " reply
    case "$reply" in
      y|Y|yes|YES) ;;
      *) echo "[kyagent] 已取消。"; exit 0 ;;
    esac
  else
    echo "[kyagent][ERROR] 非交互环境请显式加 --yes 确认。" >&2
    exit 1
  fi
fi

exec bash "$SCRIPT_DIR/setup-sudoers.sh" "${PASSTHRU[@]}"
