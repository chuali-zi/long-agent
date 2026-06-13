from __future__ import annotations

import pytest

from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.disk import DirLargestFilesTool, DiskOpenDeletedTool, DiskSmartTool


def _exec_result(stdout: str) -> ExecutionResult:
    return ExecutionResult(
        argv=[],
        returncode=0,
        stdout=stdout,
        stderr="",
        truncated=False,
        duration=0.0,
    )


def _disk_open_deleted_result(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
    run_as: str = "kyagent",
    sudo_used: bool = False,
    tool_args: dict | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        argv=["lsof", "-nP", "+L1"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=False,
        duration=0.0,
        run_as=run_as,
        sudo_used=sudo_used,
        extra={"tool_args": tool_args or {}},
    )


def test_dir_largest_files_rejects_global_storage_roots() -> None:
    tool = DirLargestFilesTool()

    for path in ("/var/log", "/var/cache", "/var/tmp", "/tmp"):
        with pytest.raises(ToolError, match="服务子目录"):
            tool.validate({"path": path})


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
    cleaned = tool.validate({"path": "/var/log/web-app01", "limit": 2})

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
    first_args = tool.validate({"path": "/var/log/web-app01", "limit": 2})
    tool.validate({"path": "/var/log/web-app01", "limit": 1})

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


def test_disk_open_deleted_pid_filters_lsof_query() -> None:
    tool = DiskOpenDeletedTool()

    cleaned = tool.validate({"pid": 4321})

    assert tool.requires_root is True
    assert tool.build_argv(cleaned) == ["lsof", "-nP", "+L1", "-p", "4321"]


def test_disk_open_deleted_rejects_port_like_pid_zero() -> None:
    tool = DiskOpenDeletedTool()

    with pytest.raises(ToolError):
        tool.validate({"pid": 0})


def test_disk_open_deleted_empty_non_root_result_has_actionable_notice() -> None:
    tool = DiskOpenDeletedTool()

    result = tool.format_result(_disk_open_deleted_result("", returncode=1, run_as="kyagent"))

    assert result.ok
    assert "non-root" in result.content
    assert result.data["permissions_may_hide_other_users"] is True
    assert result.data["open_deleted_count"] == 0
    assert result.data["filtered_count"] == 0


def test_disk_open_deleted_sudo_failure_has_actionable_notice() -> None:
    tool = DiskOpenDeletedTool()

    result = tool.format_result(
        _disk_open_deleted_result(
            "",
            returncode=1,
            stderr="sudo: a password is required",
            run_as="root",
            sudo_used=True,
        )
    )

    assert result.ok
    assert "could not get root/sudo visibility" in result.content
    assert result.data["permissions_may_hide_other_users"] is True
    assert "sudo" in result.data["lsof_stderr"]


def test_disk_open_deleted_runtime_root_and_cmdline_filter_output() -> None:
    tool = DiskOpenDeletedTool()
    stdout = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NLINK NODE NAME\n"
        "python 111 app 3u REG 8,1 1048576 0 10 /tmp/report-ops/export.log (deleted)\n"
        "java 222 app 4u REG 8,1 1048576 0 11 /tmp/billing-api/cache.log (deleted)\n"
    )

    result = tool.format_result(
        _disk_open_deleted_result(
            stdout,
            tool_args={"runtime_root": "/tmp/report-ops", "cmdline": "python"},
        )
    )

    assert result.ok
    assert "report-ops" in result.content
    assert "billing-api" not in result.content
    assert result.data["open_deleted_count"] == 2
    assert result.data["filtered_count"] == 1


def test_disk_open_deleted_filter_miss_explains_ports_are_not_pids() -> None:
    tool = DiskOpenDeletedTool()
    stdout = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NLINK NODE NAME\n"
        "python 111 app 3u REG 8,1 1048576 0 10 /tmp/report-ops/export.log (deleted)\n"
    )

    result = tool.format_result(
        _disk_open_deleted_result(stdout, tool_args={"runtime_root": "/tmp/missing"})
    )

    assert result.ok
    assert "ports are not PIDs" in result.content
    assert "permissions_may_hide_other_users" not in result.data
