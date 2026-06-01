"""In-memory approval broker for the FastAPI interactive channel."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from kyagent.confirm import ConfirmRequest


ApprovalEmitter = Callable[[str, dict], None]


@dataclass
class ApprovalRecord:
    approval_id: str
    title: str
    risk: str
    summary_lines: list[str]
    body: str | None
    session_id: str | None
    user: str
    created_at: float
    expires_at: float
    status: str = "pending"
    approved: bool | None = None
    reviewer: str = ""
    reason: str = ""
    resolved_at: float | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _emit: ApprovalEmitter | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "title": self.title,
            "risk": self.risk,
            "summary_lines": list(self.summary_lines),
            "body": self.body,
            "cmdline": self.body or "",
            "session_id": self.session_id,
            "user": self.user,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "approved": self.approved,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
        }


class ApprovalBroker:
    """Thread-safe pending approval registry.

    The Agent confirm callback creates a record and blocks on ``wait``. Browser
    endpoints resolve the record, then the waiting Agent turn continues.
    """

    def __init__(self, timeout_seconds: float = 300.0, max_records: int = 256):
        self.timeout_seconds = timeout_seconds
        self.max_records = max(1, max_records)
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    def create(
        self,
        req: ConfirmRequest,
        *,
        session_id: str | None,
        user: str,
        emit: ApprovalEmitter | None = None,
    ) -> ApprovalRecord:
        now = time.time()
        rec = ApprovalRecord(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            title=req.title,
            risk=req.risk,
            summary_lines=list(req.summary_lines or []),
            body=req.body,
            session_id=session_id,
            user=user,
            created_at=now,
            expires_at=now + self.timeout_seconds,
            _emit=emit,
        )
        with self._lock:
            self._cleanup_locked(now)
            self._records[rec.approval_id] = rec
            self._trim_locked()
        return rec

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            self._cleanup_locked(time.time())
            return self._records.get(approval_id)

    def list_records(self, *, status: str | None = None) -> list[ApprovalRecord]:
        with self._lock:
            self._cleanup_locked(time.time())
            records = list(self._records.values())
        if status:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def wait(self, approval_id: str) -> bool:
        rec = self.get(approval_id)
        if rec is None:
            return False
        if not rec._event.wait(self.timeout_seconds):
            rec = self.resolve(
                approval_id,
                approved=False,
                reviewer="system",
                reason="approval timeout",
                emit=True,
            )
        return bool(rec and rec.approved)

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        reviewer: str,
        reason: str = "",
        emit: bool = True,
    ) -> ApprovalRecord | None:
        callback: ApprovalEmitter | None = None
        payload: dict | None = None
        event_to_set: threading.Event | None = None
        with self._lock:
            rec = self._records.get(approval_id)
            if rec is None:
                return None
            if rec.status == "pending" and time.time() >= rec.expires_at:
                approved = False
                reviewer = "system"
                reason = "approval timeout"
            if rec.status == "pending":
                rec.approved = approved
                rec.status = "approved" if approved else "rejected"
                rec.reviewer = reviewer
                rec.reason = reason
                rec.resolved_at = time.time()
                event_to_set = rec._event
                callback = rec._emit
                payload = rec.to_dict()
            else:
                payload = rec.to_dict()
        if emit and callback is not None and payload is not None:
            callback("approval_resolved", payload)
        if event_to_set is not None:
            event_to_set.set()
        return rec

    def _cleanup_locked(self, now: float) -> None:
        for rec in self._records.values():
            if rec.status == "pending" and now >= rec.expires_at:
                rec.approved = False
                rec.status = "rejected"
                rec.reviewer = "system"
                rec.reason = "approval timeout"
                rec.resolved_at = now
                rec._event.set()

    def _trim_locked(self) -> None:
        while len(self._records) > self.max_records:
            oldest = min(self._records.values(), key=lambda rec: rec.created_at)
            if oldest.status == "pending":
                oldest.approved = False
                oldest.status = "rejected"
                oldest.reviewer = "system"
                oldest.reason = "approval record limit exceeded"
                oldest.resolved_at = time.time()
                oldest._event.set()
            self._records.pop(oldest.approval_id, None)
