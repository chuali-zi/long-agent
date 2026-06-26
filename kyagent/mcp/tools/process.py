"""进程感知工具：ps / lsof。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry, ToolResult
from kyagent.safety.patterns import RiskLevel

_USER_PATTERN = r"^[a-z_][a-z0-9_-]{0,31}$"

class PsListTool(Tool):
    name = "process_list"
    description = "列出当前系统进程（ps 包装）。返回 USER/PID/CPU/MEM/COMMAND。"
    input_schema = {
        "type": "object",
        "properties": {
            "sort_by": {"type": "string", "description": "排序字段：cpu | mem | pid", "enum": ["cpu", "mem", "pid"]},
            "limit": {"type": "integer", "description": "返回行数，默认 20", "minimum": 1, "maximum": 200},
            "user": {"type": "string", "pattern": _USER_PATTERN, "description": "仅返回该用户的进程"},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        self._limit = int(cleaned.get("limit", 20))
        return cleaned

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
        limit = getattr(self, "_limit", 20)
        if out.ok:
            lines = out.content.splitlines()
            header = lines[:1]
            body = lines[1:][: limit]
            out.content = "\n".join(header + body)
            out.data["row_count"] = len(body)
        return out


class LsofPortTool(Tool):
    name = "lsof_port"
    description = (
        "查看占用某 TCP/UDP 端口的进程（lsof -i :PORT 包装）。"
        "注意：以普通用户运行时，看不到 root 起的监听进程——此时本工具会自动用 root "
        "重跑一次确认。若仍返回「无进程占用」才可判定端口空闲；不要据单次普通用户的空结果"
        "断定端口空闲或「孤悬 socket」，应结合 net_listen / process_list 交叉验证。"
    )
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

    @staticmethod
    def _is_blinded_empty(exec_result) -> bool:
        # lsof 对「查询合法但无匹配」用 exit 1 + 空输出。这与「以普通用户查 root
        # 起的监听进程被内核挡住」在退出码/输出上不可区分，因此这是「可能被权限蒙蔽」
        # 的歧义信号，值得用 root 重跑一次再下结论。
        return bool(
            exec_result.returncode == 1
            and not exec_result.stdout.strip()
            and not exec_result.stderr.strip()
            and not exec_result.timed_out
            and not exec_result.skipped_reason
        )

    def wants_privileged_retry(self, exec_result) -> bool:  # type: ignore[override]
        return self._is_blinded_empty(exec_result)

    def format_result(self, exec_result):  # type: ignore[override]
        # 到这里若仍是 exit 1 + 空，表示连 root 重跑也无匹配（或本就以 root 运行），
        # 此时「端口空闲」是可信结论，而非被权限蒙蔽的假象。
        if self._is_blinded_empty(exec_result):
            data = exec_result.to_dict()
            data["no_match"] = True
            return ToolResult(
                ok=True,
                content="No process is using the requested port.",
                data=data,
            )
        return super().format_result(exec_result)


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


class ProcessZombiesTool(Tool):
    name = "process_zombies"
    description = "列出系统中的僵尸进程（STAT 以 Z 开头），用于排查父进程未回收子进程的问题。"
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "返回行数上限，默认 50", "minimum": 1, "maximum": 500},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        self._limit = int(cleaned.get("limit", 50))
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ps", "-eo", "stat,pid,ppid,user,comm"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = out.content.splitlines()
            header = lines[:1]
            zombies = [ln for ln in lines[1:] if ln.lstrip().startswith("Z")]
            limit = getattr(self, "_limit", 50)
            zombies_trimmed = zombies[:limit]
            out.content = "\n".join(header + zombies_trimmed) if zombies_trimmed else "\n".join(header) + "\n(no zombies)"
            out.data["zombie_count"] = len(zombies)
        return out


class ProcessTreeTool(Tool):
    name = "process_tree"
    description = "以森林形式列出进程父子关系（ps --forest），可按用户过滤，用于排查孤儿/异常派生。"
    input_schema = {
        "type": "object",
        "properties": {
            "user": {"type": "string", "pattern": _USER_PATTERN, "description": "仅显示该用户的进程"},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["ps", "-eo", "pid,ppid,user,comm", "--forest"]
        if user := args.get("user"):
            argv.extend(["-u", user])
        return argv


class ProcessFdCountTool(Tool):
    name = "process_fd_count"
    description = "统计指定 PID 打开的文件描述符数量（ls /proc/PID/fd），用于排查 fd 泄漏。"
    input_schema = {
        "type": "object",
        "required": ["pid"],
        "properties": {
            "pid": {"type": "integer", "minimum": 1},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ls", "-1", f"/proc/{args['pid']}/fd"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = [ln for ln in out.content.splitlines() if ln.strip()]
            fd_count = len(lines)
            out.content = f"fd_count={fd_count}"
            out.data["fd_count"] = fd_count
        return out


class ProcessResourceTool(Tool):
    name = "process_resource"
    description = "读取 /proc/PID/status 查看进程内存、线程、FD 等资源占用（VmRSS / VmSize / Threads / FDSize）。"
    input_schema = {
        "type": "object",
        "required": ["pid"],
        "properties": {
            "pid": {"type": "integer", "minimum": 1},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", f"/proc/{args['pid']}/status"]


class TopCpuSnapshotTool(Tool):
    name = "top_cpu_snapshot"
    description = "一次性 top 快照（top -bn1），返回首 30 行；用于查看当前 CPU 负载与高耗进程。"
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "返回行数（1..50），默认 30", "minimum": 1, "maximum": 50},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        self._limit = int(cleaned.get("limit", 30))
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["top", "-bn1", "-w", "256"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = out.content.splitlines()
            limit = getattr(self, "_limit", 30)
            trimmed = lines[:limit]
            out.content = "\n".join(trimmed)
            out.data["row_count"] = len(trimmed)
        return out


class ProcessKillTool(Tool):
    name = "process_kill"
    description = (
        "向指定进程发送终止信号（kill -TERM/-KILL/-HUP/-INT <pid>）。"
        "高风险，需确认；禁止 pid<2（保护 init/内核）。"
    )
    input_schema = {
        "type": "object",
        "required": ["pid"],
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 2,
                "description": "目标进程 PID（≥2，禁止 kill init/内核线程）",
            },
            "signal": {
                "type": "string",
                "enum": ["TERM", "KILL", "HUP", "INT"],
                "description": "信号名称，默认 TERM",
            },
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        pid = args["pid"]
        signal = args.get("signal", "TERM")
        return ["/usr/bin/kill", f"-{signal}", str(pid)]


def register(registry: ToolRegistry) -> None:
    registry.register(PsListTool())
    registry.register(LsofPortTool())
    registry.register(LsofPidTool())
    registry.register(ProcessZombiesTool())
    registry.register(ProcessTreeTool())
    registry.register(ProcessFdCountTool())
    registry.register(ProcessResourceTool())
    registry.register(TopCpuSnapshotTool())
    registry.register(ProcessKillTool())
