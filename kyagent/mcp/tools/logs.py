"""日志感知工具：journalctl / dmesg。"""
from __future__ import annotations

from typing import Any

from kyagent.mcp.tools.base import Tool, ToolRegistry
from kyagent.mcp.tools.filesystem import _safe_path
from kyagent.safety.patterns import RiskLevel
from kyagent.safety.write_preflight import classify_write_preflight


_PRIORITIES = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}
_JOURNAL_UNIT_PATTERN = r"^(?!-)[A-Za-z0-9@._\-+:]+$"
_JOURNAL_SINCE_PATTERN = r"^(?!-)[A-Za-z0-9 :+.\-]+$"
_NOT_OPTION_PATTERN = r"^(?!-)"


class JournalctlTool(Tool):
    name = "log_journal"
    description = "查询 systemd journal 日志（journalctl 包装，只读，支持 --since / --priority / --unit）。"
    input_schema = {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "maxLength": 50,
                "pattern": _JOURNAL_SINCE_PATTERN,
                "description": "时间窗，如 '10 min ago' / '2025-05-16 10:00'",
            },
            "priority": {
                "type": "string",
                "enum": ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"],
                "description": "最低优先级过滤",
            },
            "unit": {
                "type": "string",
                "pattern": _JOURNAL_UNIT_PATTERN,
                "description": "限定 systemd unit（如 sshd.service）",
            },
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "默认 100"},
            "grep": {
                "type": "string",
                "maxLength": 200,
                "pattern": _NOT_OPTION_PATTERN,
                "description": "正则过滤（journalctl --grep）",
            },
        },
    }
    risk_level = RiskLevel.LOW

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        if grep := cleaned.get("grep"):
            _validate_grep_pattern(grep, allow_pipe=True)
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        from kyagent.mcp.tools.base import ToolError
        argv = ["journalctl", "--no-pager"]
        if since := args.get("since"):
            argv.extend(["--since", since])
        if pri := args.get("priority"):
            pri_lower = pri.lower()
            if pri_lower not in _PRIORITIES:
                raise ToolError(f"priority {pri!r} 无效，允许值: {sorted(_PRIORITIES)}")
            argv.extend(["-p", pri_lower])
        if unit := args.get("unit"):
            argv.extend(["-u", unit])
        lines = int(args.get("lines", 100))
        argv.extend(["-n", str(lines)])
        if g := args.get("grep"):
            argv.extend(["--grep", g])
        return argv


class DmesgTool(Tool):
    name = "log_dmesg"
    description = "读取内核环形缓冲日志（dmesg --human --color=never）。"
    input_schema = {
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "pattern": r"^(?:emerg|alert|crit|err|warn|notice|info|debug)(?:,(?:emerg|alert|crit|err|warn|notice|info|debug))*$",
                "maxLength": 100,
                "description": "如 err,warn",
            },
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
    }
    risk_level = RiskLevel.LOW

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        self._lines = int(cleaned.get("lines", 200))
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["dmesg", "--human", "--color=never"]
        if lvl := args.get("level"):
            argv.extend(["-l", lvl])
        return argv

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if res.ok:
            n = getattr(self, "_lines", 200)
            lines = res.content.splitlines()
            res.content = "\n".join(lines[-n:])
        return res


_GREP_FORBIDDEN = set("`$|;&(){}<>\n\r")


def _validate_grep_pattern(p: str, *, allow_pipe: bool = False) -> None:
    """白名单清洁：禁止 shell 元字符出现在 LLM 传入的 grep 模式中。

    虽然我们走 exec(no-shell)，但 pattern 串可能被 LLM 后续误塞进其他命令；
    在工具边界提前拒绝可降低组合性风险。
    """
    forbidden = _GREP_FORBIDDEN - {"|"} if allow_pipe else _GREP_FORBIDDEN
    bad = [ch for ch in p if ch in forbidden]
    if bad:
        from kyagent.mcp.tools.base import ToolError
        raise ToolError(f"pattern 含非法字符: {bad!r}")


class LogFilesTopTool(Tool):
    name = "log_files_top"
    description = (
        "扫描指定日志目录下 >1MB 的文件并按大小倒排（默认前 20）。"
        "优先传入服务子目录（如 /var/log/auth-api01），避免扫描整个 /var/log。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": r"^/[A-Za-z0-9._/@-]+$",
                "maxLength": 200,
                "description": "日志目录，默认 /var/log；建议限定到 /var/log/<service>",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "默认 20"},
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        path = args.get("path", "/var/log")
        return [
            "find", path, "-type", "f", "-size", "+1M",
            "-printf", "%s\t%T@\t%p\n",
        ]

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if not res.ok:
            return res
        # 暂存 args 不可得；limit 由 validate 时已 coerce 但 format 阶段拿不到。
        # 简化：固定 top-20；调用方传 limit 时由 build_argv 之外无入口——
        # 通过把 limit 暂存到 self._limit 解决。
        limit = getattr(self, "_limit", 20)
        rows = []
        for line in res.content.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                size = int(parts[0])
                mtime = float(parts[1])
            except ValueError:
                continue
            rows.append({"size": size, "mtime": mtime, "path": parts[2]})
        rows.sort(key=lambda r: r["size"], reverse=True)
        rows = rows[:limit]
        res.content = "\n".join(f"{r['size']:>12}  {r['path']}" for r in rows) or "(no files)"
        res.data = dict(res.data)
        res.data["files"] = rows
        return res

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        self._limit = int(cleaned.get("limit", 20))
        if "path" in cleaned:
            cleaned["path"] = _safe_path(cleaned["path"])
        return cleaned


class LogSizeSampleTool(Tool):
    name = "log_size_sample"
    description = "对给定绝对路径批量 du -sb 一次性采样字节大小。LLM 可两次调用自行算增量速率。"
    input_schema = {
        "type": "object",
        "required": ["paths"],
        "properties": {
            "paths": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "pattern": r"^/[A-Za-z0-9._/@-]+$",
                    "maxLength": 200,
                },
                "description": "绝对路径列表，限定字符集（建议 /var/log 或 /var/lib 下）",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        paths = list(args["paths"])
        return ["du", "-sb", "--"] + paths


class LogGrepRecentTool(Tool):
    name = "log_grep_recent"
    description = "在最近时间窗内 journalctl --grep 搜索关键字。pattern 走严格白名单。"
    input_schema = {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {
                "type": "string",
                "maxLength": 200,
                "description": "搜索正则；禁止 shell 元字符 `$|;&(){}<>` 与换行",
            },
            "since": {
                "type": "string",
                "maxLength": 50,
                "pattern": r"^[A-Za-z0-9 :+-]+$",
                "description": "时间窗，默认 '1 hour ago'",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        _validate_grep_pattern(cleaned["pattern"])
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        since = str(args.get("since", "1 hour ago"))
        return ["journalctl", "--no-pager", "--since", since, "--grep", str(args["pattern"])]


class LogSshAuditTool(Tool):
    name = "log_ssh_audit"
    description = "拉取 sshd.service 在指定时间窗内的所有 journal 日志。默认 24 小时。"
    input_schema = {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "maxLength": 50,
                "pattern": r"^[A-Za-z0-9 :+-]+$",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        since = str(args.get("since", "24 hours ago"))
        return ["journalctl", "-u", "sshd.service", "--no-pager", "--since", since]


class LogAuthFailedTool(Tool):
    name = "log_auth_failed"
    description = "搜索鉴权失败事件（authentication failure / Failed password / invalid user）。"
    input_schema = {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "maxLength": 50,
                "pattern": r"^[A-Za-z0-9 :+-]+$",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        since = str(args.get("since", "24 hours ago"))
        return [
            "journalctl", "--no-pager", "--since", since,
            "--grep", "authentication failure|Failed password|invalid user",
        ]


class LogAuditSummaryTool(Tool):
    name = "log_audit_summary"
    description = "auditd 审计日志汇总报告（aureport --summary）。需 auditd 安装；走 sudoers。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = True

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["aureport", "--summary"]


class LogRotatedCountTool(Tool):
    name = "log_rotated_count"
    description = "枚举 /var/log 下已轮转的日志文件（*.gz / *.1 / *.2）。返回前 50 个 + 计数。"
    input_schema = {"type": "object", "properties": {}}
    risk_level = RiskLevel.LOW
    read_only = True
    requires_root = False

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return [
            "find", "/var/log", "-type", "f",
            "(", "-name", "*.gz", "-o", "-name", "*.1", "-o", "-name", "*.2", ")",
        ]

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if not res.ok:
            return res
        files = [ln for ln in res.content.splitlines() if ln.strip()]
        head = files[:50]
        res.content = f"rotated_count={len(files)}\n" + "\n".join(head)
        res.data = dict(res.data)
        res.data["count"] = len(files)
        res.data["sample"] = head
        return res


class LogVacuumTool(Tool):
    name = "log_vacuum"
    description = (
        "回收 systemd journal 磁盘占用（journalctl --vacuum-size / --vacuum-time）。"
        "变更类操作，需安全护栏确认。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "max_size": {
                "type": "string",
                "pattern": r"^[0-9]+[KMGT]$",
                "description": "保留大小上限，如 '500M'",
            },
            "max_age": {
                "type": "string",
                "pattern": r"^[0-9]+(s|min|h|days|weeks|months|years)$",
                "description": "保留时长上限，如 '2weeks'",
            },
        },
    }
    risk_level = RiskLevel.MEDIUM
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        from kyagent.mcp.tools.base import ToolError
        cleaned = super().validate(args)
        has_size = "max_size" in cleaned
        has_age = "max_age" in cleaned
        if not has_size and not has_age:
            raise ToolError("log_vacuum 需指定 max_size 或 max_age 之一")
        if has_size and has_age:
            raise ToolError("log_vacuum 需指定 max_size 或 max_age 之一")
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        if "max_size" in args:
            return ["journalctl", f"--vacuum-size={args['max_size']}"]
        return ["journalctl", f"--vacuum-time={args['max_age']}"]


_LOG_DELETE_SUFFIXES = (
    ".log",
    ".old",
    ".gz",
    ".xz",
    ".zst",
    ".1",
    ".2",
    ".3",
    ".4",
    ".5",
    ".6",
    ".7",
    ".8",
    ".9",
)


class LogDeleteFileTool(Tool):
    name = "log_delete_file"
    description = (
        "删除 /var/log 下单个普通日志文件或轮转日志文件。"
        "不递归、不接受 glob、不跟随符号链接；高风险需确认。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 300,
                "description": "目标日志文件绝对路径，限定 /var/log 下",
            }
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        from kyagent.mcp.tools.base import ToolError
        cleaned = super().validate(args)
        p = _safe_path(cleaned["path"])
        if not p.startswith("/var/log/"):
            raise ToolError("log_delete_file 仅允许删除 /var/log 下的单个日志文件")
        preflight = classify_write_preflight(p, operation="delete")
        if not preflight.allowed:
            raise ToolError(
                f"log_delete_file preflight denied ({preflight.rule_id}): "
                f"{preflight.reason}"
            )
        if not p.endswith(_LOG_DELETE_SUFFIXES):
            raise ToolError(
                "log_delete_file 仅允许删除常见日志/轮转日志后缀: "
                + ", ".join(_LOG_DELETE_SUFFIXES)
            )
        cleaned["path"] = p
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-file-delete", args["path"]]


def register(registry: ToolRegistry) -> None:
    registry.register(JournalctlTool())
    registry.register(DmesgTool())
    registry.register(LogFilesTopTool())
    registry.register(LogSizeSampleTool())
    registry.register(LogGrepRecentTool())
    registry.register(LogSshAuditTool())
    registry.register(LogAuthFailedTool())
    registry.register(LogAuditSummaryTool())
    registry.register(LogRotatedCountTool())
    registry.register(LogVacuumTool())
    registry.register(LogDeleteFileTool())
