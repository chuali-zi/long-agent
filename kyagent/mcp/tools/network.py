"""网络感知工具：ss / netstat。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


class SsListenTool(Tool):
    name = "net_listen"
    description = "列出所有监听端口（ss -tlnp，回退 netstat -tlnp）。"
    input_schema = {
        "type": "object",
        "properties": {
            "proto": {
                "type": "string",
                "enum": ["tcp", "udp", "all"],
                "description": "默认 tcp",
            },
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        proto = args.get("proto", "tcp")
        flag = {"tcp": "-tlnp", "udp": "-ulnp", "all": "-tulnp"}[proto]
        return ["ss", flag]


class SsConnTool(Tool):
    name = "net_connections"
    description = "列出已建立 / 等待中的连接（ss -tnp state established 等）。"
    input_schema = {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["established", "time-wait", "close-wait", "syn-sent", "all"],
            },
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        state = args.get("state", "established")
        argv = ["ss", "-tnp"]
        if state != "all":
            argv.extend(["state", state])
        return argv


class PingTool(Tool):
    name = "net_ping"
    description = "对目标主机做一次 ping 探测。"
    input_schema = {
        "type": "object",
        "required": ["host"],
        "properties": {
            "host": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "默认 3"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        count = int(args.get("count", 3))
        return ["ping", "-c", str(count), "-W", "2", str(args["host"])]


def register(registry: ToolRegistry) -> None:
    registry.register(SsListenTool())
    registry.register(SsConnTool())
    registry.register(PingTool())
