"""Agent multi-tool scheduling safety tests."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import kyagent.agent.core as core
from kyagent.agent.core import Agent
from kyagent.agent.llm import AssistantMessage, LlmBackend, TextBlock, ToolUseBlock
from kyagent.config import Config
from kyagent.executor.proxy import ExecutionResult


class ScriptedBackend(LlmBackend):
    name = "scripted"

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]):
        self.calls = calls

    def chat(self, system, messages, tools):  # noqa: ANN001
        last = messages[-1] if messages else None
        if (
            last
            and last.get("role") == "user"
            and isinstance(last.get("content"), list)
            and any(c.get("type") == "tool_result" for c in last["content"] if isinstance(c, dict))
        ):
            return AssistantMessage(blocks=[TextBlock(text="done")], stop_reason="end_turn")

        return AssistantMessage(
            blocks=[
                TextBlock(text=(
                    "TODO 1: Dispatch the scripted tool calls for this test turn.\n"
                    "TODO 2: Return the combined tool results to the agent loop."
                )),
                *[
                    ToolUseBlock(id=f"tool-{idx}", name=name, input=args)
                    for idx, (name, args) in enumerate(self.calls)
                ],
            ],
            stop_reason="tool_use",
        )


class RecordingExecutor:
    def __init__(self, *, supports_parallel_tool_execution: bool):
        self.supports_parallel_tool_execution = supports_parallel_tool_execution
        self.thread_names: list[str] = []

    def run(self, argv, *, requires_root=False, **kwargs):  # noqa: ANN001, ARG002
        self.thread_names.append(threading.current_thread().name)
        time.sleep(0.02)
        return ExecutionResult(
            argv=list(argv),
            returncode=0,
            stdout="ok\n",
            stderr="",
            truncated=False,
            duration=0.0,
            sudo_used=requires_root,
            run_as="test",
        )


def _agent(tmp_path: Path, backend: LlmBackend, executor: RecordingExecutor, confirm=None) -> Agent:
    cfg = Config(base_dir=Path(__file__).parent.parent)
    cfg.audit.database = str(tmp_path / "audit.db")
    cfg.audit.jsonl_file = str(tmp_path / "audit.jsonl")
    cfg.safety.rules_file = "configs/safety-rules.yaml"
    cfg.agent.llm_backend = "mock"
    cfg.agent.max_iterations = 2
    agent = Agent.from_config(cfg, confirm=confirm or (lambda *a, **k: False))
    agent.llm = backend
    agent.executor = executor
    return agent


def test_posix_executor_that_disallows_threaded_tools_runs_multi_tool_turn_serially(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    executor = RecordingExecutor(supports_parallel_tool_execution=False)
    agent = _agent(
        tmp_path,
        ScriptedBackend([
            ("process_list", {"sort_by": "cpu", "limit": 5}),
            ("fs_df", {}),
        ]),
        executor,
    )
    monkeypatch.setattr(core.sys, "platform", "linux")

    agent.ask("multi low risk")

    assert executor.thread_names == ["MainThread", "MainThread"]


def test_confirm_required_tools_do_not_enter_parallel_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    confirm_threads: list[str] = []

    def deny_confirm(req):  # noqa: ANN001, ARG001
        confirm_threads.append(threading.current_thread().name)
        time.sleep(0.02)
        return False

    agent = _agent(
        tmp_path,
        ScriptedBackend([
            ("svc_reload", {"unit": "nginx"}),
            ("svc_reload", {"unit": "sshd"}),
        ]),
        executor,
        confirm=deny_confirm,
    )
    monkeypatch.setattr(core.sys, "platform", "linux")

    result = agent.ask("reload services")

    assert result.denied
    assert confirm_threads == ["MainThread", "MainThread"]
    assert executor.thread_names == []


def test_is_parallel_safe_rejects_when_llm_reviewer_enabled(tmp_path):
    """C2 第一道防线：guardrail 启用 llm_reviewer 时预检不可信，禁用并行。"""
    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    agent = _agent(
        tmp_path,
        ScriptedBackend([("process_list", {"sort_by": "cpu", "limit": 5})]),
        executor,
    )
    tool_use = ToolUseBlock(id="tool-0", name="process_list",
                            input={"sort_by": "cpu", "limit": 5})

    assert agent._is_parallel_safe(tool_use) is True

    agent.guardrail.llm_reviewer = lambda cmdline: None
    assert agent._is_parallel_safe(tool_use) is False


def test_handle_tool_use_denies_confirm_off_main_thread(tmp_path):
    """C2 第二道防线：worker 线程内拿到 CONFIRM 一律 deny，不调用 self.confirm()。"""
    from concurrent.futures import ThreadPoolExecutor

    from kyagent.audit.trace import EventKind, Trace

    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    confirm_called: list[str] = []

    def confirm_should_never_run(req):  # noqa: ANN001, ARG001
        confirm_called.append("RAN")
        return True

    agent = _agent(
        tmp_path,
        ScriptedBackend([("svc_reload", {"unit": "nginx"})]),
        executor,
        confirm=confirm_should_never_run,
    )

    trace = Trace(user="tester")
    agent.audit.open(trace)
    tool_use = ToolUseBlock(id="tool-0", name="svc_reload", input={"unit": "nginx"})
    notes: list[str] = []

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ky-test") as pool:
        result_block = pool.submit(agent._handle_tool_use, trace, tool_use, notes).result()

    assert confirm_called == []
    assert result_block.is_error
    assert result_block.content.startswith("[denied]")
    assert any("非主线程" in n for n in notes)

    error_events = [e for e in trace.events if e.kind is EventKind.ERROR]
    assert any(e.payload.get("reason") == "confirm_in_worker_denied" for e in error_events)


def test_agent_run_thread_may_handle_confirm_when_not_main_thread(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Web runs Agent.ask in a worker thread; that owning run thread may confirm.

    The guard still has to reject confirm attempts from parallel tool workers,
    but the thread that is synchronously executing the turn is the right place
    for Web's blocking approval callback.
    """
    from concurrent.futures import ThreadPoolExecutor

    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    confirm_threads: list[str] = []

    def approve_confirm(req):  # noqa: ANN001, ARG001
        confirm_threads.append(threading.current_thread().name)
        return True

    agent = _agent(
        tmp_path,
        ScriptedBackend([("svc_reload", {"unit": "nginx"})]),
        executor,
        confirm=approve_confirm,
    )
    monkeypatch.setattr(core.sys, "platform", "linux")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-run") as pool:
        result = pool.submit(agent.ask, "reload nginx").result()

    assert not result.denied
    assert confirm_threads and confirm_threads[0].startswith("web-run")
    assert executor.thread_names and executor.thread_names[0].startswith("web-run")
