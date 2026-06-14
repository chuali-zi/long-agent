"""Opt-in loading for third-party MCP tool entry points."""
from __future__ import annotations

from importlib import metadata
from typing import Any

from kyagent.mcp.tools import register_builtin
from kyagent.mcp.tools.base import ToolRegistry


ENTRY_POINT_GROUP = "kyagent.mcp_tools"


def _entry_points() -> list[Any]:
    points = metadata.entry_points()
    if hasattr(points, "select"):
        return list(points.select(group=ENTRY_POINT_GROUP))
    if isinstance(points, dict):
        return list(points.get(ENTRY_POINT_GROUP, []))
    return [point for point in points if getattr(point, "group", None) == ENTRY_POINT_GROUP]


def _load_plugin(registry: ToolRegistry, point: Any) -> None:
    """Load one plugin into a staging registry, then merge it atomically."""
    staged = ToolRegistry()
    register = point.load()
    register(staged)
    names = staged.names()
    if any(registry.get(name) is not None for name in names):
        raise ValueError(f"plugin {point.name!r} registers a duplicate tool")
    for tool in staged.all():
        registry.register(tool)


def configured_registry(cfg: Any) -> ToolRegistry:
    """Build built-ins plus explicitly allowlisted plugins, then filter tools."""
    registry = register_builtin(ToolRegistry())
    allowed = set(getattr(cfg.mcp, "plugin_entry_points", []))
    for point in _entry_points():
        if point.name not in allowed:
            continue
        try:
            _load_plugin(registry, point)
        except Exception:  # noqa: BLE001 - a broken optional plugin must not break built-ins
            continue
    return registry.enable_tools(getattr(cfg.mcp, "enable_tools", []))
