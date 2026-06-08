#!/usr/bin/env bash
# cleanup-v2：布置「高伪装」清理对照样本。
#
# 与 v1 的区别：planted 文件**不含任何 bench 标记**，角色只记录在 bench-state.json。
# 被测 Agent 必须像真人运维一样，靠文件名/mtime/后缀/位置/内容语义判断可删与否。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"
LOG_ROOT="${KYBENCH_LOG_ROOT:-/var/log/web-app01}"
CACHE_ROOT="${KYBENCH_CACHE_ROOT:-/var/cache/web-app01}"
TMP_ROOT="${KYBENCH_TMP_ROOT:-/var/tmp/web-app01}"

log() { printf '[cleanup-v2:setup] %s\n' "$*"; }
die() { printf '[cleanup-v2:setup][ERROR] %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "需要 python3"

# 默认系统根（/var/log/web-app01 等）需要 root；操作者显式覆盖 KYBENCH_*_ROOT 视为沙箱，不强制。
if [[ -z "${KYBENCH_LOG_ROOT:-}" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "在默认系统根 $LOG_ROOT 布置需要 root：sudo bash $0（沙箱测试请设 KYBENCH_LOG_ROOT/CACHE_ROOT/TMP_ROOT）"
fi

# 防误伤：若目标根已存在且非本 bench 创建，拒绝覆盖。
for d in "$LOG_ROOT" "$CACHE_ROOT" "$TMP_ROOT"; do
  if [[ -d "$d" && -n "$(ls -A "$d" 2>/dev/null || true)" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
      die "$d 已存在且非空，且无 bench-state.json —— 拒绝覆盖（可能是真实目录）。请换 KYBENCH_*_ROOT 或先手动清理。"
    fi
    log "复用已存在的 bench 目录：$d"
  fi
done

log "生成伪装样本 -> log=$LOG_ROOT cache=$CACHE_ROOT tmp=$TMP_ROOT"
"$PY" "$ROOT/gen_artifacts.py" \
  --log-root "$LOG_ROOT" --cache-root "$CACHE_ROOT" --tmp-root "$TMP_ROOT" \
  --state "$STATE_FILE"

log "完成。下一步只读探测：bash $ROOT/probe.sh"
log "演示提示词见：$ROOT/README.md"
