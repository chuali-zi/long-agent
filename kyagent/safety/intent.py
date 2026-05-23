"""自然语言意图风险过滤 + Prompt Injection 检测。

赛题第 3 条要求：
  (a) 建立风险识别模型或规则库，对 LLM 生成的原始指令进行"二次过滤" → 已由 guardrail.py 承担
  (b) 对自然语言指令的意图风险过滤                                   → 由本模块承担
  (c) 抗注入能力：识别 Prompt Inject，防止攻击者通过对话诱导执行恶意代码 → 由本模块承担

设计原则：
  · 与 argv 层 Guardrail 同构（共用 RiskLevel / Decision / Policy / Verdict）
  · 零额外依赖（不引入 deberta / llama-prompt-guard 等本地模型，避免破坏 ask p50 baseline）
  · 三段式处理：
      1) 文本归一化（NFKC + 去零宽 + 同形异码替换 + lowercase）
      2) 解码变体（base64 第一层试解）
      3) 关键词扫描 + 正则扫描 + 长度闸
  · 命中即取最高 risk → Policy 映射 allow/confirm/deny
  · sanitized_text：剥离零宽 / RTL 覆盖字符的副本，可选回填给 LLM（防 stealth injection）
"""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kyagent.safety.patterns import RiskLevel
from kyagent.safety.policy import Decision, Policy


# ---- Unicode 归一化 ------------------------------------------------------
# 零宽 + RTL/LTR 覆盖 + BOM + 标签字符（U+E0000..U+E007F）
_ZERO_WIDTH = re.compile(
    "[​-‏‪-‮⁠-⁯﻿]"
    "|[\U000e0000-\U000e007f]"
)
# 同形异码：常见的西里尔 → 拉丁，覆盖最简单的伪装
_HOMOGLYPH = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c", "У": "y", "Х": "x",
    "В": "b", "Н": "h", "К": "k", "М": "m", "Т": "t",
})


def _normalize(text: str) -> str:
    """NFKC 归一化 → 去零宽 → 同形异码替换 → lowercase。"""
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t)
    return t.translate(_HOMOGLYPH).lower()


def _decode_variants(text: str) -> list[str]:
    """对长 base64 串试解一层，作为附加扫描变体。"""
    variants: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9+/=]{20,}", text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", "ignore")
            if decoded.strip():
                variants.append(decoded)
        except Exception:
            pass
    return variants


# ---- 规则数据结构 -------------------------------------------------------


@dataclass
class IntentRule:
    id: str
    risk: RiskLevel
    category: str
    keywords: list[str] = field(default_factory=list)
    pattern: re.Pattern[str] | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "IntentRule":
        pat = raw.get("pattern")
        return cls(
            id=raw["id"],
            risk=RiskLevel.parse(raw["risk"]),
            category=raw.get("category", "misc"),
            keywords=[k.lower() for k in raw.get("keywords", [])],
            pattern=re.compile(pat) if pat else None,
            description=raw.get("description", ""),
        )


def load_intent_rules(path: str | Path) -> list[IntentRule]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [IntentRule.from_dict(r) for r in data.get("rules", [])]


# ---- Verdict -----------------------------------------------------------


@dataclass
class IntentHit:
    rule_id: str
    risk: RiskLevel
    category: str
    matched: str
    variant: str = "normalized"  # raw / normalized / base64

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "risk": self.risk.value,
            "category": self.category,
            "matched": self.matched,
            "variant": self.variant,
        }


@dataclass
class IntentVerdict:
    text: str
    decision: Decision
    risk: RiskLevel
    hits: list[IntentHit] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    sanitized_text: str | None = None  # 若与原文有差异（剥了零宽字符）

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "risk": self.risk.value,
            "hits": [h.to_dict() for h in self.hits],
            "rationale": list(self.rationale),
            "text_preview": self.text[:200],
            "sanitized": self.sanitized_text is not None,
        }

    def is_blocked(self) -> bool:
        return self.decision is Decision.DENY

    def needs_confirm(self) -> bool:
        return self.decision is Decision.CONFIRM


# ---- IntentGuard ---------------------------------------------------------


class IntentGuard:
    """自然语言意图层意图 + injection 扫描器。"""

    MAX_INPUT_LEN = 8000  # 输入长度上限（防 prompt stuffing）

    def __init__(self, rules: list[IntentRule], policy: Policy):
        self.rules = rules
        self.policy = policy

    @classmethod
    def from_config(cls, cfg) -> "IntentGuard":
        rules_path = cfg.resolve(getattr(cfg.safety, "intent_rules_file",
                                         "configs/intent-rules.yaml"))
        rules = load_intent_rules(str(rules_path))
        policy = Policy.from_config(cfg.safety.policy)
        return cls(rules, policy)

    def evaluate(self, text: str, context: dict | None = None) -> IntentVerdict:
        """对用户原始自然语言做风险评估 + injection 检测。"""
        rationale: list[str] = []
        hits: list[IntentHit] = []

        # 输入长度闸（防 prompt stuffing）
        if len(text) > self.MAX_INPUT_LEN:
            rationale.append(f"输入长度 {len(text)} 超过 {self.MAX_INPUT_LEN}，按 HIGH 处理")
            hits.append(IntentHit(
                "LEN_OVERFLOW", RiskLevel.HIGH, "injection",
                matched=f"len={len(text)}", variant="raw",
            ))

        # 零宽字符独立预警（NFKC 归一化前判断，因为归一化后会消失）
        has_zero_width = bool(_ZERO_WIDTH.search(text))
        if has_zero_width:
            rationale.append("原文含零宽/RTL/标签字符 — 标记为 INJECTION/HIGH")
            hits.append(IntentHit(
                "UNICODE_HIDDEN", RiskLevel.HIGH, "injection",
                matched="zero-width or bidi-override chars", variant="raw",
            ))

        normalized = _normalize(text)
        sanitized = _ZERO_WIDTH.sub("", text)
        base64_variants = _decode_variants(normalized)

        variants: list[tuple[str, str]] = [
            ("raw", text.lower()),
            ("normalized", normalized),
        ]
        for v in base64_variants:
            variants.append(("base64", v.lower()))

        # 规则扫描：每条规则在每个变体上跑一遍，命中即记
        for rule in self.rules:
            for variant_name, blob in variants:
                matched_token: str | None = None
                for kw in rule.keywords:
                    if kw in blob:
                        matched_token = kw
                        break
                if matched_token is None and rule.pattern is not None:
                    m = rule.pattern.search(blob)
                    if m:
                        matched_token = m.group(0)[:120]
                if matched_token is not None:
                    hits.append(IntentHit(
                        rule.id, rule.risk, rule.category,
                        matched=matched_token, variant=variant_name,
                    ))
                    break  # 同一规则一个变体命中即可

        # 风险合成
        if hits:
            risk = RiskLevel.max([h.risk for h in hits])
            rationale.append(f"命中 {len(hits)} 条意图规则，最高 risk={risk.value}")
        else:
            risk = RiskLevel.LOW
            rationale.append("未命中任何意图/注入规则")

        decision = self.policy.decide(risk)
        rationale.append(f"策略映射: {risk.value} -> {decision.value}")

        return IntentVerdict(
            text=text,
            decision=decision,
            risk=risk,
            hits=hits,
            rationale=rationale,
            sanitized_text=sanitized if sanitized != text else None,
        )
