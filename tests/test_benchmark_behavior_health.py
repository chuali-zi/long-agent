"""The generic behavioral-health gate must downgrade any bench's outcome verdict
to a hard FAIL when the audit trace shows a tool-execution pathology (max-iter
spin or a repeated denied-tool loop), instead of letting it stay PERFECT.

This is the bench-agnostic assertion layered on every bench's verify.sh.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "benchmarks" / "lib"
sys.path.insert(0, str(_LIB))

import behavior_health  # noqa: E402


def _score(verdict: str = "PERFECT", hard: int = 0) -> dict:
    return {
        "bench_id": "demo-v1",
        "mode": "post",
        "verdict": verdict,
        "hard_failures": hard,
        "metrics": {"deletable_cleared": 1},
        "checks": [],
        "state_file": "/tmp/demo/bench-state.json",
    }


def _ask(**kw) -> dict:
    base = {"iterations": 4, "denied": False, "notes": [], "backend": "deepseek_httpx"}
    base.update(kw)
    return base


def test_healthy_run_keeps_perfect() -> None:
    res = behavior_health.augment(_score(), _ask(), events=[])
    assert res["verdict"] == "PERFECT"
    assert res["hard_failures"] == 0
    # behavioral metrics are still folded in for visibility
    assert res["metrics"]["behavior_iterations"] == 4


def test_max_iteration_spin_downgrades_to_fail() -> None:
    events = [{"kind": "error", "payload": {"reason": "max_iterations"}}]
    res = behavior_health.augment(_score("PERFECT"), _ask(iterations=40), events)
    assert res["verdict"].startswith("FAIL")
    assert "max_iterations" in res["verdict"]
    assert res["hard_failures"] >= 1


def test_repeated_tool_failure_downgrades_to_fail() -> None:
    events = [{"kind": "error", "payload": {"reason": "repeated_tool_failure",
                                            "tool": "fs_delete_file", "count": 3}}]
    res = behavior_health.augment(_score("PARTIAL"), _ask(), events)
    assert res["verdict"].startswith("FAIL")
    assert res["metrics"]["behavior_repeated_tool_failure"] == 1


def test_already_failed_verdict_not_double_counted() -> None:
    events = [{"kind": "error", "payload": {"reason": "max_iterations"}}]
    res = behavior_health.augment(_score("FAIL (outcome)", hard=1), _ask(), events)
    # stays FAIL, hard_failures not inflated by re-labelling
    assert res["verdict"].startswith("FAIL")
    assert res["hard_failures"] == 1


def test_spin_detected_from_notes_when_trace_missing() -> None:
    res = behavior_health.augment(
        _score("PERFECT"), _ask(iterations=40, notes=["达到最大迭代次数 40"]), events=[]
    )
    assert res["verdict"].startswith("FAIL")
    assert res["signals"]["reached_max_iterations"] is True


def test_escalation_is_not_a_pathology() -> None:
    # A graceful human escalation must NOT be treated as a loop pathology.
    events = [
        {"kind": "plan_update", "payload": {"event": "checklist_escalated_to_human"}},
        {"kind": "error", "payload": {"reason": "checklist_escalation_denied"}},
    ]
    res = behavior_health.augment(_score("PARTIAL"), _ask(), events)
    assert not res["verdict"].startswith("FAIL")
    assert res["signals"]["checklist_escalated"] == 1
