"""OpenAIBackend 单元测试：

不依赖真实 OpenAI SDK / 网络。重点验证 Anthropic 风格 ↔ OpenAI 风格的双向转换：
  - tools 字段（input_schema → parameters）
  - messages（user str / tool_result list / assistant tool_use → role:tool / tool_calls）
  - 响应（content + tool_calls → AssistantMessage(TextBlock / ToolUseBlock)）
  - build_backend 工厂能根据 cfg.agent.llm_backend == "openai" 构造该后端
"""
from __future__ import annotations

import json
import types
from dataclasses import dataclass
from typing import Any

import pytest

from kyagent.agent.llm import (
    AssistantMessage,
    MockBackend,
    OpenAIBackend,
    TextBlock,
    ToolUseBlock,
    build_backend,
)
from kyagent.config import Config


# ---------- 假的 OpenAI 响应对象 ------------------------------------------


@dataclass
class _FakeFunc:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunc
    type: str = "function"


@dataclass
class _FakeMsg:
    content: str | None
    tool_calls: list[_FakeToolCall] | None = None


@dataclass
class _FakeChoice:
    message: _FakeMsg
    finish_reason: str = "stop"


@dataclass
class _FakeResp:
    choices: list[_FakeChoice]


class _FakeCompletions:
    """记录入参；按需返回 _FakeResp。"""

    def __init__(self):
        self.last_kwargs: dict[str, Any] | None = None
        self.next_response: _FakeResp | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        assert self.next_response is not None, "测试需先设置 next_response"
        return self.next_response


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self._completions = _FakeCompletions()
        self.chat = _FakeChat(self._completions)


# ---------- 测试夹具 -------------------------------------------------------


@pytest.fixture
def backend(monkeypatch):
    """注入假的 openai.OpenAI 类，避开 SDK 与网络依赖。"""
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    be = OpenAIBackend(model="gpt-4o-mini", max_tokens=512, temperature=0.0)
    return be


# ---------- 转换：tools ----------------------------------------------------


def test_tools_translation_uses_function_envelope(backend):
    anth_tools = [{
        "name": "process_list",
        "description": "list processes",
        "input_schema": {
            "type": "object",
            "properties": {"sort_by": {"type": "string"}},
            "required": [],
        },
    }]
    out = OpenAIBackend._to_openai_tools(anth_tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "list processes",
            "parameters": {
                "type": "object",
                "properties": {"sort_by": {"type": "string"}},
                "required": [],
            },
        },
    }]


# ---------- 转换：messages -------------------------------------------------


def test_user_string_message_passes_through():
    out = OpenAIBackend._to_openai_messages(
        "SYS",
        [{"role": "user", "content": "查 80 端口"}],
    )
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "查 80 端口"}


def test_assistant_tool_use_becomes_tool_calls():
    msgs = [
        {"role": "user", "content": "查 80 端口"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我先查一下。"},
            {"type": "tool_use", "id": "call_1", "name": "lsof_port", "input": {"port": 80}},
        ]},
    ]
    out = OpenAIBackend._to_openai_messages("S", msgs)
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert asst["content"] == "我先查一下。"
    assert asst["tool_calls"][0]["id"] == "call_1"
    assert asst["tool_calls"][0]["function"]["name"] == "lsof_port"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"port": 80}


def test_tool_result_user_message_splits_into_tool_role():
    msgs = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1",
             "content": "nginx 0.0.0.0:80", "is_error": False},
        ]},
    ]
    out = OpenAIBackend._to_openai_messages("", msgs)
    assert out == [{"role": "tool", "tool_call_id": "call_1", "content": "nginx 0.0.0.0:80"}]


def test_tool_result_text_block_content_is_flattened():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x",
         "content": [{"type": "text", "text": "ABC"}, {"type": "text", "text": "DEF"}]},
    ]}]
    out = OpenAIBackend._to_openai_messages("", msgs)
    assert out[0]["content"] == "ABCDEF"


# ---------- 响应解析 -------------------------------------------------------


def test_response_with_text_only(backend):
    backend._client._completions.next_response = _FakeResp(choices=[
        _FakeChoice(message=_FakeMsg(content="hello", tool_calls=None),
                    finish_reason="stop"),
    ])
    am = backend.chat("sys", [{"role": "user", "content": "hi"}], [])
    assert isinstance(am, AssistantMessage)
    assert am.stop_reason == "end_turn"
    assert len(am.blocks) == 1
    assert isinstance(am.blocks[0], TextBlock)
    assert am.blocks[0].text == "hello"


def test_response_with_tool_call_parses_arguments(backend):
    backend._client._completions.next_response = _FakeResp(choices=[
        _FakeChoice(
            message=_FakeMsg(
                content="先查一下进程。",
                tool_calls=[_FakeToolCall(
                    id="call_42",
                    function=_FakeFunc(name="process_list",
                                       arguments='{"sort_by":"cpu","limit":5}'),
                )],
            ),
            finish_reason="tool_calls",
        ),
    ])
    am = backend.chat("sys", [{"role": "user", "content": "查 CPU"}], [
        {"name": "process_list", "description": "x",
         "input_schema": {"type": "object", "properties": {}}},
    ])
    assert am.stop_reason == "tool_use"
    tool_uses = am.tool_uses()
    assert len(tool_uses) == 1
    tu = tool_uses[0]
    assert tu.id == "call_42"
    assert tu.name == "process_list"
    assert tu.input == {"sort_by": "cpu", "limit": 5}


def test_chat_passes_tools_and_tool_choice(backend):
    backend._client._completions.next_response = _FakeResp(choices=[
        _FakeChoice(message=_FakeMsg(content="ok"), finish_reason="stop"),
    ])
    backend.chat("sys", [{"role": "user", "content": "hi"}], [
        {"name": "t", "description": "", "input_schema": {"type": "object", "properties": {}}},
    ])
    kw = backend._client._completions.last_kwargs
    assert kw["model"] == "gpt-4o-mini"
    assert kw["max_tokens"] == 512
    assert kw["temperature"] == 0.0
    assert kw["tool_choice"] == "auto"
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert kw["tools"][0]["type"] == "function"


def test_malformed_arguments_fall_back_to_raw(backend):
    backend._client._completions.next_response = _FakeResp(choices=[
        _FakeChoice(
            message=_FakeMsg(content=None,
                             tool_calls=[_FakeToolCall(
                                 id="x", function=_FakeFunc(name="t", arguments="not-json"))]),
            finish_reason="tool_calls",
        ),
    ])
    am = backend.chat("s", [{"role": "user", "content": "go"}], [])
    tu = am.tool_uses()[0]
    assert tu.input == {"_raw": "not-json"}


# ---------- 工厂注册 -------------------------------------------------------


def test_build_backend_constructs_openai(monkeypatch):
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    cfg = Config()
    cfg.agent.llm_backend = "openai"
    cfg.agent.openai.model = "deepseek-chat"
    cfg.agent.openai.base_url = "https://api.deepseek.com/v1"

    be = build_backend(cfg)
    assert isinstance(be, OpenAIBackend)
    assert be.model == "deepseek-chat"
    # base_url 应该透传到 fake client 初始化参数
    assert be._client.init_kwargs.get("base_url") == "https://api.deepseek.com/v1"


def test_build_backend_missing_key_raises(monkeypatch):
    """fallback_to_mock=False（CI/生产）模式下，缺 key 必须直接 raise。"""
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.agent.llm_backend = "openai"
    cfg.agent.fallback_to_mock = False
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_backend(cfg)


def test_build_backend_missing_key_falls_back_to_mock(monkeypatch):
    """默认 fallback_to_mock=True 时缺 key 应降级为 MockBackend 并挂上 fallback_from 标记。"""
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.agent.llm_backend = "openai"
    # 不动 fallback_to_mock，验证默认就是 True
    assert cfg.agent.fallback_to_mock is True

    be = build_backend(cfg)
    assert isinstance(be, MockBackend)
    assert getattr(be, "fallback_from", None) == "openai"
    assert "OPENAI_API_KEY" in getattr(be, "fallback_reason", "")


def test_build_backend_constructs_deepseek_preset(monkeypatch):
    """llm_backend=deepseek 应走 OpenAIBackend，且 base_url 自动设为官方端点。"""
    captured: dict[str, Any] = {}

    class _CapturingClient(_FakeOpenAIClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    fake_module = types.SimpleNamespace(OpenAI=_CapturingClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek"

    be = build_backend(cfg)
    assert isinstance(be, OpenAIBackend)
    assert be.model == "deepseek-v4-flash"  # 预设值
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["api_key"] == "sk-deepseek-test"


def test_build_backend_constructs_qwen_preset(monkeypatch):
    """llm_backend=qwen 应走 OpenAIBackend + DashScope 国内 compatible-mode 端点。"""
    captured: dict[str, Any] = {}

    class _CapturingClient(_FakeOpenAIClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    fake_module = types.SimpleNamespace(OpenAI=_CapturingClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-test")

    cfg = Config()
    cfg.agent.llm_backend = "qwen"

    be = build_backend(cfg)
    assert isinstance(be, OpenAIBackend)
    assert be.model == "qwen-plus"
    assert captured["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_build_backend_deepseek_user_override(monkeypatch):
    """用户在 yaml 里 override base_url/model 时必须生效（不被 preset 覆盖）。"""
    captured: dict[str, Any] = {}

    class _CapturingClient(_FakeOpenAIClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    fake_module = types.SimpleNamespace(OpenAI=_CapturingClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")

    cfg = Config()
    cfg.agent.llm_backend = "deepseek"
    cfg.agent.deepseek.model = "deepseek-v4-pro"
    cfg.agent.deepseek.base_url = "https://my-proxy.example.com/v1"

    be = build_backend(cfg)
    assert be.model == "deepseek-v4-pro"
    assert captured["base_url"] == "https://my-proxy.example.com/v1"


def test_build_backend_deepseek_missing_key_falls_back(monkeypatch):
    """国产 LLM 也享受 fallback 语义。"""
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = Config()
    cfg.agent.llm_backend = "deepseek"
    be = build_backend(cfg)
    assert isinstance(be, MockBackend)
    assert getattr(be, "fallback_from", None) == "deepseek"
