"""Pydantic request / response schemas for the Web API.

Uses Pydantic v1 (project-wide constraint — see pyproject.toml). All
schemas are intentionally narrow so we can swap to v2 / OpenAPI without
churn.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, validator


# ---- /api/ask -------------------------------------------------------------

def validate_session_id(value: Optional[str]) -> Optional[str]:
    if value is not None and (
        len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
    ):
        raise ValueError("session_id format is invalid")
    return value


class AskRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000, description="用户提问")
    user: str = Field("web", min_length=1, max_length=64)
    session_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="可选会话 ID；同一 session_id 共享 Agent.messages 上下文",
    )

    @validator("session_id")
    def _check_session_id(cls, value: Optional[str]) -> Optional[str]:  # noqa: N805
        return validate_session_id(value)


class AskResponse(BaseModel):
    trace_id: str
    text: str
    tool_iterations: int
    denied: bool
    notes: list[str]
    backend: str


# ---- /api/approvals -------------------------------------------------------


class ApprovalRecordResponse(BaseModel):
    approval_id: str
    title: str
    risk: str
    summary_lines: list[str]
    body: Optional[str] = None
    cmdline: str = ""
    session_id: Optional[str] = None
    user: str
    created_at: float
    expires_at: float
    status: str
    approved: Optional[bool] = None
    reviewer: str = ""
    reason: str = ""
    resolved_at: Optional[float] = None


class ApprovalListResponse(BaseModel):
    count: int
    approvals: list[ApprovalRecordResponse]


class ApprovalActionRequest(BaseModel):
    reviewer: str = Field("web", min_length=1, max_length=64)
    reason: str = Field("", max_length=500)


# ---- /api/choices ---------------------------------------------------------


class ChoiceOptionResponse(BaseModel):
    value: str
    label: str
    description: str = ""


class ChoiceRecordResponse(BaseModel):
    choice_id: str
    question: str
    options: list[ChoiceOptionResponse]
    session_id: Optional[str] = None
    user: str
    created_at: float
    expires_at: float
    status: str
    value: str = ""
    reviewer: str = ""
    resolved_at: Optional[float] = None


class ChoiceListResponse(BaseModel):
    count: int
    choices: list[ChoiceRecordResponse]


class ChoiceActionRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=128)
    reviewer: str = Field("web", min_length=1, max_length=64)


# ---- /api/tools -----------------------------------------------------------


class ToolDescriptor(BaseModel):
    name: str
    description: str
    risk: str
    requires_root: bool
    read_only: bool
    input_schema: dict[str, Any]


class ToolListResponse(BaseModel):
    count: int
    tools: list[ToolDescriptor]


# ---- /api/safety/check ----------------------------------------------------


_LAYER_CHOICES = {"intent", "argv", "both"}


class SafetyCheckRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    layer: str = Field("both", description="intent / argv / both — both 综合取最严裁决")

    # 自己写 validator 而非 Field(regex=) / Field(pattern=)，以兼容 pydantic v1/v2
    # （v1 用 ``regex=``、v2 改为 ``pattern=``，二者不能并存）
    @validator("layer")
    def _check_layer(cls, v: str) -> str:  # noqa: N805
        if v not in _LAYER_CHOICES:
            raise ValueError(f"layer 必须是 {sorted(_LAYER_CHOICES)} 之一")
        return v


class SafetyHit(BaseModel):
    rule_id: str
    description: Optional[str] = None
    risk: str
    matched: Optional[str] = None
    category: Optional[str] = None


class SafetyVerdictBlock(BaseModel):
    layer: str  # "intent" | "argv"
    risk: str
    decision: str
    hits: list[SafetyHit]
    rationale: list[str]


class SafetyCheckResponse(BaseModel):
    intent: Optional[SafetyVerdictBlock] = None
    argv: Optional[SafetyVerdictBlock] = None
    final_risk: Optional[str] = None
    final_decision: Optional[str] = None


# ---- /api/audit -----------------------------------------------------------


class TraceSummary(BaseModel):
    trace_id: str
    user: str
    started_at: float
    channel: str = ""


class TraceListResponse(BaseModel):
    count: int
    traces: list[TraceSummary]


class TraceEvent(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any]


class TraceDetailResponse(BaseModel):
    trace_id: str
    events: list[TraceEvent]


# ---- /api/health ----------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
