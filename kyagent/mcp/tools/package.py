"""软件包查询工具：麒麟以 dnf/yum 为主，兼容 apt/dpkg/rpm。

注意：本工具只暴露查询接口，安装/卸载属于高风险，单独走 Guardrail confirm。
"""
from __future__ import annotations

import shutil
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry, ToolResult
from kyagent.mcp.tools.pkg_family import PkgFamily, PkgFamilyMixin
from kyagent.safety.patterns import RiskLevel


def _detect_pm() -> str:
    """优先选择 Kylin Server 可用的 dnf > yum > rpm。"""
    for name in ("dnf", "yum", "rpm"):
        if shutil.which(name):
            return name
    raise ToolError("未检测到可用的 RPM 包命令（需要 dnf/yum/rpm）")


def _detect_rpm_frontend() -> str:
    """Choose the available RPM repository frontend for read-only queries."""
    for name in ("dnf", "yum"):
        if shutil.which(name):
            return name
    raise ToolError("未检测到 RPM 仓库前端（需要 dnf 或 yum）")


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
        if pm == "rpm":
            return ["rpm", "-qi", name]
        raise ToolError("未检测到可用的 RPM 包命令（需要 dnf/yum/rpm）")


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
        if pm == "rpm":
            return ["rpm", "-qa"]  # 调用方可结合 grep；这里只暴露原始命令
        raise ToolError("未检测到可用的 RPM 包命令（需要 dnf/yum/rpm）")


# ---- 家族感知扩展工具（PkgFamilyMixin 自动切换 rpm/dpkg argv） -----------------
#
# 这些工具不再走旧的 _detect_pm()（基于 shutil.which），而是依据
# /etc/os-release 的 ID/ID_LIKE 一次性判定 PkgFamily —— 这是麒麟服务器版（rpm）
# 与麒麟桌面版（dpkg）共存场景下最稳定的判别方式。
#
# 注意 MRO：PkgFamilyMixin 必须在 Tool 之前，保证 ``self.family`` 不被覆盖。


class PkgVerifyTool(PkgFamilyMixin, Tool):
    name = "pkg_verify"
    description = "校验已安装包的文件完整性（检测配置漂移：rpm -V / debsums -c）。"
    input_schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9._+-]+$",
                "maxLength": 100,
            }
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        name = args["name"]
        if f is PkgFamily.RPM:
            return ["rpm", "-V", "--", name]
        # DPKG: debsums 需要单独安装，但属于桌面版常见审计工具
        return ["debsums", "-c", "--", name]


class PkgUpdatesTool(PkgFamilyMixin, Tool):
    name = "pkg_updates"
    description = "列出当前发行版可升级的软件包（dnf check-update / apt list --upgradable）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        if f is PkgFamily.RPM:
            return [_detect_rpm_frontend(), "check-update", "--quiet"]
        return ["apt", "list", "--upgradable"]

    def format_result(self, exec_result):  # type: ignore[override]
        # dnf check-update: returncode=100 表示"有可升级包"，是正常状态，不算失败。
        # 仅 RPM 路径需要这个语义豁免；DPKG 的 apt list 返回 0 即可走默认逻辑。
        if (
            self.family is PkgFamily.RPM
            and not exec_result.skipped_reason
            and not exec_result.timed_out
            and exec_result.returncode == 100
        ):
            return ToolResult(
                ok=True,
                content=exec_result.stdout,
                data=exec_result.to_dict(),
            )
        return super().format_result(exec_result)


class PkgSecurityUpdatesTool(PkgFamilyMixin, Tool):
    name = "pkg_security_updates"
    description = "列出安全相关的可升级包（dnf updateinfo list security；dpkg 系无原生 flag，退化为全部可升级）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        if f is PkgFamily.RPM:
            return [_detect_rpm_frontend(), "updateinfo", "list", "security"]
        # DPKG fallback: apt 没有原生的 security 过滤（需要 unattended-upgrades
        # 的 origin 配置才能区分）。这里退化为列出全部 upgradable，由调用方/LLM
        # 进一步判断。注释保留以提醒后续接入 ubuntu-security-notices。
        return ["apt", "list", "--upgradable"]


class PkgOwnsFileTool(PkgFamilyMixin, Tool):
    name = "pkg_owns_file"
    description = "反查文件归属于哪个软件包（rpm -qf / dpkg -S）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "pattern": r"^/[A-Za-z0-9._/@+-]+$",
                "minLength": 2,
                "maxLength": 300,
            }
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        path = args["path"]
        if f is PkgFamily.RPM:
            return ["rpm", "-qf", "--", path]
        return ["dpkg", "-S", "--", path]


class PkgRepoListTool(PkgFamilyMixin, Tool):
    name = "pkg_repo_list"
    description = "列出已配置的软件源（dnf repolist / apt-cache policy）。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        if f is PkgFamily.RPM:
            return [_detect_rpm_frontend(), "repolist"]
        return ["apt-cache", "policy"]


class PkgHistoryTool(PkgFamilyMixin, Tool):
    name = "pkg_history"
    description = "查看包管理操作历史（dnf history list / tail /var/log/apt/history.log）。"
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
            }
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    _DEFAULT_LIMIT = 30

    def __init__(self) -> None:
        # MCP server 串行执行同一工具实例；用实例字段把 build_argv 时刻看到的
        # limit 透传给 format_result（后者拿不到原始 args）。
        self._last_limit: int = self._DEFAULT_LIMIT

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        f = self._require_known()
        limit = int(args.get("limit", self._DEFAULT_LIMIT))
        self._last_limit = limit
        if f is PkgFamily.RPM:
            # dnf history list 不接受 --limit；这里只发命令，截断放到 format_result。
            return [_detect_rpm_frontend(), "history", "list"]
        # DPKG: tail -n <limit> 直接生效，且不依赖 root（apt 历史日志默认可读）。
        return ["tail", "-n", str(limit), "/var/log/apt/history.log"]

    def format_result(self, exec_result):  # type: ignore[override]
        result = super().format_result(exec_result)
        # 仅 RPM 路径需要 Python 端截行；DPKG 已由 tail 完成限幅。
        if (
            result.ok
            and self.family is PkgFamily.RPM
            and result.content
        ):
            lines = result.content.splitlines()
            if len(lines) > self._last_limit:
                result.content = "\n".join(lines[: self._last_limit])
        return result


def register(registry: ToolRegistry) -> None:
    registry.register(PkgInfoTool())
    registry.register(PkgInstalledTool())
    registry.register(PkgVerifyTool())
    registry.register(PkgUpdatesTool())
    registry.register(PkgSecurityUpdatesTool())
    registry.register(PkgOwnsFileTool())
    registry.register(PkgRepoListTool())
    registry.register(PkgHistoryTool())
