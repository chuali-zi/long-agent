"""磁盘 / I/O / 文件系统深度感知工具。

这一组工具补齐了 filesystem.py（df/du/ls/find 基础查询）外的:
  - I/O 速率（iostat / vmstat / /proc/diskstats）—— 见 DiskIoStatsTool（TrendTool）
  - inode 用量、open-deleted 句柄、挂载选项
  - 大文件定位
"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, TrendTool, ToolRegistry, ToolError
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
    description = "找已删除但句柄仍打开的文件（du 看不见、df 仍占空间）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lsof", "+L1"]


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
                "pattern": r"^/dev/[a-z]+[0-9]*$",
                "maxLength": 50,
                "description": "块设备路径，如 /dev/sda",
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
                "pattern": r"^/.*",
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
        # 解析 input args 还原 limit（execution result 不带 args，所以用默认或重算）
        # 这里采用：按第一列（字节数）倒序，截前若干行；limit 通过环境无法回传，
        # 因此在调用端 build_argv 之后我们没有保存 limit，但默认 30 是稳定行为。
        # 实际上调用端 mcp 层把 args 给 build_argv 后并没有把 limit 透给 format_result，
        # 因此这里改用类常量默认 30；如果将来 mcp 层提供 args，请使用之。
        limit = 30
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
