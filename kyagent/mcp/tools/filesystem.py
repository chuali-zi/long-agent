"""文件系统感知工具：df / du / ls / find（只读）。"""
from __future__ import annotations

import posixpath
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PROTECTED_READ = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}
_ABS_PATH_PATTERN = r"^/(?!.*[\x00-\x1f\x7f])"


def _safe_path(p: str) -> str:
    if not p:
        raise ToolError("path 不能为空")
    if not p.startswith("/"):
        raise ToolError(f"path 必须是绝对路径: {p!r}")
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
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN, "description": "可选，限定某挂载点"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"]
        if p := args.get("path"):
            argv.extend(["--", _safe_path(p)])
        return argv


class DuTool(Tool):
    name = "fs_du"
    description = "目录占用统计（du -sh / -ah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN},
            "depth": {"type": "integer", "minimum": 1, "maximum": 5, "description": "默认 1"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        depth = int(args.get("depth", 1))
        return ["du", "-h", f"--max-depth={depth}", "--", _safe_path(args["path"])]


class LsTool(Tool):
    name = "fs_ls"
    description = "列出目录内容（ls -lah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string", "pattern": _ABS_PATH_PATTERN}},
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ls", "-lah", "--color=never", "--", _safe_path(args["path"])]


class FindTool(Tool):
    name = "fs_find"
    description = "在指定目录下按文件名 / 修改时间过滤（find 包装，强制不走 -exec）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN},
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


_MUTATION_ALLOWED_PREFIXES = (
    "/var/log/",
    "/var/cache/",
    "/var/tmp/",
    "/tmp/",
)


class FsTruncateTool(Tool):
    name = "fs_truncate"
    description = (
        "将日志/缓存文件就地清空（truncate -s 0，保留 inode 与文件句柄，安全回收空间）。"
        "仅限 /var/log、/var/cache、/var/tmp、/tmp 下；高风险需确认。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 300,
                "description": "目标文件绝对路径，仅允许 /var/log、/var/cache、/var/tmp、/tmp 下",
            }
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        p = _safe_path(cleaned["path"])
        if not any(p.startswith(prefix) for prefix in _MUTATION_ALLOWED_PREFIXES):
            raise ToolError(
                "fs_truncate 仅允许清空 /var/log、/var/cache、/var/tmp、/tmp 下的文件"
            )
        cleaned["path"] = p
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        # 不再裸调 truncate；走 sudoers 授权的包装器，由它在 OS 层做
        # realpath 规范化 + 白名单根目录 + O_NOFOLLOW 校验（防 .. 越界与符号链接）。
        return ["kyagent-log-clean", args["path"]]


class FsDeleteFileTool(Tool):
    name = "fs_delete_file"
    description = (
        "删除单个日志/缓存/临时普通文件（安全 unlink 包装器）。"
        "仅限 /var/log、/var/cache、/var/tmp、/tmp 下；不递归、不跟随符号链接；高风险需确认。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 300,
                "description": "目标文件绝对路径，仅允许 /var/log、/var/cache、/var/tmp、/tmp 下",
            }
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        p = _safe_path(cleaned["path"])
        if not any(p.startswith(prefix) for prefix in _MUTATION_ALLOWED_PREFIXES):
            raise ToolError(
                "fs_delete_file 仅允许删除 /var/log、/var/cache、/var/tmp、/tmp 下的单个文件"
            )
        cleaned["path"] = p
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-file-delete", args["path"]]


def register(registry: ToolRegistry) -> None:
    registry.register(DfTool())
    registry.register(DuTool())
    registry.register(LsTool())
    registry.register(FindTool())
    registry.register(FsTruncateTool())
    registry.register(FsDeleteFileTool())
