"""文件系统感知工具：df / du / ls / find（只读）。"""
from __future__ import annotations

import posixpath
from typing import Any

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel
from kyagent.safety.write_preflight import WriteOperation, categorize_cleanup_candidate, classify_write_preflight


_PROTECTED_READ = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}
_ABS_PATH_PATTERN = r"^/(?!.*[\x00-\x1f\x7f])"
_GLOBAL_STORAGE_ROOTS = frozenset({"/var/log", "/var/cache", "/var/tmp", "/tmp"})


def _safe_path(p: str) -> str:
    if not p:
        raise ToolError("path 不能为空")
    if not p.startswith("/"):
        raise ToolError(f"path 必须是绝对路径: {p!r}")
    # 目标系统是 Linux/麒麟，统一用 posix 归一化（避免 Windows 开发态把 / 翻成 \）
    p = posixpath.normpath(p)
    if any(c in p for c in [";", "|", "&", "$", "`", "\n"]):
        raise ToolError(f"非法路径字符: {p!r}")
    if p in _PROTECTED_READ:
        raise ToolError(f"路径 {p} 在工具层禁读名单内")
    return p


def _require_scoped_storage_path(p: str, *, tool_name: str) -> str:
    """Reject global /var/log|cache|tmp roots — scope to a service subdirectory."""
    path = _safe_path(p)
    if path in _GLOBAL_STORAGE_ROOTS:
        raise ToolError(
            f"{tool_name} 请指定服务子目录（如 /var/log/<service>），"
            f"不要扫描全局根 {path}"
        )
    return path


class DfTool(Tool):
    name = "fs_df"
    description = "查看挂载点磁盘使用情况（df -h）。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN, "description": "可选，限定某挂载点"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        argv = ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"]
        if p := args.get("path"):
            argv.extend(["--", _safe_path(p)])
        return argv


class DuTool(Tool):
    name = "fs_du"
    description = "目录占用统计（du -sh / -ah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN},
            "depth": {"type": "integer", "minimum": 1, "maximum": 5, "description": "默认 1"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        depth = int(args.get("depth", 1))
        return ["du", "-h", f"--max-depth={depth}", "--", _safe_path(args["path"])]


class LsTool(Tool):
    name = "fs_ls"
    description = "列出目录内容（ls -lah）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string", "pattern": _ABS_PATH_PATTERN}},
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["ls", "-lah", "--color=never", "--", _safe_path(args["path"])]


class FindTool(Tool):
    name = "fs_find"
    description = "在指定目录下按文件名 / 修改时间过滤（find 包装，强制不走 -exec）。"
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "pattern": _ABS_PATH_PATTERN},
            "name": {"type": "string", "description": "glob，如 '*.log'"},
            "mtime_days": {"type": "integer", "description": "最近 N 天内修改"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "description": "默认 3"},
        },
    }
    risk_level = RiskLevel.LOW

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        p = _safe_path(args["path"])
        max_depth = int(args.get("max_depth", 3))
        argv = ["find", p, "-maxdepth", str(max_depth)]
        if name := args.get("name"):
            if any(c in name for c in [";", "|", "&", "$", "`", "\n"]):
                raise ToolError(f"非法 name 模式: {name!r}")
            argv.extend(["-name", name])
        if (md := args.get("mtime_days")) is not None:
            argv.extend(["-mtime", f"-{int(md)}"])
        argv.extend(["-type", "f"])
        # 显式禁止 -exec / -delete 等
        return argv


class FileCleanupCandidatesTool(Tool):
    name = "file_cleanup_candidates"
    description = (
        "发现指定服务目录下的可清理文件候选（只读）。"
        "返回 path/size/mtime/suffix/category_guess/risk_markers 等结构化事实；"
        "LLM 据此标注 delete/protect/unknown，不要直接删除。"
        "必须传入服务子目录（如 /var/log/auth-api01），不要扫描 /var/log 等全局根。"
    )
    input_schema = {
        "type": "object",
        "required": ["root"],
        "properties": {
            "root": {
                "type": "string",
                "pattern": _ABS_PATH_PATTERN,
                "description": "服务子目录，如 /var/log/auth-api01",
            },
            "service_hint": {
                "type": "string",
                "maxLength": 80,
                "description": "可选服务名提示，用于输出",
            },
            "min_age_days": {
                "type": "integer",
                "minimum": 0,
                "maximum": 3650,
                "description": "仅返回修改时间早于 N 天的文件；0 表示不限",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "默认 4",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "最多返回候选数，默认 80",
            },
        },
    }
    risk_level = RiskLevel.LOW
    read_only = True

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        cleaned["root"] = _require_scoped_storage_path(
            cleaned["root"], tool_name=self.name
        )
        cleaned["max_depth"] = int(cleaned.get("max_depth", 4))
        cleaned["limit"] = int(cleaned.get("limit", 80))
        cleaned["min_age_days"] = int(cleaned.get("min_age_days", 0))
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        root = args["root"]
        max_depth = int(args.get("max_depth", 4))
        argv = [
            "find",
            root,
            "-maxdepth",
            str(max_depth),
            "-type",
            "f",
            "-printf",
            "%s\\t%T@\\t%p\\n",
        ]
        min_age_days = int(args.get("min_age_days", 0))
        if min_age_days > 0:
            argv.extend(["-mtime", f"+{min_age_days}"])
        return argv

    def format_result(self, exec_result):  # type: ignore[override]
        res = super().format_result(exec_result)
        if not res.ok:
            return res
        limit = int((exec_result.extra.get("tool_args") or {}).get("limit", 80))
        candidates: list[dict[str, Any]] = []
        for line in res.content.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                size = int(parts[0])
                mtime = float(parts[1])
            except ValueError:
                continue
            path = parts[2]
            facts = categorize_cleanup_candidate(path, mtime=mtime, size=size)
            suffix = facts.suffix
            if "." in posixpath.basename(path):
                suffix = "." + posixpath.basename(path).rsplit(".", 1)[-1]
            entry = {
                "path": facts.path,
                "size": size,
                "mtime": mtime,
                "suffix": suffix,
                "file_type": facts.file_type,
                "category_guess": facts.category_guess,
                "risk_markers": list(facts.risk_markers),
            }
            candidates.append(entry)
        candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        candidates = candidates[:limit]
        lines = [
            (
                f"{item['size']:>12}  {item['category_guess']:<12}  "
                f"{item['path']}  markers={','.join(item['risk_markers']) or '-'}"
            )
            for item in candidates
        ]
        res.content = (
            f"candidate_count={len(candidates)}\n"
            + ("\n".join(lines) if lines else "(no candidates)")
        )
        res.data = dict(res.data)
        res.data["candidates"] = candidates
        res.data["candidate_count"] = len(candidates)
        return res


_MUTATION_ALLOWED_PREFIXES = (
    "/var/log/",
    "/var/cache/",
    "/var/tmp/",
    "/tmp/",
)


def _enforce_write_preflight(path: str, operation: WriteOperation, tool_name: str) -> None:
    result = classify_write_preflight(path, operation=operation)
    if not result.allowed:
        raise ToolError(
            f"{tool_name} preflight denied ({result.rule_id}): {result.reason}"
        )


class FsTruncateTool(Tool):
    name = "fs_truncate"
    description = (
        "将日志/缓存文件就地清空（truncate -s 0，保留 inode 与文件句柄，安全回收空间）。"
        "仅限 /var/log、/var/cache、/var/tmp、/tmp 下；高风险需确认。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 300,
                "description": "目标文件绝对路径，仅允许 /var/log、/var/cache、/var/tmp、/tmp 下",
            }
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        p = _safe_path(cleaned["path"])
        if not any(p.startswith(prefix) for prefix in _MUTATION_ALLOWED_PREFIXES):
            raise ToolError(
                "fs_truncate 仅允许清空 /var/log、/var/cache、/var/tmp、/tmp 下的文件"
            )
        _enforce_write_preflight(p, "truncate", self.name)
        cleaned["path"] = p
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        # 不再裸调 truncate；走 sudoers 授权的包装器，由它在 OS 层做
        # realpath 规范化 + 白名单根目录 + O_NOFOLLOW 校验（防 .. 越界与符号链接）。
        return ["kyagent-log-clean", args["path"]]


class FsDeleteFileTool(Tool):
    name = "fs_delete_file"
    description = (
        "删除单个日志/缓存/临时普通文件（安全 unlink 包装器）。"
        "仅限 /var/log、/var/cache、/var/tmp、/tmp 下；不递归、不跟随符号链接；高风险需确认。"
    )
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 300,
                "description": "目标文件绝对路径，仅允许 /var/log、/var/cache、/var/tmp、/tmp 下",
            }
        },
    }
    risk_level = RiskLevel.HIGH
    requires_root = True
    read_only = False

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        cleaned = super().validate(args)
        p = _safe_path(cleaned["path"])
        if not any(p.startswith(prefix) for prefix in _MUTATION_ALLOWED_PREFIXES):
            raise ToolError(
                "fs_delete_file 仅允许删除 /var/log、/var/cache、/var/tmp、/tmp 下的单个文件"
            )
        _enforce_write_preflight(p, "delete", self.name)
        cleaned["path"] = p
        return cleaned

    def build_argv(self, args: dict[str, Any]) -> list[str]:
        return ["kyagent-file-delete", args["path"]]


def register(registry: ToolRegistry) -> None:
    registry.register(DfTool())
    registry.register(DuTool())
    registry.register(LsTool())
    registry.register(FindTool())
    registry.register(FileCleanupCandidatesTool())
    registry.register(FsTruncateTool())
    registry.register(FsDeleteFileTool())
