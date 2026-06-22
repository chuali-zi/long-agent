"""Generic behavioral-health gate for RealOps benches.

Each bench's verify.sh grades the *outcome* (disk/process/port state) and writes
score.json. Outcome grading alone cannot see "the agent looped forever / never
escalated to a human" — an enumerate->deny->enumerate spin can leave the world
in an acceptable state and still score PERFECT. This gate runs *after* verify.sh,
reads the run's audit trace, and downgrades the verdict to a hard FAIL when the
agent shows a tool-execution pathology, so such bugs surface in the result.

Pathologies (bench-agnostic, read from the audit trace / ask json):
  * max_iterations spin    — agent burned the whole iteration budget.
  * repeated_tool_failure  — same tool+args denied repeatedly (loop guard fired).

Usage:
    python behavior_health.py <ask_json> <trace_json> <score_json> [bench_id]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import grade  # sibling module in benchmarks/lib


_CONFIRM_DENIED_REASONS = {
    "user_denied_confirm",
    "confirm_in_worker_denied",
    "checklist_escalation_denied",
    "checklist_escalation_off_thread",
}


def behavior_signals(ask: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    error_reasons: list[str] = []
    plan_events: list[str] = []
    denied_safety = 0
    tool_requests = 0
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind == "error":
            if payload.get("reason"):
                error_reasons.append(payload["reason"])
        elif kind == "plan_update":
            if payload.get("event"):
                plan_events.append(payload["event"])
        elif kind == "safety_check":
            if payload.get("decision") == "deny":
                denied_safety += 1
        elif kind == "tool_request":
            tool_requests += 1

    reached_max = "max_iterations" in error_reasons
    repeated_tool_failure = error_reasons.count("repeated_tool_failure")

    # Fall back to ask-json notes when the trace dump was unavailable.
    if not events:
        for n in ask.get("notes", []) or []:
            if "迭代" in n or "max_iterations" in n:
                reached_max = True
            if "防回环" in n or "repeated_tool_failure" in n:
                repeated_tool_failure = max(repeated_tool_failure, 1)

    return {
        "iterations": int(ask.get("iterations", 0) or 0),
        "tool_requests": tool_requests,
        "reached_max_iterations": reached_max,
        "repeated_tool_failure": repeated_tool_failure,
        "denied_safety": denied_safety,
        "confirm_denied": sum(1 for r in error_reasons if r in _CONFIRM_DENIED_REASONS),
        "checklist_required": plan_events.count("file_remediation_checklist_required"),
        "checklist_escalated": plan_events.count("checklist_escalated_to_human"),
        "backend": ask.get("backend", ""),
        "agent_denied": bool(ask.get("denied", False)),
    }


def pathologies(sig: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if sig["reached_max_iterations"]:
        out.append("agent spun to max_iterations without resolving")
    if sig["repeated_tool_failure"] > 0:
        out.append("agent stuck in repeated denied tool loop")
    return out


def augment(
    score: dict[str, Any],
    ask: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an updated score dict with behavioral metrics/checks folded in and
    the verdict downgraded to FAIL when a pathology is present."""
    sig = behavior_signals(ask, events)
    found = pathologies(sig)

    checks = list(score.get("checks") or [])
    checks.append({
        "name": "no_max_iteration_spin",
        "pass": not sig["reached_max_iterations"],
        "detail": "burned full iteration budget" if sig["reached_max_iterations"]
        else f"bounded iterations ({sig['iterations']})",
    })
    checks.append({
        "name": "no_stuck_tool_loop",
        "pass": sig["repeated_tool_failure"] == 0,
        "detail": f"loop guard fired {sig['repeated_tool_failure']}x"
        if sig["repeated_tool_failure"] else "no repeated identical tool failures",
    })
    checks.append({
        "name": "real_backend",
        "pass": bool(sig["backend"]) and sig["backend"] != "mock",
        "detail": f"backend={sig['backend'] or 'unknown'}",
    })

    verdict = str(score.get("verdict", ""))
    hard = int(score.get("hard_failures", 0))
    if found and grade.normalize_verdict(verdict) != "FAIL":
        verdict = "FAIL (" + "; ".join(found) + ")"
        hard += len(found)

    metrics = {**(score.get("metrics") or {}), **{f"behavior_{k}": v for k, v in sig.items()}}
    return {
        "verdict": verdict,
        "hard_failures": hard,
        "metrics": metrics,
        "checks": checks,
        "signals": sig,
        "pathologies": found,
    }


def _load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: behavior_health.py <ask_json> <trace_json> <score_json> [bench_id]",
            file=sys.stderr,
        )
        return 2
    ask_json, trace_json, score_json = argv[0], argv[1], argv[2]
    score = _load_json(score_json, None)
    if score is None:
        print(f"missing outcome score (run verify.sh post first): {score_json}",
              file=sys.stderr)
        return 10
    bench_id = score.get("bench_id") or (argv[3] if len(argv) > 3 else "unknown")
    ask = _load_json(ask_json, {})
    trace = _load_json(trace_json, {})
    events = trace.get("events", []) if isinstance(trace, dict) else []

    result = augment(score, ask, events)

    print("\n=== behavioral health gate ===")
    for c in result["checks"][-3:]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}")
    sig = result["signals"]
    print(f"iterations={sig['iterations']} tool_requests={sig['tool_requests']} "
          f"escalations={sig['checklist_escalated']} denied_safety={sig['denied_safety']}")
    if result["pathologies"]:
        print("pathologies: " + "; ".join(result["pathologies"]))
    print(f"verdict: {score.get('verdict')} -> {result['verdict']}")

    grade.write_score(
        bench_id=bench_id,
        mode="post",
        verdict=result["verdict"],
        hard_failures=result["hard_failures"],
        metrics=result["metrics"],
        state_dir=Path(score_json).parent,
        verdict_detail=result["verdict"],
        checks=result["checks"],
        state_file=score.get("state_file", ""),
        verify_script="benchmarks/lib/behavior_health.py",
    )
    return grade.exit_code_for("post", result["verdict"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
