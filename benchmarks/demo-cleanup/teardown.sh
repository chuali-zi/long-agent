#!/usr/bin/env bash
# 拆除 kyagent 清理演示场景（删除演示目录，不影响系统其他日志）。
set -euo pipefail

log() { printf '[demo-cleanup:teardown] %s\n' "$*"; }
die() { printf '[demo-cleanup:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "需要 root；请使用: sudo bash $0"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYAGENT_DEMO_STATE:-$ROOT/bench-state.json}"

for dir in /var/log/kyagent-demo-bench /var/cache/kyagent-demo-bench /tmp/kyagent-demo-bench; do
  if [[ -d "$dir" ]]; then
    log "removing $dir"
    rm -rf -- "$dir"
  else
    log "skip missing $dir"
  fi
done

rm -f -- "$STATE_FILE"
log "done"
