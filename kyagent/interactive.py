"""User-choice plumbing for agent-initiated interactive selection.

When the LLM wants to ask the user "do you want A or B?", it calls the
``ask_user_choice`` tool. The tool is intercepted in Agent._handle_tool_use
(NOT executed via ExecutionProxy) and routed to a ``UserChoiceFn`` callback
that the UI provides. The callback returns the chosen value, which becomes
the tool's result fed back into the next LLM round.

This module defines only types — the tool itself lives in
``kyagent.mcp.tools.interactive`` and the Agent integration in
``kyagent.agent.core``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class UserChoiceOption:
    """One option presented to the user."""

    label: str  # human-readable text shown in the UI
    value: str  # opaque value fed back to the LLM
    description: str = ""  # optional secondary line


@dataclass
class UserChoice:
    """Payload for an agent-initiated user-choice request."""

    question: str
    options: list[UserChoiceOption] = field(default_factory=list)


UserChoiceFn = Callable[[UserChoice], str]
"""Synchronous callback: takes a UserChoice, returns the chosen ``value``.

Must always return a string. If the user cancels or the UI can't prompt,
return an empty string — Agent will surface that to the LLM as "no answer".
"""


def auto_cancel_choice(choice: UserChoice) -> str:
    """Default no-op: refuses to pick. Used when no UI is wired."""
    return ""
