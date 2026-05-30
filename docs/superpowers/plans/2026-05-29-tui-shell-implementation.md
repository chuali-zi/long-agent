# kyagent TUI Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sustainable kyagent TUI demo channel with continuous interaction, visual status, confirmation, and audit replay, while keeping LoongArch as a first-class deployment target.

**Architecture:** Add a thin `kyagent.tui` package beside the existing CLI. The TUI owns prompt/render/replay presentation only; it constructs the existing `Agent` through `Agent.from_config(cfg, confirm=...)`, reads history through `AuditStore`, and lists tools through `ToolRegistry`. Safety checks, tool argument preparation, execution, audit writes, LLM calls, and confirmation semantics remain in the existing agent/runtime pipeline.

**Tech Stack:** Python 3.10+, Typer, Rich, prompt_toolkit 3.x, pytest. `prompt_toolkit` is pure Python and acceptable on LoongArch; do not add Textual, curses-native UI frameworks, Rust-backed SDK paths, or new async event loops for this demo.

---

## Design Boundaries

- Add a lightweight TUI/REPL channel; keep the current `chat`, `ask`, `tools`, `safety`, `audit`, and `mcp` commands working unchanged.
- Default interactive input is `prompt_toolkit.PromptSession`; all visible output is rendered with Rich.
- Reuse `Agent.from_config(confirm=...)` for construction so runtime wiring, enabled tools, audit paths, guardrails, and LLM backend selection stay centralized.
- Reuse `ConfirmRequest` exactly as the UI contract. TUI confirmation returns `True` only for explicit approve input.
- Reuse `AuditStore` for replay. The replay view reads persisted events and does not mutate traces.
- Reuse `ToolRegistry` for tool visualization. The TUI never shells out or calls tools directly.
- A small event/summary helper is allowed for display shaping. It may derive counts, duration, risk badges, trace titles, and compact event rows from `Trace` or `AuditStore` rows.
- Do not rewrite `ExecutionProxy`, `kyagent.mcp.tools.pipeline`, `Guardrail`, `IntentGuard`, or the safety execution path.
- Do not implement streaming LLM output in this change. The demo can show a spinner/status while `Agent.ask()` runs and then render the completed turn.
- LoongArch default path must avoid Rust-backed required dependencies. Keep Anthropic/OpenAI SDK extras optional; prefer configured `*_httpx` backends or `mock` for demo environments.

## File Structure

- Create `kyagent/tui/__init__.py`: package marker and exported names.
- Create `kyagent/tui/events.py`: small pure helpers for summarizing traces and persisted audit events for display.
- Create `kyagent/tui/render.py`: Rich rendering functions for banner, tools, confirmation prompts, turn result, and replay timeline.
- Create `kyagent/tui/shell.py`: the interactive REPL channel built on `PromptSession`, command dispatch, `Agent.from_config`, and `AuditStore`.
- Modify `kyagent/cli.py`: add a `tui` command that delegates to `kyagent.tui.shell.run_tui`.
- Modify `pyproject.toml`: add `prompt_toolkit>=3.0,<4` to default dependencies.
- Modify `requirements.txt` and `requirements-loongarch.txt`: add matching `prompt_toolkit>=3.0,<4`.
- Add `tests/test_tui_events.py`: event summary and replay row tests.
- Add `tests/test_tui_render.py`: confirmation rendering and tools table smoke tests.
- Add `tests/test_tui_shell.py`: REPL command handling with fake prompt session and fake agent.
- Add `tests/test_cli_tui.py`: Typer command wiring test.
- Modify `docs/kyagent/README.md` and `docs/deployment/loongarch.md`: document the TUI demo command and LoongArch dependency note after behavior is green.

---

### Task 1: Dependency Declaration For LoongArch-Safe TUI

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `requirements-loongarch.txt`

- [ ] **Step 1: Write the failing dependency test**

Add this test to `tests/test_loongarch_deploy_docs.py`:

```python
def test_tui_dependency_is_prompt_toolkit_and_not_textual():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    req = Path("requirements.txt").read_text(encoding="utf-8")
    loong = Path("requirements-loongarch.txt").read_text(encoding="utf-8")

    for content in (pyproject, req, loong):
        assert "prompt_toolkit" in content
        assert "textual" not in content.lower()
        assert "prompt-toolkit" not in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_loongarch_deploy_docs.py::test_tui_dependency_is_prompt_toolkit_and_not_textual -v`

Expected: FAIL because `prompt_toolkit` is not declared yet.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, add this line to `[project].dependencies` after `rich>=13.7`:

```toml
    "prompt_toolkit>=3.0,<4",
```

In `requirements.txt` and `requirements-loongarch.txt`, add:

```text
prompt_toolkit>=3.0,<4
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_loongarch_deploy_docs.py::test_tui_dependency_is_prompt_toolkit_and_not_textual -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt requirements-loongarch.txt tests/test_loongarch_deploy_docs.py
git commit -m "test: pin loongarch safe tui dependency"
```

---

### Task 2: Event Summary Helper

**Files:**
- Create: `kyagent/tui/__init__.py`
- Create: `kyagent/tui/events.py`
- Test: `tests/test_tui_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tui_events.py`:

```python
from __future__ import annotations

from kyagent.audit.trace import EventKind, Trace
from kyagent.tui.events import event_label, summarize_trace, timeline_rows


def test_summarize_trace_counts_events_and_duration():
    trace = Trace(user="demo")
    trace.metadata["backend"] = "mock"
    trace.add(EventKind.USER_INPUT, {"text": "查 CPU"})
    trace.add(EventKind.LLM_THOUGHT, {"tool_calls": ["process_list"]})
    trace.add(EventKind.AGENT_REPLY, {"text": "ok"})

    summary = summarize_trace(trace)

    assert summary.trace_id == trace.trace_id
    assert summary.user == "demo"
    assert summary.backend == "mock"
    assert summary.event_count == 3
    assert summary.by_kind["user_input"] == 1
    assert summary.denied is False
    assert summary.duration_seconds >= 0


def test_summarize_trace_marks_denied_from_result_or_event():
    trace = Trace(user="demo")
    trace.add(EventKind.USER_INPUT, {"text": "重启 nginx"})
    trace.add(EventKind.AGENT_REPLY, {"text": "[denied] 用户拒绝"})

    summary = summarize_trace(trace, denied=True)

    assert summary.denied is True


def test_timeline_rows_extract_compact_labels():
    events = [
        {"seq": 1, "kind": "user_input", "ts": 1.0, "payload": {"text": "查 22 端口"}},
        {"seq": 2, "kind": "safety_check", "ts": 2.0, "payload": {"decision": "allow", "risk": "low"}},
        {"seq": 3, "kind": "agent_reply", "ts": 3.0, "payload": {"text": "sshd"}},
    ]

    rows = timeline_rows(events)

    assert rows[0].seq == 1
    assert rows[0].kind == "user_input"
    assert rows[0].summary == "查 22 端口"
    assert rows[1].badge == "allow/low"
    assert rows[2].summary == "sshd"


def test_event_label_uses_stable_chinese_names():
    assert event_label("user_input") == "用户输入"
    assert event_label("llm_thought") == "模型决策"
    assert event_label("unknown_kind") == "unknown_kind"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui_events.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'kyagent.tui'`.

- [ ] **Step 3: Implement the pure helper**

Create `kyagent/tui/__init__.py`:

```python
"""Lightweight Rich/prompt_toolkit TUI channel for kyagent."""
```

Create `kyagent/tui/events.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kyagent.audit.trace import Trace


_LABELS = {
    "user_input": "用户输入",
    "intent_check": "意图检查",
    "perception": "环境感知",
    "llm_thought": "模型决策",
    "tool_request": "工具请求",
    "safety_check": "安全校验",
    "execution": "执行",
    "execution_result": "执行结果",
    "agent_reply": "最终回复",
    "error": "错误",
}


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    user: str
    backend: str
    event_count: int
    by_kind: dict[str, int]
    duration_seconds: float
    denied: bool


@dataclass(frozen=True)
class TimelineRow:
    seq: int
    kind: str
    label: str
    badge: str
    summary: str


def event_label(kind: str) -> str:
    return _LABELS.get(kind, kind)


def summarize_trace(trace: Trace, denied: bool = False) -> TraceSummary:
    raw = trace.summary()
    backend = str(trace.metadata.get("backend", trace.metadata.get("channel", "?")))
    event_text = "\n".join(str(ev.payload.get("text", "")) for ev in trace.events)
    inferred_denied = denied or "[denied]" in event_text or "[blocked]" in event_text
    return TraceSummary(
        trace_id=trace.trace_id,
        user=trace.user,
        backend=backend,
        event_count=int(raw["event_count"]),
        by_kind=dict(raw["by_kind"]),
        duration_seconds=float(raw["duration"]),
        denied=inferred_denied,
    )


def timeline_rows(events: list[dict[str, Any]]) -> list[TimelineRow]:
    return [_row(ev) for ev in events]


def _row(ev: dict[str, Any]) -> TimelineRow:
    kind = str(ev.get("kind", ""))
    payload = ev.get("payload") or {}
    badge = ""
    if kind == "safety_check":
        badge = f"{payload.get('decision', '?')}/{payload.get('risk', '?')}"
    elif kind == "execution_result":
        badge = f"exit={payload.get('returncode', '?')}"
    elif kind == "llm_thought":
        calls = payload.get("tool_calls") or []
        badge = f"tools={len(calls)}"
    return TimelineRow(
        seq=int(ev.get("seq", 0)),
        kind=kind,
        label=event_label(kind),
        badge=badge,
        summary=_summary(kind, payload),
    )


def _summary(kind: str, payload: dict[str, Any]) -> str:
    if kind == "user_input":
        return _clip(str(payload.get("text", "")))
    if kind == "agent_reply":
        return _clip(str(payload.get("text", "")))
    if kind == "tool_request":
        return _clip(str(payload.get("tool", payload.get("name", ""))))
    if kind == "execution":
        return _clip(" ".join(str(x) for x in payload.get("argv", [])))
    if kind == "execution_result":
        stdout = str(payload.get("stdout", ""))
        stderr = str(payload.get("stderr", ""))
        return _clip(stdout or stderr)
    if kind == "safety_check":
        return _clip(str(payload.get("reason", payload.get("decision", ""))))
    if kind == "llm_thought":
        return _clip(str(payload.get("text", "")))
    return _clip(str(payload))


def _clip(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tui_events.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kyagent/tui/__init__.py kyagent/tui/events.py tests/test_tui_events.py
git commit -m "feat: add tui event summary helpers"
```

---

### Task 3: Rich Rendering Layer

**Files:**
- Create: `kyagent/tui/render.py`
- Test: `tests/test_tui_render.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tui_render.py`:

```python
from __future__ import annotations

from rich.console import Console

from kyagent.confirm import ConfirmRequest
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.mcp.tools.process import ProcessListTool
from kyagent.tui.render import build_tools_table, confirm_panel, replay_table


def _render(renderable) -> str:
    console = Console(record=True, width=100, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_confirm_panel_contains_risk_body_and_rules():
    req = ConfirmRequest(
        title="tool svc_restart",
        risk="high",
        summary_lines=["service_restart: high risk"],
        body="systemctl restart nginx",
    )

    output = _render(confirm_panel(req))

    assert "tool svc_restart" in output
    assert "high" in output
    assert "systemctl restart nginx" in output
    assert "service_restart: high risk" in output


def test_tools_table_uses_registry_metadata():
    registry = ToolRegistry()
    registry.register(ProcessListTool())

    output = _render(build_tools_table(registry))

    assert "process_list" in output
    assert "read-only" in output


def test_replay_table_renders_rows():
    events = [
        {"seq": 1, "kind": "user_input", "ts": 1.0, "payload": {"text": "查 CPU"}},
        {"seq": 2, "kind": "agent_reply", "ts": 2.0, "payload": {"text": "ok"}},
    ]

    output = _render(replay_table(events))

    assert "用户输入" in output
    assert "最终回复" in output
    assert "查 CPU" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui_render.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'kyagent.tui.render'`.

- [ ] **Step 3: Implement Rich renderers**

Create `kyagent/tui/render.py`:

```python
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from kyagent.confirm import ConfirmRequest
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.tui.events import timeline_rows


def confirm_panel(req: ConfirmRequest) -> Panel:
    lines = [f"风险等级: {req.risk}"]
    if req.body:
        lines.append(f"详情: {req.body}")
    if req.summary_lines:
        lines.append("命中规则:")
        lines.extend(f"  - {line}" for line in req.summary_lines)
    return Panel("\n".join(lines), title=req.title, border_style="yellow")


def build_tools_table(registry: ToolRegistry) -> Table:
    table = Table(title="可用工具", show_lines=False)
    table.add_column("name", style="cyan")
    table.add_column("risk")
    table.add_column("mode")
    table.add_column("root")
    table.add_column("description")
    for tool in registry.all():
        table.add_row(
            tool.name,
            tool.risk_level.value,
            "read-only" if tool.read_only else "write",
            "yes" if tool.requires_root else "no",
            tool.description.splitlines()[0],
        )
    return table


def replay_table(events: list[dict]) -> Table:
    table = Table(title="审计回放", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("event")
    table.add_column("badge")
    table.add_column("summary")
    for row in timeline_rows(events):
        table.add_row(str(row.seq), row.label, row.badge, row.summary)
    return table


def command_help_panel() -> Panel:
    return Panel(
        "\n".join([
            "/help        显示命令",
            "/tools       查看工具",
            "/audit       回放上一轮 trace",
            "/audit <id>  回放指定 trace",
            "/reset       清空对话上下文",
            "/exit        退出",
        ]),
        title="TUI 命令",
        border_style="cyan",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tui_render.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kyagent/tui/render.py tests/test_tui_render.py
git commit -m "feat: add rich renderers for tui"
```

---

### Task 4: TUI Shell Command Loop

**Files:**
- Create: `kyagent/tui/shell.py`
- Test: `tests/test_tui_shell.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tui_shell.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console

from kyagent.agent.core import AgentRunResult
from kyagent.audit.trace import Trace
from kyagent.confirm import ConfirmRequest
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.mcp.tools.process import ProcessListTool
from kyagent.tui.shell import KyagentTuiShell


class FakeSession:
    def __init__(self, inputs: list[str]):
        self.inputs = list(inputs)

    def prompt(self, _message: str) -> str:
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)


@dataclass
class FakeAgent:
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    messages: list[dict] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.registry.register(ProcessListTool())

    def ask(self, text: str, user: str = "anonymous") -> AgentRunResult:
        self.asked.append(text)
        trace = Trace(user=user)
        trace.metadata["backend"] = "mock"
        trace.add("user_input", {"text": text})  # type: ignore[arg-type]
        trace.add("agent_reply", {"text": "answer"})  # type: ignore[arg-type]
        return AgentRunResult(trace=trace, final_text="answer", tool_iterations=1)


def _console() -> Console:
    return Console(record=True, width=120, color_system=None)


def test_shell_runs_turn_and_remembers_last_trace():
    agent = FakeAgent()
    console = _console()
    shell = KyagentTuiShell(agent=agent, console=console, session=FakeSession(["查 CPU", "/exit"]), user="tester")

    shell.run()

    output = console.export_text()
    assert agent.asked == ["查 CPU"]
    assert "answer" in output
    assert shell.last_trace_id is not None


def test_shell_tools_command_uses_registry():
    agent = FakeAgent()
    console = _console()
    shell = KyagentTuiShell(agent=agent, console=console, session=FakeSession(["/tools", "/exit"]), user="tester")

    shell.run()

    assert "process_list" in console.export_text()


def test_shell_reset_clears_agent_messages():
    agent = FakeAgent()
    agent.messages.append({"role": "user", "content": "old"})
    console = _console()
    shell = KyagentTuiShell(agent=agent, console=console, session=FakeSession(["/reset", "/exit"]), user="tester")

    shell.run()

    assert agent.messages == []
    assert "上下文已清空" in console.export_text()


def test_confirm_callback_approves_only_yes():
    console = _console()
    shell = KyagentTuiShell(agent=FakeAgent(), console=console, session=FakeSession(["y"]), user="tester")

    approved = shell.confirm(ConfirmRequest(title="tool", risk="high", body="cmd"))

    assert approved is True
    assert "tool" in console.export_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui_shell.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'kyagent.tui.shell'`.

- [ ] **Step 3: Implement the shell**

Create `kyagent/tui/shell.py`:

```python
from __future__ import annotations

from typing import Protocol

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.panel import Panel
from rich.status import Status

from kyagent.agent.core import Agent
from kyagent.audit.store import AuditStore
from kyagent.config import Config, load_config
from kyagent.confirm import ConfirmRequest
from kyagent.tui.render import build_tools_table, command_help_panel, confirm_panel, replay_table


class PromptLike(Protocol):
    def prompt(self, message: str) -> str:
        ...


class KyagentTuiShell:
    def __init__(
        self,
        agent: Agent,
        console: Console,
        session: PromptLike,
        user: str,
        audit_store: AuditStore | None = None,
    ):
        self.agent = agent
        self.console = console
        self.session = session
        self.user = user
        self.audit_store = audit_store
        self.last_trace_id: str | None = None

    def run(self) -> None:
        self.console.print(command_help_panel())
        while True:
            try:
                text = self.session.prompt("你 > ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("[dim]再见[/]")
                return
            text = text.strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                return
            if self._handle_command(text):
                continue
            with self.console.status("kyagent 正在处理...", spinner="dots"):
                result = self.agent.ask(text, user=self.user)
            self.last_trace_id = result.trace.trace_id
            self.console.print(Panel(
                result.final_text or "(空)",
                title=f"trace={result.trace.trace_id[:12]} iter={result.tool_iterations}",
                border_style="blue",
            ))
            if result.notes:
                self.console.print("[dim]" + " | ".join(result.notes) + "[/]")

    def confirm(self, req: ConfirmRequest) -> bool:
        self.console.print(confirm_panel(req))
        try:
            answer = self.session.prompt("放行? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}

    def _handle_command(self, text: str) -> bool:
        if text == "/help":
            self.console.print(command_help_panel())
            return True
        if text == "/tools":
            self.console.print(build_tools_table(self.agent.registry))
            return True
        if text == "/reset":
            self.agent.messages.clear()
            self.console.print("[dim]上下文已清空[/]")
            return True
        if text == "/audit" or text.startswith("/audit "):
            trace_id = text.split(maxsplit=1)[1] if " " in text else self.last_trace_id
            self._show_audit(trace_id)
            return True
        return False

    def _show_audit(self, trace_id: str | None) -> None:
        if not trace_id:
            self.console.print("[dim]还没有 trace[/]")
            return
        if self.audit_store is None:
            self.console.print("[yellow]当前 shell 未配置 AuditStore，无法回放[/]")
            return
        events = self.audit_store.get_events(trace_id)
        if not events:
            self.console.print(f"[red]找不到 trace {trace_id}[/]")
            return
        self.console.print(replay_table(events))


def run_tui(config: str | None = None, user: str = "tui") -> None:
    cfg: Config = load_config(config)
    console = Console()
    session = PromptSession()
    holder: dict[str, KyagentTuiShell] = {}

    def confirm(req: ConfirmRequest) -> bool:
        return holder["shell"].confirm(req)

    agent = Agent.from_config(cfg, confirm=confirm)
    store = AuditStore(cfg.resolve(cfg.audit.database))
    shell = KyagentTuiShell(agent=agent, console=console, session=session, user=user, audit_store=store)
    holder["shell"] = shell
    shell.run()
```

- [ ] **Step 4: Fix the test double enum mismatch**

If `Trace.add("user_input", ...)` errors because the type is a string, change the test to import and use `EventKind.USER_INPUT` and `EventKind.AGENT_REPLY`:

```python
from kyagent.audit.trace import EventKind, Trace

trace.add(EventKind.USER_INPUT, {"text": text})
trace.add(EventKind.AGENT_REPLY, {"text": "answer"})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tui_shell.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add kyagent/tui/shell.py tests/test_tui_shell.py
git commit -m "feat: add prompt toolkit tui shell"
```

---

### Task 5: CLI Wiring

**Files:**
- Modify: `kyagent/cli.py`
- Test: `tests/test_cli_tui.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_tui.py`:

```python
from __future__ import annotations

from typer.testing import CliRunner

from kyagent.cli import app


def test_tui_command_delegates_to_shell(monkeypatch):
    called = {}

    def fake_run_tui(config=None, user="tui"):
        called["config"] = config
        called["user"] = user

    monkeypatch.setattr("kyagent.tui.shell.run_tui", fake_run_tui)

    result = CliRunner().invoke(app, ["tui", "--config", "configs/default.yaml", "--user", "demo"])

    assert result.exit_code == 0
    assert called == {"config": "configs/default.yaml", "user": "demo"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_tui.py -v`

Expected: FAIL because the `tui` command does not exist.

- [ ] **Step 3: Add Typer command**

In `kyagent/cli.py`, add this command near the existing `chat` command:

```python
@app.command()
def tui(
    config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    user: str = typer.Option("tui", "--user", "-u"),
):
    """启动 prompt_toolkit + Rich TUI demo。"""
    from kyagent.tui.shell import run_tui

    run_tui(config=config, user=user)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_tui.py -v`

Expected: PASS.

- [ ] **Step 5: Run existing CLI smoke tests**

Run: `python -m pytest tests/test_integration.py tests/test_audit.py tests/test_cli_tui.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add kyagent/cli.py tests/test_cli_tui.py
git commit -m "feat: wire tui command"
```

---

### Task 6: Audit Replay From Persisted Store

**Files:**
- Modify: `tests/test_tui_shell.py`
- Modify: `kyagent/tui/shell.py`

- [ ] **Step 1: Write the failing replay test**

Append to `tests/test_tui_shell.py`:

```python
from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind


def test_shell_audit_command_replays_last_trace(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    logger = AuditLogger(store)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "查 80 端口"})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "nginx"})
    logger.close(trace)

    agent = FakeAgent()
    console = _console()
    shell = KyagentTuiShell(
        agent=agent,
        console=console,
        session=FakeSession(["/audit", "/exit"]),
        user="tester",
        audit_store=store,
    )
    shell.last_trace_id = trace.trace_id

    shell.run()

    output = console.export_text()
    assert "审计回放" in output
    assert "查 80 端口" in output
    assert "nginx" in output
```

- [ ] **Step 2: Run test to verify it fails if replay is incomplete**

Run: `python -m pytest tests/test_tui_shell.py::test_shell_audit_command_replays_last_trace -v`

Expected: FAIL if `/audit` does not read `AuditStore` and render persisted events.

- [ ] **Step 3: Complete replay behavior**

Ensure `KyagentTuiShell._show_audit()` uses exactly this behavior:

```python
def _show_audit(self, trace_id: str | None) -> None:
    if not trace_id:
        self.console.print("[dim]还没有 trace[/]")
        return
    if self.audit_store is None:
        self.console.print("[yellow]当前 shell 未配置 AuditStore，无法回放[/]")
        return
    events = self.audit_store.get_events(trace_id)
    if not events:
        self.console.print(f"[red]找不到 trace {trace_id}[/]")
        return
    self.console.print(replay_table(events))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tui_shell.py::test_shell_audit_command_replays_last_trace -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kyagent/tui/shell.py tests/test_tui_shell.py
git commit -m "feat: replay audit traces in tui"
```

---

### Task 7: Confirmation Integration Contract

**Files:**
- Modify: `tests/test_tui_shell.py`
- Modify: `kyagent/tui/shell.py`

- [ ] **Step 1: Write the failing denial test**

Append to `tests/test_tui_shell.py`:

```python
def test_confirm_callback_denies_empty_no_and_interrupt():
    no_shell = KyagentTuiShell(agent=FakeAgent(), console=_console(), session=FakeSession(["n"]), user="tester")
    empty_shell = KyagentTuiShell(agent=FakeAgent(), console=_console(), session=FakeSession([""]), user="tester")
    interrupted_shell = KyagentTuiShell(agent=FakeAgent(), console=_console(), session=FakeSession([]), user="tester")

    req = ConfirmRequest(title="tool", risk="critical", body="rm -rf /")

    assert no_shell.confirm(req) is False
    assert empty_shell.confirm(req) is False
    assert interrupted_shell.confirm(req) is False
```

- [ ] **Step 2: Run test to verify it fails if confirm is permissive**

Run: `python -m pytest tests/test_tui_shell.py::test_confirm_callback_denies_empty_no_and_interrupt -v`

Expected: FAIL if anything other than explicit `y`/`yes` is approved.

- [ ] **Step 3: Keep confirmation conservative**

Ensure `KyagentTuiShell.confirm()` is:

```python
def confirm(self, req: ConfirmRequest) -> bool:
    self.console.print(confirm_panel(req))
    try:
        answer = self.session.prompt("放行? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tui_shell.py::test_confirm_callback_denies_empty_no_and_interrupt -v`

Expected: PASS.

- [ ] **Step 5: Run integration test for high-risk denial**

Run: `python -m pytest tests/test_integration.py::test_high_risk_tool_denied_in_oneshot tests/test_tui_shell.py -v`

Expected: PASS. This confirms the existing agent safety path remains intact while the TUI confirm contract is conservative.

- [ ] **Step 6: Commit**

```bash
git add kyagent/tui/shell.py tests/test_tui_shell.py
git commit -m "test: lock tui confirmation contract"
```

---

### Task 8: Documentation For Demo And LoongArch

**Files:**
- Modify: `docs/kyagent/README.md`
- Modify: `docs/deployment/loongarch.md`
- Test: `tests/test_loongarch_deploy_docs.py`

- [ ] **Step 1: Write the failing docs test**

Append to `tests/test_loongarch_deploy_docs.py`:

```python
def test_tui_demo_documented_for_loongarch():
    readme = Path("docs/kyagent/README.md").read_text(encoding="utf-8")
    deploy = Path("docs/deployment/loongarch.md").read_text(encoding="utf-8")

    assert "kyagent tui" in readme
    assert "prompt_toolkit" in readme
    assert "kyagent tui" in deploy
    assert "prompt_toolkit" in deploy
    assert "Textual" not in deploy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_loongarch_deploy_docs.py::test_tui_demo_documented_for_loongarch -v`

Expected: FAIL because the TUI demo is not documented yet.

- [ ] **Step 3: Document the demo command in README**

Add this section to `docs/kyagent/README.md`:

```markdown
## TUI Demo

`kyagent tui` starts the lightweight prompt_toolkit + Rich demo shell. It keeps multi-turn context in the existing `Agent`, renders tool inventory and safety confirmation panels, and replays persisted audit traces from the configured SQLite audit database.

Useful commands inside the shell:

```text
/tools       show the registered ToolRegistry tools
/audit       replay the latest trace
/audit <id>  replay a specific trace from AuditStore
/reset       clear the current Agent message context
/exit        leave the shell
```

The TUI channel uses `Agent.from_config(confirm=...)`; it does not bypass `ConfirmRequest`, guardrails, execution proxy, or audit logging.
```

- [ ] **Step 4: Document LoongArch note**

Add this section to `docs/deployment/loongarch.md`:

```markdown
## TUI Demo On LoongArch

The TUI demo command is:

```bash
kyagent tui --config configs/default.yaml
```

The UI dependency is `prompt_toolkit>=3.0,<4` plus the existing `rich` dependency. Both are pure Python on the default install path. Do not add Textual or other UI frameworks that expand the LoongArch dependency surface for this demo.

For offline or contest demos, use the `mock` backend or an HTTPX-compatible backend such as `deepseek_httpx`, `qwen_httpx`, or `openai_httpx`. Keep Anthropic/OpenAI SDK extras optional because those paths can pull Rust-backed transitive dependencies that are unsuitable for LoongArch Old World by default.
```

- [ ] **Step 5: Run docs test to verify it passes**

Run: `python -m pytest tests/test_loongarch_deploy_docs.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/kyagent/README.md docs/deployment/loongarch.md tests/test_loongarch_deploy_docs.py
git commit -m "docs: document tui demo for loongarch"
```

---

### Task 9: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
python -m pytest tests/test_tui_events.py tests/test_tui_render.py tests/test_tui_shell.py tests/test_cli_tui.py -v
```

Expected: PASS.

- [ ] **Step 2: Run regression suite around reused boundaries**

Run:

```bash
python -m pytest tests/test_integration.py tests/test_audit.py tests/test_safety.py tests/test_executor.py tests/test_mcp.py tests/test_mcp_protocol.py -v
```

Expected: PASS.

- [ ] **Step 3: Run LoongArch documentation/dependency checks**

Run:

```bash
python -m pytest tests/test_loongarch_deploy_docs.py -v
```

Expected: PASS.

- [ ] **Step 4: Manual smoke test with mock backend**

Run:

```bash
kyagent tui --config configs/default.yaml --user demo
```

Inside the shell, enter:

```text
/tools
查下 CPU 占用最高的进程
/audit
/reset
/exit
```

Expected:
- `/tools` renders registered tools from `ToolRegistry`.
- The natural language query runs through `Agent.ask()`.
- High-risk requests show a `ConfirmRequest` panel and deny unless the user types `y` or `yes`.
- `/audit` renders the last persisted trace from `AuditStore`.
- `/reset` clears `agent.messages`.

- [ ] **Step 5: Final commit**

```bash
git status --short
git add kyagent/tui tests pyproject.toml requirements.txt requirements-loongarch.txt kyagent/cli.py docs/kyagent/README.md docs/deployment/loongarch.md
git commit -m "feat: add lightweight tui demo"
```

---

## Self-Review Checklist

- Spec coverage: continuous interaction is covered by `KyagentTuiShell.run`; visualization by `kyagent.tui.render`; confirmation by `KyagentTuiShell.confirm` using `ConfirmRequest`; replay by `/audit` using `AuditStore`; LoongArch by dependency and deployment tests.
- Boundary coverage: `Agent.from_config(confirm=...)`, `ConfirmRequest`, `AuditStore`, and `ToolRegistry` are reused directly. No task edits `ExecutionProxy`, `Guardrail`, `IntentGuard`, or `kyagent.mcp.tools.pipeline`.
- Placeholder scan: every test, command, and implementation step above includes concrete paths and expected results.
- Type consistency: `TraceSummary`, `TimelineRow`, `KyagentTuiShell`, and `run_tui` are introduced before use; test imports match the files that define them.

