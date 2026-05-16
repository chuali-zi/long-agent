"""规则数据结构与 YAML 加载。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def order(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]

    @classmethod
    def parse(cls, s: str) -> "RiskLevel":
        return cls(s.lower())

    @classmethod
    def max(cls, levels: list["RiskLevel"]) -> "RiskLevel":
        return max(levels, key=lambda r: r.order) if levels else cls.LOW


@dataclass
class Rule:
    """从 YAML 反序列化得到的单条规则。所有字段是 AND 关系，空字段忽略。"""

    id: str
    risk: RiskLevel
    description: str
    pattern: re.Pattern[str] | None = None
    command: str | None = None
    flags_any: list[str] = field(default_factory=list)
    flags_all: list[str] = field(default_factory=list)
    target_in: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Rule":
        pat = raw.get("pattern")
        return cls(
            id=raw["id"],
            risk=RiskLevel.parse(raw["risk"]),
            description=raw.get("description", ""),
            pattern=re.compile(pat) if pat else None,
            command=raw.get("command"),
            flags_any=list(raw.get("flags_any", [])),
            flags_all=list(raw.get("flags_all", [])),
            target_in=list(raw.get("target_in", [])),
        )


def load_rules(path: str | Path) -> list[Rule]:
    """从 YAML 文件加载规则。"""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [Rule.from_dict(r) for r in data.get("rules", [])]
