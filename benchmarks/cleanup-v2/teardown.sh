#!/usr/bin/env bash
# 拆除 cleanup-v2 场景：仅删除 bench-state.json 记录的产物 + 我们创建的 root 目录。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYBENCH_STATE:-$ROOT/bench-state.json}"

log() { printf '[cleanup-v2:teardown] %s\n' "$*"; }
die() { printf '[cleanup-v2:teardown][ERROR] %s\n' "$*" >&2; exit 1; }

[[ -f "$STATE_FILE" ]] || die "找不到 $STATE_FILE，无法安全确定要删什么。"

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "需要 python3"

# 用 state 里记录的精确路径删除，避免误伤真实日志。
"$PY" - "$STATE_FILE" <<'PY'
import json, os, sys, shutil
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for art in data["artifacts"]:
    p = Path(art["path"])
    if p.exists():
        p.unlink()
        print(f"  - removed {p}")
# 自底向上删空目录（只删我们的 root 子树里残留的空目录）
for root in sorted(data["roots"].values(), key=len, reverse=True):
    rp = Path(root)
    if rp.exists():
        # 仅当目录为空或只剩空子目录时整树删除
        shutil.rmtree(rp, ignore_errors=True)
        print(f"  - removed tree {rp}")
PY

rm -f "$STATE_FILE"
log "done"
