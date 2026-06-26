"""Dynamic remediation scope extracted from user natural language."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

_ABS_PATH_RE = re.compile(r"/[A-Za-z0-9._@%+=:,~-]+(?:/[A-Za-z0-9._@%+=:,~-]+)*")
_SERVICE_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9-]*[a-z][0-9]{2}\b", re.IGNORECASE)

_RESOURCE_KEYWORDS: dict[str, frozenset[str]] = {
    "log": frozenset({"log", "日志", "journal", "归档", "archive", "rotated", "轮转"}),
    "cache": frozenset({"cache", "缓存", "stale cache", "metadata.cache"}),
    "tmp": frozenset({"tmp", "temp", "临时", "dump", "core", "残留"}),
    "port": frozenset({"port", "端口", "listen", "监听", "占用"}),
    "process": frozenset({"process", "进程", "pid", "cpu", "占用", "kill", "终止"}),
    "cron": frozenset({"cron", "计划任务", "定时", "crontab"}),
}

_ACTION_KEYWORDS: dict[str, frozenset[str]] = {
    "cleanup": frozenset({"cleanup", "clean up", "清理", "清除", "删除", "回收", "泄漏", "leak", "spill"}),
    "disable": frozenset({"disable", "禁用", "停用", "关闭入口"}),
    "terminate": frozenset({"terminate", "kill", "stop", "终止", "结束", "杀", "释放"}),
    "repair": frozenset({"repair", "fix", "修复", "收紧", "权限"}),
}

_PROTECTED_KEYWORDS: dict[str, frozenset[str]] = {
    "current-log": frozenset({"current", "当前", "正在写", "active", "live", "业务日志"}),
    "audit": frozenset({"audit", "审计", "取证", "forensic", "incident", "review", "合规"}),
    "comparison": frozenset({"对照", "comparison", "保留", "不要动", "protected", "keep"}),
    "access-log": frozenset({"access.log", "访问日志", "nginx access"}),
}

_STORAGE_BASES = ("/var/log", "/var/cache", "/var/tmp", "/tmp")
# Keep aligned with kyagent.mcp.tools.filesystem._GLOBAL_STORAGE_ROOTS — tools reject these.
_GLOBAL_STORAGE_ROOTS = frozenset({"/var/log", "/var/cache", "/var/tmp", "/tmp"})
_STORAGE_RESOURCES = frozenset({"log", "cache", "tmp"})
_NON_FILE_RESOURCES = frozenset({"port", "process", "cron"})
_FILE_LIKE_SUFFIX_RE = re.compile(
    r"\.(?:sock|lock|log(?:\.\d+)?|gz|bz2|xz|zst|core|dump|pid|tmp|old|bak|jsonl?)$",
    re.IGNORECASE,
)
_ACTION_INTENT_RE = re.compile(
    r"(结束|终止|杀|释放|处理|处置|清理|修复|"
    r"stop|kill|terminate|release|remediate|cleanup|clean up|resolve|"
    r"确认后结束|确认后终止)",
    re.IGNORECASE,
)


def normalize_abs_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        return ""
    return posixpath.normpath(path)


def path_is_within(path: str, root: str) -> bool:
    path = normalize_abs_path(path)
    root = normalize_abs_path(root)
    return bool(path and root and (path == root or path.startswith(root.rstrip("/") + "/")))


def extract_absolute_paths(text: str) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw in _ABS_PATH_RE.findall(text or ""):
        path = normalize_abs_path(raw.rstrip(".,;:)]}'\""))
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return tuple(paths)


def _match_keywords(text: str, table: dict[str, frozenset[str]]) -> frozenset[str]:
    lowered = (text or "").lower()
    matched: set[str] = set()
    for label, keywords in table.items():
        if any(
            kw.lower() in lowered
            and not (label == "cleanup" and _keyword_is_negated(lowered, kw.lower()))
            for kw in keywords
        ):
            matched.add(label)
    return frozenset(matched)


def _keyword_is_negated(text: str, keyword: str) -> bool:
    start = 0
    found = False
    while True:
        idx = text.find(keyword, start)
        if idx < 0:
            return found
        found = True
        prefix = text[max(0, idx - 12):idx]
        if not any(
            marker in prefix
            for marker in ("不要", "别", "不得", "禁止", "do not", "don't", "not ")
        ):
            return False
        start = idx + len(keyword)


def _extract_services(text: str) -> tuple[str, ...]:
    services: list[str] = []
    seen: set[str] = set()
    for token in _SERVICE_TOKEN_RE.findall(text or ""):
        normalized = token.lower()
        if normalized not in seen:
            seen.add(normalized)
            services.append(normalized)
    return tuple(services)


def _looks_like_file_path(path: str) -> bool:
    """Heuristic: explicit paths in tickets are usually directories, not scan roots."""
    base = posixpath.basename(path)
    if not base or base in {".", ".."}:
        return False
    if _FILE_LIKE_SUFFIX_RE.search(base):
        return True
    return "." in base and not base.startswith(".")


def _explicit_storage_roots(paths: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = normalize_abs_path(raw)
        if not path or path in _GLOBAL_STORAGE_ROOTS:
            continue
        if _looks_like_file_path(path):
            path = posixpath.dirname(path) or path
        if path in _GLOBAL_STORAGE_ROOTS:
            continue
        for base in _STORAGE_BASES:
            if path == base or path.startswith(base + "/"):
                if path not in seen:
                    seen.add(path)
                    roots.append(path)
                break
    return tuple(roots)


def _explicit_root_storage_files(paths: tuple[str, ...]) -> tuple[str, ...]:
    files: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = normalize_abs_path(raw)
        if not path or not _looks_like_file_path(path):
            continue
        parent = posixpath.dirname(path) or path
        if parent not in _GLOBAL_STORAGE_ROOTS:
            continue
        for base in _STORAGE_BASES:
            if path.startswith(base + "/") and path not in seen:
                seen.add(path)
                files.append(path)
                break
    return tuple(files)


def _collapse_nested_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Keep only minimal directory roots — scanning a parent covers children."""
    ordered = sorted(set(roots), key=len)
    kept: list[str] = []
    for root in ordered:
        if any(path_is_within(root, parent) for parent in kept):
            continue
        kept.append(root)
    return tuple(kept)


def _service_storage_roots(
    services: tuple[str, ...],
    resource_types: frozenset[str],
    *,
    include_tmp_root: bool = False,
) -> tuple[str, ...]:
    if not services:
        return ()
    bases: tuple[str, ...]
    if resource_types:
        selected: list[str] = []
        if "log" in resource_types:
            selected.append("/var/log")
        if "cache" in resource_types:
            selected.append("/var/cache")
        if "tmp" in resource_types:
            selected.append("/var/tmp")
            if include_tmp_root:
                selected.append("/tmp")
        bases = tuple(selected) if selected else _STORAGE_BASES[:3]
    else:
        bases = _STORAGE_BASES[:3]
    roots: list[str] = []
    seen: set[str] = set()
    for service in services:
        for base in bases:
            root = normalize_abs_path(f"{base}/{service}")
            if root and root not in seen:
                seen.add(root)
                roots.append(root)
    return tuple(roots)


@dataclass
class RemediationScope:
    """Scope model for a single remediation turn."""

    services: tuple[str, ...] = ()
    resource_types: frozenset[str] = field(default_factory=frozenset)
    actions: frozenset[str] = field(default_factory=frozenset)
    protected_hints: frozenset[str] = field(default_factory=frozenset)
    explicit_paths: tuple[str, ...] = ()
    search_round: int = 1

    @classmethod
    def from_user_text(cls, text: str) -> RemediationScope:
        explicit_paths = extract_absolute_paths(text)
        services = _extract_services(text)
        resource_types = _match_keywords(text, _RESOURCE_KEYWORDS)
        actions = _match_keywords(text, _ACTION_KEYWORDS)
        protected_hints = _match_keywords(text, _PROTECTED_KEYWORDS)
        if not actions and _ACTION_INTENT_RE.search(text or ""):
            actions = frozenset({"cleanup"})
        return cls(
            services=services,
            resource_types=resource_types,
            actions=actions,
            protected_hints=protected_hints,
            explicit_paths=explicit_paths,
        )

    def round1_roots(self) -> tuple[str, ...]:
        """Paths explicitly mentioned or tied to named services + resource types."""
        roots = list(_explicit_storage_roots(self.explicit_paths))
        seen = set(roots)
        for root in _service_storage_roots(
            self.services, self.resource_types, include_tmp_root=False
        ):
            if root not in seen:
                seen.add(root)
                roots.append(root)
        if not roots and self.services and (self.actions or self.resource_types):
            for root in _service_storage_roots(
                self.services,
                frozenset({"log", "cache", "tmp"}),
                include_tmp_root=False,
            ):
                if root not in seen:
                    seen.add(root)
                    roots.append(root)
        return _collapse_nested_roots(tuple(roots))

    def round2_roots(self) -> tuple[str, ...]:
        """Expand to common runtime roots for the same services."""
        if not self.services:
            return ()
        expanded: list[str] = []
        seen = set(self.round1_roots())
        for service in self.services:
            for root in _service_storage_roots(
                (service,),
                frozenset({"log", "cache", "tmp"}),
                include_tmp_root=True,
            ):
                if root not in seen:
                    seen.add(root)
                    expanded.append(root)
        return tuple(expanded)

    def search_roots(self, *, round: int | None = None) -> tuple[str, ...]:
        active_round = self.search_round if round is None else round
        if active_round <= 1:
            return self.round1_roots()
        merged = list(self.round1_roots())
        seen = set(merged)
        for root in self.round2_roots():
            if root not in seen:
                seen.add(root)
                merged.append(root)
        return tuple(merged)

    def root_for_path(self, path: str) -> str | None:
        path = normalize_abs_path(path)
        if not path:
            return None
        for root in self.search_roots(round=2):
            if path_is_within(path, root):
                return root
        return None

    def path_in_scope(self, path: str) -> bool:
        return self.root_for_path(path) is not None

    def explicit_root_storage_files(self) -> tuple[str, ...]:
        return _explicit_root_storage_files(self.explicit_paths)

    def summary(self) -> str:
        parts = [
            f"services={','.join(self.services) or 'none'}",
            f"resource_types={','.join(sorted(self.resource_types)) or 'none'}",
            f"actions={','.join(sorted(self.actions)) or 'none'}",
            f"protected={','.join(sorted(self.protected_hints)) or 'none'}",
            f"roots_r1={','.join(self.round1_roots()) or 'none'}",
        ]
        return "; ".join(parts)


def file_remediation_checklist_applies(scope: RemediationScope) -> bool:
    """Whether the file-delete candidate checklist should gate this turn."""
    storage = scope.resource_types & _STORAGE_RESOURCES
    if not storage:
        return False
    actions = scope.actions
    if actions & {"disable", "terminate"}:
        return False
    if (
        "process" in scope.resource_types
        and not scope.services
        and not (scope.resource_types & {"log", "cache"})
    ):
        return False
    if "cleanup" in actions:
        return True
    if "repair" in actions:
        return False
    return bool(scope.services and storage and not (actions & _NON_FILE_RESOURCES))


def file_cleanup_required_roots(user_text: str) -> list[str]:
    """Backward-compatible helper used by the remediation checklist."""
    scope = RemediationScope.from_user_text(user_text)
    if not file_remediation_checklist_applies(scope):
        return []
    return list(scope.search_roots(round=1))
