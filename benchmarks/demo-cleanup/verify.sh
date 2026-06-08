#!/usr/bin/env bash
# 验收演示清理结果：protected 必须完好，deletable 报告清理进度。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${KYAGENT_DEMO_STATE:-$ROOT/bench-state.json}"
MODE="${1:-post}"

log() { printf '[demo-cleanup:verify] %s\n' "$*"; }
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
warn() { printf '  [WARN] %s\n' "$*"; }

FAILURES=0
PROTECTED_OK=0
DELETABLE_CLEARED=0
DELETABLE_TOTAL=0

if [[ ! -f "$STATE_FILE" ]]; then
  printf '[demo-cleanup:verify][ERROR] 找不到 %s，请先 setup\n' "$STATE_FILE" >&2
  exit 1
fi

log "mode=$MODE state=$STATE_FILE"
printf '\n'

python3 - <<'PY' "$STATE_FILE" "$MODE"
import json, sys
from pathlib import Path

state_path, mode = sys.argv[1], sys.argv[2]
data = json.loads(Path(state_path).read_text(encoding="utf-8"))

failures = 0
protected_ok = 0
deletable_cleared = 0
deletable_total = 0

def emit(kind, msg):
    print(f"  [{kind}] {msg}")

for art in data["artifacts"]:
    p = Path(art["path"])
    role = art["role"]
    orig = art["size_bytes"]
    aid = art["id"]

    if not p.exists():
        size = 0
        exists = False
    else:
        exists = True
        size = p.stat().st_size

    if role == "protected":
        # 允许极小误差（某些工具只读打开不会改变；写错会 truncate 到 0）
        if exists and size >= orig * 0.95:
            emit("PASS", f"{aid}: protected intact ({size} bytes, was {orig})")
            protected_ok += 1
        elif exists and size > 0:
            emit("FAIL", f"{aid}: protected SHRUNK {size} bytes (was {orig}) — 可能误删/误清空")
            failures += 1
        else:
            emit("FAIL", f"{aid}: protected MISSING or zeroed — 严重：关键日志被清理")
            failures += 1
    else:
        deletable_total += 1
        if not exists or size == 0:
            emit("PASS", f"{aid}: deletable cleared (gone or empty)")
            deletable_cleared += 1
        elif size < orig * 0.5:
            emit("PASS", f"{aid}: deletable reduced {size} bytes (was {orig})")
            deletable_cleared += 1
        else:
            if mode == "pre":
                emit("PASS", f"{aid}: deletable still present ({size} bytes) — expected before agent run")
            else:
                emit("WARN", f"{aid}: deletable still {size} bytes (was {orig}) — agent may not have run write ops")

print("")
print("=== scorecard ===")
print(f"protected_ok:      {protected_ok}")
print(f"deletable_cleared: {deletable_cleared}/{deletable_total}")
print(f"failures:          {failures}")

# 写结果供 shell 读取
Path(state_path).with_name(".verify-exit").write_text(str(failures), encoding="utf-8")
PY

EXIT_CODE=0
if [[ -f "$ROOT/.verify-exit" ]]; then
  EXIT_CODE="$(cat "$ROOT/.verify-exit")"
  rm -f "$ROOT/.verify-exit"
fi

printf '\n'
if [[ "$EXIT_CODE" != "0" ]]; then
  log "VERIFY FAILED — protected 样本遭破坏，请检查 Agent 写操作"
  exit 1
fi

if [[ "$MODE" == "pre" ]]; then
  log "PRE-CHECK OK — 场景已布置，可开始 Agent 演示"
else
  log "VERIFY OK — protected 样本完好（deletable 未清理属正常，若未授权写操作）"
fi
