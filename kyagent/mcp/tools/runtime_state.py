"""Runtime stale lock / Unix socket remediation tools."""
from __future__ import annotations

import posixpath
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_ABS_PATH_PATTERN = r"^/(?!.*[\x00-\x1f\x7f])"
_ALLOWED_RUNTIME_ROOTS = ("/tmp/", "/var/tmp/", "/run/", "/var/run/")
_LOCK_SUFFIXES = (".lock", ".lck", ".pid")
_LOCK_COMPONENTS = {"lock", "locks", "run", "runtime", "pids"}
_CRITICAL_SOCKET_COMPONENTS = {
    "systemd",
    "dbus",
    "docker",
    "containerd",
    "sshd",
    "podman",
    "crio",
    "kubelet",
}
_CRITICAL_SOCKET_NAMES = {
    "docker.sock",
    "containerd.sock",
    "dbus.sock",
    "sshd.sock",
    "podman.sock",
    "crio.sock",
}


def _runtime_path(raw: str) -> str:
    if not raw:
        raise ToolError("path 不能为空")
    if not raw.startswith("/"):
        raise ToolError(f"path 必须是绝对路径: {raw!r}")
    if any(c in raw for c in ["\0", ";", "|", "&", "$", "`", "\n", "\r"]):
        raise ToolError(f"非法路径字符: {raw!r}")
    path = posixpath.normpath(raw)
    if path in {"/tmp", "/var/tmp", "/run", "/var/run"}:
        raise ToolError("path 必须指向运行态文件，不能是根目录本身")
    if not any(path.startswith(root) for root in _ALLOWED_RUNTIME_ROOTS):
        raise ToolError("仅允许 /tmp、/var/tmp、/run、/var/run 下的运行态文件")
    return path


def _validate_lock_path(raw: str) -> str:
    path = _runtime_path(raw)
    basename = posixpath.basename(path).lower()
    components = {part.lower() for part in path.split("/") if part}
    if not (
        basename.endswith(_LOCK_SUFFIXES)
        or bool(components & _LOCK_COMPONENTS)
    ):
        raise ToolError("lock 工具仅处理 lock/lck/pid 文件或 runtime/locks 路径")
    return path


def _validate_socket_path(raw: str) -> str:
    path = _runtime_path(raw)
    basename = posixpath.basename(path).lower()
    components = {part.lower() for part in path.split("/") if part}
    if basename in _CRITICAL_SOCKET_NAMES or components & _CRITICAL_SOCKET_COMPONENTS:
        raise ToolError("拒绝处理 systemd/dbus/docker/containerd/sshd 等关键 socket")
    return path


class LockInspectTool(Tool):
    name = "lock_inspect"
    description = (
        "检查运行态 lock/pid 文件是否 stale：仅限 /tmp、/var/tmp、/run、/var/run 下，"
        "读取纯 PID 或 pid=<num>，并检查进程与持有者证据。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "maxLength": 300, "pattern": _ABS_PATH_PATTERN}
        },
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["path"] = _validate_lock_path(cleaned["path"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-lock-stale", "inspect", args["path"]]


class LockRemoveStaleTool(Tool):
    name = "lock_remove_stale"
    description = (
        "删除确认 stale 的单个 lock/pid 文件。仅当文件为末端非 symlink 的普通文件、"
        "内容为纯 PID 或 pid=<num>、PID 不存在且无 live 持有者时，由专用 wrapper unlink。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "maxLength": 300, "pattern": _ABS_PATH_PATTERN},
            "expected_pid": {"type": "integer", "minimum": 2, "maximum": 4_194_304},
        },
        "additionalProperties": False,
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["path"] = _validate_lock_path(cleaned["path"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["kyagent-lock-stale", "remove", args["path"]]
        if "expected_pid" in args:
            argv.extend(["--expected-pid", str(args["expected_pid"])])
        return argv


class UnixSocketInspectTool(Tool):
    name = "unix_socket_inspect"
    description = (
        "检查 Unix socket 文件及 listener 状态。仅限 /tmp、/var/tmp、/run、/var/run 下，"
        "并拒绝 systemd/dbus/docker/containerd/sshd 等关键 socket。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "maxLength": 300, "pattern": _ABS_PATH_PATTERN}
        },
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["path"] = _validate_socket_path(cleaned["path"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-unix-socket-stale", "inspect", args["path"]]


class UnixSocketRemoveStaleTool(Tool):
    name = "unix_socket_remove_stale"
    description = (
        "删除确认 stale 的单个 Unix socket 文件。仅当目标是 socket、非关键路径、"
        "且 ss/lsof 未发现 listener 时，由专用 wrapper unlink。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "maxLength": 300, "pattern": _ABS_PATH_PATTERN}
        },
        "additionalProperties": False,
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["path"] = _validate_socket_path(cleaned["path"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-unix-socket-stale", "remove", args["path"]]


def register(registry: ToolRegistry) -> None:
    registry.register(LockInspectTool())
    registry.register(LockRemoveStaleTool())
    registry.register(UnixSocketInspectTool())
    registry.register(UnixSocketRemoveStaleTool())
