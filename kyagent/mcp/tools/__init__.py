"""内置 MCP 工具集合。"""
from kyagent.mcp.tools.base import Tool, ToolRegistry, ToolResult, RiskLevel
from kyagent.mcp.tools import (
    process,
    network,
    logs,
    service,
    filesystem,
    package,
    interactive,
)


def register_builtin(registry: ToolRegistry) -> ToolRegistry:
    """把所有内置工具注册到 registry。"""
    process.register(registry)
    network.register(registry)
    logs.register(registry)
    service.register(registry)
    filesystem.register(registry)
    package.register(registry)
    # ask_user_choice 是纯逻辑工具（不走 ExecutionProxy），默认就注册——
    # LLM 不会主动叫，没用到时零成本。
    interactive.register(registry)
    return registry


def default_registry() -> ToolRegistry:
    return register_builtin(ToolRegistry())


__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "RiskLevel",
    "register_builtin",
    "default_registry",
]
