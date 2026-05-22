"""HttpxBackend 单元测试：

验证不依赖 openai SDK 的纯 httpx 路径在协议层与 OpenAIBackend 等价：
  - 构造时正确规范化 base_url（强制以 / 结尾，否则 httpx URL join 会丢路径段）
  - chat() 请求 payload 与 OpenAIBackend.chat() 给 SDK 的 kwargs 在协议字段上一致
  - JSON 响应解析与 OpenAIBackend._from_openai_choice 在 AssistantMessage 上等价
  - 工厂 build_backend 能根据 llm_backend in {openai_httpx, deepseek_httpx, qwen_httpx}
    路由到 HttpxBackend，且复用现有 cfg.agent.{openai,deepseek,qwen} 子节
  - 缺 key 时遵循 fallback_to_mock 语义，与 OpenAIBackend 路径完全一致

不打真实网络（无 httpx.Client 真实实例化）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from kyagent.agent.llm import (
    AssistantMessage,
    HttpxBackend,
    MockBackend,
    OpenAIBackend,
    TextBlock,
    ToolUseBlock,
    build_backend,
)
from kyagent.config import Config


# ---------- 假的 httpx.Client / Response ----------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class _FakeHttpxClient:
    """记录入参；按需返回 _FakeResponse。"""

    def __init__(self, base_url: str = "", headers: dict | None = None, timeout: float = 60.0):
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout = timeout
        self.last_path: str | None = None
        self.last_payload: dict | None = None
        self.next_response: _FakeResponse | None = None

    def post(self, path: str, json: Any = None):  # noqa: A002 - 模拟 httpx 签名
        self.last_path = path
        self.last_payload = json
        assert self.next_response is not None, "测试需先设置 next_response"
        return self.next_response


def _make_backend(
    model: str = "deepseek-v4-flash",
    max_tokens: int = 512,
    temperature: float = 0.0,
    base_url: str = "https://api.deepseek.com",
) -> tuple[HttpxBackend, _FakeHttpxClient]:
    """构造带注入 fake client 的 HttpxBackend，跳过环境变量校验。"""
    # 用 client= 注入参数绕开真实 httpx；规范化 base_url 仅在自造 client 时执行，
    # 这里我们手动给 fake client 设置 base_url 以模拟同样的拼接逻辑
    base = base_url.rstrip("/") + "/"
    fake = _FakeHttpxClient(base_url=base, headers={"Authorization": "Bearer test"})
    be = HttpxBackend(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        base_url=base_url,
        client=fake,
    )
    return be, fake


# ---------- 构造与 base_url 规范化 ----------------------------------------


def test_construct_real_client_normalizes_base_url(monkeypatch):
    """没传 client 时应自己造 httpx.Client，并把 base_url 强制补 / 结尾。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, headers, timeout):
            captured["base_url"] = base_url
            captured["headers"] = headers
            captured["timeout"] = timeout

    import types
    fake_httpx = types.SimpleNamespace(Client=_CaptureClient)
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")

    HttpxBackend(
        model="m",
        max_tokens=10,
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",  # 无尾 /
    )
    # 关键：必须补成尾 /，否则 httpx URL join 会把 /chat/completions 当绝对路径替换 base 的 path
    assert captured["base_url"] == "https://api.deepseek.com/"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


def test_construct_with_trailing_slash_idempotent(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    HttpxBackend(
        model="m", max_tokens=10,
        api_key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1/",  # 已经带尾 /
    )
    assert captured["base_url"] == "https://api.openai.com/v1/"


def test_construct_default_base_url_for_openai(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    HttpxBackend(model="m", max_tokens=10, api_key_env="OPENAI_API_KEY", base_url=None)
    assert captured["base_url"] == "https://api.openai.com/v1/"


def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=lambda **_: None))
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        HttpxBackend(model="m", max_tokens=10, api_key_env="DEEPSEEK_API_KEY")


# ---------- chat() 请求 payload 与 OpenAI 协议字段对齐 --------------------


def test_chat_payload_minimal_text():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
    })
    am = be.chat("sys", [{"role": "user", "content": "hi"}], [])

    # 协议字段
    assert fake.last_path == "chat/completions"  # 相对路径，无前导 /
    assert fake.last_payload["model"] == "deepseek-v4-flash"
    assert fake.last_payload["max_tokens"] == 512
    assert fake.last_payload["temperature"] == 0.0
    assert fake.last_payload["messages"][0] == {"role": "system", "content": "sys"}
    assert fake.last_payload["messages"][1] == {"role": "user", "content": "hi"}
    # 无 tools 时不应出现 tool_choice
    assert "tools" not in fake.last_payload
    assert "tool_choice" not in fake.last_payload

    # 响应解析
    assert isinstance(am, AssistantMessage)
    assert am.stop_reason == "end_turn"
    assert len(am.blocks) == 1 and isinstance(am.blocks[0], TextBlock)
    assert am.blocks[0].text == "hello"


def test_chat_payload_includes_tools_and_tool_choice():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    be.chat("sys", [{"role": "user", "content": "hi"}], [
        {"name": "lsof_port", "description": "find port",
         "input_schema": {"type": "object", "properties": {"port": {"type": "integer"}}}},
    ])
    payload = fake.last_payload
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "lsof_port"
    assert payload["tools"][0]["function"]["parameters"]["properties"]["port"]["type"] == "integer"


def test_chat_response_with_tool_calls_parses_arguments():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={
        "choices": [{
            "message": {
                "content": "先查一下端口。",
                "tool_calls": [{
                    "id": "call_42",
                    "type": "function",
                    "function": {
                        "name": "lsof_port",
                        "arguments": '{"port": 80}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    am = be.chat("sys", [{"role": "user", "content": "查 80 端口"}], [
        {"name": "lsof_port", "description": "",
         "input_schema": {"type": "object", "properties": {}}},
    ])
    assert am.stop_reason == "tool_use"  # tool_calls → tool_use 映射
    uses = am.tool_uses()
    assert len(uses) == 1
    assert uses[0].id == "call_42"
    assert uses[0].name == "lsof_port"
    assert uses[0].input == {"port": 80}
    # 同时保留 text block
    assert any(isinstance(b, TextBlock) and b.text == "先查一下端口。" for b in am.blocks)


def test_chat_malformed_arguments_fall_back_to_raw():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "x",
                    "type": "function",
                    "function": {"name": "t", "arguments": "not-json"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    am = be.chat("s", [{"role": "user", "content": "go"}], [])
    tu = am.tool_uses()[0]
    assert tu.input == {"_raw": "not-json"}


def test_chat_empty_choices_returns_empty_message():
    """供应商有时在限流时返回 choices=[] —— 不能崩，应给一个空 AssistantMessage。"""
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={"choices": []})
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks == []
    assert am.stop_reason == "end_turn"


def test_chat_unknown_finish_reason_passes_through():
    """OpenAI 协议未来可能加新的 finish_reason，不映射应原样透传。"""
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(json_body={
        "choices": [{"message": {"content": "x"}, "finish_reason": "function_call"}],
    })
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    # function_call 是 _STOP_MAP 已知 → tool_use
    assert am.stop_reason == "tool_use"

    fake.next_response = _FakeResponse(json_body={
        "choices": [{"message": {"content": "x"}, "finish_reason": "future_reason"}],
    })
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.stop_reason == "future_reason"


# ---------- HTTP / JSON 错误处理 ------------------------------------------


def test_chat_http_400_raises_with_body_snippet():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(status_code=400, text='{"error":"bad request"}')
    with pytest.raises(RuntimeError, match=r"HTTP 400.*bad request"):
        be.chat("s", [{"role": "user", "content": "x"}], [])


def test_chat_http_401_does_not_leak_authorization_header():
    """错误 message 必须不含 Authorization / token 信息。"""
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(status_code=401, text="Unauthorized")
    with pytest.raises(RuntimeError) as exc:
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert "Authorization" not in str(exc.value)
    assert "Bearer" not in str(exc.value)


def test_chat_body_snippet_truncated_to_500_chars():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(status_code=500, text="x" * 5000)
    with pytest.raises(RuntimeError) as exc:
        be.chat("s", [{"role": "user", "content": "x"}], [])
    # 500 字符 + 前缀，整体长度可控
    assert len(str(exc.value)) < 700


def test_chat_invalid_json_raises():
    be, fake = _make_backend()
    fake.next_response = _FakeResponse(status_code=200, json_body=ValueError("not json"),
                                       text="<html>upstream error</html>")
    with pytest.raises(RuntimeError, match="非 JSON"):
        be.chat("s", [{"role": "user", "content": "x"}], [])


# ---------- preset 工厂 ----------------------------------------------------


def test_preset_deepseek_uses_official_endpoint(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")

    be = HttpxBackend.preset("deepseek")
    assert be.model == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com/"


def test_preset_qwen_uses_dashscope_endpoint(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-q")

    be = HttpxBackend.preset("qwen")
    assert be.model == "qwen-plus"
    # DashScope 预设 base 带 /v1，规范化后 v1/ 保留
    assert captured["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/"


def test_preset_unknown_provider_raises():
    with pytest.raises(ValueError, match="未知 OpenAI 协议兼容供应商"):
        HttpxBackend.preset("nonexistent")


# ---------- build_backend 工厂路由 -----------------------------------------


def test_build_backend_deepseek_httpx(monkeypatch):
    """llm_backend=deepseek_httpx 应返回 HttpxBackend，复用 cfg.agent.deepseek 配置。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"

    be = build_backend(cfg)
    assert isinstance(be, HttpxBackend)
    assert be.model == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com/"


def test_build_backend_qwen_httpx(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-q")

    cfg = Config()
    cfg.agent.llm_backend = "qwen_httpx"

    be = build_backend(cfg)
    assert isinstance(be, HttpxBackend)
    assert be.model == "qwen-plus"
    assert captured["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/"


def test_build_backend_openai_httpx(monkeypatch):
    """openai_httpx 走 cfg.agent.openai 子节，不调 preset。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")

    cfg = Config()
    cfg.agent.llm_backend = "openai_httpx"
    cfg.agent.openai.model = "gpt-4o-mini"

    be = build_backend(cfg)
    assert isinstance(be, HttpxBackend)
    assert be.model == "gpt-4o-mini"
    assert captured["base_url"] == "https://api.openai.com/v1/"


def test_build_backend_deepseek_httpx_user_override(monkeypatch):
    """yaml 里 override base_url / model 必须生效，不被预设覆盖。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=_CaptureClient))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.deepseek.model = "deepseek-v4-pro"
    cfg.agent.deepseek.base_url = "https://my-proxy.example.com/v1"

    be = build_backend(cfg)
    assert be.model == "deepseek-v4-pro"
    assert captured["base_url"] == "https://my-proxy.example.com/v1/"


def test_build_backend_deepseek_httpx_missing_key_falls_back(monkeypatch):
    """缺 key 应降级到 mock，与 deepseek 路径行为完全一致。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=lambda **_: None))
    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"

    be = build_backend(cfg)
    assert isinstance(be, MockBackend)
    assert getattr(be, "fallback_from", None) == "deepseek_httpx"
    assert "DEEPSEEK_API_KEY" in getattr(be, "fallback_reason", "")


def test_build_backend_deepseek_httpx_missing_key_raises_in_strict(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import types
    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        types.SimpleNamespace(Client=lambda **_: None))
    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.fallback_to_mock = False
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_backend(cfg)


# ---------- 与 OpenAIBackend 等价性回归（同一 JSON 应产出同一 AssistantMessage）


def test_httpx_and_openai_backend_produce_equivalent_messages():
    """同一份 OpenAI 协议响应，HttpxBackend._from_openai_dict 与
    OpenAIBackend._from_openai_choice 应产出语义等价的 AssistantMessage。

    这是抽象层契约的保障：调用方（Agent.core）感知不到底下是哪条路径。
    """
    # 用 dict 构造 HttpxBackend 路径
    response_dict = {
        "choices": [{
            "message": {
                "content": "调一下工具。",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "process_list",
                                 "arguments": '{"sort_by":"cpu","limit":5}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    am_httpx = HttpxBackend._from_openai_dict(response_dict)

    # 用 attr-style 对象（mirror SDK 行为）构造 OpenAIBackend 路径
    import types as _t
    fn = _t.SimpleNamespace(name="process_list", arguments='{"sort_by":"cpu","limit":5}')
    tc = _t.SimpleNamespace(id="call_1", function=fn, type="function")
    msg = _t.SimpleNamespace(content="调一下工具。", tool_calls=[tc])
    choice = _t.SimpleNamespace(message=msg, finish_reason="tool_calls")
    am_openai = OpenAIBackend._from_openai_choice(choice, raw=None)

    # 两条路径的 AssistantMessage 在 block 内容和 stop_reason 上必须等价
    assert am_httpx.stop_reason == am_openai.stop_reason
    assert len(am_httpx.blocks) == len(am_openai.blocks)
    # text block
    txt_h = [b for b in am_httpx.blocks if isinstance(b, TextBlock)]
    txt_o = [b for b in am_openai.blocks if isinstance(b, TextBlock)]
    assert [b.text for b in txt_h] == [b.text for b in txt_o]
    # tool use block
    tu_h = am_httpx.tool_uses()
    tu_o = am_openai.tool_uses()
    assert len(tu_h) == len(tu_o) == 1
    assert tu_h[0].id == tu_o[0].id
    assert tu_h[0].name == tu_o[0].name
    assert tu_h[0].input == tu_o[0].input
