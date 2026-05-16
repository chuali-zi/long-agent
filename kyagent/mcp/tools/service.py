"""systemd 服务管理工具。

设计：
  - svc_status / svc_list / svc_show：只读，risk=low
  - svc_restart / svc_start / svc_stop / svc_reload：变更类，requires_root=True, risk=high
  - 任何"mask/disable/enable"操作不直接暴露给 LLM，必须人工介入（防止误关 sshd）
"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_FORBIDDEN_UNITS = {
    "systemd-logind", "systemd-journald", "systemd-udevd",
    "dbus", "dbus.service", "polkit", "polkit.service",
}


def _validate_unit(unit: str) -> str:
    if not unit or any(c in unit for c in [" ", ";", "|", "&", "$", "`"]):
        raise ToolError(f"非法 unit 名: {unit!r}")
    if unit.split(".")[0] in _FORBIDDEN_UNITS:
        raise ToolError(f"unit {unit!r} 在工具层禁用名单内（防误关核心服务）")
    return unit


class SvcStatusTool(Tool):
    name = "svc_status"
    description = "查看 systemd unit 状态（systemctl status / is-active 包装，只读）。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "description": "如 sshd / nginx.service"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "status", "--no-pager", "--lines=50", unit]


class SvcListTool(Tool):
    name = "svc_list"
    description = "列出所有已加载 unit（systemctl list-units）。"
    input_schema = {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["running", "failed", "exited", "active", "all"]},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        state = args.get("state", "active")
        argv = ["systemctl", "list-units", "--no-pager", "--no-legend", "--all"]
        if state != "all":
            argv.extend(["--state", state])
        return argv


class SvcRestartTool(Tool):
    name = "svc_restart"
    description = (
        "重启指定 systemd unit（systemctl restart）。"
        "高风险操作，须经安全护栏 + 用户确认；仅 sudoers 白名单允许的 unit 可成功。"
    )
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string"},
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "restart", unit]


class SvcReloadTool(Tool):
    name = "svc_reload"
    description = "热加载 systemd unit 配置（systemctl reload）。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {"unit": {"type": "string"}},
    }
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "reload", unit]


def register(registry: ToolRegistry) -> None:
    registry.register(SvcStatusTool())
    registry.register(SvcListTool())
    registry.register(SvcRestartTool())
    registry.register(SvcReloadTool())
