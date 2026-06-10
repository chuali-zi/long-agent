"""Agent ask_user_choice + chat_stream thinking_delta tests.

Locks in the two seams added on branch experimental/tui-streaming:

  1. ``ask_user_choice`` tool round-trip: LLM emits a tool_use with
     question/options → Agent intercepts (does NOT hit ExecutionProxy) →
     ``on_user_choice`` callback returns the chosen value → ToolResultBlock
     is fed back to the LLM. Refusal path returns is_error=True.

  2. Agent main loop emits ``thinking_delta`` events while streaming. When
     the underlying backend does not implement ``chat_stream`` (this branch
     gates on parallel LLM work), the Agent's getattr fallback still emits
     a single delta carrying the full text — UI contract preserved.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kyagent.agent.core import Agent
from kyagent.agent.llm import AssistantMessage, LlmBackend, TextBlock, ToolUseBlock
from kyagent.config import Config
from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.interactive import AskUserChoiceTool
from kyagent.progress import ProgressEvent


# ---- 1. Schema validation -------------------------------------------------


def test_ask_user_choice_schema_accepts_valid():
    tool = AskUserChoiceTool()
    cleaned = tool.validate({
        "question": "go?",
        "options": [
            {"value": "y", "label": "Yes"},
            {"value": "n", "label": "No"},
        ],
    })
    assert cleaned["question"] == "go?"
    assert len(cleaned["options"]) == 2


def test_ask_user_choice_schema_rejects_missing_options():
    tool = AskUserChoiceTool()
    with pytest.raises(ToolError):
        tool.validate({"question": "go?"})


def test_ask_user_choice_schema_rejects_single_option():
    tool = AskUserChoiceTool()
    with pytest.raises(ToolError):
        tool.validate({
            "question": "go?",
            "options": [{"value": "y", "label": "Yes"}],
        })


# ---- 2. Agent routing for ask_user_choice --------------------------------


class _ChoiceBackend(LlmBackend):
    """First turn: emit ask_user_choice tool_use. Second turn: emit final text."""

    name = "scripted-choice"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_args = {
            "question": "yes or no?",
            "options": [
                {"value": "y", "label": "Yes"},
                {"value": "n", "label": "No"},
            ],
        }

    def chat(self, system, messages, tools):  # noqa: ANN001
        self.calls += 1
        last = messages[-1] if messages else None
        if (
            last
            and last.get("role") == "user"
            and isinstance(last.get("content"), list)
            and any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in last["content"]
            )
        ):
            return AssistantMessage(
                blocks=[TextBlock(text="done")],
                stop_reason="end_turn",
            )
        return AssistantMessage(
            blocks=[
                TextBlock(text=(
                    "TODO 1: Ask the user to choose one allowed option.\n"
                    "TODO 2: Continue only after the choice tool returns."
                )),
                ToolUseBlock(
                    id="tool-0", name="ask_user_choice", input=self.tool_args
                ),
            ],
            stop_reason="tool_use",
        )


class _NeverRunExecutor:
    """Asserts ask_user_choice never falls through to ExecutionProxy."""

    supports_parallel_tool_execution = False

    def run(self, *a: Any, **kw: Any) -> ExecutionResult:  # pragma: no cover
        raise AssertionError("ask_user_choice must NOT touch executor")


def _agent_for_choice(
    tmp_path: Path,
    backend: LlmBackend,
    *,
    on_user_choice,
    on_progress=None,
    auto_approve_safe_remediation: bool = False,
) -> Agent:
    cfg = Config(base_dir=Path(__file__).parent.parent)
    cfg.audit.database = str(tmp_path / "audit.db")
    cfg.audit.jsonl_file = str(tmp_path / "audit.jsonl")
    cfg.safety.rules_file = "configs/safety-rules.yaml"
    cfg.agent.llm_backend = "mock"
    cfg.agent.max_iterations = 3
    agent = Agent.from_config(
        cfg,
        confirm=lambda *a, **k: False,
        on_progress=on_progress,
        on_user_choice=on_user_choice,
        auto_approve_safe_remediation=auto_approve_safe_remediation,
    )
    agent.llm = backend
    agent.executor = _NeverRunExecutor()
    return agent


def test_agent_routes_user_choice_to_callback(tmp_path: Path):
    events: list[ProgressEvent] = []
    backend = _ChoiceBackend()
    agent = _agent_for_choice(
        tmp_path,
        backend,
        on_user_choice=lambda c: "y",
        on_progress=events.append,
    )

    result = agent.ask("please decide")

    # The LLM saw "用户选择: y" in tool_result and answered "done"
    assert result.final_text == "done"
    # progress contract: at least one user_choice event with options meta
    choice_events = [e for e in events if e.kind == "user_choice"]
    assert len(choice_events) == 1
    assert choice_events[0].text == "yes or no?"
    assert {o["value"] for o in choice_events[0].meta["options"]} == {"y", "n"}
    # back-end was called twice (1 ask, 1 wrap-up)
    assert backend.calls == 2


def test_agent_user_choice_refusal_returns_is_error(tmp_path: Path):
    backend = _ChoiceBackend()
    agent = _agent_for_choice(
        tmp_path,
        backend,
        on_user_choice=lambda c: "",  # 用户拒绝
    )

    agent.ask("please decide")

    # Round 1 tool_result must be is_error=True with the canonical refusal text.
    # The injected tool_result block lives in messages as the user msg right
    # after the assistant tool_use.
    tool_results: list[dict] = []
    for m in agent.messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tool_results.append(c)
    assert tool_results, "expected at least one tool_result in transcript"
    refusal = tool_results[0]
    assert refusal["is_error"] is True
    assert "未做出选择" in refusal["content"]


def test_agent_user_choice_auto_remediation_does_not_call_callback(tmp_path: Path):
    backend = _ChoiceBackend()
    callback_called = False

    def choose(_choice):
        nonlocal callback_called
        callback_called = True
        raise AssertionError("auto remediation mode must not wait for user choice")

    agent = _agent_for_choice(
        tmp_path,
        backend,
        on_user_choice=choose,
        auto_approve_safe_remediation=True,
    )

    agent.ask("please decide")

    assert callback_called is False
    tool_result = next(
        item
        for message in agent.messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for item in message["content"]
        if isinstance(item, dict) and item.get("type") == "tool_result"
    )
    assert tool_result["is_error"] is True
    assert "非交互安全修复模式" in tool_result["content"]


def test_agent_user_choice_rejects_invalid_value(tmp_path: Path):
    """LLM 不应通过 callback 偷塞 options 里没出现过的 value。"""
    backend = _ChoiceBackend()
    agent = _agent_for_choice(
        tmp_path,
        backend,
        on_user_choice=lambda c: "maybe",  # 不在 {y, n} 集合内
    )

    agent.ask("please decide")

    refusal: dict | None = None
    for m in agent.messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    refusal = c
                    break
    assert refusal is not None
    assert refusal["is_error"] is True


def test_agent_user_choice_validates_schema_before_callback(tmp_path: Path):
    backend = _ChoiceBackend()
    backend.tool_args["options"] = [{"value": "y", "label": "Only option"}]
    callback_called = False

    def choose(_choice):
        nonlocal callback_called
        callback_called = True
        return "y"

    agent = _agent_for_choice(tmp_path, backend, on_user_choice=choose)
    agent.ask("please decide")

    assert callback_called is False
    tool_result = next(
        item
        for message in agent.messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for item in message["content"]
        if isinstance(item, dict) and item.get("type") == "tool_result"
    )
    assert tool_result["is_error"] is True
    assert "参数" in tool_result["content"]


# ---- 3. thinking_delta fallback ------------------------------------------


class _PlainBackend(LlmBackend):
    """No chat_stream method — exercises the getattr fallback in Agent.ask."""

    name = "plain"

    def __init__(self, text: str = "hello world") -> None:
        self.text = text

    def chat(self, system, messages, tools):  # noqa: ANN001
        return AssistantMessage(
            blocks=[TextBlock(text=self.text)],
            stop_reason="end_turn",
        )


def test_agent_emits_thinking_delta_via_fallback(tmp_path: Path):
    """Backend without a chat_stream override uses LlmBackend.chat_stream default
    (chat() one-shot + single full-text delta). Agent main loop must surface it
    as one thinking_delta progress event."""
    events: list[ProgressEvent] = []
    backend = _PlainBackend(text="the quick brown fox")
    # Confirm we are exercising the base-class default (not a per-backend override)
    assert type(backend).chat_stream is LlmBackend.chat_stream

    agent = _agent_for_choice(
        tmp_path,
        backend,
        on_user_choice=lambda c: "",
        on_progress=events.append,
    )
    agent.ask("hi")

    deltas = [e for e in events if e.kind == "thinking_delta"]
    assert len(deltas) == 1
    assert deltas[0].delta == "the quick brown fox"
