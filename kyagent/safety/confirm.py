"""Confirm 协议：统一所有"需要用户二次确认"请求的形状。

设计动机（解耦）：
    argv 层 Verdict 与 intent 层 IntentVerdict 的 hits 结构不同，但都需要走同一个
    交互式 confirm UI。如果让 UI 直接读 verdict.to_dict()，UI 就被迫认识每一种
    verdict 的字段；再加一种 verdict 类型时 UI 必须改。

    本模块定义一个 UI-friendly 的中间形态 ConfirmRequest：
      · 每种 verdict 自己负责通过 to_confirm_request(...) 把自己"翻译"成它；
      · UI 层（CLI 的 _cli_confirm / 其它通道）只认 ConfirmRequest，
        对 verdict 类型零依赖。

    新增 verdict 类型 → 实现 to_confirm_request(...) → CLI 完全不用改（OCP）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class ConfirmRequest:
    """需要用户审查放行的一次请求。

    所有字段都已 stringify，UI 层直接渲染即可，无需再了解上游数据结构。

    Fields:
      title:         展示给用户的标题（"tool rm" / "自然语言意图审查"）。
      risk:          风险等级字符串（"low" / "medium" / "high" / "critical"）。
      summary_lines: 命中说明列表，一行一条。由生产方决定一行写什么
                     （argv 层："{rule_id} ({risk}): {description}"；
                       intent 层："{rule_id} ({risk}, {category}): {matched!r}"）。
      body:          可选正文。argv 层一般放 cmdline；intent 层放 rationale。
    """

    title: str
    risk: str
    summary_lines: list[str] = field(default_factory=list)
    body: str | None = None


# Confirm 回调签名：调用方只需要看到 ConfirmRequest，与具体 verdict 类型解耦。
ConfirmFn = Callable[[ConfirmRequest], bool]


def auto_deny(_req: ConfirmRequest) -> bool:
    """默认回调：无人值守通道一律拒绝。CLI 会注入交互版本覆盖它。"""
    return False
