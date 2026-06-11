"""受控权限修复工具。"""
from __future__ import annotations

import os
import posixpath
import re
import stat
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_LOG_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_log_dir(path: str) -> str:
    if not path:
        raise ToolError("path 不能为空")
    if not path.startswith("/"):
        raise ToolError("path 必须是绝对路径")
    if any(c in path for c in ["\x00", ";", "|", "&", "$", "`", "\n", "\r"]):
        raise ToolError("path 含非法字符")
    raw_parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in raw_parts):
        raise ToolError("path 不能包含 . 或 .. 路径片段")

    normalized = posixpath.normpath(path)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) not in {3, 4} or parts[:2] != ["var", "log"]:
        raise ToolError("仅允许 /var/log/<service> 或其一层子目录")
    if normalized == "/var/log":
        raise ToolError("不允许修改 /var/log 本身")
    for part in parts[2:]:
        if not _LOG_DIR_PATTERN.fullmatch(part):
            raise ToolError(f"非法日志目录片段: {part!r}")
    return normalized


class LogDirRepairPermissionsTool(Tool):
    name = "log_dir_repair_permissions"
    description = (
        "受控收紧日志目录权限：仅限 /var/log/<service> 或其一层子目录，"
        "目标必须是当前 group/world writable 的目录；只允许 chmod 0750/0755，"
        "不递归、不 chown、不开放 world-writable。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 240,
                "description": "目标目录，只允许 /var/log/<service> 或 /var/log/<service>/<subdir>",
            },
            "mode": {
                "type": "string",
                "enum": ["0750", "0755"],
                "description": "目标权限，默认 0750；只能收紧 group/world 写权限",
            },
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        path = _normalize_log_dir(cleaned["path"])
        mode = cleaned.get("mode", "0750")
        target_mode = int(mode, 8)

        if target_mode & stat.S_IWOTH:
            raise ToolError("目标权限不能 world-writable")

        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise ToolError("目标目录不存在")
        except OSError as exc:
            raise ToolError(f"无法读取目标目录元数据: {exc.__class__.__name__}")

        if stat.S_ISLNK(st.st_mode):
            raise ToolError("目标不能是符号链接")
        if not stat.S_ISDIR(st.st_mode):
            raise ToolError("目标必须是目录")

        current_mode = stat.S_IMODE(st.st_mode)
        if current_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0:
            raise ToolError("当前目录并非 group/world writable，无需权限修复")

        current_write_bits = current_mode & 0o222
        target_write_bits = target_mode & 0o222
        if target_write_bits & ~current_write_bits:
            raise ToolError("目标权限不能增加任何写权限")

        cleaned["path"] = path
        cleaned["mode"] = mode
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-log-dir-perms", args["path"], args.get("mode", "0750")]


def register(registry: ToolRegistry) -> None:
    registry.register(LogDirRepairPermissionsTool())
