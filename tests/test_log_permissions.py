from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.permissions import LogDirRepairPermissionsTool

ROOT = Path(__file__).parents[1]
LOG_DIR_PERMS_WRAPPER = ROOT / "scripts" / "kyagent-log-dir-perms"


def _stat_result(mode: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))


def _patch_lstat(monkeypatch: pytest.MonkeyPatch, mode: int) -> None:
    monkeypatch.setattr(
        "kyagent.mcp.tools.permissions.os.lstat",
        lambda path: _stat_result(stat.S_IFDIR | mode),
    )


def test_log_dir_repair_permissions_registered() -> None:
    tool = default_registry().get("log_dir_repair_permissions")
    assert isinstance(tool, LogDirRepairPermissionsTool)
    assert tool.requires_root is True
    assert tool.read_only is False


def test_log_dir_repair_permissions_allows_service_dir_to_0750(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o777)
    tool = LogDirRepairPermissionsTool()

    args = tool.validate({"path": "/var/log/payroll-api", "mode": "0750"})

    assert args == {"path": "/var/log/payroll-api", "mode": "0750"}
    assert tool.build_argv(args) == [
        "kyagent-log-dir-perms",
        "/var/log/payroll-api",
        "0750",
    ]


def test_log_dir_repair_permissions_defaults_to_0750_for_one_child(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o775)
    tool = LogDirRepairPermissionsTool()

    args = tool.validate({"path": "/var/log/payroll-api/app"})

    assert args == {"path": "/var/log/payroll-api/app", "mode": "0750"}
    assert tool.build_argv(args) == [
        "kyagent-log-dir-perms",
        "/var/log/payroll-api/app",
        "0750",
    ]


def test_log_dir_repair_permissions_allows_0755_when_it_does_not_add_write(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o777)
    tool = LogDirRepairPermissionsTool()

    args = tool.validate({"path": "/var/log/payroll-api/app", "mode": "0755"})

    assert tool.build_argv(args) == [
        "kyagent-log-dir-perms",
        "/var/log/payroll-api/app",
        "0755",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/var/log",
        "/var/log/payroll-api/app/deeper",
        "/etc/log/payroll-api",
        "var/log/payroll-api",
        "/var/log/payroll-api;id",
        "/var/log/payroll-api/../other",
    ],
)
def test_log_dir_repair_permissions_rejects_out_of_scope_paths(monkeypatch, path) -> None:
    _patch_lstat(monkeypatch, 0o777)
    with pytest.raises(ToolError):
        LogDirRepairPermissionsTool().validate({"path": path, "mode": "0750"})


def test_log_dir_repair_permissions_rejects_non_directory(monkeypatch) -> None:
    monkeypatch.setattr(
        "kyagent.mcp.tools.permissions.os.lstat",
        lambda path: _stat_result(stat.S_IFREG | 0o666),
    )
    with pytest.raises(ToolError, match="目录"):
        LogDirRepairPermissionsTool().validate(
            {"path": "/var/log/payroll-api", "mode": "0750"}
        )


def test_log_dir_repair_permissions_requires_group_or_world_writable(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o755)
    with pytest.raises(ToolError, match="无需权限修复"):
        LogDirRepairPermissionsTool().validate(
            {"path": "/var/log/payroll-api", "mode": "0750"}
        )


def test_log_dir_repair_permissions_rejects_mode_that_adds_owner_write(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o077)
    with pytest.raises(ToolError, match="不能增加任何写权限"):
        LogDirRepairPermissionsTool().validate(
            {"path": "/var/log/payroll-api", "mode": "0755"}
        )


def test_log_dir_repair_permissions_rejects_unlisted_mode(monkeypatch) -> None:
    _patch_lstat(monkeypatch, 0o777)
    with pytest.raises(ToolError):
        LogDirRepairPermissionsTool().validate(
            {"path": "/var/log/payroll-api", "mode": "0777"}
        )


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX directory permissions")
def test_log_dir_perms_wrapper_repairs_child_before_parent() -> None:
    if os.geteuid() != 0:
        pytest.skip("requires root to create writable dirs under /var/log")
    root = Path(f"/var/log/kyagent-perms-test-{os.getpid()}")
    app_dir = root / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o777)
    os.chmod(app_dir, 0o777)
    try:
        result = subprocess.run(
            [sys.executable, str(LOG_DIR_PERMS_WRAPPER), str(root), "0750"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stderr
        assert stat.S_IMODE(app_dir.stat().st_mode) == 0o750
        assert stat.S_IMODE(root.stat().st_mode) == 0o750
        assert str(app_dir) in result.stdout or "/app" in result.stdout
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
