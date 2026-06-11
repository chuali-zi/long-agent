"""Shared RealOps benchmark grading — strict automation contract.

Post-mode automation_pass is True only when verdict == PERFECT.
INCONCLUSIVE and PARTIAL exit non-zero so VM/opencode loops surface real gaps.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"

VERDICT_EXIT_POST = {
    "PERFECT": 0,
    "FAIL": 1,
    "PARTIAL": 2,
    "INCONCLUSIVE": 3,
}

VERDICT_EXIT_PRE = {
    "SETUP_OK": 0,
    "SETUP_BROKEN": 4,
}


def normalize_verdict(raw: str) -> str:
    token = raw.split("(", 1)[0].strip().upper()
    if token.startswith("FAIL"):
        return "FAIL"
    if token.startswith("SETUP"):
        return "SETUP_OK" if "OK" in token else "SETUP_BROKEN"
    return token


def exit_code_for(mode: str, verdict: str) -> int:
    norm = normalize_verdict(verdict)
    if mode == "pre":
        return VERDICT_EXIT_PRE.get(norm, 10)
    return VERDICT_EXIT_POST.get(norm, 10)


def automation_pass(mode: str, verdict: str) -> bool:
    norm = normalize_verdict(verdict)
    if mode == "pre":
        return norm == "SETUP_OK"
    return norm == "PERFECT"


def write_score(
    *,
    bench_id: str,
    mode: str,
    verdict: str,
    hard_failures: int,
    metrics: dict[str, Any],
    state_dir: Path,
    verdict_detail: str = "",
    checks: list[dict[str, Any]] | None = None,
    state_file: str = "",
    verify_script: str = "",
) -> Path:
    norm = normalize_verdict(verdict)
    code = exit_code_for(mode, norm)
    score_path = Path(
        os.environ.get("KYBENCH_SCORE_JSON")
        or state_dir / "score.json"
    )
    doc = {
        "schema_version": SCHEMA_VERSION,
        "bench_id": bench_id,
        "mode": mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": norm,
        "verdict_detail": verdict_detail or verdict,
        "exit_code": code,
        "automation_pass": automation_pass(mode, norm),
        "hard_failures": hard_failures,
        "metrics": metrics,
        "checks": checks or [],
        "state_file": state_file,
        "verify_script": verify_script,
    }
    score_path.parent.mkdir(parents=True, exist_ok=True)
    score_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return score_path


def shell_exit_from_score(score_path: Path) -> int:
    data = json.loads(score_path.read_text(encoding="utf-8"))
    return int(data.get("exit_code", 10))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: grade.py write <payload.json>  |  grade.py exit <score.json>")
        return 0
    cmd = args[0]
    if cmd == "write" and len(args) >= 2:
        payload = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        write_score(**payload)
        return 0
    if cmd == "exit" and len(args) >= 2:
        return shell_exit_from_score(Path(args[1]))
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 10


if __name__ == "__main__":
    sys.exit(main())
