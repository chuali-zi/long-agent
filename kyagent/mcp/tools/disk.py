"""磁盘 / I/O / 文件系统深度感知工具。

这一组工具补齐了 filesystem.py（df/du/ls/find 基础查询）外的:
  - I/O 速率（iostat / vmstat / /proc/diskstats）—— 见 DiskIoStatsTool（TrendTool）
  - inode 用量、open-deleted 句柄、挂载选项
  - 大文件定位
"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, TrendTool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


# 路径白名单 pattern：必须以 / 开头，仅允许 [A-Za-z0-9._/@-]
_PATH_PATTERN = r"^/[A-Za-z0-9._/@-]+$"


class DiskIoStatsTool(TrendTool):
    name = "disk_io_stats"
    description = (
        "采样 I/O 速率（iostat -dx）。默认 1 秒间隔 × 2 次采样，第二次输出就是 delta。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "interval": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "采样间隔（秒），默认 1",
            },
            "count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 5,
                "description": "采样次数，默认 2（第一次基线，后续为 delta）",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        interval = int(args.get("interval", 1))
        count = int(args.get("count", 2))
        return ["iostat", "-dx", str(interval), str(count)]


class DiskIoDiskstatsTool(Tool):
    name = "disk_io_diskstats"
    description = (
        "读 /proc/diskstats 原始计数。需要 LLM 自行间隔调用两次算 delta。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/proc/diskstats"]


class DiskInodeUsageTool(Tool):
    name = "disk_inode_usage"
    description = "查 inode 使用率（'空间够但写不进'经典陷阱）。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 200,
                "description": "可选，限定某挂载点",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        if path := args.get("path"):
            return ["df", "-i", "--", path]
        return ["df", "-i"]


class DiskOpenDeletedTool(Tool):
    name = "disk_open_deleted"
    description = (
        "Find deleted-but-still-open files that still consume disk space. "
        "Requires root/sudo visibility for other users' file descriptors. "
        "Optional filters narrow by runtime_root, process PID, or command/name substring; "
        "a service port number is not a PID."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "runtime_root": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 200,
                "description": "Optional runtime root/path prefix to match against the deleted file name.",
            },
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional process PID. Do not pass a TCP/UDP service port here.",
            },
            "cmdline": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "pattern": r"^[A-Za-z0-9._/@:+=, -]+$",
                "description": "Optional process name, command line, or lsof row substring. This is not a port.",
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["lsof", "-nP", "+L1"]
        if pid := args.get("pid"):
            argv.extend(["-p", str(pid)])
        return argv

    def format_result(self, exec_result):  # type: ignore[override]
        if exec_result.skipped_reason or exec_result.timed_out:
            return super().format_result(exec_result)
        if exec_result.returncode not in (0, 1):
            return super().format_result(exec_result)

        tool_args = exec_result.extra.get("tool_args", {})
        stdout = exec_result.stdout or ""
        lines = stdout.splitlines()
        has_header = bool(lines and lines[0].lstrip().startswith("COMMAND"))
        header = lines[:1] if has_header else []
        body = lines[1:] if has_header else lines
        body = [ln for ln in body if ln.strip()]
        original_count = len(body)

        runtime_root = tool_args.get("runtime_root")
        cmdline = tool_args.get("cmdline")
        filtered = list(body)
        if runtime_root:
            root = str(runtime_root).rstrip("/")
            filtered = [
                ln for ln in filtered
                if self._line_name(ln).startswith(root + "/") or self._line_name(ln) == root
            ]
        if cmdline:
            needle = str(cmdline).lower()
            filtered = [ln for ln in filtered if needle in ln.lower()]

        permission_notice = self._permission_notice(exec_result, original_count, len(filtered))
        content_lines = header + filtered if filtered else []
        if not content_lines and permission_notice:
            content_lines = [permission_notice]

        result = super().format_result(exec_result)
        result.ok = True
        result.error = None
        result.content = "\n".join(content_lines)
        result.data["open_deleted_count"] = original_count
        result.data["filtered_count"] = len(filtered)
        result.data["filters"] = {
            k: v for k, v in {
                "runtime_root": runtime_root,
                "pid": tool_args.get("pid"),
                "cmdline": cmdline,
            }.items() if v is not None
        }
        if permission_notice:
            result.data["notice"] = permission_notice
            if self._notice_is_permission_related(permission_notice):
                result.data["permissions_may_hide_other_users"] = True
            result.data["next_steps"] = [
                "Run disk_open_deleted as root/sudo-capable kyagent to see other users' file descriptors.",
                "If you know the process, retry with pid or cmdline; if you know the fixture root, retry with runtime_root.",
            ]
        if exec_result.stderr:
            result.data["lsof_stderr"] = exec_result.stderr
        return result

    @staticmethod
    def _line_name(line: str) -> str:
        parts = line.split(None, 9)
        if len(parts) >= 10:
            return parts[9]
        return line

    @staticmethod
    def _permission_notice(exec_result, original_count: int, filtered_count: int) -> str:
        stderr = (exec_result.stderr or "").lower()
        run_as = exec_result.run_as or ""
        sudo_error_words = ("sudo:", "password is required", "not in the sudoers", "a password is required")
        sudo_failed = exec_result.sudo_used and any(word in stderr for word in sudo_error_words)
        privileged = (run_as == "root" or exec_result.sudo_used) and not sudo_failed
        permission_words = ("permission", "not permitted", "operation not permitted", "can't stat")
        if sudo_failed:
            return (
                "disk_open_deleted could not get root/sudo visibility, so lsof may not see "
                "other users' deleted-open file descriptors. Configure sudoers for lsof or "
                "run with root-capable execution, then retry."
            )
        if original_count == 0 and not privileged:
            return (
                "No deleted-open files were visible to this non-root tool run. "
                "lsof may hide other users' file descriptors without root/sudo privileges; "
                "retry with root/sudo-capable execution or narrow by pid/cmdline/runtime_root."
            )
        if filtered_count == 0 and original_count > 0:
            return (
                "lsof found deleted-open files, but none matched the requested filters. "
                "Check runtime_root, pid, and cmdline; remember service ports are not PIDs."
            )
        if any(word in stderr for word in permission_words) and not privileged:
            return (
                "lsof reported permission limitations, so results may omit other users' "
                "deleted-open files. Retry with root/sudo-capable execution if this scope matters."
            )
        return ""

    @staticmethod
    def _notice_is_permission_related(notice: str) -> bool:
        lowered = notice.lower()
        return any(word in lowered for word in ("permission", "non-root", "sudo", "root/sudo"))


class DiskMountTool(Tool):
    name = "disk_mount"
    description = "挂载状态 + 挂载选项审计（findmnt -J，JSON 输出）。"
    input_schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 200,
                "description": "可选，限定某挂载点",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        if target := args.get("target"):
            return ["findmnt", "-J", "--", target]
        return ["findmnt", "-J"]


class DiskSmartTool(Tool):
    name = "disk_smart"
    description = "块设备 SMART 健康（需安装 smartmontools）。"
    input_schema = {
        "type": "object",
        "required": ["device"],
        "properties": {
            "device": {
                "type": "string",
                "pattern": r"^/dev/(?:sd[a-z][0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?)$",
                "maxLength": 50,
                "description": "块设备路径，如 /dev/sda 或 /dev/nvme0n1",
            },
        },
    }
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["smartctl", "-H", "-A", "--", args["device"]]


class DirLargestFilesTool(Tool):
    name = "dir_largest_files"
    description = "在指定目录树下找占地最大的若干文件（find + size 排序）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 200,
                "description": "起始目录（必须以 / 开头）",
            },
            "min_size_mb": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10000,
                "description": "最小文件大小（MB），默认 100",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "返回前 N 个最大的文件，默认 30",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        path = args["path"]
        min_size_mb = int(args.get("min_size_mb", 100))
        return [
            "find",
            path,
            "-type",
            "f",
            "-size",
            f"+{min_size_mb}M",
            "-printf",
            "%s\t%p\n",
        ]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        limit = int(exec_result.extra.get("tool_args", {}).get("limit", 30))
        lines = [ln for ln in out.content.splitlines() if ln.strip()]
        parsed: list[tuple[int, str]] = []
        for ln in lines:
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                size = int(parts[0])
            except ValueError:
                continue
            parsed.append((size, parts[1]))
        parsed.sort(key=lambda x: x[0], reverse=True)
        parsed = parsed[:limit]
        out.content = "\n".join(f"{s}\t{p}" for s, p in parsed)
        out.data["row_count"] = len(parsed)
        return out


def register(registry: ToolRegistry) -> None:
    registry.register(DiskIoStatsTool())
    registry.register(DiskIoDiskstatsTool())
    registry.register(DiskInodeUsageTool())
    registry.register(DiskOpenDeletedTool())
    registry.register(DiskMountTool())
    registry.register(DiskSmartTool())
    registry.register(DirLargestFilesTool())
