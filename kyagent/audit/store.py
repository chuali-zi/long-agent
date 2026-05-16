"""SQLite 审计存储。

设计要点：
- 两张表：traces（一次 user turn 的元信息） + events（具体事件，外键 trace_id）
- 事件 payload 用 TEXT(JSON) 存储，方便排查；同时建索引便于按 kind / ts 检索
- 任何写入都用单条事务，避免长事务锁库
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from kyagent.audit.trace import EventKind, Trace, TraceEvent


_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    user        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    metadata    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL,
    payload     TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);

CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);
"""


class AuditStore:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # ---- 写入 ----------------------------------------------------------

    def open_trace(self, trace: Trace) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO traces(trace_id,user,started_at,metadata) VALUES(?,?,?,?)",
                (trace.trace_id, trace.user, trace.started_at, json.dumps(trace.metadata)),
            )

    def append_event(self, trace_id: str, event: TraceEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(trace_id,seq,kind,ts,payload) VALUES(?,?,?,?,?)",
                (trace_id, event.seq, event.kind.value, event.ts,
                 json.dumps(event.payload, ensure_ascii=False, default=str)),
            )

    def close_trace(self, trace: Trace) -> None:
        """flush 整条 trace（幂等：上面是 INSERT OR REPLACE，事件是 append）。"""
        # 当前 append 流式写入已落库，此处只做元信息刷新
        with self._lock:
            self._conn.execute(
                "UPDATE traces SET metadata=? WHERE trace_id=?",
                (json.dumps(trace.metadata, ensure_ascii=False, default=str), trace.trace_id),
            )

    # ---- 查询 ----------------------------------------------------------

    def list_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT trace_id,user,started_at,metadata FROM traces ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        out = []
        for trace_id, user, started_at, metadata in cur.fetchall():
            out.append({
                "trace_id": trace_id,
                "user": user,
                "started_at": started_at,
                "metadata": json.loads(metadata) if metadata else {},
            })
        return out

    def get_events(self, trace_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT seq,kind,ts,payload FROM events WHERE trace_id=? ORDER BY seq",
            (trace_id,),
        )
        return [
            {"seq": s, "kind": k, "ts": t, "payload": json.loads(p)}
            for s, k, t, p in cur.fetchall()
        ]

    def find_events_by_kind(self, kind: EventKind, limit: int = 100) -> Iterable[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT trace_id,seq,ts,payload FROM events WHERE kind=? ORDER BY ts DESC LIMIT ?",
            (kind.value, limit),
        )
        for trace_id, seq, ts, payload in cur.fetchall():
            yield {"trace_id": trace_id, "seq": seq, "ts": ts, "payload": json.loads(payload)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
