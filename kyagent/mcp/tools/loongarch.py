"""LoongArch 架构专属工具（赛题部署目标 = LoongArch + Kylin V11）。"""
from __future__ import annotations
from typing import Any
from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PATH_PATTERN = r"^/[A-Za-z0-9._/@+\-]+$"

_LA_CPUINFO_KEYS = (
    "CPU Family",
    "Model Name",
    "Revision",
    "BogoMIPS",
    "Features",
    "Machine",
)


# ---- 工具 1：CPU 架构信息 ----------------------------------------------------
class LaArchInfoTool(Tool):
    name = "la_arch_info"
    description = (
        "读取 /proc/cpuinfo 关键字段（CPU Family / Model Name / Revision / BogoMIPS 等）。"
        "用例：确认运行在 LoongArch 处理器（3A5000 / 3C5000 等）。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/proc/cpuinfo"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        kept: list[str] = []
        for line in out.content.splitlines():
            stripped = line.lstrip()
            for key in _LA_CPUINFO_KEYS:
                if stripped.startswith(key):
                    kept.append(line)
                    break
        out.content = "\n".join(kept)
        out.data["kept_lines"] = len(kept)
        return out


# ---- 工具 2：Old World / New World 判定 --------------------------------------
class LaWorldCheckTool(Tool):
    name = "la_world_check"
    description = (
        "判定 LoongArch 系统的 ABI 世界（old / new / mixed / unknown）。"
        "原理：同时检查 Old World 的 /lib64/ld.so.1 与 New World 的 "
        "/lib64/ld-linux-loongarch-lp64d.so.1。Old World 与 New World 是 LoongArch 生态的关键"
        "分水岭，二者 ABI / glibc 不兼容，影响二进制可执行性。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return [
            "ls",
            "-1",
            "/lib64/ld.so.1",
            "/lib64/ld-linux-loongarch-lp64d.so.1",
        ]

    def format_result(self, exec_result):  # type: ignore[override]
        # ``ls`` returns non-zero if either loader is absent. Parse the paths
        # it did find instead of turning every non-zero status into Old World.
        if exec_result.skipped_reason == "windows_mock":
            from kyagent.mcp.tools.base import ToolResult as _TR
            return _TR(ok=True, content=exec_result.stdout, data=exec_result.to_dict())
        if exec_result.skipped_reason:
            return super().format_result(exec_result)
        if exec_result.timed_out:
            return super().format_result(exec_result)

        from kyagent.mcp.tools.base import ToolResult as _TR
        old_loader = "/lib64/ld.so.1"
        new_loader = "/lib64/ld-linux-loongarch-lp64d.so.1"
        has_old = old_loader in exec_result.stdout
        has_new = new_loader in exec_result.stdout
        if has_old and has_new:
            verdict = "mixed"
        elif has_old:
            verdict = "old"
        elif has_new:
            verdict = "new"
        else:
            verdict = "unknown"
        body = f"verdict: {verdict}\n--- raw stdout ---\n{exec_result.stdout}"
        if exec_result.stderr:
            body += f"\n--- raw stderr ---\n{exec_result.stderr}"
        return _TR(
            ok=True,
            content=body,
            data={
                **exec_result.to_dict(),
                "world": verdict,
            },
        )


# ---- 工具 3：二进制兼容性 ----------------------------------------------------
class LaBinaryCompatTool(Tool):
    name = "la_binary_compat"
    description = (
        "用 file(1) 检查二进制文件的目标架构。"
        "用例：判断异架构二进制（x86_64 / aarch64）能否在 LoongArch 直接运行。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "pattern": _PATH_PATTERN,
                "maxLength": 300,
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["file", "--", args["path"]]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        # 提取 ELF 架构字符串：file 输出形如
        # "/bin/ls: ELF 64-bit LSB pie executable, LoongArch, ..."
        text = out.content
        arch = None
        candidates = (
            "loongarch64", "LoongArch",
            "x86-64", "x86_64",
            "aarch64", "ARM aarch64",
            "ARM",
            "RISC-V",
            "MIPS",
            "PowerPC",
        )
        lower = text.lower()
        for c in candidates:
            if c.lower() in lower:
                arch = c
                break
        if arch is not None:
            out.data["arch"] = arch
        return out


def register(registry: ToolRegistry) -> None:
    registry.register(LaArchInfoTool())
    registry.register(LaWorldCheckTool())
    registry.register(LaBinaryCompatTool())
