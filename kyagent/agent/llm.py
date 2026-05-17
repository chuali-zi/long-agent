"""LLM 后端抽象。

提供三种后端：
  - AnthropicBackend：调用真实 Claude API（需 ANTHROPIC_API_KEY）
  - OpenAIBackend：调用 OpenAI Chat Completions（需 OPENAI_API_KEY），同时兼容
                   任何 OpenAI 协议（Azure OpenAI / vLLM / DeepSeek / 智谱 / Ollama …）
  - MockBackend：纯规则路由，不需要任何外部依赖；可用于离线 demo / CI

每个后端实现统一的 chat(messages, tools) -> AssistantMessage。
Agent.core 始终以 Anthropic 风格组装 messages/tools；OpenAIBackend 内部完成双向翻译。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any


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

    def __init__(
        self,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        organization: str | None = None,
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


def build_backend(cfg) -> LlmBackend:
    """根据配置构造后端。"""
    name = (cfg.agent.llm_backend or "mock").lower()
    if name == "mock":
        return MockBackend()
    if name == "anthropic":
        return AnthropicBackend(
            model=cfg.agent.anthropic.model,
            max_tokens=cfg.agent.anthropic.max_tokens,
            api_key_env=cfg.agent.anthropic.api_key_env,
        )
    if name == "openai":
        return OpenAIBackend(
            model=cfg.agent.openai.model,
            max_tokens=cfg.agent.openai.max_tokens,
            temperature=cfg.agent.openai.temperature,
            api_key_env=cfg.agent.openai.api_key_env,
            base_url=cfg.agent.openai.base_url,
            organization=cfg.agent.openai.organization,
        )
    raise ValueError(f"未知 LLM 后端：{name}")
