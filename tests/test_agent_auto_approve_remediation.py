from __future__ import annotations

import os
import stat
from types import SimpleNamespace

from kyagent.agent.core import Agent
from kyagent.agent.llm import ToolUseBlock
from kyagent.audit.trace import Trace
from kyagent.mcp.tools.permissions import LogDirRepairPermissionsTool


def _stat_result(mode: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))


def test_auto_approve_dedicated_remediation_revalidates_tool_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        "kyagent.mcp.tools.permissions.os.lstat",
        lambda path: _stat_result(stat.S_IFDIR | 0o777),
    )
    agent = Agent.__new__(Agent)
    agent.auto_approve_safe_remediation = True
    tool = LogDirRepairPermissionsTool()
    cleaned = {"path": "/var/log/payroll-api", "mode": "0750"}
    prep = SimpleNamespace(tool=tool, cleaned=cleaned)
    tu = ToolUseBlock(id="tool-1", name=tool.name, input=cleaned)

    reason = Agent._safe_remediation_auto_approval_reason(agent, Trace(), tu, prep)

    assert reason == "dedicated remediation tool preflight passed"


def test_auto_approve_dedicated_remediation_refuses_failed_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        "kyagent.mcp.tools.permissions.os.lstat",
        lambda path: _stat_result(stat.S_IFDIR | 0o755),
    )
    agent = Agent.__new__(Agent)
    agent.auto_approve_safe_remediation = True
    tool = LogDirRepairPermissionsTool()
    cleaned = {"path": "/var/log/payroll-api", "mode": "0750"}
    prep = SimpleNamespace(tool=tool, cleaned=cleaned)
    tu = ToolUseBlock(id="tool-1", name=tool.name, input=cleaned)

    reason = Agent._safe_remediation_auto_approval_reason(agent, Trace(), tu, prep)

    assert reason == ""
