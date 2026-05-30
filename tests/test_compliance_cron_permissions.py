"""Permission-boundary regressions for compliance crontab inspection."""
from __future__ import annotations

import pytest

from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.compliance import ComplCronDumpTool, ComplUserCronDumpTool
from kyagent.mcp.tools.pipeline import execute_and_format, prepare_call


class _AuditStub:
    def event(self, trace, kind, payload):  # noqa: ANN001, ARG002
        pass


class _ExecutorSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def run(self, argv, *, requires_root=False, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append((argv, requires_root))
        return ExecutionResult(
            argv=argv,
            returncode=0,
            stdout="",
            stderr="",
            truncated=False,
            duration=0.0,
        )


def _execute(tool, args):  # noqa: ANN001
    audit = _AuditStub()
    prepared = prepare_call(tool, args, trace=object(), audit=audit)
    assert not hasattr(prepared, "reason"), prepared
    executor = _ExecutorSpy()
    execute_and_format(prepared, trace=object(), audit=audit, executor=executor)
    return executor.calls


def test_registered_system_cron_dump_keeps_minimal_privilege():
    tool = default_registry().get("compl_cron_dump")
    assert tool is not None
    assert isinstance(tool, ComplCronDumpTool)
    assert tool.requires_root is False
    assert tool.build_argv(tool.validate({})) == ["cat", "/etc/crontab"]

    with pytest.raises(ToolError):
        tool.validate({"user": "root"})

    assert _execute(tool, {}) == [(["cat", "/etc/crontab"], False)]


def test_registered_user_cron_dump_is_statically_root_required():
    tool = default_registry().get("compl_user_cron_dump")
    assert tool is not None
    assert isinstance(tool, ComplUserCronDumpTool)
    assert tool.requires_root is True
    assert _execute(tool, {"user": "root"}) == [
        (["crontab", "-l", "-u", "root"], True),
    ]
