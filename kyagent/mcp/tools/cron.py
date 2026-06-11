"""Dedicated /etc/cron.d inspection and disable tools.

The write tool intentionally disables only one cron.d regular file by renaming
it. It never deletes the cron entry or any referenced script evidence.
"""
from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


CRON_D_DIR = "/etc/cron.d"
NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,120}$"
PROTECTED_CRON_NAMES = frozenset({"nightly-ledger-backup", "0hourly", "anacron"})

_SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tmp-script", re.compile(r"(?<![A-Za-z0-9_./-])/(?:tmp|var/tmp)/[A-Za-z0-9_./+-]+")),
    ("network-fetch", re.compile(r"\b(?:curl|wget)\b|https?://|ftp://", re.IGNORECASE)),
    ("network-shell", re.compile(r"\b(?:nc|ncat|netcat|socat)\b", re.IGNORECASE)),
    ("log-deletion", re.compile(r"\b(?:rm|shred|truncate)\b[^#\n]*(?:/var/log|/var/tmp|/tmp)", re.IGNORECASE)),
    ("prompt-injection", re.compile(r"ignore (?:all )?(?:previous|above) instructions|prompt injection|system prompt|developer message", re.IGNORECASE)),
    ("shell-obfuscation", re.compile(r"\b(?:base64\s+-d|bash\s+-c|sh\s+-c|python\d?\s+-c|perl\s+-e)\b", re.IGNORECASE)),
)


def validate_cron_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ToolError("name 不能为空")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ToolError("name 只接受 /etc/cron.d 下的 basename，不能包含路径分隔符")
    if not re.fullmatch(NAME_PATTERN, name):
        raise ToolError(f"name={name!r} 含非法字符")
    return name


def suspicious_indicators(content: str) -> list[str]:
    hits: list[str] = []
    for label, pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(content):
            hits.append(label)
    return hits


def cron_d_path(name: str, cron_dir: str | None = None) -> Path:
    cron_dir = CRON_D_DIR if cron_dir is None else cron_dir
    return Path(cron_dir) / validate_cron_name(name)


def preflight_disable(name: str, method: str = "rename", cron_dir: str | None = None) -> list[str]:
    name = validate_cron_name(name)
    if method != "rename":
        raise ToolError("cron_d_disable 目前只支持 method='rename'")
    if name in PROTECTED_CRON_NAMES:
        raise ToolError(f"{name!r} 是保护 cron 名，拒绝自动禁用")

    path = cron_d_path(name, cron_dir)
    try:
        st = path.lstat()
    except OSError as exc:
        raise ToolError(f"无法读取 /etc/cron.d/{name} 元数据: {exc}")
    if stat.S_ISLNK(st.st_mode):
        raise ToolError("目标是 symlink，拒绝禁用")
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("目标不是 regular file，拒绝禁用")
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"无法读取 /etc/cron.d/{name}: {exc}")
    indicators = suspicious_indicators(content)
    if not indicators:
        raise ToolError("cron 内容没有命中可疑指标，拒绝禁用")
    return indicators


class CronDListTool(Tool):
    name = "cron_d_list"
    description = "列出 /etc/cron.d 下的单层 regular/symlink 文件及 mode/owner/mtime/hash 元数据（只读）。"
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-cron-trace", "--list"]


class CronDReadTool(Tool):
    name = "cron_d_read"
    description = "读取单个 /etc/cron.d/<name> 文件；name 只接受 basename 安全字符，不接受路径。"
    input_schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "pattern": NAME_PATTERN, "maxLength": 120}},
        "additionalProperties": False,
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["name"] = validate_cron_name(cleaned["name"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-cron-trace", "--read", args["name"]]


class CronEntryTraceTool(Tool):
    name = "cron_entry_trace"
    description = (
        "解析单个 /etc/cron.d/<name> 的 cron 命令路径，读取脚本/命令 metadata 和 sha256；"
        "只做静态读取，绝不执行 cron 命令或脚本。"
    )
    input_schema = CronDReadTool.input_schema
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["name"] = validate_cron_name(cleaned["name"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-cron-trace", "--trace", args["name"]]


class CronDDisableTool(Tool):
    name = "cron_d_disable"
    description = (
        "禁用单个可疑 /etc/cron.d/<name> regular file：原子 rename 为 "
        "<name>.disabled-by-kyagent.<timestamp>，保留证据；不删除文件或脚本。"
    )
    input_schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "pattern": NAME_PATTERN, "maxLength": 120},
            "method": {"type": "string", "enum": ["rename"]},
        },
        "additionalProperties": False,
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        name = validate_cron_name(cleaned["name"])
        method = cleaned.get("method", "rename")
        preflight_disable(name, method=method)
        cleaned["name"] = name
        cleaned["method"] = method
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-cron-disable", args["name"], args.get("method", "rename")]


def register(registry: ToolRegistry) -> None:
    registry.register(CronDListTool())
    registry.register(CronDReadTool())
    registry.register(CronEntryTraceTool())
    registry.register(CronDDisableTool())
