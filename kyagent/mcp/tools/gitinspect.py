"""Read-only Git inspection tools."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_SAFE_REF = re.compile(r"^[A-Za-z0-9._/@:+-]{1,160}$")
_SAFE_REPO = re.compile(r"^[A-Za-z0-9_./:\\ -]{1,260}$")


def _safe_repo(value: str | None) -> str:
    repo = (value or ".").strip()
    if not repo or "\0" in repo or not _SAFE_REPO.fullmatch(repo):
        raise ToolError("repo path contains forbidden characters")
    if repo.startswith("-"):
        raise ToolError("repo path must not start with '-'")
    return repo


def _safe_ref(value: str | None, default: str = "HEAD") -> str:
    ref = (value or default).strip()
    if ref.startswith("-") or not _SAFE_REF.fullmatch(ref) or ".." in ref:
        raise ToolError("unsafe git revision/ref")
    return ref


def _safe_path(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    path = value.strip().replace("\\", "/")
    if path.startswith("/") or path.startswith("-") or "\0" in path:
        raise ToolError("git path must be relative and must not start with '-'")
    p = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in p.parts):
        raise ToolError("git path must not contain empty, '.', or '..' segments")
    if re.search(r"[;&|`$<>]", path):
        raise ToolError("git path contains shell metacharacters")
    return path


def _git_prefix(repo: str) -> list[str]:
    return [
        "git",
        "--no-pager",
        "-c", "core.pager=cat",
        "-c", "color.ui=false",
        "-c", "protocol.file.allow=never",
        "-C", repo,
    ]


class GitStatusTool(Tool):
    name = "git_status"
    description = "Read git status for a repository without mutating it."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": 260},
        },
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        return _git_prefix(_safe_repo(args.get("repo"))) + [
            "status", "--short", "--branch",
        ]


class GitDiffTool(GitStatusTool):
    name = "git_diff"
    description = "Read git diff for a repository, optionally scoped to one relative path."
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": 260},
            "path": {"type": "string", "maxLength": 240},
        },
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        argv = _git_prefix(_safe_repo(args.get("repo"))) + ["diff", "--"]
        path = _safe_path(args.get("path"))
        if path:
            argv.append(path)
        return argv


class GitLogTool(GitStatusTool):
    name = "git_log"
    description = "Read recent git commit log entries."
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": 260},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        limit = int(args.get("limit", 20))
        return _git_prefix(_safe_repo(args.get("repo"))) + [
            "log", "--oneline", "-n", str(limit),
        ]


class GitShowTool(GitStatusTool):
    name = "git_show"
    description = "Read one git revision with stat and patch."
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": 260},
            "rev": {"type": "string", "maxLength": 160},
        },
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        return _git_prefix(_safe_repo(args.get("repo"))) + [
            "show", "--stat", "--patch", _safe_ref(args.get("rev")),
        ]


class GitBlameTool(GitStatusTool):
    name = "git_blame"
    description = "Read git blame for one relative file path."
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": 260},
            "path": {"type": "string", "minLength": 1, "maxLength": 240},
            "rev": {"type": "string", "maxLength": 160},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        return _git_prefix(_safe_repo(args.get("repo"))) + [
            "blame", _safe_ref(args.get("rev")), "--", _safe_path(args.get("path")) or "",
        ]


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitShowTool())
    registry.register(GitBlameTool())
    return registry
