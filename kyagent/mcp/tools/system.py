"""系统态势感知工具：uptime / 内存 / CPU / 时钟 / 块设备等。

全部为只读、参数极简（多数无参），便于 LLM 串成"开机巡检"序列。
"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


class SysUptimeTool(Tool):
    name = "sys_uptime"
    description = "系统已运行时长 + 1/5/15 分钟负载（uptime）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["uptime"]


class SysLoadavgTool(Tool):
    name = "sys_loadavg"
    description = "读 /proc/loadavg（原始浮点 + 运行队列长度）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/proc/loadavg"]


class SysMemoryTool(Tool):
    name = "sys_memory"
    description = "内存 / 缓冲 / 交换 总览（free -h）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["free", "-h"]


class SysSwapTool(Tool):
    name = "sys_swap"
    description = "查看启用中的 swap 设备 / 文件（swapon --show）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["swapon", "--show"]


class SysKernelTool(Tool):
    name = "sys_kernel"
    description = "内核版本 + 主机名 + 架构（uname -a）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["uname", "-a"]


class SysCpuInfoTool(Tool):
    name = "sys_cpu_info"
    description = "CPU 拓扑 / 型号 / 缓存（lscpu）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lscpu"]


class SysDmiTool(Tool):
    name = "sys_dmi"
    description = "DMI/SMBIOS 系统产品型号（dmidecode，需 root）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["dmidecode", "-s", "system-product-name"]


class SysTimeSyncTool(Tool):
    name = "sys_time_sync"
    description = "时钟 / NTP 同步状态（timedatectl）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["timedatectl"]


class SysBlockDevicesTool(Tool):
    name = "sys_block_devices"
    description = "块设备树（lsblk -J，JSON 输出）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lsblk", "-J"]


def register(registry: ToolRegistry) -> None:
    registry.register(SysUptimeTool())
    registry.register(SysLoadavgTool())
    registry.register(SysMemoryTool())
    registry.register(SysSwapTool())
    registry.register(SysKernelTool())
    registry.register(SysCpuInfoTool())
    registry.register(SysDmiTool())
    registry.register(SysTimeSyncTool())
    registry.register(SysBlockDevicesTool())
