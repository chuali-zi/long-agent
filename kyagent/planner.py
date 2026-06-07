"""Durable task plan state for long-running agent work.

This module is intentionally small: it records plan snapshots and step status
transitions, but it never executes commands or changes privileges. The Agent
uses it as a durable state machine around each turn so complex work is no
longer represented only by transient LLM messages inside ``max_iterations``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


PlanStatus = Literal["pending", "running", "blocked", "complete", "failed"]
StepStatus = Literal["pending", "running", "complete", "failed", "skipped"]


@dataclass
class PlanStep:
    step_id: str
    title: str
    status: StepStatus = "pending"
    detail: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "updated_at": self.updated_at,
        }


@dataclass
class PlanSnapshot:
    plan_id: str
    trace_id: str
    user: str
    title: str
    status: PlanStatus
    created_at: float
    updated_at: float
    steps: list[PlanStep]
    current_step: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "trace_id": self.trace_id,
            "user": self.user,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_step": self.current_step,
            "metadata": self.metadata,
            "steps": [s.to_dict() for s in self.steps],
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id      TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL,
    user         TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,
    current_step TEXT NOT NULL DEFAULT '',
    metadata     TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id    TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (plan_id, step_id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_plans_trace ON plans(trace_id);
CREATE INDEX IF NOT EXISTS idx_plans_updated ON plans(updated_at DESC);
"""


class PlanStore:
    """Thread-safe SQLite-backed plan state store."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def create_run_plan(
        self,
        *,
        trace_id: str,
        user: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> PlanSnapshot:
        now = time.time()
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        steps = [
            PlanStep("receive", "Receive and risk-check request"),
            PlanStep("reason", "Reason, inspect, and call tools"),
            PlanStep("verify", "Validate results and safety state"),
            PlanStep("respond", "Return final answer"),
        ]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO plans(
                    plan_id,trace_id,user,title,status,current_step,metadata,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    trace_id,
                    user,
                    title[:240],
                    "running",
                    "receive",
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            for ordinal, step in enumerate(steps):
                self._conn.execute(
                    """
                    INSERT INTO plan_steps(plan_id,step_id,ordinal,title,status,detail,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        plan_id,
                        step.step_id,
                        ordinal,
                        step.title,
                        step.status,
                        step.detail,
                        now,
                    ),
                )
            self._set_step_locked(plan_id, "receive", "running", "Request accepted")
        return self.get(plan_id)

    def set_step(
        self,
        plan_id: str,
        step_id: str,
        status: StepStatus,
        detail: str = "",
        *,
        current: bool = True,
    ) -> PlanSnapshot:
        with self._lock:
            self._set_step_locked(plan_id, step_id, status, detail)
            if current:
                self._conn.execute(
                    "UPDATE plans SET current_step=?, updated_at=? WHERE plan_id=?",
                    (step_id, time.time(), plan_id),
                )
        return self.get(plan_id)

    def set_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        current_step: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanSnapshot:
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            old_meta = json.loads(row[0]) if row and row[0] else {}
            if metadata:
                old_meta.update(metadata)
            self._conn.execute(
                """
                UPDATE plans
                SET status=?, current_step=COALESCE(?, current_step), metadata=?, updated_at=?
                WHERE plan_id=?
                """,
                (
                    status,
                    current_step,
                    json.dumps(old_meta, ensure_ascii=False, default=str),
                    time.time(),
                    plan_id,
                ),
            )
        return self.get(plan_id)

    def get(self, plan_id: str) -> PlanSnapshot:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT plan_id,trace_id,user,title,status,current_step,metadata,created_at,updated_at
                FROM plans WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(plan_id)
            step_rows = self._conn.execute(
                """
                SELECT step_id,title,status,detail,updated_at
                FROM plan_steps WHERE plan_id=? ORDER BY ordinal
                """,
                (plan_id,),
            ).fetchall()
        return PlanSnapshot(
            plan_id=row[0],
            trace_id=row[1],
            user=row[2],
            title=row[3],
            status=row[4],
            current_step=row[5],
            metadata=json.loads(row[6]) if row[6] else {},
            created_at=row[7],
            updated_at=row[8],
            steps=[
                PlanStep(step_id=s[0], title=s[1], status=s[2], detail=s[3], updated_at=s[4])
                for s in step_rows
            ],
        )

    def latest(self, limit: int = 20) -> list[PlanSnapshot]:
        with self._lock:
            ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT plan_id FROM plans ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        return [self.get(plan_id) for plan_id in ids]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _set_step_locked(
        self,
        plan_id: str,
        step_id: str,
        status: StepStatus,
        detail: str,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE plan_steps SET status=?, detail=?, updated_at=?
            WHERE plan_id=? AND step_id=?
            """,
            (status, detail[:500], now, plan_id, step_id),
        )
        self._conn.execute(
            "UPDATE plans SET updated_at=? WHERE plan_id=?",
            (now, plan_id),
        )
