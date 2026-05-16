"""日志感知工具：journalctl / dmesg。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PRIORITIES = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}


class JournalctlTool(Tool):
    name = "log_journal"
    description = "查询 systemd journal 日志（journalctl 包装，只读，支持 --since / --priority / --unit）。"
    input_schema = {
        "type": "object",
        "properties": {
            "since": {"type": "string", "description": "时间窗，如 '10 min ago' / '2025-05-16 10:00'"},
            "priority": {"type": "string", "description": "最低优先级：err/warning/info/..."},
            "unit": {"type": "string", "description": "限定 systemd unit（如 sshd.service）"},
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "默认 100"},
            "grep": {"type": "string", "description": "正则过滤（journalctl --grep）"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["journalctl", "--no-pager"]
        if since := args.get("since"):
            argv.extend(["--since", since])
        if pri := args.get("priority"):
            if pri.lower() in _PRIORITIES:
                argv.extend(["-p", pri.lower()])
        if unit := args.get("unit"):
            argv.extend(["-u", unit])
        lines = int(args.get("lines", 100))
        argv.extend(["-n", str(lines)])
        if g := args.get("grep"):
            argv.extend(["--grep", g])
        return argv


class DmesgTool(Tool):
    name = "log_dmesg"
    description = "读取内核环形缓冲日志（dmesg --human --color=never）。"
    input_schema = {
        "type": "object",
        "properties": {
            "level": {"type": "string", "description": "如 err,warn"},
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["dmesg", "--human", "--color=never"]
        if lvl := args.get("level"):
            argv.extend(["-l", lvl])
        return argv

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if res.ok:
            lines_arg = 200
            lines = res.content.splitlines()
            res.content = "\n".join(lines[-lines_arg:])
        return res


def register(registry: ToolRegistry) -> None:
    registry.register(JournalctlTool())
    registry.register(DmesgTool())
