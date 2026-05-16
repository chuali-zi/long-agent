"""风险等级 → 处置决策 的策略映射。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kyagent.config import SafetyPolicy
from kyagent.safety.patterns import RiskLevel


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"   # 需要用户二次确认
    DENY = "deny"

    @property
    def order(self) -> int:
        return {"allow": 0, "confirm": 1, "deny": 2}[self.value]


@dataclass
class Policy:
    """把 risk -> action 字符串转换成 Decision。"""
    critical: Decision
    high: Decision
    medium: Decision
    low: Decision

    @classmethod
    def from_config(cls, cfg: SafetyPolicy) -> "Policy":
        return cls(
            critical=Decision(cfg.critical),
            high=Decision(cfg.high),
            medium=Decision(cfg.medium),
            low=Decision(cfg.low),
        )

    def decide(self, risk: RiskLevel) -> Decision:
        return getattr(self, risk.value)
