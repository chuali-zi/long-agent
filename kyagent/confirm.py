"""UI 层契约：统一所有"需要用户二次确认"请求的形状。

为什么放在顶层包而不是 safety 包？
    ConfirmRequest 是 UI 数据，不是 safety domain。把它放在 safety 包下，会让
    safety 反过来依赖 UI，违反 Clean Architecture 的 Dependency Rule。
    它属于跨层的契约——UI 端的渲染器、Agent 层的调用方、各种通道（CLI / 未来的
    Slack / Web）都依赖它，但它谁都不依赖。

为什么不让 Verdict 自带 to_confirm_request？
    Verdict 是纯 domain 对象（"裁决了什么"），不该认识 UI 数据；也不该接收
    tool_name / argv 这种"调用上下文"。翻译逻辑放在 adapter 层
    （kyagent/agent/confirm_adapter.py），让 safety domain 保持纯净。
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
      summary_lines: 命中说明列表，一行一条。由 adapter 决定一行写什么。
      body:          可选正文（argv / rationale 等）。
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
