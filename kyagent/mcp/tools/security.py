"""安全态势检测工具组（赛题第 3 条"安全意图校验器" + 第 4 条"最小权限"）。

设计要点：
  - 全 read_only；写操作（如重置 SELinux）一概不放
  - 几条全盘扫描类工具（setuid、world-writable、capabilities）risk MEDIUM —— 因为
    `find /` 类操作可能造成 I/O 风暴或被 LLM 频繁调用
  - kysec 是麒麟（Kylin）特有的强制访问控制框架，是赛题加分项
"""
from __future__ import annotations
from typing import Any
from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.safety.patterns import RiskLevel


# ---- 工具 1：SELinux 状态 ----------------------------------------------------
class SecSelinuxStatusTool(Tool):
    name = "sec_selinux_status"
    description = (
        "查询 SELinux 状态（sestatus 包装）。"
        "用例：判断系统强制访问控制是否启用 / 处于 enforcing 模式。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["sestatus"]


# ---- 工具 2：AppArmor 状态 ---------------------------------------------------
class SecApparmorStatusTool(Tool):
    name = "sec_apparmor_status"
    description = (
        "查询 AppArmor 状态（aa-status 包装）。"
        "用例：列出已加载的配置文件及其模式（enforce/complain）。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["aa-status"]


# ---- 工具 3：KySec 状态（麒麟特有）------------------------------------------
class SecKysecStatusTool(Tool):
    name = "sec_kysec_status"
    description = (
        "查询麒麟 KySec 强制访问控制框架状态（读 /sys/kernel/security/kysec/state）。"
        "用例：赛题部署目标 = 麒麟 V11，本工具直接验证国产安全子系统启用情况；"
        "非麒麟环境路径不存在，返回非零退出并优雅降级为错误结果。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/sys/kernel/security/kysec/state"]


# ---- 工具 4：全盘扫描 SUID 文件 ----------------------------------------------
class SecSetuidFilesTool(Tool):
    name = "sec_setuid_files"
    description = (
        "全盘扫描 setuid 可执行文件（find / -perm -4000）。"
        "用例：盘点可能存在的本地提权面。注意：一次性扫描，避免短时多次调用。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.MEDIUM
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["find", "/", "-xdev", "-perm", "-4000", "-type", "f"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = out.content.splitlines()
            total = len(lines)
            out.content = "\n".join(lines[:100])
            out.data["count"] = total
        return out


# ---- 工具 5：世界可写文件 ----------------------------------------------------
class SecWorldWritableTool(Tool):
    name = "sec_world_writable"
    description = (
        "全盘扫描世界可写文件（find / -perm -0002）。"
        "用例：定位错误权限配置。一次性扫描，注意 I/O 开销。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.MEDIUM
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return [
            "find", "/", "-xdev", "-perm", "-0002", "-type", "f",
            "-not", "-path", "/proc/*", "-not", "-path", "/sys/*",
        ]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if out.ok:
            lines = out.content.splitlines()
            total = len(lines)
            out.content = "\n".join(lines[:100])
            out.data["count"] = total
        return out


# ---- 工具 6：文件 capabilities ----------------------------------------------
class SecCapabilitiesTool(Tool):
    name = "sec_capabilities"
    description = (
        "递归列出指定路径下设置了 file capabilities 的可执行文件（getcap -r）。"
        "用例：发现绕过 setuid 的细粒度权限授予。默认扫描 /usr。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": r"^/[A-Za-z0-9._/@+\-]*$",
                "maxLength": 200,
                "description": "扫描根路径，默认 /usr",
            },
        },
    }
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        path = args.get("path", "/usr")
        return ["getcap", "-r", path]


# ---- 工具 7：/etc/passwd 审计 ------------------------------------------------
class SecPasswdAuditTool(Tool):
    name = "sec_passwd_audit"
    description = (
        "审计 /etc/passwd 中的高风险账户：UID=0（root 同权）或登录 shell 以 sh 结尾。"
        "用例：发现隐藏 root 后门账户、规范化 nologin 配置。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["getent", "passwd"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        flagged: list[str] = []
        for line in out.content.splitlines():
            parts = line.split(":")
            if len(parts) < 7:
                continue
            uid = parts[2]
            shell = parts[6]
            if uid == "0" or shell.endswith("sh"):
                flagged.append(line)
        out.content = "\n".join(flagged)
        out.data["flagged_count"] = len(flagged)
        return out


# ---- 工具 8：sudoers 审计 ----------------------------------------------------
class SecSudoersAuditTool(Tool):
    name = "sec_sudoers_audit"
    description = (
        "审计指定用户的 sudo 授权（sudo -l -U USER）。"
        "用例：核查是否存在 NOPASSWD 全权 / 通配符滥用。"
        "默认 sudoers 仅允许审计部署运行账户自身；其他账户需人工执行。"
    )
    input_schema = {
        "type": "object",
        "required": ["user"],
        "properties": {
            "user": {
                "type": "string",
                "pattern": r"^[a-z_][a-z0-9_-]{0,31}$",
                "maxLength": 32,
            },
        },
    }
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["sudo", "-l", "-U", args["user"]]


# ---- 工具 9：sshd_config -----------------------------------------------------
class SecSshConfigTool(Tool):
    name = "sec_ssh_config"
    description = (
        "读取 /etc/ssh/sshd_config 的有效配置（剔除注释与空行）。"
        "用例：核查 PermitRootLogin / PasswordAuthentication 等关键项。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/etc/ssh/sshd_config"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        effective: list[str] = []
        for line in out.content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            effective.append(stripped)
        out.content = "\n".join(effective)
        out.data["effective_count"] = len(effective)
        return out


# ---- 工具 10：内核 taint 标记 ------------------------------------------------
# 参考 kernel/panic.c 中 TAINT_* 位定义
_TAINT_FLAGS = {
    0: "PROPRIETARY_MODULE",
    1: "FORCED_MODULE",
    2: "CPU_OUT_OF_SPEC",
    3: "FORCED_RMMOD",
    4: "MACHINE_CHECK",
    5: "BAD_PAGE",
    6: "USER",
    7: "DIE",
    8: "OVERRIDDEN_ACPI_TABLE",
    9: "WARN",
    10: "CRAP",
    11: "FIRMWARE_WORKAROUND",
    12: "OOT_MODULE",
    13: "UNSIGNED_MODULE",
    14: "SOFTLOCKUP",
    15: "LIVEPATCH",
    16: "AUX",
    17: "RANDSTRUCT",
    18: "TEST",
}


class SecKernelTaintsTool(Tool):
    name = "sec_kernel_taints"
    description = (
        "读取并解码 /proc/sys/kernel/tainted 位掩码。"
        "用例：检测内核是否加载了未签名模块、是否进入异常状态（机器检查、bad page 等）。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["cat", "/proc/sys/kernel/tainted"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        raw = out.content.strip()
        try:
            mask = int(raw)
        except ValueError:
            out.data["taint_flags"] = []
            return out
        flags: list[str] = []
        for bit, name in _TAINT_FLAGS.items():
            if mask & (1 << bit):
                flags.append(name)
        out.content = f"tainted={mask} flags={','.join(flags) if flags else 'NONE'}"
        out.data["taint_flags"] = flags
        out.data["taint_mask"] = mask
        return out


# ---- 工具 11：lsmod ----------------------------------------------------------
class SecKernelModulesTool(Tool):
    name = "sec_kernel_modules"
    description = (
        "列出已加载的内核模块（lsmod 包装）。"
        "用例：与 baseline 对比检测异常 LKM、rootkit。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["lsmod"]


# ---- 工具 12：监听外网接口的端口 ---------------------------------------------
class SecListeningExternalTool(Tool):
    name = "sec_listening_external"
    description = (
        "列出绑定到外网接口（0.0.0.0 或 [::]）的监听端口（ss -tlnp）。"
        "用例：发现意外暴露的服务面。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ss", "-tlnp"]

    def format_result(self, exec_result):  # type: ignore[override]
        out = super().format_result(exec_result)
        if not out.ok:
            return out
        kept: list[str] = []
        header_seen = False
        for line in out.content.splitlines():
            if not header_seen:
                kept.append(line)
                header_seen = True
                continue
            if "0.0.0.0" in line or "[::]" in line:
                kept.append(line)
        out.content = "\n".join(kept)
        out.data["external_count"] = max(len(kept) - 1, 0)
        return out


# ---- 工具 13：auditd 状态 ---------------------------------------------------
class SecAuditStatusTool(Tool):
    name = "sec_audit_status"
    description = (
        "查询 auditd 服务是否在运行（systemctl is-active auditd）。"
        "用例：合规检查（等保 2.0 要求开启审计）。"
    )
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["systemctl", "is-active", "auditd"]


def register(registry: ToolRegistry) -> None:
    registry.register(SecSelinuxStatusTool())
    registry.register(SecApparmorStatusTool())
    registry.register(SecKysecStatusTool())
    registry.register(SecSetuidFilesTool())
    registry.register(SecWorldWritableTool())
    registry.register(SecCapabilitiesTool())
    registry.register(SecPasswdAuditTool())
    registry.register(SecSudoersAuditTool())
    registry.register(SecSshConfigTool())
    registry.register(SecKernelTaintsTool())
    registry.register(SecKernelModulesTool())
    registry.register(SecListeningExternalTool())
    registry.register(SecAuditStatusTool())
