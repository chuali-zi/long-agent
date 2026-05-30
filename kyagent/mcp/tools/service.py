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
_UNIT_PATTERN = r"^(?!-)[A-Za-z0-9@._\-+:]+$"


def _validate_unit(unit: str) -> str:
    import re
    if not unit or not re.fullmatch(_UNIT_PATTERN, unit):
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
            "unit": {"type": "string", "pattern": _UNIT_PATTERN, "description": "如 sshd / nginx.service"},
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
            "unit": {"type": "string", "pattern": _UNIT_PATTERN},
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
        "properties": {"unit": {"type": "string", "pattern": _UNIT_PATTERN}},
    }
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "reload", unit]


class SvcIsActiveTool(Tool):
    name = "svc_is_active"
    description = "查询 systemd unit 是否处于 active 状态（systemctl is-active，只读）。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "pattern": _UNIT_PATTERN},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "is-active", "--", unit]


class SvcIsEnabledTool(Tool):
    name = "svc_is_enabled"
    description = "查询 systemd unit 是否已 enable（systemctl is-enabled，只读）。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "pattern": _UNIT_PATTERN},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "is-enabled", "--", unit]


class SvcShowTool(Tool):
    name = "svc_show"
    description = "显示 unit 的全部属性键值（systemctl show），用于深入排查配置细节。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "pattern": _UNIT_PATTERN},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "show", "--no-pager", "--", unit]


class SvcCatTool(Tool):
    name = "svc_cat"
    description = "查看 unit 的完整定义文件内容（systemctl cat），含 drop-in。"
    input_schema = {
        "type": "object",
        "required": ["unit"],
        "properties": {
            "unit": {"type": "string", "pattern": _UNIT_PATTERN},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        unit = _validate_unit(args["unit"])
        return ["systemctl", "cat", "--no-pager", "--", unit]


class SvcFailedTool(Tool):
    name = "svc_failed"
    description = "列出当前所有处于 failed 状态的 unit（systemctl --failed），用于快速发现故障服务。"
    input_schema = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["systemctl", "--no-pager", "--no-legend", "--failed"]


class SvcTimersTool(Tool):
    name = "svc_timers"
    description = "列出所有 systemd timer（含未激活，systemctl list-timers --all），用于排查计划任务。"
    input_schema = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"]


class BootAnalyzeTool(Tool):
    name = "boot_analyze"
    description = "分析启动耗时分布（systemd-analyze blame），返回首 30 行以定位慢启动 unit。"
    input_schema = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["systemd-analyze", "blame", "--no-pager"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = out.content.splitlines()
            trimmed = lines[:30]
            out.content = "\n".join(trimmed)
            out.data["row_count"] = len(trimmed)
        return out


class BootLogsTool(Tool):
    name = "boot_logs"
    description = "查看指定 boot 的错误级日志（journalctl -b -p err）；boot_offset=0 当前，-N 表示 N 次之前。"
    input_schema = {
        "type": "object",
        "properties": {
            "boot_offset": {
                "type": "integer",
                "description": "0 表示当前 boot，-1 表示上次开机，最低 -10",
                "minimum": -10,
                "maximum": 0,
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        offset = args.get("boot_offset", 0)
        if offset != 0:
            return ["journalctl", "-b", str(offset), "-p", "err", "--no-pager"]
        return ["journalctl", "-b", "-p", "err", "--no-pager"]


def register(registry: ToolRegistry) -> None:
    registry.register(SvcStatusTool())
    registry.register(SvcListTool())
    registry.register(SvcRestartTool())
    registry.register(SvcReloadTool())
    registry.register(SvcIsActiveTool())
    registry.register(SvcIsEnabledTool())
    registry.register(SvcShowTool())
    registry.register(SvcCatTool())
    registry.register(SvcFailedTool())
    registry.register(SvcTimersTool())
    registry.register(BootAnalyzeTool())
    registry.register(BootLogsTool())
