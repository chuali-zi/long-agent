"""软件包查询工具：麒麟以 dnf/yum 为主，兼容 apt/dpkg/rpm。

注意：本工具只暴露查询接口，安装/卸载属于高风险，单独走 Guardrail confirm。
"""
from __future__ import annotations

import shutil
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


def _detect_pm() -> str:
    """优先 dnf > yum > apt > rpm > dpkg。"""
    for name in ("dnf", "yum", "apt", "rpm", "dpkg"):
        if shutil.which(name):
            return name
    return "dnf"  # 默认 dnf（麒麟主流）


class PkgInfoTool(Tool):
    name = "pkg_info"
    description = "查询单个软件包信息（dnf info / apt show / rpm -qi 自动适配）。"
    input_schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        name = args["name"]
        if any(c in name for c in [";", "|", "&", "$", "`", " "]):
            raise ToolError(f"非法包名: {name!r}")
        pm = _detect_pm()
        if pm in ("dnf", "yum"):
            return [pm, "info", name]
        if pm == "apt":
            return ["apt", "show", name]
        if pm == "rpm":
            return ["rpm", "-qi", name]
        if pm == "dpkg":
            return ["dpkg", "-s", name]
        raise ToolError("未检测到可用的包管理器")


class PkgInstalledTool(Tool):
    name = "pkg_installed"
    description = "列出已安装包，可按关键字过滤（dnf list installed / dpkg -l 适配）。"
    input_schema = {
        "type": "object",
        "properties": {"filter": {"type": "string", "description": "可选关键字（不支持 shell 元字符）"}},
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        keyword = args.get("filter", "")
        if keyword and any(c in keyword for c in [";", "|", "&", "$", "`", " "]):
            raise ToolError(f"非法过滤字符: {keyword!r}")
        pm = _detect_pm()
        if pm in ("dnf", "yum"):
            argv = [pm, "list", "installed"]
            if keyword:
                argv.append(keyword)
            return argv
        if pm == "apt":
            return ["apt", "list", "--installed"] + ([keyword] if keyword else [])
        if pm == "rpm":
            return ["rpm", "-qa"]  # 调用方可结合 grep；这里只暴露原始命令
        if pm == "dpkg":
            return ["dpkg", "-l"]
        raise ToolError("未检测到可用的包管理器")


def register(registry: ToolRegistry) -> None:
    registry.register(PkgInfoTool())
    registry.register(PkgInstalledTool())
