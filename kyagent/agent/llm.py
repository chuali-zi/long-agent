"""LLM 后端抽象。

提供以下后端：
  - AnthropicBackend：调用真实 Claude API（需 ANTHROPIC_API_KEY）
  - OpenAIBackend：调用 OpenAI Chat Completions（需 OPENAI_API_KEY），同时兼容
                   任何 OpenAI 协议（Azure OpenAI / vLLM / DeepSeek / 智谱 / Ollama …）
  - MockBackend：纯规则路由，不需要任何外部依赖；可用于离线 demo / CI

国产 LLM（赛题鼓励的 DeepSeek / Qwen）走 OpenAI 协议兼容路径，由
build_backend 工厂识别 llm_backend 别名后转成 OpenAIBackend(base_url=预设)。
2026-05 验证：DeepSeek 官方推荐 openai SDK + base_url=https://api.deepseek.com；
DashScope 提供 compatible-mode/v1 端点。tools/tool_calls 协议与 OpenAI 完全一致。

每个后端实现统一的 chat(messages, tools) -> AssistantMessage。
Agent.core 始终以 Anthropic 风格组装 messages/tools；OpenAIBackend 内部完成双向翻译。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx  # 主依赖（pyproject.toml [project.dependencies]）—— HttpxBackend 用

logger = logging.getLogger("kyagent.llm")


# ---- 统一的消息模型 -------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


@dataclass
class AssistantMessage:
    blocks: list[TextBlock | ToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw: Any = None

    def texts(self) -> list[str]:
        return [b.text for b in self.blocks if isinstance(b, TextBlock)]

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]


class LlmBackend:
    name = "base"

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantMessage:
        raise NotImplementedError


# ---- Anthropic 实现 -------------------------------------------------------


class AnthropicBackend(LlmBackend):
    name = "anthropic"

    # 龙芯（LoongArch64 Old World）部署用 `pip install --no-deps anthropic==0.39.0`，未装 jiter。
    # 切勿改用 messages.stream() / 任何 anthropic.lib.streaming.* — 会触发 ImportError: jiter。
    # 如需流式：要么先解决 LoongArch 上的 Rust 编译，要么改走 httpx 手打 SSE。

    def __init__(
        self,
        model: str,
        max_tokens: int,
        api_key_env: str = "ANTHROPIC_API_KEY",
        prompt_cache: bool = True,
    ):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise RuntimeError("缺少依赖：pip install anthropic") from e
        from anthropic import Anthropic

        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(f"环境变量 {api_key_env} 未设置")
        self._client = Anthropic(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.prompt_cache = prompt_cache

    def chat(self, system, messages, tools):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        # Anthropic 显式 prompt cache：在 system 与 tools 上挂 ephemeral 断点。
        # 多轮对话共享同一前缀时直接复用，TTFT 与输入 token 成本都下降。
        if self.prompt_cache and system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            kwargs["system"] = system

        if tools:
            if self.prompt_cache:
                tools_with_cache = [dict(t) for t in tools]
                # 只给最后一个工具加 cache breakpoint，之前的工具自然被涵盖在
                # 同一前缀里——Anthropic 的 ephemeral cache 是前缀缓存。
                tools_with_cache[-1]["cache_control"] = {"type": "ephemeral"}
                kwargs["tools"] = tools_with_cache
            else:
                kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)
        blocks: list[TextBlock | ToolUseBlock] = []
        for blk in resp.content:
            if blk.type == "text":
                blocks.append(TextBlock(text=blk.text))
            elif blk.type == "tool_use":
                blocks.append(ToolUseBlock(id=blk.id, name=blk.name, input=dict(blk.input)))
        return AssistantMessage(blocks=blocks, stop_reason=resp.stop_reason or "end_turn", raw=resp)


# ---- OpenAI 实现 ----------------------------------------------------------


class OpenAIBackend(LlmBackend):
    """OpenAI Python SDK 适配后端。

    对外仍按 Anthropic 风格交互（system + messages with tool_use/tool_result blocks，
    tools 用 input_schema 字段），内部把它转换成 OpenAI chat.completions 的
    tool_calls / role=tool 形式，调用完毕再翻译回 AssistantMessage(TextBlock/ToolUseBlock)。
    这样 Agent.core 不必感知后端差异。
    """

    name = "openai"

    # OpenAI 的 finish_reason 与本项目内统一的 stop_reason 映射
    _STOP_MAP = {
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "content_filter",
    }

    # 国产 LLM 预设：(base_url, 推荐 model, 默认 api_key_env, 显示名)
    # 数据来源：2026-05 DeepSeek / DashScope 官方文档。模型 ID 选「tools 支持完整 + 性价比」挡。
    _PRESETS: dict[str, dict[str, str]] = {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
            "display": "DeepSeek (V4 Flash)",
        },
        "qwen": {
            # 国内默认；海外用户改 dashscope-intl.aliyuncs.com / dashscope-us.aliyuncs.com
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
            "api_key_env": "DASHSCOPE_API_KEY",
            "display": "Qwen (DashScope)",
        },
    }

    def __init__(
        self,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        organization: str | None = None,
        display_name: str | None = None,
    ):
        try:
            import openai  # noqa: F401
        except ImportError as e:
            raise RuntimeError("缺少依赖：pip install openai") from e
        from openai import OpenAI

        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(f"环境变量 {api_key_env} 未设置")
        client_kwargs: dict[str, Any] = {"api_key": key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if organization:
            client_kwargs["organization"] = organization
        self._client = OpenAI(**client_kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # display_name 仅供 CLI banner / 审计 metadata 显示，不影响协议
        if display_name:
            self.name = f"openai({display_name})"

    @classmethod
    def preset(
        cls,
        provider: str,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        api_key_env: str | None = None,
        base_url_override: str | None = None,
    ) -> OpenAIBackend:
        """国产 LLM 便捷构造：provider in {'deepseek', 'qwen'}。

        所有参数都可被显式 override；不传则使用 _PRESETS 中的推荐值。
        """
        key = provider.lower()
        if key not in cls._PRESETS:
            raise ValueError(
                f"未知 OpenAI 协议兼容供应商：{provider!r}；"
                f"已知预设：{sorted(cls._PRESETS)}"
            )
        p = cls._PRESETS[key]
        return cls(
            model=model or p["model"],
            max_tokens=max_tokens,
            temperature=temperature,
            api_key_env=api_key_env or p["api_key_env"],
            base_url=base_url_override or p["base_url"],
            display_name=p["display"],
        )

    # ---- 公共入口 ------------------------------------------------------

    def chat(self, system, messages, tools):
        oai_messages = self._to_openai_messages(system, messages)
        oai_tools = self._to_openai_tools(tools) if tools else None

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        return self._from_openai_choice(choice, raw=resp)

    # ---- Anthropic → OpenAI 翻译 ---------------------------------------

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    @classmethod
    def _to_openai_messages(
        cls,
        system: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "user":
                # 形态 1：纯字符串 user
                if isinstance(content, str):
                    out.append({"role": "user", "content": content})
                    continue
                # 形态 2：含 tool_result 块的 user（Anthropic 用 user 角色回传工具结果）
                if isinstance(content, list):
                    text_buf: list[str] = []
                    tool_msgs: list[dict[str, Any]] = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "text":
                            text_buf.append(c.get("text", ""))
                        elif ctype == "tool_result":
                            tool_msgs.append({
                                "role": "tool",
                                "tool_call_id": c.get("tool_use_id", ""),
                                "content": cls._flatten_tool_result(c.get("content")),
                            })
                    if text_buf:
                        out.append({"role": "user", "content": "\n".join(text_buf)})
                    out.extend(tool_msgs)
                    continue
                # 兜底
                out.append({"role": "user", "content": str(content) if content is not None else ""})

            elif role == "assistant":
                # assistant content 在 Agent.core 里始终是 list[block dict]
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
                        elif c.get("type") == "tool_use":
                            tool_calls.append({
                                "id": c.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": c.get("name", ""),
                                    "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                                },
                            })
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(p for p in text_parts if p) or None,
                }
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)

            elif role == "system":
                # 极少见，但允许显式 system 消息混入
                out.append({"role": "system", "content": content if isinstance(content, str) else str(content)})
        return out

    @staticmethod
    def _flatten_tool_result(content: Any) -> str:
        """tool_result.content 在 Anthropic 里可能是 str 或 [{type:text,text}]。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            return "".join(parts)
        return str(content)

    # ---- OpenAI → 内部统一表示 -----------------------------------------

    @classmethod
    def _from_openai_choice(cls, choice: Any, raw: Any) -> AssistantMessage:
        msg = choice.message
        blocks: list[TextBlock | ToolUseBlock] = []

        text = getattr(msg, "content", None)
        if text:
            blocks.append(TextBlock(text=text))

        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", "") or ""
            args_raw = getattr(fn, "arguments", "") or ""
            try:
                parsed = json.loads(args_raw) if args_raw else {}
                if not isinstance(parsed, dict):
                    parsed = {"_raw": parsed}
            except json.JSONDecodeError:
                parsed = {"_raw": args_raw}
            blocks.append(ToolUseBlock(id=getattr(tc, "id", "") or "", name=name, input=parsed))

        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        stop_reason = cls._STOP_MAP.get(finish_reason, finish_reason)
        return AssistantMessage(blocks=blocks, stop_reason=stop_reason, raw=raw)


# ---- Httpx 实现（OpenAI 协议兼容，零 openai SDK 依赖）---------------------


class HttpxBackend(LlmBackend):
    """基于 httpx 的 OpenAI 协议兼容后端。

    与 OpenAIBackend 接口契约完全一致（输入 system+messages+tools，输出
    AssistantMessage），但内部不 import openai —— 直接拼 HTTP 请求 +
    解析 JSON 响应。设计目标：在缺少 jiter Rust 编译环境的平台（典型为
    龙芯 LoongArch Old World，pip install openai 会自动拉 jiter 触发
    Rust 编译）上仍可对接 DeepSeek / Qwen / OpenAI 兼容服务。

    依赖：仅 httpx（项目其它地方已经在用，零新增编译依赖）。

    协议事实（2026-05 校对）：OpenAI Chat Completions JSON 响应字段
    （choices[].message.{content,tool_calls}、tool_calls[].function.
    {name,arguments}、choices[].finish_reason）与 SDK 对象属性命名严格
    一一对应；DeepSeek / DashScope 兼容端点同样遵循此协议，无需差异化。

    运行时可靠性对齐 openai SDK（_base_client.py 默认值，2026-05 校对）：
      - DEFAULT_TIMEOUT = 600s（10 分钟，覆盖长推理 / 思考模式响应）
      - DEFAULT_MAX_RETRIES = 2（共最多 3 次尝试 = 1 + 2 retry）
      - 重试触发集：HTTP 408 / 409 / 429 / 5xx，以及 httpx 的连接 / 超时
        类异常（ConnectError、ConnectTimeout、ReadTimeout、WriteTimeout、
        PoolTimeout、RemoteProtocolError）
      - Retry-After 响应头优先（数字秒数；HTTP-date 不解析，回落退避），
        上限 60 秒防恶意服务器拖死调用方
      - 退避公式：min(0.5 × 2^attempt, 8.0) + uniform(0, 0.25) jitter
      - 4xx 非重试码（400/401/403/404 等）一次失败立即抛 RuntimeError
        不浪费配额；JSON 解析失败同理 fail-fast（不是网络问题）

    上述策略与 openai 1.x SDK 行为等价 —— 协议测试 + 运行时可靠性都是
    drop-in replacement。
    """

    name = "httpx_openai"

    # 复用 OpenAIBackend 的协议常量，确保两条路径行为一致
    _STOP_MAP = OpenAIBackend._STOP_MAP
    _PRESETS = OpenAIBackend._PRESETS

    # 对齐 openai SDK 默认值（参考 openai/_base_client.py
    # DEFAULT_TIMEOUT / DEFAULT_MAX_RETRIES 常量，2026-05 校对）
    DEFAULT_TIMEOUT: float = 600.0
    DEFAULT_MAX_RETRIES: int = 2
    # 触发重试的 HTTP 状态码集合（5xx 单独判断）
    _RETRY_STATUS: frozenset[int] = frozenset({408, 409, 429})
    # 触发重试的 httpx 异常类型（连接 / 超时类）
    _RETRY_EXC_TYPES: tuple[type[BaseException], ...] = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
    )
    # 退避上限与 Retry-After 上限（秒）
    _BACKOFF_MAX: float = 8.0
    _RETRY_AFTER_MAX: float = 60.0

    def __init__(
        self,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        organization: str | None = None,
        display_name: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        client: Any = None,
    ):
        # 默认值用类常量；显式 None 表示走默认（区分"用户主动传 0/快超时"）
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        if max_retries is None:
            max_retries = self.DEFAULT_MAX_RETRIES

        # client 参数是测试 / 高级用法的注入口；正常用 None 让我们自己造
        if client is None:
            key = os.environ.get(api_key_env)
            if not key:
                raise RuntimeError(f"环境变量 {api_key_env} 未设置")
            # 规范化 base_url：必须以 / 结尾，否则 httpx 的相对路径 join
            # 会把 base 的最后一段（如 /v1）替换掉
            base = (base_url or "https://api.openai.com/v1").rstrip("/") + "/"
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            if organization:
                headers["OpenAI-Organization"] = organization
            self._client = httpx.Client(base_url=base, headers=headers, timeout=timeout)
        else:
            self._client = client

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        if display_name:
            self.name = f"httpx({display_name})"

    @classmethod
    def preset(
        cls,
        provider: str,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        api_key_env: str | None = None,
        base_url_override: str | None = None,
    ) -> HttpxBackend:
        """国产 LLM 便捷构造：provider in {'deepseek', 'qwen'}。

        与 OpenAIBackend.preset 同语义，但底层不依赖 openai SDK。
        """
        key = provider.lower()
        if key not in cls._PRESETS:
            raise ValueError(
                f"未知 OpenAI 协议兼容供应商：{provider!r}；"
                f"已知预设：{sorted(cls._PRESETS)}"
            )
        p = cls._PRESETS[key]
        return cls(
            model=model or p["model"],
            max_tokens=max_tokens,
            temperature=temperature,
            api_key_env=api_key_env or p["api_key_env"],
            base_url=base_url_override or p["base_url"],
            display_name=p["display"],
        )

    # ---- 公共入口 ------------------------------------------------------

    def chat(self, system, messages, tools):
        # 复用 OpenAIBackend 的格式翻译函数（@staticmethod，不依赖 SDK）
        oai_messages = OpenAIBackend._to_openai_messages(system, messages)
        oai_tools = OpenAIBackend._to_openai_tools(tools) if tools else None

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if oai_tools:
            payload["tools"] = oai_tools
            payload["tool_choice"] = "auto"

        return self._post_with_retry(payload)

    def _post_with_retry(self, payload: dict[str, Any]) -> AssistantMessage:
        """带重试的 POST，对齐 openai SDK _base_client 的重试语义。

        共 max_retries + 1 次尝试（默认 3 次）。重试触发：
          - HTTP 408 / 409 / 429 / 5xx 响应
          - httpx 的连接 / 超时类异常（见 _RETRY_EXC_TYPES）
        4xx 非重试码 + JSON 解析失败一次性 fail-fast。
        """
        attempt = 0
        while True:
            try:
                resp = self._client.post("chat/completions", json=payload)
            except self._RETRY_EXC_TYPES as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"HttpxBackend network error after {attempt} retries: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                self._sleep(self._compute_backoff(attempt, None))
                attempt += 1
                continue

            status = resp.status_code
            # 成功路径
            if status < 400:
                try:
                    data = resp.json()
                except Exception as e:  # noqa: BLE001
                    # JSON 解析失败：网络/服务器返回了 200 但 body 不是 JSON。
                    # 不是瞬时故障，重试无意义 → fail-fast。
                    raise RuntimeError(
                        f"HttpxBackend 响应非 JSON: {(resp.text or '')[:200]}"
                    ) from e
                return self._from_openai_dict(data)

            # 应该重试的状态码
            should_retry = (status in self._RETRY_STATUS) or (status >= 500)
            if should_retry and attempt < self.max_retries:
                retry_after = None
                try:
                    retry_after = resp.headers.get("retry-after")
                except Exception:  # noqa: BLE001
                    pass
                self._sleep(self._compute_backoff(attempt, retry_after))
                attempt += 1
                continue

            # 4xx 非重试 或 重试用尽：抛错（不打印 headers 含 Authorization；
            # body 截 500 字防 PII / token 泄露）
            body_snippet = (resp.text or "")[:500]
            suffix = f" (after {attempt} retries)" if attempt > 0 else ""
            raise RuntimeError(
                f"HttpxBackend HTTP {status} from upstream{suffix}: {body_snippet}"
            )

    @classmethod
    def _compute_backoff(cls, attempt: int, retry_after: str | None) -> float:
        """计算下一次重试前的等待秒数。

        优先：服务器 Retry-After 数值头（上限 _RETRY_AFTER_MAX 防恶意拖延）。
        兜底：min(0.5 * 2^attempt, _BACKOFF_MAX) + uniform(0, 0.25) jitter。
        HTTP-date 格式的 Retry-After 不解析，直接回落退避。
        """
        if retry_after:
            try:
                v = float(retry_after)
                if v >= 0:
                    return min(v, cls._RETRY_AFTER_MAX)
            except (ValueError, TypeError):
                pass
        base = min(0.5 * (2 ** attempt), cls._BACKOFF_MAX)
        return base + random.uniform(0, 0.25)

    @staticmethod
    def _sleep(seconds: float) -> None:
        """单独包装便于测试 monkeypatch；< 0 视为 0 不睡。"""
        if seconds > 0:
            time.sleep(seconds)

    # ---- JSON → 内部统一表示 -------------------------------------------

    @classmethod
    def _from_openai_dict(cls, data: dict[str, Any]) -> AssistantMessage:
        """把 OpenAI 协议 JSON 响应解析成 AssistantMessage。

        与 OpenAIBackend._from_openai_choice 语义等价，但从 dict 读字段
        而非从 SDK 对象读属性（字段名一一对应）。
        """
        choices = data.get("choices") or []
        if not choices:
            return AssistantMessage(blocks=[], stop_reason="end_turn", raw=data)

        choice = choices[0]
        msg = choice.get("message") or {}
        blocks: list[TextBlock | ToolUseBlock] = []

        text = msg.get("content")
        if text:
            blocks.append(TextBlock(text=text))

        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or ""
            try:
                parsed = json.loads(args_raw) if args_raw else {}
                if not isinstance(parsed, dict):
                    parsed = {"_raw": parsed}
            except json.JSONDecodeError:
                parsed = {"_raw": args_raw}
            blocks.append(ToolUseBlock(id=tc.get("id") or "", name=name, input=parsed))

        finish_reason = choice.get("finish_reason") or "stop"
        stop_reason = cls._STOP_MAP.get(finish_reason, finish_reason)
        return AssistantMessage(blocks=blocks, stop_reason=stop_reason, raw=data)


# ---- Mock 实现 ------------------------------------------------------------


_PORT_RE = re.compile(r"\b(\d{1,5})\b")
_PID_RE = re.compile(r"\bpid\s*=?\s*(\d+)|\b进程号\s*(\d+)", re.IGNORECASE)
_UNIT_RE = re.compile(r"(sshd?|nginx|httpd?|mysqld?|mariadb|redis|postgresql|docker|firewalld|kylin-\w+)")


class MockBackend(LlmBackend):
    """规则路由 mock：
       1. 看 user 最近一条消息（如果有 tool_result，进入"总结"模式）
       2. 否则按关键词匹配工具，返回 1 个 tool_use；找不到工具就给出常规中文回复。
    """

    name = "mock"

    def chat(self, system, messages, tools):
        last = messages[-1] if messages else None
        # 阶段二：上一轮 assistant 已发起 tool_use，user 在送 tool_result 进来
        if last and last["role"] == "user" and isinstance(last["content"], list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in last["content"]
        ):
            return self._summarize(last["content"])

        # 阶段一：根据 user 文本路由到工具
        text = self._extract_user_text(messages)
        if not text:
            return AssistantMessage(blocks=[TextBlock(text="（mock 后端）需要更具体的问题。")])

        tool_name, args = self._route(text)
        if tool_name is None:
            return AssistantMessage(blocks=[TextBlock(text=self._fallback_reply(text))])

        # 校验工具确实存在
        names = {t["name"] for t in tools}
        if tool_name not in names:
            return AssistantMessage(blocks=[
                TextBlock(text=f"（mock）希望调用 {tool_name} 但该工具未注册，已退回文本回复。")
            ])
        return AssistantMessage(
            blocks=[
                TextBlock(text=f"我先通过工具 `{tool_name}` 感知一下系统再回答。"),
                ToolUseBlock(id=f"mock-{uuid.uuid4().hex[:8]}", name=tool_name, input=args),
            ],
            stop_reason="tool_use",
        )

    # ---- 私有 ----------------------------------------------------------

    def _extract_user_text(self, messages: list[dict[str, Any]]) -> str:
        for m in reversed(messages):
            if m["role"] != "user":
                continue
            content = m["content"]
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        return c.get("text", "")
        return ""

    def _route(self, text: str) -> tuple[str | None, dict[str, Any]]:
        t = text.lower()
        # 重启服务（明确的高风险路由）
        if any(k in t for k in ["重启", "restart"]):
            m = _UNIT_RE.search(t)
            unit = m.group(1) if m else None
            if unit:
                return "svc_restart", {"unit": unit}
        # 服务状态
        if any(k in t for k in ["服务状态", "status", "状态"]):
            m = _UNIT_RE.search(t)
            if m:
                return "svc_status", {"unit": m.group(1)}
        # 端口占用
        if any(k in t for k in ["端口", "port"]):
            m = _PORT_RE.search(t)
            if m:
                return "lsof_port", {"port": int(m.group(1))}
        # 监听
        if any(k in t for k in ["监听", "listen", "listening"]):
            return "net_listen", {"proto": "tcp"}
        # CPU/内存高的进程
        if any(k in t for k in ["cpu", "进程", "占用", "process"]):
            sort = "cpu"
            if "内存" in t or "mem" in t:
                sort = "mem"
            return "process_list", {"sort_by": sort, "limit": 10}
        # 磁盘
        if any(k in t for k in ["磁盘", "disk", "挂载", "df"]):
            return "fs_df", {}
        # 日志
        if any(k in t for k in ["日志", "log", "journal", "错误"]):
            args: dict[str, Any] = {"lines": 50}
            if "错误" in t or "error" in t or "err" in t:
                args["priority"] = "err"
            if "ssh" in t:
                args["unit"] = "sshd"
            return "log_journal", args
        # 防火墙状态（只读路由）
        if "防火墙" in t or "firewall" in t:
            return "svc_status", {"unit": "firewalld"}
        # 软件包
        if any(k in t for k in ["软件包", "package", "已安装"]):
            return "pkg_installed", {}
        return None, {}

    def _fallback_reply(self, text: str) -> str:
        return (
            "（mock 后端）我没有匹配到合适的工具。可以试着问：\n"
            " - 哪个进程 CPU 占用最高？\n"
            " - 80 端口被谁占了？\n"
            " - sshd 服务状态？\n"
            " - 看下磁盘使用情况\n"
            " - 最近一小时的错误日志"
        )

    def _summarize(self, tool_results: list[dict[str, Any]]) -> AssistantMessage:
        parts = []
        for r in tool_results:
            if not isinstance(r, dict) or r.get("type") != "tool_result":
                continue
            content = r.get("content", "")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if r.get("is_error"):
                parts.append(f"[!] 工具返回错误：\n{content}")
            else:
                snippet = content[:1200]
                parts.append(
                    "下面是工具返回的关键内容（已截取前 1200 字）：\n"
                    "```\n"
                    f"{snippet}\n"
                    "```\n"
                    "（mock 后端不做推理总结，真实部署请配置 Anthropic 后端）"
                )
        return AssistantMessage(
            blocks=[TextBlock(text="\n\n".join(parts) or "（无结果）")],
            stop_reason="end_turn",
        )


# ---- 工厂 -----------------------------------------------------------------


class _MissingKeyError(RuntimeError):
    """专用类型：仅当环境变量缺失时由后端构造函数抛出，工厂据此判断是否降级。"""


def _construct_backend(cfg) -> LlmBackend:
    """根据 cfg.agent.llm_backend 直接构造，缺 key 时抛 _MissingKeyError。"""
    name = (cfg.agent.llm_backend or "mock").lower()

    if name == "mock":
        return MockBackend()

    if name == "anthropic":
        a = cfg.agent.anthropic
        if not os.environ.get(a.api_key_env):
            raise _MissingKeyError(a.api_key_env)
        return AnthropicBackend(
            model=a.model, max_tokens=a.max_tokens, api_key_env=a.api_key_env
        )

    if name == "openai":
        o = cfg.agent.openai
        if not os.environ.get(o.api_key_env):
            raise _MissingKeyError(o.api_key_env)
        return OpenAIBackend(
            model=o.model,
            max_tokens=o.max_tokens,
            temperature=o.temperature,
            api_key_env=o.api_key_env,
            base_url=o.base_url or None,
            organization=o.organization or None,
        )

    if name in ("deepseek", "qwen"):
        # 国产 LLM：走 OpenAI 协议兼容路径
        sub = getattr(cfg.agent, name)
        env = sub.api_key_env
        if not os.environ.get(env):
            raise _MissingKeyError(env)
        return OpenAIBackend.preset(
            provider=name,
            model=sub.model or None,
            max_tokens=sub.max_tokens,
            temperature=sub.temperature,
            api_key_env=env,
            base_url_override=sub.base_url or None,
        )

    if name in ("openai_httpx", "deepseek_httpx", "qwen_httpx"):
        # 与 openai / deepseek / qwen 同义，但走 HttpxBackend（不依赖 openai SDK）。
        # 用途：龙芯 LoongArch Old World 等缺 jiter Rust 编译环境的部署。
        # 配置层复用 cfg.agent.{openai,deepseek,qwen} 三个子节，无需新增 Pydantic 字段。
        provider = name.removesuffix("_httpx")
        if provider == "openai":
            sub = cfg.agent.openai
            env = sub.api_key_env
            if not os.environ.get(env):
                raise _MissingKeyError(env)
            return HttpxBackend(
                model=sub.model,
                max_tokens=sub.max_tokens,
                temperature=sub.temperature,
                api_key_env=sub.api_key_env,
                base_url=sub.base_url or None,
                organization=sub.organization or None,
            )
        sub = getattr(cfg.agent, provider)
        env = sub.api_key_env
        if not os.environ.get(env):
            raise _MissingKeyError(env)
        return HttpxBackend.preset(
            provider=provider,
            model=sub.model or None,
            max_tokens=sub.max_tokens,
            temperature=sub.temperature,
            api_key_env=env,
            base_url_override=sub.base_url or None,
        )

    raise ValueError(f"未知 LLM 后端：{name}")


def build_backend(cfg) -> LlmBackend:
    """根据配置构造后端；缺 key 时按 fallback_to_mock 策略降级到 Mock。

    降级规则（仅 key 缺失）：
      - cfg.agent.fallback_to_mock = True（默认）：WARN + 返回 MockBackend
      - cfg.agent.fallback_to_mock = False：直接 raise，用于 CI / 生产
    其他错误（缺依赖包、配置非法）一律 raise，不被吞掉。
    """
    name = (cfg.agent.llm_backend or "mock").lower()
    try:
        backend = _construct_backend(cfg)
    except _MissingKeyError as e:
        env_var = str(e)
        if not getattr(cfg.agent, "fallback_to_mock", True):
            raise RuntimeError(
                f"LLM 后端 {name!r} 需要环境变量 {env_var}，但未设置；"
                f"且 agent.fallback_to_mock=false 禁止降级。"
            ) from e
        logger.warning(
            "LLM 后端 %r 缺少环境变量 %s，已自动降级到 mock。"
            "设置该变量并重启即可启用真实后端。",
            name, env_var,
        )
        mb = MockBackend()
        # 把降级原因挂到实例上，CLI / 审计可读取
        mb.fallback_from = name  # type: ignore[attr-defined]
        mb.fallback_reason = f"环境变量 {env_var} 未设置"  # type: ignore[attr-defined]
        return mb
    return backend
