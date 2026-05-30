"""B/S 接入层（FastAPI）冒烟测试。

测试目标：
  * 路由表覆盖（health / tools / ask / safety / audit / 静态 index）
  * 静态文件存在且可被 build_app 找到
  * 路由响应模型 schema 不漂移（key set 锁定）
  * 不真正跑 LLM —— Agent.ask 用 monkeypatch 替身

整个测试在 ``fastapi`` 未安装时自动 SKIP，不阻塞默认 LoongArch 路径。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None:
    pytest.skip("fastapi 未安装（需 pip install -e .[web]）", allow_module_level=True)


from fastapi.testclient import TestClient  # noqa: E402

from kyagent.config import load_config  # noqa: E402
from kyagent.web.server import build_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    cfg = load_config(None)
    app = build_app(cfg)
    return TestClient(app)


def test_static_index_exists():
    assert (Path(__file__).parent.parent / "kyagent" / "web" / "static" / "index.html").is_file()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "version", "backend", "tools", "audit_db"}
    assert body["status"] == "ok"


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
    r = client.get("/api/audit/traces/this-id-does-not-exist")
    assert r.status_code == 404


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    # 至少包含核心字段
    assert "kyagent" in r.text.lower()
