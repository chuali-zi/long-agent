"""进程感知工具：ps / lsof。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


class PsListTool(Tool):
    name = "process_list"
    description = "列出当前系统进程（ps 包装）。返回 USER/PID/CPU/MEM/COMMAND。"
    input_schema = {
        "type": "object",
        "properties": {
            "sort_by": {"type": "string", "description": "排序字段：cpu | mem | pid", "enum": ["cpu", "mem", "pid"]},
            "limit": {"type": "integer", "description": "返回行数，默认 20", "minimum": 1, "maximum": 200},
            "user": {"type": "string", "description": "仅返回该用户的进程"},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        sort_by = args.get("sort_by", "cpu")
        argv = ["ps", "-eo", "user,pid,pcpu,pmem,etime,stat,comm,args"]
        sort_map = {"cpu": "-pcpu", "mem": "-pmem", "pid": "pid"}
        argv.extend(["--sort", sort_map[sort_by]])
        if user := args.get("user"):
            argv.extend(["-u", user])
        return argv

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        # 截断到 limit 行
        limit = 20
        if out.ok:
            lines = out.content.splitlines()
            header = lines[:1]
            body = lines[1:][: limit]
            out.content = "\n".join(header + body)
            out.data["row_count"] = len(body)
        return out


class LsofPortTool(Tool):
    name = "lsof_port"
    description = "查看占用某 TCP/UDP 端口的进程（lsof -i :PORT 包装）。"
    input_schema = {
        "type": "object",
        "required": ["port"],
        "properties": {
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "proto": {"type": "string", "enum": ["tcp", "udp"], "description": "默认 tcp"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        proto = args.get("proto", "tcp")
        port = args["port"]
        return ["lsof", "-nP", "-i", f"{proto.upper()}:{port}"]


class LsofPidTool(Tool):
    name = "lsof_pid"
    description = "查看指定 PID 打开的所有文件 / 套接字（lsof -p PID 包装）。"
    input_schema = {
        "type": "object",
        "required": ["pid"],
        "properties": {
            "pid": {"type": "integer", "minimum": 1},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lsof", "-nP", "-p", str(args["pid"])]


def register(registry: ToolRegistry) -> None:
    registry.register(PsListTool())
    registry.register(LsofPortTool())
    registry.register(LsofPidTool())
