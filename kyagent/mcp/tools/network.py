"""网络感知工具：ss / netstat。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry, ToolResult
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


class NetRoutesTool(Tool):
    name = "net_routes"
    description = "查看内核路由表（ip -j route，JSON 输出）。用于排查路由/默认网关问题。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ip", "-j", "route"]


class NetArpTool(Tool):
    name = "net_arp"
    description = "查看 ARP/邻居表（ip -j neigh，JSON）。用于定位同网段主机/MAC 异常。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ip", "-j", "neigh"]


class NetLinkStatsTool(Tool):
    name = "net_link_stats"
    description = "查看网卡链路与计数器（ip -s -j link，JSON）。可指定 iface 缩小范围。"
    input_schema = {
        "type": "object",
        "properties": {
            "iface": {
                "type": "string",
                "pattern": r"^[a-zA-Z0-9._@-]+$",
                "maxLength": 32,
                "description": "可选，限定网卡名（如 eth0）",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["ip", "-s", "-j", "link"]
        if iface := args.get("iface"):
            argv.extend(["show", "dev", iface])
        return argv


class NetAddrTool(Tool):
    name = "net_addr"
    description = "查看本机所有 IP 地址绑定（ip -j addr，JSON）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ip", "-j", "addr"]


class NetFirewallIptablesTool(Tool):
    name = "net_firewall_iptables"
    description = "查看 iptables 防火墙规则（iptables -L -n -v --line-numbers，需 root）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["iptables", "-L", "-n", "-v", "--line-numbers"]


class NetFirewallNftTool(Tool):
    name = "net_firewall_nft"
    description = "查看 nftables 完整规则集（nft list ruleset，需 root）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["nft", "list", "ruleset"]


class NetConnStateSummaryTool(Tool):
    name = "net_conn_state_summary"
    description = "统计 TCP 连接各状态计数（ss -ant 解析后聚合）。用于快速判断 TIME-WAIT/ESTAB 分布。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ss", "-ant"]

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if not res.ok:
            return res
        counts: dict[str, int] = {}
        lines = res.content.splitlines()
        # 跳过 header（首行通常 "State Recv-Q Send-Q Local Address:Port ..."）
        for line in lines[1:]:
            parts = line.split()
            if not parts:
                continue
            state = parts[0]
            counts[state] = counts.get(state, 0) + 1
        summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        res.content = summary or "(no connections)"
        res.data = dict(res.data)
        res.data["state_count"] = counts
        return res


class NetDnsResolveTool(Tool):
    name = "net_dns_resolve"
    description = "通过 getent 解析主机名为 IP（不发包，走 nsswitch）。用于验证 DNS/hosts 配置。"
    input_schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9._:-]+$",
                "maxLength": 253,
                "description": "主机名或 IP",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["getent", "hosts", "--", str(args["name"])]


class NetTcpStatsTool(Tool):
    name = "net_tcp_stats"
    description = "查看内核 TCP 协议栈总览统计（ss -s）。包含连接总数/TIME-WAIT 等。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ss", "-s"]


def register(registry: ToolRegistry) -> None:
    registry.register(SsListenTool())
    registry.register(SsConnTool())
    registry.register(PingTool())
    registry.register(NetRoutesTool())
    registry.register(NetArpTool())
    registry.register(NetLinkStatsTool())
    registry.register(NetAddrTool())
    registry.register(NetFirewallIptablesTool())
    registry.register(NetFirewallNftTool())
    registry.register(NetConnStateSummaryTool())
    registry.register(NetDnsResolveTool())
    registry.register(NetTcpStatsTool())
