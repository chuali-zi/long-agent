from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest

from kyagent.mcp.server import McpServer
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import Tool, ToolRegistry


def _cfg(**mcp_overrides):
    mcp = {
        "server_name": "test-server",
        "server_version": "1.0",
        "enable_tools": [],
        "plugin_entry_points": [],
    }
    mcp.update(mcp_overrides)
    return SimpleNamespace(mcp=SimpleNamespace(**mcp))


def _server(*, registry=None, audit=None):
    return McpServer(
        _cfg(),
        registry or ToolRegistry(),
        guardrail=SimpleNamespace(),
        executor=SimpleNamespace(),
        audit=audit or SimpleNamespace(),
    )


def _init(req_id=1, version="2024-11-05"):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }


@pytest.mark.parametrize(
    "bad_message",
    [
        [],
        "text",
        1,
        None,
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 123},
    ],
)
def test_invalid_request_returns_32600_and_server_continues(monkeypatch, bad_message):
    stdin = StringIO(
        json.dumps(bad_message) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    )
    stdout = StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    _server().serve()

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32600
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


@pytest.mark.parametrize(
    "params",
    [
        [],
        {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}},
        {"protocolVersion": "2024-11-05", "clientInfo": {"name": "c", "version": "1"}},
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}},
        {"protocolVersion": "unsupported", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}},
    ],
)
def test_initialize_rejects_invalid_params(params):
    response = _server()._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert response["error"]["code"] == -32602


def test_tools_require_initialized_notification_but_ping_does_not():
    server = _server()
    before = server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ping = server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    server._dispatch(_init())
    waiting = server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    notification = server._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
    after = server._dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})

    assert before["error"]["code"] == -32600
    assert ping["result"] == {}
    assert waiting["error"]["code"] == -32600
    assert notification is None
    assert after["result"] == {"tools": []}


def test_notification_with_invalid_params_is_silently_ignored():
    response = _server()._dispatch({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": [],
    })
    assert response is None


def test_params_and_tool_arguments_must_be_objects():
    server = _server()
    server._dispatch(_init())
    server._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})

    bad_params = server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": []})
    bad_args = server._dispatch({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "missing", "arguments": []},
    })

    assert bad_params["error"]["code"] == -32602
    assert bad_args["error"]["code"] == -32602


def test_unknown_tool_is_protocol_invalid_params():
    server = _server()
    server._dispatch(_init())
    server._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = server._dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "missing", "arguments": {}},
    })

    assert response["error"]["code"] == -32602


def test_internal_error_is_sanitized_and_trace_is_closed(monkeypatch):
    class Audit:
        def __init__(self):
            self.closed = 0

        def open(self, trace):
            pass

        def event(self, trace, kind, data):
            pass

        def close(self, trace):
            self.closed += 1

    class ExampleTool(Tool):
        name = "explode"

        def build_argv(self, args):
            return ["true"]

    audit = Audit()
    registry = ToolRegistry()
    registry.register(ExampleTool())
    server = _server(registry=registry, audit=audit)
    monkeypatch.setattr("kyagent.mcp.server.prepare_call", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError(r"D:\race\long\secret.py traceback details")
    ))
    stdin = StringIO(
        json.dumps(_init()) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "explode", "arguments": {}}}) + "\n"
    )
    stdout = StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    server.serve()

    response = json.loads(stdout.getvalue().splitlines()[-1])
    raw = json.dumps(response)
    assert response["error"]["code"] == -32603
    assert "secret.py" not in raw
    assert "traceback" not in raw.lower()
    assert audit.closed == 1


def test_registry_enable_tools_filters_through_public_method():
    registry = default_registry()
    returned = registry.enable_tools(["process_list"])
    assert returned is registry
    assert registry.names() == ["process_list"]


def test_configured_registry_loads_only_allowlisted_plugins_atomically(monkeypatch):
    from kyagent.mcp import plugins

    class ExtraTool(Tool):
        name = "extra"

        def build_argv(self, args):
            return ["true"]

    class PartialTool(Tool):
        name = "partial"

        def build_argv(self, args):
            return ["true"]

    class EntryPoint:
        group = "kyagent.mcp_tools"

        def __init__(self, name, loader):
            self.name = name
            self._loader = loader

        def load(self):
            return self._loader

    def good(registry):
        registry.register(ExtraTool())

    def broken(registry):
        registry.register(PartialTool())
        raise RuntimeError("plugin failed")

    entry_points = [
        EntryPoint("good", good),
        EntryPoint("broken", broken),
        EntryPoint("not-allowed", good),
    ]
    monkeypatch.setattr(plugins.metadata, "entry_points", lambda: entry_points)

    registry = plugins.configured_registry(_cfg(plugin_entry_points=["good", "broken"]))

    assert registry.get("extra") is not None
    assert registry.get("partial") is None


def test_runtime_uses_configured_registry(monkeypatch, tmp_path):
    from kyagent import runtime

    sentinel = ToolRegistry()
    monkeypatch.setattr(runtime, "configured_registry", lambda cfg: sentinel)
    cfg = SimpleNamespace(
        executor=SimpleNamespace(
            account="test", timeout=1, output_cap=10, path=[], forbid_root=True, forbid_root_strict=True,
        ),
        audit=SimpleNamespace(database=str(tmp_path / "audit.db"), jsonl_file=None),
        resolve=lambda value: value,
    )
    monkeypatch.setattr(runtime.Guardrail, "from_config", lambda cfg: SimpleNamespace())

    assert runtime.build_runtime(cfg).registry is sentinel
