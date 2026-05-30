"""Distribution-family detection for package tooling.

Kylin ships two main flavors:
  - Kylin Advanced Server V10/V11 — RHEL-derived, uses ``rpm`` + ``dnf``/``yum``
  - Kylin Desktop                   — Debian/Ubuntu-derived, uses ``dpkg`` + ``apt``

Each package tool needs to pick the right command per host. We detect once
per process by reading ``/etc/os-release`` (cheap, deterministic, no exec).

Design:
  * Pure read of ``/etc/os-release`` — no subprocess, no audit event.
  * Cached for the lifetime of the process; tests can monkey-patch
    ``detect_pkg_family`` or use ``set_pkg_family_for_tests``.
  * Tools call ``pkg_family()`` inside ``build_argv()`` to branch.

We deliberately do NOT make detection a subclass tree (no
``DnfPkgInfoTool`` / ``AptPkgInfoTool``) — that doubles tool count and
forces the LLM to know which to call. A single ``pkg_info`` whose argv
adapts is cleaner from the LLM's point of view.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path


class PkgFamily(str, Enum):
    """Package family detected on the host."""

    RPM = "rpm"  # dnf / yum, e.g. Kylin Server, CentOS, RHEL
    DPKG = "dpkg"  # apt / dpkg, e.g. Kylin Desktop, Ubuntu, Debian
    UNKNOWN = "unknown"


_OS_RELEASE_PATHS: tuple[str, ...] = ("/etc/os-release", "/usr/lib/os-release")

# Process-lifetime cache. ``None`` means "not yet detected".
_CACHED: PkgFamily | None = None


def _read_os_release() -> dict[str, str]:
    """Parse ``/etc/os-release`` into a plain dict.

    Returns an empty dict on any IO / parse error — callers fall back to
    ``PkgFamily.UNKNOWN`` and emit a friendly tool error.
    """
    for path in _OS_RELEASE_PATHS:
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
        return out
    return {}


def _classify(os_release: dict[str, str]) -> PkgFamily:
    """Map os-release dict to PkgFamily."""
    fields = " ".join(
        (
            os_release.get("ID", ""),
            os_release.get("ID_LIKE", ""),
            os_release.get("PRETTY_NAME", ""),
        )
    ).lower()
    # ID_LIKE is the most reliable signal; ID covers the rest.
    rpm_signals = ("rhel", "centos", "fedora", "openeuler", "anolis", "kylin")
    dpkg_signals = ("debian", "ubuntu")
    # Kylin Desktop self-identifies as ID=kylin but ID_LIKE=debian — check that first.
    if any(s in fields for s in dpkg_signals):
        return PkgFamily.DPKG
    if any(s in fields for s in rpm_signals):
        # Kylin Desktop also has 'kylin' in ID; the dpkg branch above already
        # caught the debian-derived flavor, so 'kylin' alone here means the
        # server (RHEL-derived) variant.
        return PkgFamily.RPM
    return PkgFamily.UNKNOWN


def detect_pkg_family() -> PkgFamily:
    """Detect the host's package family. Cached after first call."""
    global _CACHED
    if _CACHED is None:
        _CACHED = _classify(_read_os_release())
    return _CACHED


def pkg_family() -> PkgFamily:
    """Public accessor; alias of :func:`detect_pkg_family`."""
    return detect_pkg_family()


def set_pkg_family_for_tests(family: PkgFamily | None) -> None:
    """Override the cached family in tests. ``None`` clears the cache."""
    global _CACHED
    _CACHED = family


class PkgFamilyMixin:
    """Tool mixin that exposes ``self.family`` lazily.

    Inherit alongside :class:`Tool`. Use ``self.family`` inside
    ``build_argv()`` to fork between rpm / dpkg / unknown paths.
    """

    @property
    def family(self) -> PkgFamily:
        return pkg_family()

    def _require_known(self) -> PkgFamily:
        """Helper for build_argv: raise ToolError if family is UNKNOWN.

        Tools that genuinely can't run on the host should fail fast with a
        clear message rather than emit a confusing argv.
        """
        from kyagent.mcp.tools.base import ToolError

        f = self.family
        if f is PkgFamily.UNKNOWN:
            raise ToolError(
                "无法识别当前发行版的包管理器（缺 /etc/os-release ID/ID_LIKE 信号）"
            )
        return f
