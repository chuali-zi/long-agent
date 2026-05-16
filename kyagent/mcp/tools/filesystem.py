"""文件系统感知工具：df / du / ls / find（只读）。"""
from __future__ import annotations

import posixpath
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PROTECTED_READ = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}


def _safe_path(p: str) -> str:
    if not p:
        raise ToolError("path 不能为空")
    # 目标系统是 Linux/麒麟，统一用 posix 归一化（避免 Windows 开发态把 / 翻成 \）
    p = posixpath.normpath(p)
    if any(c in p for c in [";", "|", "&", "$", "`", "\n"]):
        raise ToolError(f"非法路径字符: {p!r}")
    if p in _PROTECTED_READ:
        raise ToolError(f"路径 {p} 在工具层禁读名单内")
    return p


class DfTool(Tool):
    name = "fs_df"
    description = "查看挂载点磁盘使用情况（df -h）。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "可选，限定某挂载点"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"]
        if p := args.get("path"):
            argv.append(_safe_path(p))
        return argv


class DuTool(Tool):
    name = "fs_du"
    description = "目录占用统计（du -sh / -ah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 5, "description": "默认 1"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        depth = int(args.get("depth", 1))
        return ["du", "-h", f"--max-depth={depth}", _safe_path(args["path"])]


class LsTool(Tool):
    name = "fs_ls"
    description = "列出目录内容（ls -lah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ls", "-lah", "--color=never", _safe_path(args["path"])]


class FindTool(Tool):
    name = "fs_find"
    description = "在指定目录下按文件名 / 修改时间过滤（find 包装，强制不走 -exec）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string", "description": "glob，如 '*.log'"},
            "mtime_days": {"type": "integer", "description": "最近 N 天内修改"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "description": "默认 3"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        p = _safe_path(args["path"])
        max_depth = int(args.get("max_depth", 3))
        argv = ["find", p, "-maxdepth", str(max_depth)]
        if name := args.get("name"):
            if any(c in name for c in [";", "|", "&", "$", "`", "\n"]):
                raise ToolError(f"非法 name 模式: {name!r}")
            argv.extend(["-name", name])
        if (md := args.get("mtime_days")) is not None:
            argv.extend(["-mtime", f"-{int(md)}"])
        argv.extend(["-type", "f"])
        # 显式禁止 -exec / -delete 等
        return argv


def register(registry: ToolRegistry) -> None:
    registry.register(DfTool())
    registry.register(DuTool())
    registry.register(LsTool())
    registry.register(FindTool())
