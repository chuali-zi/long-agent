from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.runtime_state import (
    LockInspectTool,
    LockRemoveStaleTool,
    UnixSocketInspectTool,
    UnixSocketRemoveStaleTool,
)


ROOT = Path(__file__).parents[1]
LOCK_WRAPPER = ROOT / "scripts" / "kyagent-lock-stale"
SOCKET_WRAPPER = ROOT / "scripts" / "kyagent-unix-socket-stale"


def _argv(tool, args: dict) -> list[str]:
    return tool.build_argv(tool.validate(args))


def _run(script: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_lock_tools_argv_and_expected_pid() -> None:
    assert _argv(LockInspectTool(), {"path": "/tmp/app/locks/release.lock"}) == [
        "kyagent-lock-stale",
        "inspect",
        "/tmp/app/locks/release.lock",
    ]
    assert _argv(
        LockRemoveStaleTool(),
        {"path": "/run/app/app.pid", "expected_pid": 4242},
    ) == [
        "kyagent-lock-stale",
        "remove",
        "/run/app/app.pid",
        "--expected-pid",
        "4242",
    ]


def test_lock_tools_reject_out_of_bounds_and_metachar_paths() -> None:
    tool = LockRemoveStaleTool()
    for path in ("/etc/app.lock", "tmp/app.lock", "/tmp/app.lock;rm"):
        with pytest.raises(ToolError):
            tool.validate({"path": path})


def test_socket_tools_argv_and_critical_denies() -> None:
    assert _argv(UnixSocketInspectTool(), {"path": "/tmp/app/api.sock"}) == [
        "kyagent-unix-socket-stale",
        "inspect",
        "/tmp/app/api.sock",
    ]
    assert _argv(UnixSocketRemoveStaleTool(), {"path": "/var/tmp/app/api.sock"}) == [
        "kyagent-unix-socket-stale",
        "remove",
        "/var/tmp/app/api.sock",
    ]
    for path in (
        "/run/systemd/private",
        "/run/dbus/system_bus_socket",
        "/var/run/docker.sock",
        "/run/containerd/containerd.sock",
        "/run/sshd/session.sock",
    ):
        with pytest.raises(ToolError):
            UnixSocketRemoveStaleTool().validate({"path": path})


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX file/socket semantics")
def test_lock_wrapper_removes_dead_pid_lock() -> None:
    lock_dir = Path("/tmp") / f"kyagent_lock_test_{os.getpid()}"
    lock_dir.mkdir(exist_ok=True)
    path = lock_dir / "release.lock"
    path.write_text("pid=4194303\n", encoding="ascii")
    try:
        inspect = _run(LOCK_WRAPPER, "inspect", str(path))
        assert inspect.returncode == 0, inspect.stderr
        payload = json.loads(inspect.stdout)
        assert payload["pid"] == 4194303
        assert payload["stale"] is True

        removed = _run(LOCK_WRAPPER, "remove", str(path), "--expected-pid", "4194303")
        assert removed.returncode == 0, removed.stderr
        assert not path.exists()
    finally:
        path.unlink(missing_ok=True)
        lock_dir.rmdir()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX /proc semantics")
def test_lock_wrapper_rejects_live_pid() -> None:
    path = Path("/tmp") / f"kyagent_live_{os.getpid()}.lock"
    path.write_text(str(os.getpid()), encoding="ascii")
    try:
        result = _run(LOCK_WRAPPER, "remove", str(path))
        assert result.returncode != 0
        assert "PID 仍存在" in result.stderr
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX Unix sockets")
def test_socket_wrapper_removes_unlistened_socket() -> None:
    sock_path = Path("/tmp") / f"kyagent_stale_{os.getpid()}.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
    finally:
        sock.close()
    try:
        inspect = _run(SOCKET_WRAPPER, "inspect", str(sock_path))
        assert inspect.returncode == 0, inspect.stderr
        payload = json.loads(inspect.stdout)
        assert payload["is_socket"] is True
        assert payload["stale"] is True

        removed = _run(SOCKET_WRAPPER, "remove", str(sock_path))
        assert removed.returncode == 0, removed.stderr
        assert not sock_path.exists()
    finally:
        sock_path.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX Unix sockets")
def test_socket_wrapper_rejects_live_listener() -> None:
    sock_path = Path("/tmp") / f"kyagent_live_{os.getpid()}.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
        sock.listen(1)
        result = _run(SOCKET_WRAPPER, "remove", str(sock_path))
        assert result.returncode != 0
        assert "live listener" in result.stderr
        assert sock_path.exists()
    finally:
        sock.close()
        sock_path.unlink(missing_ok=True)
