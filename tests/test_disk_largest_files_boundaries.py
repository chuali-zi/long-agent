from __future__ import annotations

import pytest

from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.disk import DirLargestFilesTool, DiskSmartTool


def _exec_result(stdout: str) -> ExecutionResult:
    return ExecutionResult(
        argv=[],
        returncode=0,
        stdout=stdout,
        stderr="",
        truncated=False,
        duration=0.0,
    )


def test_dir_largest_files_allows_reasonable_absolute_path() -> None:
    tool = DirLargestFilesTool()

    cleaned = tool.validate({"path": "/var/lib/app-1/data_files"})

    assert tool.build_argv(cleaned)[:2] == ["find", "/var/lib/app-1/data_files"]


@pytest.mark.parametrize(
    "path",
    [
        "/var/log;rm",
        "/var/log|cat",
        "/var/log$(id)",
        "/var/log`id`",
        "/var/log\n/etc",
    ],
)
def test_dir_largest_files_rejects_path_metacharacters(path: str) -> None:
    tool = DirLargestFilesTool()

    with pytest.raises(ToolError):
        tool.validate({"path": path})


def test_dir_largest_files_limit_controls_formatted_output() -> None:
    tool = DirLargestFilesTool()
    cleaned = tool.validate({"path": "/var/log", "limit": 2})

    result = tool.format_result(
        ExecutionResult(
            argv=[],
            returncode=0,
            stdout="10\t/small\n30\t/large\n20\t/medium\n",
            stderr="",
            truncated=False,
            duration=0.0,
            extra={"tool_args": cleaned},
        )
    )

    assert result.content.splitlines() == ["30\t/large", "20\t/medium"]
    assert result.data["row_count"] == 2


def test_dir_largest_files_limit_does_not_leak_between_calls() -> None:
    tool = DirLargestFilesTool()
    first_args = tool.validate({"path": "/var/log", "limit": 2})
    tool.validate({"path": "/var/log", "limit": 1})

    result = tool.format_result(
        ExecutionResult(
            argv=[],
            returncode=0,
            stdout="30\t/large\n20\t/medium\n10\t/small\n",
            stderr="",
            truncated=False,
            duration=0.0,
            extra={"tool_args": first_args},
        )
    )

    assert result.content.splitlines() == ["30\t/large", "20\t/medium"]


def test_disk_smart_allows_nvme_device_granted_by_sudoers() -> None:
    tool = DiskSmartTool()

    cleaned = tool.validate({"device": "/dev/nvme0n1"})

    assert tool.build_argv(cleaned) == ["smartctl", "-H", "-A", "--", "/dev/nvme0n1"]
