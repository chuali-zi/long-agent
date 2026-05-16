"""LLM 后端抽象。

提供两种后端：
  - AnthropicBackend：调用真实 Claude API（需 ANTHROPIC_API_KEY）
  - MockBackend：纯规则路由，不需要任何外部依赖；可用于离线 demo / CI

每个后端实现统一的 chat(messages, tools) -> AssistantMessage。
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

    def __init__(self, model: str, max_tokens: int, api_key_env: str = "ANTHROPIC_API_KEY"):
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

    def chat(self, system, messages, tools):
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools or None,
        )
        blocks: list[TextBlock | ToolUseBlock] = []
        for blk in resp.content:
            if blk.type == "text":
                blocks.append(TextBlock(text=blk.text))
            elif blk.type == "tool_use":
                blocks.append(ToolUseBlock(id=blk.id, name=blk.name, input=dict(blk.input)))
        return AssistantMessage(blocks=blocks, stop_reason=resp.stop_reason or "end_turn", raw=resp)


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
    raise ValueError(f"未知 LLM 后端：{name}")
