"""端到端集成测试：mock LLM + 内置工具 + guardrail + audit 闭环。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kyagent.agent.core import Agent
from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind
from kyagent.config import Config


@pytest.fixture
def agent(tmp_path):
    cfg = Config(base_dir=Path(__file__).parent.parent)
    cfg.audit.database = str(tmp_path / "a.db")
    cfg.audit.jsonl_file = str(tmp_path / "a.jsonl")
    cfg.safety.rules_file = "configs/safety-rules.yaml"
    cfg.agent.llm_backend = "mock"
    return Agent.from_config(cfg, confirm=lambda *a, **k: False)


def test_low_risk_query_flows_through(agent):
    """问 CPU 占用 → mock 触发 process_list → 规则放行 → 执行 → 审计完整。"""
    result = agent.ask("查下 CPU 占用最高的进程")
    assert not result.denied
    # 推理链必须包含：USER_INPUT、LLM_THOUGHT、TOOL_REQUEST、SAFETY_CHECK、EXECUTION、EXECUTION_RESULT、AGENT_REPLY
    kinds = [e.kind.value for e in result.trace.events]
    assert EventKind.USER_INPUT.value in kinds
    assert EventKind.TOOL_REQUEST.value in kinds
    assert EventKind.SAFETY_CHECK.value in kinds
    assert EventKind.EXECUTION.value in kinds
    assert EventKind.AGENT_REPLY.value in kinds


def test_high_risk_tool_denied_in_oneshot(agent):
    """重启服务 → svc_restart 声明 HIGH → confirm 被拒绝 → denied=True。"""
    result = agent.ask("重启 nginx")
    assert result.denied
    # 至少有一次 SAFETY_CHECK 事件
    safety_events = [e for e in result.trace.events if e.kind is EventKind.SAFETY_CHECK]
    assert safety_events
    # 第一次 verdict 应为 confirm（或更严）
    first_verdict = safety_events[0].payload
    assert first_verdict["decision"] in ("confirm", "deny")


def test_agent_audit_persistence(agent):
    """跑完 ask 后能从 SQLite 取回完整事件流。"""
    result = agent.ask("查 22 端口")
    store: AuditStore = agent.audit.store
    events = store.get_events(result.trace.trace_id)
    assert len(events) >= 5
    assert events[0]["kind"] == EventKind.USER_INPUT.value
    assert events[-1]["kind"] in (EventKind.AGENT_REPLY.value, EventKind.ERROR.value)


def test_unknown_query_fallback(agent):
    """无法路由的提问应得到 mock 兜底文本，不应崩。"""
    result = agent.ask("帮我写一首五言绝句")
    assert result.final_text
    assert not result.denied
