"""Deterministic preflight rules for destructive file cleanup tools."""
from __future__ import annotations

import os
import posixpath
import re
import stat
import time
from dataclasses import dataclass
from enum import Enum
from typing import Literal


RECENT_MODIFICATION_WINDOW_SECONDS = 60 * 60
WriteOperation = Literal["truncate", "delete"]


class WritePreflightDecision(str, Enum):
    DENY = "deny"
    ALLOW_CONFIRM = "allow-confirm"


@dataclass(frozen=True)
class PathMetadata:
    exists: bool
    is_regular_file: bool = False
    is_symlink: bool = False
    mtime: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class WritePreflightResult:
    decision: WritePreflightDecision
    rule_id: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is WritePreflightDecision.ALLOW_CONFIRM


def read_path_metadata(path: str) -> PathMetadata:
    """Best-effort lstat metadata for the final pre-execution decision."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return PathMetadata(exists=False)
    except OSError as exc:
        return PathMetadata(exists=True, error=exc.__class__.__name__)
    return PathMetadata(
        exists=True,
        is_regular_file=stat.S_ISREG(st.st_mode),
        is_symlink=stat.S_ISLNK(st.st_mode),
        mtime=st.st_mtime,
    )


def classify_write_preflight(
    path: str,
    *,
    operation: WriteOperation,
    metadata: PathMetadata | None = None,
    now: float | None = None,
    recent_window_seconds: int = RECENT_MODIFICATION_WINDOW_SECONDS,
) -> WritePreflightResult:
    """Classify a cleanup write as either hard deny or allow-confirm.

    The rules are intentionally path/metadata based and do not depend on LLM
    judgment.  ``allow-confirm`` still means the normal high-risk confirmation
    path must approve the tool before execution.
    """
    if operation not in {"truncate", "delete"}:
        return _deny("invalid-operation", f"unsupported write operation: {operation!r}")

    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        return _deny("relative-path", "target path must be absolute")

    meta = read_path_metadata(normalized) if metadata is None else metadata
    if meta.error:
        return _deny(
            "metadata-unreadable",
            f"could not inspect target metadata before {operation}: {meta.error}",
        )
    if meta.exists:
        if meta.is_symlink:
            return _deny("symlink-target", "target is a symlink")
        if not meta.is_regular_file:
            return _deny("not-regular-file", "target is not a regular file")

    lower_path = normalized.lower()
    basename = posixpath.basename(lower_path)
    segments = tuple(seg for seg in lower_path.split("/") if seg)

    sensitive_rule = _sensitive_log_rule(lower_path, basename, segments)
    if sensitive_rule is not None:
        rule_id, reason = sensitive_rule
        return _deny(rule_id, reason)

    if (
        _is_active_log_name(basename)
        and not _is_dated_log_name(basename)
        and not _is_stale_named_log(lower_path, basename)
        and not _is_temp_build_residual(lower_path, basename, segments)
        and not _is_core_dump_residual(lower_path, basename, segments)
    ):
        return _deny("active-log", "active .log files are not cleanup targets")

    if not meta.exists:
        return _deny("target-not-found", "target does not exist for preflight inspection")

    if (
        _is_recent(meta, now=now, recent_window_seconds=recent_window_seconds)
        and not _allows_recent_cleanup(lower_path, basename, segments)
    ):
        minutes = max(0.0, ((now if now is not None else time.time()) - (meta.mtime or 0)) / 60.0)
        return _deny(
            "recently-modified",
            f"target was modified {minutes:.1f} minutes ago; "
            f"minimum age is {recent_window_seconds // 60} minutes",
        )

    if _is_rotated_log_name(basename):
        return _allow("old-rotated-log", "rotated or compressed log target")

    if _is_stale_cache_target(lower_path, basename, segments):
        return _allow("stale-cache-target", "stale cache file under /var/cache")

    if lower_path.startswith("/var/cache/"):
        return _allow("cache-target", "cache file under /var/cache")

    if _is_stale_named_log(lower_path, basename):
        return _allow("stale-named-log", "stale-named log target")

    if _is_temp_build_residual(lower_path, basename, segments):
        return _allow("temp-build-residual", "temporary build/cache residual")

    if _is_core_dump_residual(lower_path, basename, segments):
        return _allow("temp-core-dump", "temporary core/dump residual")

    if _is_dated_log_name(basename):
        return _allow("dated-log", "dated log target")

    if _is_active_log_name(basename):
        return _deny("active-log", "active .log files are not cleanup targets")

    return _deny(
        "unclassified-cleanup-target",
        "target is not an old rotated log, cache file, or temporary build residual",
    )


def _deny(rule_id: str, reason: str) -> WritePreflightResult:
    return WritePreflightResult(WritePreflightDecision.DENY, rule_id, reason)


def _allow(rule_id: str, reason: str) -> WritePreflightResult:
    return WritePreflightResult(WritePreflightDecision.ALLOW_CONFIRM, rule_id, reason)


def _is_recent(
    metadata: PathMetadata,
    *,
    now: float | None,
    recent_window_seconds: int,
) -> bool:
    if not metadata.exists or metadata.mtime is None:
        return False
    reference = time.time() if now is None else now
    return reference - metadata.mtime < recent_window_seconds


def _allows_recent_cleanup(
    lower_path: str,
    basename: str,
    segments: tuple[str, ...],
) -> bool:
    """Recent files are normally protected; only disposable scopes bypass it."""
    return lower_path.startswith("/var/cache/") or _is_temp_build_residual(
        lower_path, basename, segments
    )


def _sensitive_log_rule(
    lower_path: str,
    basename: str,
    segments: tuple[str, ...],
) -> tuple[str, str] | None:
    if basename == "audit.log" or basename.startswith("audit.log."):
        return ("audit-log", "audit.log* is protected")
    if basename == "wtmp" or basename.startswith("wtmp."):
        return ("login-record-log", "wtmp login records are protected")
    if basename == "btmp" or basename.startswith("btmp."):
        return ("login-record-log", "btmp login records are protected")
    if basename == "secure" or basename.startswith("secure."):
        return ("auth-log", "secure authentication logs are protected")
    if basename.startswith("mysql-bin."):
        return ("database-log", "mysql binary logs are protected")

    if "/audit/" in lower_path or "audit" in segments:
        return ("audit-log", "audit log paths are protected")

    auth_names = (
        "auth.log",
        "faillog",
        "lastlog",
        "tallylog",
        "sudo.log",
        "sshd.log",
    )
    if basename in auth_names or any(basename.startswith(name + ".") for name in auth_names):
        return ("auth-log", "authentication logs are protected")

    db_markers = (
        "mysql",
        "mysqld",
        "mariadb",
        "postgres",
        "postgresql",
        "mongodb",
        "mongod",
        "redis",
        "oracle",
    )
    if any(marker in segments for marker in db_markers):
        return ("database-log", "database log paths are protected")
    if basename.startswith(tuple(marker + "." for marker in db_markers)):
        return ("database-log", "database logs are protected")
    if basename.startswith(tuple(marker + "-" for marker in db_markers)):
        return ("database-log", "database logs are protected")

    return None


def _is_active_log_name(basename: str) -> bool:
    return basename.endswith(".log")


_DATED_LOG_RE = re.compile(
    r"(?:^|[-_.])(?:20\d{2}(?:[-_.]?\d{2}){0,2}|\d{8})(?:[-_.]|$)"
)


def _is_dated_log_name(basename: str) -> bool:
    return basename.endswith(".log") and bool(_DATED_LOG_RE.search(basename))


def _is_rotated_log_name(basename: str) -> bool:
    if basename.endswith((".gz", ".xz", ".zst", ".old")):
        return True
    suffix = basename.rsplit(".", 1)[-1]
    return suffix.isdigit()


def _is_stale_named_log(lower_path: str, basename: str) -> bool:
    if not basename.endswith(".log"):
        return False
    if not lower_path.startswith(("/var/log/", "/tmp/", "/var/tmp/")):
        return False
    return any(marker in basename for marker in ("stale", "old", "archive"))


def _is_stale_cache_target(
    lower_path: str,
    basename: str,
    segments: tuple[str, ...],
) -> bool:
    if not lower_path.startswith("/var/cache/"):
        return False
    if basename.endswith((".cache", ".metadata", ".meta")):
        return True
    if basename in {"metadata", "metadata.cache", "http.cache"}:
        return True
    return any(seg in {"cache", ".cache"} for seg in segments[2:])


def _is_temp_build_residual(
    lower_path: str,
    basename: str,
    segments: tuple[str, ...],
) -> bool:
    if not (lower_path.startswith("/tmp/") or lower_path.startswith("/var/tmp/")):
        return False
    if basename.endswith((".tmp", ".temp", ".bak", ".part", ".cache")):
        return True
    if basename.startswith(("tmp", "temp", "build-")):
        return True
    if "spool" in basename:
        return True
    residual_segments = {
        ".cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "cache",
        "dist",
        "target",
        "tmp",
        "temp",
    }
    app_segments = _app_path_segments(lower_path, segments)
    if any(seg in residual_segments for seg in app_segments):
        return True
    return any(
        seg.startswith(("pip-build", "build-", "tmp-", "temp-"))
        for seg in app_segments
    )


def _is_core_dump_residual(
    lower_path: str,
    basename: str,
    segments: tuple[str, ...],
) -> bool:
    if not (lower_path.startswith("/tmp/") or lower_path.startswith("/var/tmp/")):
        return False
    if "core" in segments or "dump" in segments:
        return True
    return (
        ".core" in basename
        or basename.startswith(("core.", "dump."))
        or basename.endswith((".core", ".dump", ".core.txt", ".dump.txt"))
    )


def _app_path_segments(lower_path: str, segments: tuple[str, ...]) -> tuple[str, ...]:
    if lower_path.startswith("/var/tmp/"):
        return segments[2:]
    if lower_path.startswith("/tmp/"):
        return segments[1:]
    return segments
