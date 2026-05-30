"""Agent-initiated interactive tools.

This module hosts tools that, instead of running a subprocess, hand control
back to the user via a UI callback. The flagship tool is ``ask_user_choice``:
the LLM asks "do you want A or B?" and the chosen value is fed back as the
tool result of the next round.

These tools are NOT routed through ``ExecutionProxy``. ``Agent._handle_tool_use_inner``
special-cases them by name and never calls ``prepare_call`` / ``check_safety``
/ ``execute_and_format``. ``build_argv`` therefore returns a harmless placeholder
that is never consumed.
"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


class AskUserChoiceTool(Tool):
    """Ask the user to pick one of N predefined options.

    Use this tool when the agent reached a decision point with a small,
    closed set of choices (e.g. "restart now vs reload config vs cancel").
    Do NOT use it for open-ended questions — there is no free-form input
    affordance and the LLM must enumerate every acceptable answer.
    """

    name = "ask_user_choice"
    description = (
        "向用户提一个二选一/多选一问题，等待用户从给定选项中挑一个。\n"
        "用于：执行前需要用户在几个明确分支中做选择（例：是否继续 / 重启或仅 reload / 选目标主机）。\n"
        "不要用于开放式问题：本工具没有自由输入框，用户只能选 options 里出现过的 value。\n"
        "options 至少 2 个；每个 option 含 value（喂回 LLM 的不透明 token）与 label（给人看的文本）。"
    )
    input_schema = {
        "type": "object",
        "required": ["question", "options"],
        "properties": {
            "question": {
                "type": "string",
                "description": "Question presented to the user",
            },
            "options": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "required": ["value", "label"],
                    "properties": {
                        "value": {
                            "type": "string",
                            "description": "Opaque token returned to the LLM",
                        },
                        "label": {
                            "type": "string",
                            "description": "Human-readable choice text",
                        },
                        "description": {"type": "string"},
                    },
                },
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        # 占位：Agent._handle_tool_use_inner 会按 name 特判并直接返回，
        # 永远不会落到 ExecutionProxy。返回非空 argv 仅为防御性兼容。
        return ["true"]


def register(registry: ToolRegistry) -> None:
    registry.register(AskUserChoiceTool())
