"""合规 / 完整性检测工具（赛题"配置漂移检测"场景）。"""
from __future__ import annotations
from typing import Any
from kyagent.mcp.tools.base import Tool, ToolRegistry, ToolError
from kyagent.safety.patterns import RiskLevel


_PATH_PATTERN = r"^/[A-Za-z0-9._/@+\-]+$"
_USER_PATTERN = r"^[a-z_][a-z0-9_-]{0,31}$"


# ---- 工具 1：AIDE 完整性检查 -------------------------------------------------
class ComplAideCheckTool(Tool):
    name = "compl_aide_check"
    description = (
        "运行 AIDE 完整性检查（aide --check）。"
        "用例：对比当前文件状态与基线数据库，发现配置漂移 / 文件篡改。"
        "注意：可能耗时数分钟。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["aide", "--check", "--config", "/etc/aide.conf"]


# ---- 工具 2：lsattr ----------------------------------------------------------
class ComplFileAttrTool(Tool):
    name = "compl_file_attr"
    description = (
        "查看文件 ext 文件系统属性（lsattr）。"
        "用例：检查关键文件是否设置了 immutable（i）/ append-only（a）保护位。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 300,
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lsattr", "--", args["path"]]


# ---- 工具 3：sha256 hash -----------------------------------------------------
class ComplFileHashTool(Tool):
    name = "compl_file_hash"
    description = (
        "计算文件 SHA-256 哈希（sha256sum）。"
        "用例：LLM 可两次调用对比 hash 检测篡改；也可比对厂商发布的官方校验和。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 300,
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["sha256sum", "--", args["path"]]


# ---- 工具 4：timestamp 审计 --------------------------------------------------
class ComplTimestampAuditTool(Tool):
    name = "compl_timestamp_audit"
    description = (
        "批量查询文件的 mtime / ctime（stat）。"
        "用例：识别近期被修改的关键配置文件，配合 baseline 检测漂移。"
    )
    input_schema = {
        "type": "object",
        "required": ["paths"],
        "properties": {
            "paths": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "pattern": _PATH_PATTERN,
                    "maxLength": 300,
                },
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        paths = args["paths"]
        # 数组成员的 pattern/maxLength 不会被基类的 _check_constraints 递归校验，
        # 这里显式再过一遍白名单，杜绝注入。
        import re as _re
        pat = _re.compile(_PATH_PATTERN)
        cleaned: list[str] = []
        for p in paths:
            if not isinstance(p, str):
                raise ToolError("paths 元素必须为 string")
            if len(p) > 300 or not pat.match(p):
                raise ToolError(f"paths 元素非法: {p!r}")
            cleaned.append(p)
        return ["stat", "-c", "%n\t%y\t%z", "--", *cleaned]


# ---- 工具 5：/etc/hosts ------------------------------------------------------
class ComplHostsTool(Tool):
    name = "compl_hosts"
    description = (
        "读取 /etc/hosts。"
        "用例：LLM 可与 baseline 对比检测 DNS 劫持 / 恶意域名注入。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/etc/hosts"]


# ---- 工具 6：crontab dump ----------------------------------------------------
class ComplCronDumpTool(Tool):
    name = "compl_cron_dump"
    description = (
        "导出 crontab：不带 user 读取系统 /etc/crontab；带 user 读取该用户 crontab。"
        "用例：检测后门定时任务。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "user": {
                "type": "string",
                "pattern": _USER_PATTERN,
                "maxLength": 32,
            },
        },
    }
    # 默认 LOW / 非 root（仅读系统 crontab）；运行时若传 user 则升级 root 需求由 sudoers
    # 决定。risk_level 取 MEDIUM 涵盖最坏情形以触发守门规则。
    risk_level = RiskLevel.MEDIUM
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        user = args.get("user")
        if user:
            return ["crontab", "-l", "-u", user]
        return ["cat", "/etc/crontab"]


def register(registry: ToolRegistry) -> None:
    registry.register(ComplAideCheckTool())
    registry.register(ComplFileAttrTool())
    registry.register(ComplFileHashTool())
    registry.register(ComplTimestampAuditTool())
    registry.register(ComplHostsTool())
    registry.register(ComplCronDumpTool())
