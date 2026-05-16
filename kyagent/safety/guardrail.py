"""安全护栏主流水线：把规则引擎 + 策略 + 可选 LLM 复审组合成完整流程。

Verdict 的产出是 Executor 是否放行的唯一依据；Verdict 同时落到审计 trace 上。
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

from kyagent.config import Config
from kyagent.safety.patterns import RiskLevel
from kyagent.safety.policy import Decision, Policy
from kyagent.safety.rules import Hit, RuleEngine


@dataclass
class Verdict:
    """一次安全校验的产出。"""

    cmdline: str
    decision: Decision
    risk: RiskLevel
    hits: list[Hit] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cmdline": self.cmdline,
            "decision": self.decision.value,
            "risk": self.risk.value,
            "hits": [
                {"rule_id": h.rule_id, "risk": h.risk.value,
                 "description": h.description, "matched": h.matched}
                for h in self.hits
            ],
            "rationale": list(self.rationale),
        }

    def is_blocked(self) -> bool:
        return self.decision is Decision.DENY

    def needs_confirm(self) -> bool:
        return self.decision is Decision.CONFIRM

    @property
    def is_allowed(self) -> bool:
        return self.decision is Decision.ALLOW


# 可选 LLM 复审：接受 cmdline，返回 (risk, rationale) 或 None
LlmReviewer = Callable[[str], Optional[tuple[RiskLevel, str]]]


class Guardrail:
    """多级安全流水线。

    流程：
      Stage 1  规则引擎扫描（patterns / argv / target）
      Stage 2  策略映射 (risk -> Decision)
      Stage 3  （可选）LLM 二次复审：若返回更高 risk，会提升 decision
      Stage 4  最终决策：取所有阶段中最严的处置
    """

    def __init__(
        self,
        engine: RuleEngine,
        policy: Policy,
        llm_reviewer: LlmReviewer | None = None,
    ):
        self.engine = engine
        self.policy = policy
        self.llm_reviewer = llm_reviewer

    @classmethod
    def from_config(cls, cfg: Config, llm_reviewer: LlmReviewer | None = None) -> "Guardrail":
        rules_path = cfg.resolve(cfg.safety.rules_file)
        engine = RuleEngine.from_yaml(str(rules_path))
        policy = Policy.from_config(cfg.safety.policy)
        if not cfg.safety.llm_review:
            llm_reviewer = None
        return cls(engine, policy, llm_reviewer)

    # ---- 主入口 --------------------------------------------------------

    def check_cmdline(self, cmdline: str, declared_risk: RiskLevel | None = None) -> Verdict:
        return self._check(cmdline, declared_risk=declared_risk)

    def check_argv(self, argv: list[str], declared_risk: RiskLevel | None = None) -> Verdict:
        cmdline = " ".join(shlex.quote(a) for a in argv)
        return self._check(cmdline, declared_risk=declared_risk)

    # ---- 内部 ----------------------------------------------------------

    def _check(self, cmdline: str, declared_risk: RiskLevel | None = None) -> Verdict:
        rationale: list[str] = []
        hits = self.engine.scan_cmdline(cmdline)

        if hits:
            risk = RiskLevel.max([h.risk for h in hits])
            rationale.append(f"命中 {len(hits)} 条规则，最高 risk={risk.value}")
        else:
            risk = RiskLevel.LOW
            rationale.append("未命中任何危险模式")

        # 工具自己声明的 risk 作为下限（高风险工具即使没命中规则也至少 confirm）
        if declared_risk is not None and declared_risk.order > risk.order:
            rationale.append(
                f"工具声明 risk={declared_risk.value} 高于规则结果，按工具声明提升"
            )
            risk = declared_risk

        decision = self.policy.decide(risk)
        rationale.append(f"策略映射: {risk.value} -> {decision.value}")

        # 可选 LLM 复审：只能升级 risk/decision，不能降级
        if self.llm_reviewer is not None:
            try:
                reviewed = self.llm_reviewer(cmdline)
            except Exception as e:
                reviewed = None
                rationale.append(f"LLM 复审异常: {e!r}")
            if reviewed:
                rev_risk, rev_reason = reviewed
                rationale.append(f"LLM 复审: risk={rev_risk.value} reason={rev_reason}")
                if rev_risk.order > risk.order:
                    risk = rev_risk
                    new_decision = self.policy.decide(risk)
                    if new_decision.order > decision.order:
                        decision = new_decision
                        rationale.append(f"复审提升决策为 {decision.value}")

        return Verdict(
            cmdline=cmdline,
            decision=decision,
            risk=risk,
            hits=hits,
            rationale=rationale,
        )
