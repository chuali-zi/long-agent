from __future__ import annotations

import importlib.util
import threading
import time

import pytest

if importlib.util.find_spec("fastapi") is None:
    pytest.skip("fastapi is not installed", allow_module_level=True)

from fastapi.testclient import TestClient
from starlette.requests import Request

from kyagent.config import load_config
from kyagent.confirm import ConfirmRequest
from kyagent.interactive import UserChoice, UserChoiceOption
from kyagent.web.approvals import ApprovalBroker
from kyagent.web.server import _AgentSessionRegistry, build_app
from kyagent.web.security import UnsafeBindError, ensure_safe_bind
from kyagent.web.security import WebSecurity


@pytest.fixture
def cfg():
    return load_config(None)


def _set_tokens(monkeypatch):
    monkeypatch.setenv("KYAGENT_WEB_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("KYAGENT_WEB_REVIEWER_TOKEN", "reviewer-secret")
    monkeypatch.setenv("KYAGENT_WEB_AUDITOR_TOKEN", "auditor-secret")
    monkeypatch.setenv("KYAGENT_WEB_ADMIN_TOKEN", "admin-secret")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_minimal_and_docs_are_disabled(cfg):
    client = TestClient(build_app(cfg))
    assert client.get("/api/health").json().keys() == {"status", "version"}
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_default_local_dev_access_is_limited_to_non_sensitive_api(cfg):
    client = TestClient(build_app(cfg))
    assert client.get("/api/tools").status_code == 200
    assert client.get("/api/choices").status_code == 200
    assert client.get("/api/audit/traces").status_code == 401
    assert client.get("/api/approvals").status_code == 401


def test_require_auth_protects_all_api_and_enforces_roles(cfg, monkeypatch):
    _set_tokens(monkeypatch)
    monkeypatch.setenv("KYAGENT_WEB_REQUIRE_AUTH", "1")
    client = TestClient(build_app(cfg))

    assert client.get("/api/health").status_code == 401
    assert client.get("/api/tools", headers=_auth("operator-secret")).status_code == 200
    assert client.get("/api/audit/traces", headers=_auth("operator-secret")).status_code == 403
    assert client.get("/api/audit/traces", headers=_auth("auditor-secret")).status_code == 200
    assert client.get("/api/approvals", headers=_auth("reviewer-secret")).status_code == 200


def test_authenticated_ask_uses_token_role_instead_of_body_user(cfg, monkeypatch):
    from kyagent.agent.core import Agent, AgentRunResult
    from kyagent.audit.trace import Trace

    _set_tokens(monkeypatch)
    monkeypatch.setenv("KYAGENT_WEB_REQUIRE_AUTH", "1")
    seen: dict[str, str] = {}

    def fake_ask(self, text: str, user: str = "anonymous"):
        seen["user"] = user
        return AgentRunResult(trace=Trace(user=user), final_text="ok")

    monkeypatch.setattr(Agent, "ask", fake_ask)
    client = TestClient(build_app(cfg))
    response = client.post(
        "/api/ask",
        headers=_auth("operator-secret"),
        json={"text": "hello", "user": "spoofed-admin"},
    )

    assert response.status_code == 200
    assert seen["user"] == "operator"


def test_origin_requires_exact_same_origin_or_allowlist(cfg, monkeypatch):
    monkeypatch.setenv("KYAGENT_WEB_ALLOWED_ORIGINS", "https://console.example.test")
    client = TestClient(build_app(cfg))

    assert client.get("/api/tools", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/tools", headers={"Origin": "https://console.example.test"}).status_code == 200
    assert client.get("/api/tools", headers={"Origin": "https://console.example.test.evil"}).status_code == 403


def test_local_dev_mode_rejects_dns_rebinding_host(cfg):
    client = TestClient(build_app(cfg))

    assert client.get(
        "/api/tools",
        headers={"Host": "console.attacker.test", "Origin": "http://console.attacker.test"},
    ).status_code == 401


def test_non_loopback_bind_requires_explicit_authenticated_mode(monkeypatch):
    for name in (
        "KYAGENT_WEB_ALLOW_NON_LOOPBACK",
        "KYAGENT_WEB_REQUIRE_AUTH",
        "KYAGENT_WEB_OPERATOR_TOKEN",
        "KYAGENT_WEB_REVIEWER_TOKEN",
        "KYAGENT_WEB_AUDITOR_TOKEN",
        "KYAGENT_WEB_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(UnsafeBindError):
        ensure_safe_bind("0.0.0.0")

    monkeypatch.setenv("KYAGENT_WEB_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("KYAGENT_WEB_REQUIRE_AUTH", "1")
    _set_tokens(monkeypatch)
    ensure_safe_bind("0.0.0.0")


def test_allowlisted_cors_preflight_does_not_require_bearer_token():
    security = WebSecurity(
        require_auth=True,
        tokens={"operator": "secret"},
        allowed_origins=("https://console.example.test",),
    )
    request = Request({
        "type": "http",
        "method": "OPTIONS",
        "path": "/api/tools",
        "headers": [(b"origin", b"https://console.example.test")],
        "scheme": "https",
        "server": ("api.example.test", 443),
        "client": ("203.0.113.7", 1234),
    })
    assert security.check(request) is None


def test_session_id_format_is_bounded(cfg):
    client = TestClient(build_app(cfg))
    assert client.post("/api/ask", json={"text": "hello", "session_id": "../escape"}).status_code == 422
    assert client.post("/api/ask", json={"text": "hello", "session_id": "x" * 65}).status_code == 422
    assert client.post("/api/sessions/%24bad/reset").status_code == 422


class _FakeAgent:
    def __init__(self):
        self.messages = ["context"]
        self._run_lock = threading.Lock()
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


def test_session_registry_lru_and_ttl_evict_with_shutdown(cfg, monkeypatch):
    registry = _AgentSessionRegistry(cfg, max_sessions=2, ttl_seconds=0.01)
    created: list[_FakeAgent] = []

    def fresh():
        agent = _FakeAgent()
        created.append(agent)
        return agent

    monkeypatch.setattr(registry, "_fresh", fresh)
    first = registry.get_or_create("first")
    registry.get_or_create("second")
    registry.get_or_create("third")
    assert first.shutdown_calls == 1

    second = created[1]
    time.sleep(0.02)
    registry.get_or_create("fourth")
    assert second.shutdown_calls == 1


def test_session_registry_isolates_same_session_id_by_owner(cfg, monkeypatch):
    registry = _AgentSessionRegistry(cfg)
    created: list[_FakeAgent] = []

    def fresh():
        agent = _FakeAgent()
        created.append(agent)
        return agent

    monkeypatch.setattr(registry, "_fresh", fresh)
    operator_agent = registry.get_or_create("shared", owner="operator")
    reviewer_agent = registry.get_or_create("shared", owner="reviewer")

    assert operator_agent is not reviewer_agent


def test_reset_returns_busy_without_waiting(cfg, monkeypatch):
    registry = _AgentSessionRegistry(cfg)
    agent = _FakeAgent()
    monkeypatch.setattr(registry, "_fresh", lambda: agent)
    registry.get_or_create("busy")
    assert agent._run_lock.acquire(blocking=False)
    try:
        assert registry.reset("busy") == "busy"
    finally:
        agent._run_lock.release()


def _confirm() -> ConfirmRequest:
    return ConfirmRequest(title="danger", risk="high", summary_lines=["danger"], body="rm -rf /tmp/x")


def test_approval_broker_rejects_expired_and_bounds_records():
    broker = ApprovalBroker(timeout_seconds=0.01, max_records=2)
    expired = broker.create(_confirm(), session_id=None, user="web")
    time.sleep(0.02)
    resolved = broker.resolve(expired.approval_id, approved=True, reviewer="reviewer")
    assert resolved is not None
    assert resolved.status == "rejected"
    assert resolved.reason == "approval timeout"

    broker.create(_confirm(), session_id=None, user="web")
    broker.create(_confirm(), session_id=None, user="web")
    assert len(broker.list_records()) <= 2


def test_stream_worker_exception_emits_error_and_final(cfg, monkeypatch):
    from kyagent.agent.core import Agent

    def explode(self, text: str, user: str = "anonymous"):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(Agent, "ask", explode)
    client = TestClient(build_app(cfg))
    text = client.post("/api/ask/stream", json={"text": "boom", "session_id": "stream-error"}).text
    assert "event: error" in text
    assert "worker exploded" in text
    assert "event: final" in text


def test_choice_broker_api_roundtrip(cfg, monkeypatch):
    from kyagent.agent.core import Agent, AgentRunResult
    from kyagent.audit.trace import Trace

    def fake_ask(self, text: str, user: str = "anonymous"):
        selected = self.on_user_choice(
            UserChoice(
                question="restart now?",
                options=[
                    UserChoiceOption(value="yes", label="Yes"),
                    UserChoiceOption(value="no", label="No"),
                ],
            )
        )
        return AgentRunResult(trace=Trace(user=user), final_text=selected)

    monkeypatch.setattr(Agent, "ask", fake_ask)
    monkeypatch.setenv("KYAGENT_WEB_REVIEWER_TOKEN", "reviewer-secret")
    client = TestClient(build_app(cfg))
    events: list[str] = []

    def consume():
        events.append(client.post("/api/ask/stream", json={"text": "choose", "session_id": "choice"}).text)

    thread = threading.Thread(target=consume)
    thread.start()
    deadline = time.time() + 3
    choice = None
    while time.time() < deadline:
        rows = client.get("/api/choices", headers=_auth("reviewer-secret")).json().get("choices", [])
        if rows:
            choice = rows[0]
            break
        time.sleep(0.01)
    assert choice is not None
    response = client.post(
        f"/api/choices/{choice['choice_id']}/select",
        headers=_auth("reviewer-secret"),
        json={"value": "yes", "reviewer": "web"},
    )
    assert response.status_code == 200
    assert response.json()["reviewer"] == "reviewer"
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert "event: choice_required" in events[0]
    assert '"text": "yes"' in events[0]
