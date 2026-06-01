"""In-memory broker for Agent initiated closed-set user choices."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from kyagent.interactive import UserChoice


ChoiceEmitter = Callable[[str, dict], None]


@dataclass
class ChoiceRecord:
    choice_id: str
    question: str
    options: list[dict[str, str]]
    session_id: str | None
    user: str
    created_at: float
    expires_at: float
    status: str = "pending"
    value: str = ""
    reviewer: str = ""
    resolved_at: float | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _emit: ChoiceEmitter | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "choice_id": self.choice_id,
            "question": self.question,
            "options": list(self.options),
            "session_id": self.session_id,
            "user": self.user,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "value": self.value,
            "reviewer": self.reviewer,
            "resolved_at": self.resolved_at,
        }


class ChoiceBroker:
    def __init__(self, timeout_seconds: float = 300.0, max_records: int = 256):
        self.timeout_seconds = timeout_seconds
        self.max_records = max(1, max_records)
        self._lock = threading.Lock()
        self._records: dict[str, ChoiceRecord] = {}

    def create(self, choice: UserChoice, *, session_id: str | None, user: str,
               emit: ChoiceEmitter | None = None) -> ChoiceRecord:
        now = time.time()
        rec = ChoiceRecord(
            choice_id=f"choice_{uuid.uuid4().hex[:12]}",
            question=choice.question,
            options=[
                {"value": item.value, "label": item.label, "description": item.description}
                for item in choice.options
            ],
            session_id=session_id,
            user=user,
            created_at=now,
            expires_at=now + self.timeout_seconds,
            _emit=emit,
        )
        with self._lock:
            self._cleanup_locked(now)
            self._records[rec.choice_id] = rec
            self._trim_locked()
        return rec

    def list_records(self, *, status: str | None = None) -> list[ChoiceRecord]:
        with self._lock:
            self._cleanup_locked(time.time())
            records = list(self._records.values())
        if status:
            records = [record for record in records if record.status == status]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def wait(self, choice_id: str) -> str:
        with self._lock:
            rec = self._records.get(choice_id)
        if rec is None:
            return ""
        rec._event.wait(self.timeout_seconds)
        resolved = self.resolve(choice_id, value="", reviewer="system")
        return resolved.value if resolved else ""

    def resolve(self, choice_id: str, *, value: str, reviewer: str) -> ChoiceRecord | None:
        callback = None
        payload = None
        with self._lock:
            rec = self._records.get(choice_id)
            if rec is None:
                return None
            now = time.time()
            valid = {option["value"] for option in rec.options}
            if rec.status == "pending":
                rec.value = value if now < rec.expires_at and value in valid else ""
                rec.status = "selected" if rec.value else "expired"
                rec.reviewer = reviewer
                rec.resolved_at = now
                callback = rec._emit
                payload = rec.to_dict()
                rec._event.set()
        if callback and payload:
            callback("choice_resolved", payload)
        return rec

    def _cleanup_locked(self, now: float) -> None:
        for rec in self._records.values():
            if rec.status == "pending" and now >= rec.expires_at:
                rec.status = "expired"
                rec.reviewer = "system"
                rec.resolved_at = now
                rec._event.set()

    def _trim_locked(self) -> None:
        while len(self._records) > self.max_records:
            oldest = min(self._records.values(), key=lambda record: record.created_at)
            if oldest.status == "pending":
                oldest.status = "expired"
                oldest.reviewer = "system"
                oldest.resolved_at = time.time()
                oldest._event.set()
            self._records.pop(oldest.choice_id, None)
