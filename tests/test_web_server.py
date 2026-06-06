"""B/S 接入层（FastAPI）冒烟测试。

测试目标：
  * 路由表覆盖（health / tools / ask / safety / audit / 静态 index）
  * 静态文件存在且可被 build_app 找到
  * 路由响应模型 schema 不漂移（key set 锁定）
  * 不真正跑 LLM —— Agent.ask 用 monkeypatch 替身

整个测试在 ``fastapi`` 未安装时自动 SKIP，不阻塞默认 LoongArch 路径。
"""
from __future__ import annotations

import json
import importlib.util
import os
import threading
import time
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None:
    pytest.skip("fastapi 未安装（需 pip install -e .[web]）", allow_module_level=True)


from fastapi.testclient import TestClient  # noqa: E402

from kyagent.config import load_config  # noqa: E402
from kyagent.web.server import build_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    old_reviewer = os.environ.get("KYAGENT_WEB_REVIEWER_TOKEN")
    old_auditor = os.environ.get("KYAGENT_WEB_AUDITOR_TOKEN")
    os.environ["KYAGENT_WEB_REVIEWER_TOKEN"] = "test-reviewer-token"
    os.environ["KYAGENT_WEB_AUDITOR_TOKEN"] = "test-auditor-token"
    cfg = load_config(None)
    app = build_app(cfg)
    yield TestClient(app)
    if old_reviewer is None:
        os.environ.pop("KYAGENT_WEB_REVIEWER_TOKEN", None)
    else:
        os.environ["KYAGENT_WEB_REVIEWER_TOKEN"] = old_reviewer
    if old_auditor is None:
        os.environ.pop("KYAGENT_WEB_AUDITOR_TOKEN", None)
    else:
        os.environ["KYAGENT_WEB_AUDITOR_TOKEN"] = old_auditor


REVIEWER_HEADERS = {"Authorization": "Bearer test-reviewer-token"}
AUDITOR_HEADERS = {"Authorization": "Bearer test-auditor-token"}


def test_static_index_exists():
    assert (Path(__file__).parent.parent / "kyagent" / "web" / "static" / "index.html").is_file()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "version"}
    assert body["status"] == "ok"


def test_build_app_preflights_audit_store(monkeypatch, tmp_path):
    cfg = load_config(None)
    cfg.audit.database = str(tmp_path / "audit.db")

    def fail_preflight(_cfg):
        raise OSError("unable to open database file")

    monkeypatch.setattr("kyagent.web.server.build_audit_store", fail_preflight)

    with pytest.raises(RuntimeError, match="audit store is not writable"):
        build_app(cfg)


def test_tools_list_shape(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.json()
    assert "count" in body and "tools" in body
    assert body["count"] >= 19  # 至少包含原 19 个内置工具
    sample = body["tools"][0]
    assert {"name", "description", "risk", "requires_root", "read_only", "input_schema"} <= set(sample.keys())


def test_safety_check_intent_only(client):
    r = client.post("/api/safety/check", json={"text": "rm -rf /", "layer": "argv"})
    assert r.status_code == 200
    body = r.json()
    # 仅 argv 层裁决；intent 块为 null
    assert body["intent"] is None
    assert body["argv"] is not None
    assert body["argv"]["layer"] == "argv"
    assert body["argv"]["decision"] in ("allow", "confirm", "deny")


def test_safety_check_invalid_layer_rejected(client):
    r = client.post("/api/safety/check", json={"text": "ls", "layer": "bogus"})
    assert r.status_code == 422  # pydantic regex 校验失败


def test_ask_uses_threadpool_and_returns_trace(client, monkeypatch):
    """ask 路由用 monkeypatch 替身避免触发真实 LLM 调用。"""
    from kyagent.agent.core import Agent, AgentRunResult
    from kyagent.audit.trace import Trace

    def fake_ask(self, text: str, user: str = "anonymous"):
        return AgentRunResult(
            trace=Trace(user=user),
            final_text=f"echo: {text}",
            tool_iterations=0,
            denied=False,
            notes=["fake"],
        )

    monkeypatch.setattr(Agent, "ask", fake_ask)
    r = client.post("/api/ask", json={"text": "hello", "user": "tester"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "echo: hello"
    assert body["denied"] is False
    assert "trace_id" in body


def test_audit_404(client):
    r = client.get("/api/audit/traces/this-id-does-not-exist", headers=AUDITOR_HEADERS)
    assert r.status_code == 404


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    # 至少包含核心字段
    assert "kyagent" in r.text.lower()


def _sse_events(response):
    event = "message"
    data: list[str] = []
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line == "":
            if data:
                yield event, json.loads("".join(data))
            event = "message"
            data = []
            continue
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data.append(line[6:])


def test_stream_confirmation_roundtrip_allows_browser_decision(client, monkeypatch):
    """Web stream must surface high-risk confirm and wait for approve/reject."""
    from kyagent.agent.core import Agent, AgentRunResult
    from kyagent.audit.trace import Trace
    from kyagent.confirm import ConfirmRequest

    def fake_ask(self, text: str, user: str = "anonymous"):
        approved = self.confirm(
            ConfirmRequest(
                title="tool svc_restart",
                risk="high",
                summary_lines=["svc-restart-high (high): restart service"],
                body="systemctl restart sshd",
            )
        )
        return AgentRunResult(
            trace=Trace(user=user),
            final_text="approved" if approved else "denied",
            tool_iterations=1,
            denied=not approved,
            notes=[],
        )

    monkeypatch.setattr(Agent, "ask", fake_ask)

    events = []

    def consume_stream():
        with client.stream(
            "POST",
            "/api/ask/stream",
            json={"text": "restart sshd", "user": "tester", "session_id": "approval-test"},
        ) as resp:
            assert resp.status_code == 200
            events.extend(_sse_events(resp))

    t = threading.Thread(target=consume_stream, name="stream-consumer")
    t.start()

    approval = None
    deadline = time.time() + 5
    while time.time() < deadline:
        pending = client.get("/api/approvals", params={"status": "pending"}, headers=REVIEWER_HEADERS)
        assert pending.status_code == 200
        rows = pending.json()["approvals"]
        if rows:
            approval = rows[0]
            break
        time.sleep(0.02)

    assert approval is not None
    assert approval["risk"] == "high"
    assert approval["body"] == "systemctl restart sshd"
    assert approval["status"] == "pending"

    approve = client.post(
        f"/api/approvals/{approval['approval_id']}/approve",
        headers=REVIEWER_HEADERS,
        json={"reviewer": "tester", "reason": "demo approval"},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"
    t.join(timeout=5)
    assert not t.is_alive()

    approval_event = None
    resolved = None
    final = None
    for event, payload in events:
        if event == "approval_required":
            approval_event = payload
        if event == "approval_resolved":
            resolved = payload
        if event == "final":
            final = payload

    assert approval_event is not None
    assert approval_event["approval_id"] == approval["approval_id"]
    assert resolved is not None
    assert resolved["status"] == "approved"
    assert final is not None
    assert final["text"] == "approved"


def test_approvals_list_endpoint_exists(client):
    r = client.get("/api/approvals", headers=REVIEWER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"count", "approvals"}
    assert isinstance(body["approvals"], list)


def test_static_index_exposes_live_shell_review_ui():
    html = (Path(__file__).parent.parent / "kyagent" / "web" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "单文件前端（drop-in 替换 kyagent/web/static/index.html）" in html
    assert "telemetry-orb" in html
    assert "Anthropic / Claude 美学" in html
    assert "POST /api/ask/stream" in html
    assert "POST /api/sessions/{id}/reset" in html
    assert "POST /api/approvals/{id}/approve|reject" in html
    assert "POST /api/choices/{id}/select" in html
    assert "POST /api/safety/check" in html
    assert "GET  /api/audit/traces[/{id}]" in html
    assert "approval_required" in html
    assert "approval_resolved" in html
    assert "choice_required" in html
    assert "choice_resolved" in html
    assert "selectChoice" in html
    assert "apiHeaders" in html
    assert "approveApproval" in html
    assert "rejectApproval" in html
    assert ".msg.tool" in html and "var(--clay)" in html
    assert ".msg.thinking" in html and "spark-inline" in html
