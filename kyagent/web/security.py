"""Conservative HTTP security defaults for the optional Web console."""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


_ROLE_ENV = {
    "operator": "KYAGENT_WEB_OPERATOR_TOKEN",
    "reviewer": "KYAGENT_WEB_REVIEWER_TOKEN",
    "auditor": "KYAGENT_WEB_AUDITOR_TOKEN",
    "admin": "KYAGENT_WEB_ADMIN_TOKEN",
}


class UnsafeBindError(ValueError):
    """Raised when the Web console would be exposed without explicit auth."""


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_safe_bind(host: str) -> None:
    """Fail closed before exposing the Web console beyond loopback."""
    if _is_loopback(host):
        return
    if not _enabled(os.environ.get("KYAGENT_WEB_ALLOW_NON_LOOPBACK")):
        raise UnsafeBindError(
            "non-loopback bind requires KYAGENT_WEB_ALLOW_NON_LOOPBACK=1"
        )
    if not _enabled(os.environ.get("KYAGENT_WEB_REQUIRE_AUTH")):
        raise UnsafeBindError(
            "non-loopback bind requires KYAGENT_WEB_REQUIRE_AUTH=1"
        )
    missing = [name for name in _ROLE_ENV.values() if not os.environ.get(name)]
    if missing:
        raise UnsafeBindError(
            "non-loopback bind requires role tokens: " + ", ".join(missing)
        )


@dataclass(frozen=True)
class WebSecurity:
    require_auth: bool
    tokens: dict[str, str]
    allowed_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "WebSecurity":
        origins = tuple(
            origin.strip()
            for origin in os.environ.get("KYAGENT_WEB_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        return cls(
            require_auth=_enabled(os.environ.get("KYAGENT_WEB_REQUIRE_AUTH")),
            tokens={role: os.environ.get(name, "") for role, name in _ROLE_ENV.items()},
            allowed_origins=origins,
        )

    def role_for_request(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        supplied = header[7:]
        matched = None
        for role, expected in self.tokens.items():
            if expected and secrets.compare_digest(supplied, expected):
                matched = role
        return matched

    def check(self, request: Request) -> JSONResponse | None:
        if not request.url.path.startswith("/api/"):
            return None
        if not self._origin_allowed(request):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        if request.method == "OPTIONS":
            return None

        role = self.role_for_request(request)
        roles = self._required_roles(request.url.path)
        if roles is None:
            return None
        if role is None:
            if not self.require_auth and self._local_dev_allowed(request):
                return None
            return JSONResponse(
                {"detail": "bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if role not in roles:
            return JSONResponse({"detail": "insufficient role"}, status_code=403)
        return None

    def _required_roles(self, path: str) -> set[str] | None:
        if path == "/api/health" and not self.require_auth:
            return None
        if path.startswith("/api/audit/"):
            return {"auditor", "admin"}
        if path.startswith("/api/approvals") or path.startswith("/api/choices"):
            return {"reviewer", "admin"}
        return {"operator", "admin"}

    def _local_dev_allowed(self, request: Request) -> bool:
        path = request.url.path
        dev_path = (
            path == "/api/tools"
            or path == "/api/ask"
            or path == "/api/ask/stream"
            or path == "/api/safety/check"
            or path.startswith("/api/sessions/")
        )
        return (
            dev_path
            and _is_loopback(request.client.host if request.client else "")
            and _is_loopback(request.url.hostname or "")
        )

    def _origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return True
        same_origin = f"{request.url.scheme}://{request.url.netloc}"
        return origin == same_origin or origin in self.allowed_origins


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "testclient", "testserver"}:  # TestClient uses an in-process scope.
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
