"""内置 MCP 工具集合。"""
from kyagent.mcp.tools.base import Tool, ToolRegistry, ToolResult, RiskLevel, TrendTool
from kyagent.mcp.tools import (
    process,
    network,
    logs,
    service,
    filesystem,
    package,
    disk,
    system,
    security,
    compliance,
    loongarch,
    interactive,
    gitinspect,
    validation,
    webfacts,
    docintake,
    planstate,
)


def register_builtin(registry: ToolRegistry) -> ToolRegistry:
    """把所有内置工具注册到 registry。"""
    # 基础感知
    process.register(registry)
    network.register(registry)
    logs.register(registry)
    service.register(registry)
    filesystem.register(registry)
    package.register(registry)
    # 扩展感知（v2 工具拓展）
    disk.register(registry)
    system.register(registry)
    # 安全 / 合规 / LoongArch 专属（赛题关键场景）
    security.register(registry)
    compliance.register(registry)
    loongarch.register(registry)
    gitinspect.register(registry)
    validation.register(registry)
    webfacts.register(registry)
    docintake.register(registry)
    planstate.register(registry)
    # ask_user_choice 是纯逻辑工具（不走 ExecutionProxy），默认就注册——
    # LLM 不会主动叫，没用到时零成本。
    interactive.register(registry)
    return registry


def default_registry() -> ToolRegistry:
    return register_builtin(ToolRegistry())


__all__ = [
    "Tool",
    "TrendTool",
    "ToolRegistry",
    "ToolResult",
    "RiskLevel",
    "register_builtin",
    "default_registry",
]
