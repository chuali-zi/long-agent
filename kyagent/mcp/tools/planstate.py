"""Read-only durable plan state tools."""
from __future__ import annotations

import re

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


class PlanListTool(Tool):
    name = "plan_list"
    description = "List recent durable task plans."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        return [
            "python", "-m", "kyagent.plan_cli",
            "list", "--limit", str(int(args.get("limit", 20))),
        ]


class PlanGetTool(Tool):
    name = "plan_get"
    description = "Read one durable task plan by plan_id."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "minLength": 6, "maxLength": 64},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        plan_id = args["plan_id"]
        if not re.fullmatch(r"plan-[A-Za-z0-9_-]{1,48}", plan_id):
            raise ToolError("invalid plan_id")
        return ["python", "-m", "kyagent.plan_cli", "get", plan_id]


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(PlanListTool())
    registry.register(PlanGetTool())
    return registry
