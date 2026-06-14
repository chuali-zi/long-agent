from __future__ import annotations

from kyagent.agent.llm import AssistantMessage, ToolUseBlock
from kyagent.agent.planning_contract import (
    PLAN_CONTRACT_FIELD,
    choose_plan_candidate,
    strip_plan_contract_from_assistant,
    wrap_tools_with_plan_contract,
)


def test_wrap_tools_requires_structured_plan_field():
    wrapped = wrap_tools_with_plan_contract([
        {
            "name": "process_list",
            "description": "List processes",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": ["limit"],
                "additionalProperties": False,
            },
        }
    ])

    schema = wrapped[0]["input_schema"]
    assert PLAN_CONTRACT_FIELD in schema["properties"]
    assert schema["required"] == ["limit", PLAN_CONTRACT_FIELD]
    assert schema["additionalProperties"] is False


def test_structured_plan_is_extracted_and_stripped_from_tool_args():
    assistant = AssistantMessage(blocks=[
        ToolUseBlock(
            id="call-1",
            name="process_list",
            input={
                "limit": 3,
                PLAN_CONTRACT_FIELD: {"items": ["Inspect processes", "Report result"]},
            },
        )
    ])

    candidate = choose_plan_candidate(
        texts=assistant.texts(),
        tool_uses=assistant.tool_uses(),
        tool_lookup=lambda name: None,
    )
    assert candidate is not None
    assert candidate.source == "tool_contract"
    assert [todo.content for todo in candidate.todos] == ["Inspect processes", "Report result"]

    stripped = strip_plan_contract_from_assistant(assistant)
    assert stripped.tool_uses()[0].input == {"limit": 3}


def test_text_todo_beats_structured_plan_for_compatibility():
    candidate = choose_plan_candidate(
        texts=["TODO 1: Use the text plan."],
        tool_uses=[
            ToolUseBlock(
                id="call-1",
                name="process_list",
                input={PLAN_CONTRACT_FIELD: {"items": ["Use the structured plan"]}},
            )
        ],
        tool_lookup=lambda name: None,
    )

    assert candidate is not None
    assert candidate.source == "text_todo"
    assert [todo.content for todo in candidate.todos] == ["Use the text plan."]


def test_missing_text_and_contract_is_visible_legacy_inference():
    class _Tool:
        read_only = True

    candidate = choose_plan_candidate(
        texts=[],
        tool_uses=[ToolUseBlock(id="call-1", name="process_list", input={})],
        tool_lookup=lambda name: _Tool(),
    )

    assert candidate is not None
    assert candidate.event == "plan_legacy_inferred"
    assert candidate.source == "legacy_inferred"
    assert candidate.todos[0].content.startswith("调用只读工具 process_list")


def test_missing_text_and_contract_can_disable_legacy_for_retry_gate():
    candidate = choose_plan_candidate(
        texts=[],
        tool_uses=[ToolUseBlock(id="call-1", name="process_list", input={})],
        tool_lookup=lambda name: None,
        allow_legacy=False,
    )

    assert candidate is None
