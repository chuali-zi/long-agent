"""HttpxBackend 单元测试：

验证不依赖 openai SDK 的纯 httpx 路径在协议层与 OpenAIBackend 等价：
  - 构造时正确规范化 base_url（强制以 / 结尾，否则 httpx URL join 会丢路径段）
  - chat() 请求 payload 与 OpenAIBackend.chat() 给 SDK 的 kwargs 在协议字段上一致
  - JSON 响应解析与 OpenAIBackend._from_openai_choice 在 AssistantMessage 上等价
  - 工厂 build_backend 能根据 llm_backend in {openai_httpx, deepseek_httpx, qwen_httpx}
    路由到 HttpxBackend，且复用现有 cfg.agent.{openai,deepseek,qwen} 子节
  - 缺 key 时直接报错，与 OpenAIBackend 路径完全一致

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
    def __init__(
        self,
        status_code: int = 200,
        json_body: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class _FakeHttpxClient:
    """记录入参；按需返回 _FakeResponse 或抛异常。

    支持两种使用模式：
      - 单次模式（向后兼容）：设置 .next_response = _FakeResponse(...)
      - 队列模式（重试测试）：append 多个响应 / 异常到 .responses_queue，
        每次 post 按 FIFO 弹出；列表元素是 Exception 实例时直接 raise
    """

    def __init__(self, base_url: str = "", headers: dict | None = None, timeout: float = 60.0):
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout = timeout
        self.last_path: str | None = None
        self.last_payload: dict | None = None
        self.next_response: _FakeResponse | None = None
        self.responses_queue: list = []
        self.call_count: int = 0

    def post(self, path: str, json: Any = None):  # noqa: A002 - 模拟 httpx 签名
        self.last_path = path
        self.last_payload = json
        self.call_count += 1
        if self.responses_queue:
            item = self.responses_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        assert self.next_response is not None, "测试需先设置 next_response 或 responses_queue"
        return self.next_response


def _make_backend(
    model: str = "deepseek-v4-flash",
    max_tokens: int = 512,
    temperature: float = 0.0,
    base_url: str = "https://api.deepseek.com",
    max_retries: int = 0,
) -> tuple[HttpxBackend, _FakeHttpxClient]:
    """构造带注入 fake client 的 HttpxBackend，跳过环境变量校验。

    测试默认 max_retries=0（禁重试），让大多数协议层 / 错误处理测试保持
    单次 POST 语义。生产默认是 HttpxBackend.DEFAULT_MAX_RETRIES=2，重试
    行为另由 test_retry_* 系列覆盖。
    """
    # 用 client= 注入参数绕开真实 httpx；规范化 base_url 仅在自造 client 时执行，
    # 这里我们手动给 fake client 设置 base_url 以模拟同样的拼接逻辑
    base = base_url.rstrip("/") + "/"
    fake = _FakeHttpxClient(base_url=base, headers={"Authorization": "Bearer test"})
    be = HttpxBackend(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        base_url=base_url,
        max_retries=max_retries,
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    HttpxBackend(model="m", max_tokens=10, api_key_env="OPENAI_API_KEY", base_url=None)
    assert captured["base_url"] == "https://api.openai.com/v1/"


def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", lambda **_: None)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")

    be = HttpxBackend.preset("deepseek")
    assert be.model == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com/"


def test_preset_qwen_uses_dashscope_endpoint(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, **_):
            captured["base_url"] = base_url

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
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

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-d")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.deepseek.model = "deepseek-v4-pro"
    cfg.agent.deepseek.base_url = "https://my-proxy.example.com/v1"

    be = build_backend(cfg)
    assert be.model == "deepseek-v4-pro"
    assert captured["base_url"] == "https://my-proxy.example.com/v1/"


def test_build_backend_deepseek_httpx_uses_config_api_key_when_env_missing(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, headers, **_):
            captured["base_url"] = base_url
            captured["headers"] = headers

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.deepseek.api_key = "sk-json"

    be = build_backend(cfg)

    assert isinstance(be, HttpxBackend)
    assert captured["headers"]["Authorization"] == "Bearer sk-json"


def test_build_backend_deepseek_httpx_env_key_takes_precedence_over_config_key(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, headers, **_):
            captured["base_url"] = base_url
            captured["headers"] = headers

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.deepseek.api_key = "sk-json"

    build_backend(cfg)

    assert captured["headers"]["Authorization"] == "Bearer sk-env"


def test_build_backend_deepseek_httpx_missing_key_raises(monkeypatch):
    """缺 key 应直接报错，与 deepseek 路径行为完全一致。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", lambda **_: None)
    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_backend(cfg)


def test_build_backend_default_is_deepseek_httpx_and_raises_without_key(monkeypatch):
    """Config() 默认使用真实 deepseek_httpx；无 key 时直接报错。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", lambda **_: None)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_backend(Config())

def test_build_backend_deepseek_httpx_missing_key_raises_even_if_legacy_fallback_flag_is_true(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", lambda **_: None)
    cfg = Config()
    cfg.agent.llm_backend = "deepseek_httpx"
    cfg.agent.fallback_to_mock = True
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


# ---------- 重试 / 退避 / 超时（运行时可靠性对齐 openai SDK）-----------------


def _patch_sleep(monkeypatch) -> list[float]:
    """让 HttpxBackend 用的 time.sleep 不真睡，返回 sleeps 调用记录列表。"""
    sleeps: list[float] = []
    monkeypatch.setattr("kyagent.agent.llm.time.sleep", lambda s: sleeps.append(s))
    return sleeps


def _ok_response(text: str = "ok") -> _FakeResponse:
    return _FakeResponse(json_body={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}]
    })


# ---------- 默认值对齐 OpenAI SDK -----------------------------------------


def test_defaults_aligned_with_openai_sdk():
    """DEFAULT_TIMEOUT / DEFAULT_MAX_RETRIES 必须对齐 openai SDK 默认值
    （参考 openai/_base_client.py：600s timeout + 2 retries）。"""
    assert HttpxBackend.DEFAULT_TIMEOUT == 600.0
    assert HttpxBackend.DEFAULT_MAX_RETRIES == 2


def test_real_client_uses_default_timeout(monkeypatch):
    """没传 timeout 参数时，httpx.Client 应收到 600.0 而非旧的 60.0。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, headers, timeout):
            captured["timeout"] = timeout

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")

    HttpxBackend(model="m", max_tokens=10, api_key_env="DEEPSEEK_API_KEY")
    assert captured["timeout"] == 600.0


def test_real_client_respects_explicit_timeout(monkeypatch):
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, base_url, headers, timeout):
            captured["timeout"] = timeout

    monkeypatch.setattr("kyagent.agent.llm.httpx.Client", _CaptureClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")

    HttpxBackend(model="m", max_tokens=10, api_key_env="DEEPSEEK_API_KEY", timeout=30.0)
    assert captured["timeout"] == 30.0


def test_backend_records_max_retries():
    """构造时保留 max_retries 到实例属性，便于审计 / 调试。"""
    be, _ = _make_backend(max_retries=5)
    assert be.max_retries == 5
    assert be.timeout == HttpxBackend.DEFAULT_TIMEOUT


# ---------- 重试触发：HTTP 状态码 ----------------------------------------


def test_retry_on_429_then_success(monkeypatch):
    """429 触发一次重试，第 2 次成功；Retry-After 头优先生效。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        _FakeResponse(status_code=429, text='{"error":"rate limit"}',
                      headers={"retry-after": "2"}),
        _ok_response("after_429"),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "after_429"
    assert fake.call_count == 2
    assert sleeps == [2.0]


def test_retry_on_500_chain_then_success(monkeypatch):
    """500 → 503 → 200 三次成功；无 Retry-After 时走 exponential backoff。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        _FakeResponse(status_code=500, text="err1"),
        _FakeResponse(status_code=503, text="err2"),
        _ok_response("done"),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "done"
    assert fake.call_count == 3
    assert len(sleeps) == 2
    # exponential: 第 0 次重试 ~0.5+jitter, 第 1 次 ~1.0+jitter
    assert 0.5 <= sleeps[0] <= 0.75 + 1e-6
    assert 1.0 <= sleeps[1] <= 1.25 + 1e-6


def test_retry_exhausted_raises_with_count_in_message(monkeypatch):
    """3 次都 503 后用尽重试，错误信息含 'after 2 retries' 和最后一次的 body。"""
    _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        _FakeResponse(status_code=503, text="down1"),
        _FakeResponse(status_code=503, text="down2"),
        _FakeResponse(status_code=503, text="give_up"),
    ]
    with pytest.raises(RuntimeError, match=r"HTTP 503.*after 2 retries.*give_up"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 3


def test_408_retries(monkeypatch):
    """HTTP 408 Request Timeout → 重试。"""
    _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=1)
    fake.responses_queue = [
        _FakeResponse(status_code=408, text="request timeout"),
        _ok_response(),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "ok"


def test_409_retries(monkeypatch):
    """HTTP 409 Conflict → 重试（对齐 openai SDK 行为）。"""
    _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=1)
    fake.responses_queue = [
        _FakeResponse(status_code=409, text="conflict"),
        _ok_response(),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "ok"


def test_504_retries(monkeypatch):
    """504 Gateway Timeout → 5xx 范围内重试。"""
    _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=1)
    fake.responses_queue = [
        _FakeResponse(status_code=504, text="gateway timeout"),
        _ok_response(),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "ok"


# ---------- 不应重试的场景 ------------------------------------------------


def test_400_does_not_retry(monkeypatch):
    """400 Bad Request 不在重试集合 → 立即失败，无 sleep。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [_FakeResponse(status_code=400, text="bad request")]
    with pytest.raises(RuntimeError, match=r"HTTP 400.*bad request"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 1
    assert sleeps == []


def test_401_does_not_retry(monkeypatch):
    """401 Unauthorized → 立即失败（重试不会让无效 key 变有效）。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [_FakeResponse(status_code=401, text="bad key")]
    with pytest.raises(RuntimeError, match=r"HTTP 401"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 1
    assert sleeps == []


def test_404_does_not_retry(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [_FakeResponse(status_code=404, text="not found")]
    with pytest.raises(RuntimeError, match=r"HTTP 404"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 1
    assert sleeps == []


def test_invalid_json_does_not_retry(monkeypatch):
    """200 OK 但 body 非 JSON 不是瞬时故障 → fail-fast 不重试。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        _FakeResponse(status_code=200, json_body=ValueError("not json"),
                      text="<html>upstream error</html>"),
    ]
    with pytest.raises(RuntimeError, match="非 JSON"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 1
    assert sleeps == []


def test_max_retries_zero_disables_retry(monkeypatch):
    """max_retries=0 时 5xx 也不重试 —— 用于 CI / 测试场景禁用重试。"""
    sleeps = _patch_sleep(monkeypatch)
    be, fake = _make_backend(max_retries=0)
    fake.responses_queue = [_FakeResponse(status_code=503, text="down")]
    with pytest.raises(RuntimeError, match=r"HTTP 503"):
        be.chat("s", [{"role": "user", "content": "x"}], [])
    assert fake.call_count == 1
    assert sleeps == []


# ---------- httpx 异常类重试 ----------------------------------------------


def test_retry_on_connect_error(monkeypatch):
    """httpx.ConnectError → 重试。"""
    _patch_sleep(monkeypatch)
    import httpx
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        httpx.ConnectError("connection refused"),
        _ok_response(),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "ok"
    assert fake.call_count == 2


def test_retry_on_read_timeout(monkeypatch):
    """httpx.ReadTimeout（典型："服务器拉新流式 token 卡了"）→ 重试。"""
    _patch_sleep(monkeypatch)
    import httpx
    be, fake = _make_backend(max_retries=2)
    fake.responses_queue = [
        httpx.ReadTimeout("read timeout 1"),
        httpx.ReadTimeout("read timeout 2"),
        _ok_response(),
    ]
    am = be.chat("s", [{"role": "user", "content": "x"}], [])
    assert am.blocks[0].text == "ok"
    assert fake.call_count == 3


def test_connect_error_exhausted_raises(monkeypatch):
    """连续 ConnectError 用尽重试 → 抛 RuntimeError 含 retries 计数和异常类型。"""
    _patch_sleep(monkeypatch)
    import httpx
    be, fake = _make_backend(max_retries=1)
    fake.responses_queue = [
        httpx.ConnectError("refused 1"),
        httpx.ConnectError("refused 2"),
    ]
    with pytest.raises(RuntimeError, match=r"network error after 1 retries.*ConnectError"):
        be.chat("s", [{"role": "user", "content": "x"}], [])


# ---------- _compute_backoff 单元 ----------------------------------------


def test_compute_backoff_exponential_growth():
    """退避基础按 0.5 * 2^attempt 增长，每次都加少量 jitter。"""
    # attempt 0: base=0.5, 范围 [0.5, 0.75]
    v0 = HttpxBackend._compute_backoff(0, None)
    assert 0.5 <= v0 <= 0.75 + 1e-6
    # attempt 1: base=1.0, 范围 [1.0, 1.25]
    v1 = HttpxBackend._compute_backoff(1, None)
    assert 1.0 <= v1 <= 1.25 + 1e-6
    # attempt 2: base=2.0, 范围 [2.0, 2.25]
    v2 = HttpxBackend._compute_backoff(2, None)
    assert 2.0 <= v2 <= 2.25 + 1e-6


def test_compute_backoff_caps_at_8s():
    """高 attempt 时退避 base 上限 8 秒（对齐 openai SDK 行为）。"""
    for attempt in [4, 5, 6, 10, 100]:
        v = HttpxBackend._compute_backoff(attempt, None)
        assert 8.0 <= v <= 8.25 + 1e-6


def test_compute_backoff_uses_retry_after_when_valid():
    """Retry-After 数字头优先于 exponential backoff。"""
    assert HttpxBackend._compute_backoff(0, "5") == 5.0
    assert HttpxBackend._compute_backoff(3, "0.5") == 0.5
    # 含小数也接受
    assert HttpxBackend._compute_backoff(0, "2.5") == 2.5


def test_compute_backoff_caps_retry_after_at_60s():
    """Retry-After: 9999 被 cap 到 60 秒，防恶意 / bug 服务器拖死调用方。"""
    assert HttpxBackend._compute_backoff(0, "9999") == 60.0
    assert HttpxBackend._compute_backoff(0, "60") == 60.0
    assert HttpxBackend._compute_backoff(0, "61") == 60.0


def test_compute_backoff_negative_retry_after_falls_back():
    """Retry-After: -1（无效值）应回落 exponential backoff。"""
    v = HttpxBackend._compute_backoff(0, "-1")
    assert 0.5 <= v <= 0.75 + 1e-6


def test_compute_backoff_invalid_retry_after_falls_back():
    """Retry-After 非数字（HTTP-date 等）→ 不解析，回落退避。"""
    v = HttpxBackend._compute_backoff(0, "Wed, 23 May 2026 12:00:00 GMT")
    assert 0.5 <= v <= 0.75 + 1e-6
    v = HttpxBackend._compute_backoff(0, "")
    assert 0.5 <= v <= 0.75 + 1e-6
    v = HttpxBackend._compute_backoff(0, None)
    assert 0.5 <= v <= 0.75 + 1e-6
