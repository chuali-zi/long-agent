"""Verdict → ConfirmRequest 翻译层。

依赖方向（Clean Architecture）：
    本模块**同时**认识 safety domain（Verdict/IntentVerdict）和 UI 契约
    （ConfirmRequest），充当二者之间的 adapter。

    Safety domain 自身不应认识 ConfirmRequest——那是 UI 数据，让 domain 依赖 UI
    会让"危险判定"这件事被 UI 字段牵着走（UI 加一个 mitigation 字段，所有
    Verdict 类型都得跟着改）。把翻译逻辑收敛在这里，保证 safety 只产纯 domain
    verdict，UI 只认 ConfirmRequest，谁都不污染谁。

    调用上下文（tool_name / argv）作为函数参数传入，而不是塞进 Verdict——
    它们不是 Verdict 的内在属性，是触发它的"调用站点信息"。
"""
from __future__ import annotations

from kyagent.confirm import ConfirmRequest
from kyagent.safety.guardrail import Verdict
from kyagent.safety.intent import IntentVerdict


def for_tool_call(verdict: Verdict, tool_name: str, argv: list[str]) -> ConfirmRequest:
    """argv 层裁决 + 调用上下文 → ConfirmRequest。"""
    return ConfirmRequest(
        title=f"tool {tool_name}",
        risk=verdict.risk.value,
        summary_lines=[
            f"{h.rule_id} ({h.risk.value}): {h.description}"
            for h in verdict.hits
        ],
        body=" ".join(argv),
    )


def for_intent(verdict: IntentVerdict) -> ConfirmRequest:
    """意图层裁决 → ConfirmRequest。

    IntentHit 没有 description（intent 规则的 description 在 IntentRule 上而非
    hit 上），所以展示 category + matched token，足以让用户判断。
    """
    return ConfirmRequest(
        title="自然语言意图审查",
        risk=verdict.risk.value,
        summary_lines=[
            f"{h.rule_id} ({h.risk.value}, {h.category}): matched={h.matched!r}"
            for h in verdict.hits
        ],
        body="\n".join(verdict.rationale) if verdict.rationale else None,
    )
