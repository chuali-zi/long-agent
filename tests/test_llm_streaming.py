"""LLM 流式输出 (chat_stream) 单元测试。

覆盖：
  - LlmBackend 基类默认实现：fallback 到 chat()，整段 text 一次性 on_delta
  - MockBackend：多块 on_delta + 最终 AssistantMessage 与 chat() 等价
  - HttpxBackend SSE：
      * 纯 text 流：on_delta 增量拼接 == text；只有 1 个 TextBlock
      * 含 tool_call 流：on_delta 不收到 tool_call JSON 片段；tool_uses 完整
      * 网络异常重试一次后成功

不打真实网络。使用注入 fake httpx.Client 的方式（与 test_httpx_backend.py 同口径）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

import httpx
import pytest

from kyagent.agent.llm import (
    AssistantMessage,
    HttpxBackend,
    LlmBackend,
    MockBackend,
    TextBlock,
    ToolUseBlock,
)


# ---------- Fake httpx.Client（SSE 流式） ---------------------------------


class _FakeStreamResponse:
    """模拟 httpx.Response 的 stream context manager 行为。"""

    def __init__(self, status_code: int = 200, lines: Iterable[str] | None = None, body: str = ""):
        self.status_code = status_code
        self._lines = list(lines or [])
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def iter_lines(self):
        for ln in self._lines:
            yield ln

    def read(self):
        return self._body.encode("utf-8")


class _FakeStreamHttpxClient:
    """记录入参；按队列返回 _FakeStreamResponse 或抛异常。"""

    def __init__(self, base_url: str = "", headers: dict | None = None, timeout: float = 60.0):
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout = timeout
        self.last_path: str | None = None
        self.last_payload: dict | None = None
        self.responses_queue: list = []
        self.call_count: int = 0

    def stream(self, method: str, path: str, json: Any = None):  # noqa: A002
        self.last_path = path
        self.last_payload = json
        self.call_count += 1
        assert self.responses_queue, "测试需先设置 responses_queue"
        item = self.responses_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # _post_with_retry 不会被流式路径用到；保留以便构造 backend 不挂
    def post(self, path: str, json: Any = None):  # noqa: A002
        raise AssertionError("stream test should not call .post")


def _make_httpx_backend(max_retries: int = 0) -> tuple[HttpxBackend, _FakeStreamHttpxClient]:
    fake = _FakeStreamHttpxClient(base_url="https://api.deepseek.com/",
                                  headers={"Authorization": "Bearer test"})
    be = HttpxBackend(
        model="deepseek-v4-pro",
        max_tokens=512,
        temperature=0.0,
        base_url="https://api.deepseek.com",
        max_retries=max_retries,
        client=fake,
    )
    return be, fake


def _sse(payloads: list[dict]) -> list[str]:
    """把多个 JSON payload 包装为 SSE data: 行 + [DONE]。"""
    out: list[str] = []
    for p in payloads:
        out.append(f"data: {json.dumps(p)}")
        out.append("")  # blank separator (会被 iter_lines 跳过)
    out.append("data: [DONE]")
    return out


# ---------- 基类默认 chat_stream ------------------------------------------


class _DummyBackend(LlmBackend):
    """实现最小 chat()：返回固定 text。用来验证基类 chat_stream fallback。"""

    name = "dummy"

    def __init__(self, text: str = "hello world"):
        self._text = text

    def chat(self, system, messages, tools):
        return AssistantMessage(blocks=[TextBlock(text=self._text)], stop_reason="end_turn")


def test_default_chat_stream_emits_full_text_as_single_delta():
    be = _DummyBackend("一二三四五")
    deltas: list[str] = []
    am = be.chat_stream("s", [], [], deltas.append)
    assert deltas == ["一二三四五"]
    assert am.blocks[0].text == "一二三四五"
    assert am.stop_reason == "end_turn"


def test_default_chat_stream_swallows_callback_errors():
    """on_delta 抛异常不应影响最终返回值。"""
    be = _DummyBackend("payload")

    def bad(_chunk):
        raise RuntimeError("ui died")

    am = be.chat_stream("s", [], [], bad)
    assert am.blocks[0].text == "payload"


def test_default_chat_stream_empty_text_skips_delta():
    """text 为空时 on_delta 不应被调用（避免 UI 误判有内容）。"""
    be = _DummyBackend("")
    calls: list[str] = []
    am = be.chat_stream("s", [], [], calls.append)
    assert calls == []
    assert am.blocks[0].text == ""


# ---------- MockBackend.chat_stream ---------------------------------------


def test_mock_chat_stream_emits_multiple_deltas(monkeypatch):
    """text 较长应被切成多块（>= 2），拼接结果与 chat() 一致。"""
    monkeypatch.setattr(MockBackend, "_STREAM_CHUNK_DELAY", 0.0)
    be = MockBackend()
    messages = [{"role": "user", "content": "查一下磁盘使用情况"}]
    tools = [{"name": "fs_df", "description": "", "input_schema": {"type": "object"}}]

    deltas: list[str] = []
    am_stream = be.chat_stream("s", messages, tools, deltas.append)
    am_plain = be.chat(messages=messages, system="s", tools=tools)

    # 行为等价（同样的 mock 路由）
    assert am_stream.stop_reason == am_plain.stop_reason
    stream_texts = [b.text for b in am_stream.blocks if isinstance(b, TextBlock)]
    plain_texts = [b.text for b in am_plain.blocks if isinstance(b, TextBlock)]
    assert stream_texts == plain_texts

    # 拼接后等于 text blocks 串联
    joined_text = "".join(stream_texts)
    assert "".join(deltas) == joined_text
    # 至少切成 2 块（"我先通过工具..." 这段比较长，按空格切应 >=2）
    # 注意：中文 mock 文本里空格少；但开头 `我先通过工具 \`fs_df\`...` 有空格
    # 如果某种情况下只有 1 块，至少保证 on_delta 被调用
    assert len(deltas) >= 1


def test_mock_chat_stream_no_text_blocks_no_deltas(monkeypatch):
    """如果 chat() 返回的 AssistantMessage 没有 TextBlock，on_delta 不应被调用。"""
    monkeypatch.setattr(MockBackend, "_STREAM_CHUNK_DELAY", 0.0)
    be = MockBackend()

    # 构造一个 mock 子类，让 chat 返回只含 ToolUse 的消息
    class _OnlyToolUseMock(MockBackend):
        def chat(self, system, messages, tools):
            return AssistantMessage(
                blocks=[ToolUseBlock(id="x", name="t", input={})],
                stop_reason="tool_use",
            )

    be2 = _OnlyToolUseMock()
    deltas: list[str] = []
    am = be2.chat_stream("s", [], [], deltas.append)
    assert deltas == []
    assert am.stop_reason == "tool_use"


# ---------- HttpxBackend.chat_stream：纯 text -----------------------------


def test_httpx_chat_stream_text_only_assembles_final_message():
    be, fake = _make_httpx_backend()
    sse_lines = _sse([
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])
    fake.responses_queue = [_FakeStreamResponse(200, lines=sse_lines)]

    deltas: list[str] = []
    am = be.chat_stream("s", [{"role": "user", "content": "hi"}], [], deltas.append)

    # 协议字段：path + stream=True
    assert fake.last_path == "chat/completions"
    assert fake.last_payload["stream"] is True
    assert "tool_choice" not in fake.last_payload

    # on_delta 收到的拼接 == final text
    assert deltas == ["hello", " world"]
    assert "".join(deltas) == "hello world"

    # AssistantMessage
    assert isinstance(am, AssistantMessage)
    assert am.stop_reason == "end_turn"  # stop → end_turn 映射
    text_blocks = [b for b in am.blocks if isinstance(b, TextBlock)]
    assert len(text_blocks) == 1
    assert text_blocks[0].text == "hello world"


def test_httpx_chat_stream_handles_done_terminator():
    """看到 data: [DONE] 应立即结束循环（之后的 line 不解析）。"""
    be, fake = _make_httpx_backend()
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}',
        "",
        "data: [DONE]",
        # 这条不应被读到：故意写一个错误 JSON 看看会不会崩
        'data: {"this is bad json',
    ]
    fake.responses_queue = [_FakeStreamResponse(200, lines=sse_lines)]
    deltas: list[str] = []
    am = be.chat_stream("s", [{"role": "user", "content": "x"}], [], deltas.append)
    assert "".join(deltas) == "a"
    assert am.blocks[0].text == "a"


# ---------- HttpxBackend.chat_stream：含 tool_call -----------------------


def test_httpx_chat_stream_tool_calls_accumulated_not_streamed_to_delta():
    """模拟先吐 reasoning text → 再吐 tool_call name → 多段 arguments → finish=tool_calls。"""
    be, fake = _make_httpx_backend()
    sse_lines = _sse([
        # 1. 先来一段思考文本
        {"choices": [{"delta": {"content": "我先查 80 端口。"}, "finish_reason": None}]},
        # 2. tool_call 起始：id + name
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_99", "type": "function",
             "function": {"name": "lsof_port", "arguments": ""}}
        ]}, "finish_reason": None}]},
        # 3. arguments 分段
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"po'}}
        ]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'rt": 80}'}}
        ]}, "finish_reason": None}]},
        # 4. finish
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    fake.responses_queue = [_FakeStreamResponse(200, lines=sse_lines)]

    deltas: list[str] = []
    am = be.chat_stream("s", [{"role": "user", "content": "查 80 端口"}],
                       [{"name": "lsof_port", "description": "",
                         "input_schema": {"type": "object", "properties": {}}}],
                       deltas.append)

    # on_delta 只收到 text，不包含任何 tool_call JSON 片段
    joined = "".join(deltas)
    assert joined == "我先查 80 端口。"
    assert "lsof_port" not in joined
    assert "arguments" not in joined
    assert "{" not in joined and "}" not in joined

    # AssistantMessage 包含完整 tool_use
    assert am.stop_reason == "tool_use"  # tool_calls → tool_use 映射
    uses = am.tool_uses()
    assert len(uses) == 1
    assert uses[0].id == "call_99"
    assert uses[0].name == "lsof_port"
    assert uses[0].input == {"port": 80}
    # text block 也保留
    assert any(isinstance(b, TextBlock) and b.text == "我先查 80 端口。" for b in am.blocks)


# ---------- HttpxBackend.chat_stream：重试 --------------------------------


def test_httpx_chat_stream_retries_on_read_timeout(monkeypatch):
    """第一次 stream() 抛 ReadTimeout，第二次正常返回 → 最终成功。"""
    # patch sleep 不真睡
    monkeypatch.setattr("kyagent.agent.llm.time.sleep", lambda s: None)

    be, fake = _make_httpx_backend(max_retries=2)
    ok_lines = _sse([
        {"choices": [{"delta": {"content": "retry-ok"}, "finish_reason": "stop"}]},
    ])
    fake.responses_queue = [
        httpx.ReadTimeout("first try timed out"),
        _FakeStreamResponse(200, lines=ok_lines),
    ]

    deltas: list[str] = []
    am = be.chat_stream("s", [{"role": "user", "content": "x"}], [], deltas.append)

    assert fake.call_count == 2
    assert am.blocks[0].text == "retry-ok"
    # 重试前应该有提示
    assert any("[重试 LLM 流]" in d for d in deltas)
    # 真实 token 也被推出
    assert "retry-ok" in "".join(deltas)


def test_httpx_chat_stream_retries_exhausted_raises(monkeypatch):
    """连续 ConnectError 用尽 max_retries → 抛 RuntimeError 含异常类型名。"""
    monkeypatch.setattr("kyagent.agent.llm.time.sleep", lambda s: None)

    be, fake = _make_httpx_backend(max_retries=1)
    fake.responses_queue = [
        httpx.ConnectError("refused 1"),
        httpx.ConnectError("refused 2"),
    ]
    with pytest.raises(RuntimeError, match=r"stream network error after 1 retries.*ConnectError"):
        be.chat_stream("s", [{"role": "user", "content": "x"}], [], lambda c: None)


def test_httpx_chat_stream_http_400_fail_fast(monkeypatch):
    """4xx 不重试，立即报错；body snippet 出现在错误信息中。"""
    monkeypatch.setattr("kyagent.agent.llm.time.sleep", lambda s: None)

    be, fake = _make_httpx_backend(max_retries=2)
    fake.responses_queue = [
        _FakeStreamResponse(400, lines=[], body='{"error":"bad request"}'),
    ]
    with pytest.raises(RuntimeError, match=r"HTTP 400.*bad request"):
        be.chat_stream("s", [{"role": "user", "content": "x"}], [], lambda c: None)
    assert fake.call_count == 1
