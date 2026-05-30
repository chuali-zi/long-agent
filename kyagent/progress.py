"""Streaming progress events emitted by Agent and consumed by interactive UIs.

The Agent main loop calls ``on_progress(ProgressEvent(...))`` at well-defined
seams (LLM call start/end, tool call start/end, final answer, error). UIs that
want a live display (TUI) subscribe; UIs that don't care (`ask`, `chat`) leave
the callback as ``None`` and incur zero overhead.

The schema is intentionally narrow and stable so it can be serialized to JSON
later (MCP streaming, web UI bridge) without churn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal


ProgressKind = Literal[
    "agent_start",       # Agent.ask() entered; payload: text=user_input
    "thinking_start",    # LLM round-trip begins; payload: meta={"iteration": N}
    "thinking_end",      # LLM round-trip ends; payload: text=preview, meta={"tool_calls": [...]}
    "tool_call_start",   # About to invoke tool; payload: tool, argv
    "tool_call_end",     # Tool returned; payload: tool, text=truncated_output, meta={"ok": bool}
    "agent_final",       # Final answer ready; payload: text=final_text
    "error",             # Any unrecoverable error; payload: text=detail, meta={"reason": code}
]


@dataclass
class ProgressEvent:
    """A single live update from the Agent loop.

    All fields default to safe empty values so emitters can fill only what's
    meaningful for the kind. Consumers should treat unknown kinds as no-op to
    stay forward-compatible.
    """

    kind: ProgressKind
    text: str = ""
    tool: str | None = None
    argv: list[str] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]
"""Sink for ProgressEvent. Must not raise; Agent wraps calls defensively."""


def noop_progress(event: ProgressEvent) -> None:
    """Drop-in default that ignores every event."""
    return None
