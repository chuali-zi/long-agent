#!/usr/bin/env bash
# 只读探测：模拟 Agent 在「清理前」应看到的磁盘/日志线索（不调用 LLM）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYAGENT_DEMO_STATE:-$ROOT/bench-state.json}"
LOG_ROOT=/var/log/kyagent-demo-bench

log() { printf '[demo-cleanup:probe] %s\n' "$*"; }
section() { printf '\n=== %s ===\n' "$*"; }

if [[ ! -d "$LOG_ROOT" ]]; then
  printf '[demo-cleanup:probe][ERROR] 演示目录不存在，请先: sudo bash %s/setup.sh\n' "$ROOT" >&2
  exit 1
fi

section "演示目录占用"
du -sh "$LOG_ROOT" /var/cache/kyagent-demo-bench /tmp/kyagent-demo-bench 2>/dev/null || true

section "/var/log 下 >1MB 文件（log_files_top 同类线索）"
find /var/log/kyagent-demo-bench -type f -size +1M -printf '%12s  %TY-%Tm-%Td %TH:%TM  %p\n' 2>/dev/null \
  | sort -rn \
  || find /var/log/kyagent-demo-bench -type f -size +1048576c -exec ls -lh {} \;

section "按角色分类（manifest 预期）"
if [[ -f "$STATE_FILE" ]]; then
  python3 - <<'PY' "$STATE_FILE"
import json, sys
from datetime import datetime
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for role in ("protected", "deletable"):
    print(f"\n-- {role} --")
    for art in data["artifacts"]:
        if art["role"] != role:
            continue
        p = Path(art["path"])
        if not p.exists():
            print(f"  MISSING  {art['path']}")
            continue
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {st.st_size/1024/1024:6.1f} MB  mtime={mtime}  {art['path']}")
PY
else
  log "bench-state.json 不存在，仅列出文件"
  find /var/log/kyagent-demo-bench /var/cache/kyagent-demo-bench /tmp/kyagent-demo-bench -type f -printf '%p %s\n' 2>/dev/null | sort
fi

section "建议 Agent 演示提示词（中文）"
cat <<'EOF'
1) 感知-only（安全，适合录像前半段）:
   「/var/log/kyagent-demo-bench 里哪些日志占空间？哪些像数据库日志不能删？」

2) 赛题完整场景（需 permissions-prod + 人工/Web 确认写操作）:
   「帮我清理系统垃圾，重点看 /var/log/kyagent-demo-bench 和 /var/cache/kyagent-demo-bench，
    陈旧归档可以清理，但不要动 MariaDB/PostgreSQL 数据库日志。」

3) 英文对照:
   "Under /var/log/kyagent-demo-bench, which files are safe to clean and which database logs must stay?"
EOF

section "写操作前置条件（若要让 Agent 真清理）"
cat <<'EOF'
  sudo bash scripts/setup-sudoers-prod.sh --yes
  sudo visudo -cf /etc/sudoers.d/kyagent
  # Web/CLI 对 fs_truncate / log_delete_file 会走 confirm；单轮 ask 默认等同拒绝写操作。
  # 演示「真清理」请用 Web 审核卡片，或 chat/tui 模式确认。
EOF

log "probe complete"
