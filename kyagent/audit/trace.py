"""推理链 trace：一次用户请求从接收到回复的完整事件流。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    USER_INPUT = "user_input"          # 1. 接收指令
    PERCEPTION = "perception"          # 2. 感知环境（工具被动收集的上下文）
    LLM_THOUGHT = "llm_thought"        # 3. 推理决策（LLM 思维链 / 文本输出）
    TOOL_REQUEST = "tool_request"      # 3b. LLM 提议调用工具（含原始参数）
    SAFETY_CHECK = "safety_check"      # 4. 安全校验（命中规则 + verdict）
    EXECUTION = "execution"            # 5. 命令实际执行（落地账户、cmdline）
    EXECUTION_RESULT = "execution_result"  # 5b. 执行结果
    AGENT_REPLY = "agent_reply"        # 6. Agent 最终回复给用户
    ERROR = "error"


@dataclass
class TraceEvent:
    """trace 中的单条事件。"""
    seq: int
    kind: EventKind
    ts: float
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "ts": self.ts,
            "payload": self.payload,
        }


@dataclass
class Trace:
    """单次 user turn 的推理链。每条 trace 由唯一 trace_id 串联所有事件。"""

    trace_id: str = field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:12]}")
    user: str = "anonymous"
    started_at: float = field(default_factory=time.time)
    events: list[TraceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _seq: int = 0

    def add(self, kind: EventKind, payload: dict[str, Any] | None = None) -> TraceEvent:
        self._seq += 1
        ev = TraceEvent(
            seq=self._seq,
            kind=kind,
            ts=time.time(),
            payload=payload or {},
        )
        self.events.append(ev)
        return ev

    def duration(self) -> float:
        if not self.events:
            return 0.0
        return self.events[-1].ts - self.started_at

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for ev in self.events:
            counts[ev.kind.value] = counts.get(ev.kind.value, 0) + 1
        return {
            "trace_id": self.trace_id,
            "user": self.user,
            "started_at": self.started_at,
            "duration": round(self.duration(), 3),
            "event_count": len(self.events),
            "by_kind": counts,
        }
