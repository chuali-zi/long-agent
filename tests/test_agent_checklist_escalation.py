"""Checklist-blocked destructive writes escalate to human approval, and a
repeated-failure loop guard aborts instead of spinning to max_iterations.

Regression for the secret-spill-v1 bug: the file-remediation checklist used to
return a "re-enumerate then retry" message straight back to the model, which
looped forever and never surfaced a human-approval popup.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from kyagent.agent.core import Agent
from kyagent.agent.llm import ToolUseBlock
from kyagent.audit.trace import Trace


def _agent_with_confirm(confirm) -> Agent:
    agent = Agent.__new__(Agent)
    agent.audit = SimpleNamespace(event=lambda *a, **k: None)
    agent._active_run_thread_id = threading.get_ident()
    agent.confirm = confirm
    return agent


def _delete_call(path: str = "/var/log/auth-api01/app/current.log") -> tuple:
    cleaned = {"path": path}
    prep = SimpleNamespace(cleaned=cleaned)
    tu = ToolUseBlock(id="tool-1", name="fs_delete_file", input=cleaned)
    return tu, prep


def test_checklist_block_approved_returns_none() -> None:
    seen: list = []
    agent = _agent_with_confirm(lambda req: seen.append(req) or True)
    tu, prep = _delete_call()

    decision = Agent._escalate_checklist_block(
        agent, Trace(), tu, prep, "candidate is marked protect"
    )

    assert decision is None  # approved -> caller proceeds to execute
    assert len(seen) == 1  # a human approval request was raised


def test_checklist_block_rejected_returns_terminal_stop() -> None:
    agent = _agent_with_confirm(lambda req: False)
    tu, prep = _delete_call()

    decision = Agent._escalate_checklist_block(
        agent, Trace(), tu, prep, "candidate is marked protect"
    )

    assert decision is not None
    assert decision.is_error
    assert decision.content.startswith("[stop]")
    # must not tell the model to retry / re-enumerate
    assert "retry" not in decision.content.lower()
    assert "重试" not in decision.content or "请勿重试" in decision.content


def test_checklist_block_off_thread_does_not_call_confirm() -> None:
    called = {"n": 0}

    def confirm(_req):
        called["n"] += 1
        return True

    agent = _agent_with_confirm(confirm)
    agent._active_run_thread_id = -1  # pretend we are not on the run thread
    tu, prep = _delete_call()

    decision = Agent._escalate_checklist_block(
        agent, Trace(), tu, prep, "candidate roots are incomplete"
    )

    assert decision is not None
    assert decision.is_error
    assert decision.content.startswith("[stop]")
    assert called["n"] == 0  # no popup attempted off the run thread


def test_record_tool_failure_aborts_after_threshold() -> None:
    agent = Agent.__new__(Agent)
    agent.cfg = SimpleNamespace(agent=SimpleNamespace(max_repeated_tool_failures=3))
    counters: dict[str, int] = {}
    tu = ToolUseBlock(id="t", name="fs_delete_file", input={"path": "/var/tmp/x"})

    assert Agent._record_tool_failure(agent, counters, tu) is None
    assert Agent._record_tool_failure(agent, counters, tu) is None
    result = Agent._record_tool_failure(agent, counters, tu)

    assert result == ("fs_delete_file", 3)


def test_record_tool_failure_distinct_args_do_not_accumulate() -> None:
    agent = Agent.__new__(Agent)
    agent.cfg = SimpleNamespace(agent=SimpleNamespace(max_repeated_tool_failures=2))
    counters: dict[str, int] = {}
    tu_a = ToolUseBlock(id="t", name="fs_delete_file", input={"path": "/a"})
    tu_b = ToolUseBlock(id="t", name="fs_delete_file", input={"path": "/b"})

    assert Agent._record_tool_failure(agent, counters, tu_a) is None
    assert Agent._record_tool_failure(agent, counters, tu_b) is None  # different sig
    assert Agent._record_tool_failure(agent, counters, tu_a) == ("fs_delete_file", 2)
