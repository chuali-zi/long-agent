"""Runtime plan contract for tool-using assistant turns.

The old Agent treated TODO plans as a text convention: if the assistant emitted
``TODO 1: ...`` in a text block before tool calls, the UI and durable plan store
could show useful work state. Real tool-calling APIs do not guarantee that text
exists, though; a valid response may be ``content: null`` with only
``tool_calls``. This module moves the plan requirement into the structured tool
schema that the model sees, while keeping text TODO parsing and a clearly marked
legacy inference path for scripted or older backends.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Callable

from kyagent.agent.llm import AssistantMessage, TextBlock, ToolUseBlock
from kyagent.planner import PlanTodoItem


PLAN_CONTRACT_FIELD = "kyagent_plan"
PLAN_SOURCE_TEXT = "text_todo"
PLAN_SOURCE_TOOL_CONTRACT = "tool_contract"
PLAN_SOURCE_LEGACY_INFERRED = "legacy_inferred"

PLAN_CONTRACT_SYSTEM_APPENDIX = """

## Runtime Plan Contract
When you call any tool, the tool arguments include a required `kyagent_plan`
object. Put the action plan there even if the provider returns `content: null`.
Use:
`"kyagent_plan": {"items": ["inspect current state", "act only on confirmed target"]}`.
Plain-text `TODO 1: ...` remains accepted, but structured `kyagent_plan.items`
is the primary contract for tool calls.
"""

_PLAN_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Required kyagent runtime plan for this assistant turn. It is consumed "
        "by the Agent before tool execution and stripped before the underlying "
        "OS tool runs."
    ),
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "description": "Ordered action plan for the tool calls in this assistant turn.",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string", "minLength": 3, "maxLength": 240},
        }
    },
}

_TODO_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:TODO|Todo|todo|PLAN|Plan|plan|任务|步骤)\s*"
    r"\d{1,2}\s*[:：.)、-]\s*(.+?)\s*$"
)
_CHECKBOX_TODO_RE = re.compile(r"^\s*[-*]\s*\[[ xX-]\]\s*(.+?)\s*$")


@dataclass(frozen=True)
class PlanCandidate:
    todos: list[PlanTodoItem]
    source: str
    event: str
    detail: str = ""


def wrap_tools_with_plan_contract(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return LLM-facing tool schemas with a required structured plan field."""
    wrapped: list[dict[str, Any]] = []
    for tool in tools:
        item = deepcopy(tool)
        schema = deepcopy(item.get("input_schema") or {})
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}, "additionalProperties": False}

        props = dict(schema.get("properties") or {})
        props[PLAN_CONTRACT_FIELD] = deepcopy(_PLAN_CONTRACT_SCHEMA)
        schema["properties"] = props

        required = list(schema.get("required") or [])
        if PLAN_CONTRACT_FIELD not in required:
            required.append(PLAN_CONTRACT_FIELD)
        schema["required"] = required

        description = str(item.get("description") or "")
        suffix = (
            "\n\nKyagent runtime plan contract: include required "
            f"`{PLAN_CONTRACT_FIELD}.items` before tool execution."
        )
        if "Kyagent runtime plan contract" not in description:
            item["description"] = description + suffix
        item["input_schema"] = schema
        wrapped.append(item)
    return wrapped


def strip_plan_contract_from_assistant(assistant: AssistantMessage) -> AssistantMessage:
    """Remove the Agent-only plan field before validation/execution/history replay."""
    blocks: list[TextBlock | ToolUseBlock] = []
    for block in assistant.blocks:
        if isinstance(block, ToolUseBlock):
            cleaned = dict(block.input or {})
            cleaned.pop(PLAN_CONTRACT_FIELD, None)
            blocks.append(ToolUseBlock(id=block.id, name=block.name, input=cleaned))
        else:
            blocks.append(block)
    return AssistantMessage(blocks=blocks, stop_reason=assistant.stop_reason, raw=assistant.raw)


def extract_text_todos(texts: list[str]) -> PlanCandidate | None:
    items: list[str] = []
    for text in texts:
        for line in text.splitlines():
            match = _TODO_LINE_RE.match(line) or _CHECKBOX_TODO_RE.match(line)
            if not match:
                continue
            content = match.group(1).strip()
            if content:
                items.append(content)
    todos = _items_to_todos(items)
    if not todos:
        return None
    return PlanCandidate(
        todos=todos,
        source=PLAN_SOURCE_TEXT,
        event="plan_declared",
        detail="assistant_text_todo",
    )


def extract_tool_contract_todos(tool_uses: list[ToolUseBlock]) -> PlanCandidate | None:
    for tool_use in tool_uses:
        raw = (tool_use.input or {}).get(PLAN_CONTRACT_FIELD)
        items = _coerce_plan_items(raw)
        todos = _items_to_todos(items)
        if todos:
            return PlanCandidate(
                todos=todos,
                source=PLAN_SOURCE_TOOL_CONTRACT,
                event="plan_declared",
                detail=f"tool_arg:{tool_use.name}",
            )
    return None


def infer_legacy_todos(
    tool_uses: list[ToolUseBlock],
    tool_lookup: Callable[[str], Any | None],
) -> PlanCandidate | None:
    """Last-resort compatibility for backends that cannot follow the contract.

    This path is intentionally visible in audit as ``plan_legacy_inferred``. It
    keeps non-interactive benches and scripted backends from hard-deadlocking,
    without pretending the LLM supplied an explicit plan.
    """
    items: list[tuple[str, str]] = []
    for tool_use in tool_uses[:12]:
        tool = tool_lookup(tool_use.name)
        if tool is not None and getattr(tool, "read_only", False):
            content = f"调用只读工具 {tool_use.name} 感知当前系统状态。"
            priority = "medium"
        elif tool is not None:
            content = f"在安全校验后调用变更工具 {tool_use.name} 处理已确认目标。"
            priority = "high"
        else:
            content = f"校验并调用工具 {tool_use.name}。"
            priority = "medium"
        items.append((content, priority))
    if items:
        items.append(("根据工具结果验证影响范围并返回结论。", "medium"))
    todos = [
        PlanTodoItem(
            todo_id=f"todo-{idx + 1}",
            content=content[:500],
            status="in_progress" if idx == 0 else "pending",
            priority=priority,  # type: ignore[arg-type]
        )
        for idx, (content, priority) in enumerate(items[:20])
    ]
    if not todos:
        return None
    return PlanCandidate(
        todos=todos,
        source=PLAN_SOURCE_LEGACY_INFERRED,
        event="plan_legacy_inferred",
        detail="tool_calls_without_text_or_structured_plan",
    )


def choose_plan_candidate(
    *,
    texts: list[str],
    tool_uses: list[ToolUseBlock],
    tool_lookup: Callable[[str], Any | None],
) -> PlanCandidate | None:
    return (
        extract_text_todos(texts)
        or extract_tool_contract_todos(tool_uses)
        or infer_legacy_todos(tool_uses, tool_lookup)
    )


def _coerce_plan_items(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw_items = raw.get("items") or raw.get("todos") or raw.get("steps")
    elif isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, str):
        raw_items = [line for line in raw.splitlines() if line.strip()]
    else:
        return []

    if not isinstance(raw_items, list):
        return []
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _items_to_todos(items: list[str]) -> list[PlanTodoItem]:
    todos: list[PlanTodoItem] = []
    for content in items[:20]:
        todos.append(
            PlanTodoItem(
                todo_id=f"todo-{len(todos) + 1}",
                content=content[:500],
                status="in_progress" if not todos else "pending",
                priority="high" if _looks_high_priority(content) else "medium",
            )
        )
    return todos


def _looks_high_priority(content: str) -> bool:
    lowered = content.lower()
    return any(
        token in lowered
        for token in (
            "high",
            "critical",
            "危险",
            "高危",
            "变更",
            "重启",
            "删除",
            "清空",
            "restart",
            "remove",
            "truncate",
            "kill",
        )
    )
