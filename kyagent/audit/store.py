"""SQLite audit storage with optional tamper-evident event sealing."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
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
    prev_hash   TEXT,
    event_hash  TEXT,
    event_hmac  TEXT,
    key_id      TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);

CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);
"""

_INTEGRITY_COLUMNS = {
    "prev_hash": "TEXT",
    "event_hash": "TEXT",
    "event_hmac": "TEXT",
    "key_id": "TEXT",
}


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Some filesystems do not expose POSIX permissions. The database still
        # remains usable; callers can enforce platform ACLs at deployment time.
        pass


def _canonical_event(
    trace_id: str,
    seq: int,
    kind: str,
    ts: float,
    payload: str,
    prev_hash: str,
) -> bytes:
    return json.dumps(
        {
            "trace_id": trace_id,
            "seq": seq,
            "kind": kind,
            "ts": ts,
            "payload": json.loads(payload),
            "prev_hash": prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class AuditStore:
    """Thread-safe SQLite wrapper for append-only audit events."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        hmac_key: bytes | None = None,
        key_id: str | None = None,
    ):
        if key_id is not None and hmac_key is None:
            raise ValueError("key_id requires hmac_key")
        self.db_path = Path(db_path)
        self.hmac_key = hmac_key
        self.key_id = key_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod(self.db_path.parent, 0o700)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._tail_hash_cache: dict[str, str] = {}
        _chmod(self.db_path, 0o600)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._secure_database_files()

    def _migrate(self) -> None:
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        for name, sql_type in _INTEGRITY_COLUMNS.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_trace_seq ON events(trace_id, seq)"
        )

    def _secure_database_files(self) -> None:
        _chmod(self.db_path, 0o600)
        for suffix in ("-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                _chmod(path, 0o600)

    def open_trace(self, trace: Trace) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO traces(trace_id,user,started_at,metadata) VALUES(?,?,?,?)",
                (trace.trace_id, trace.user, trace.started_at, json.dumps(trace.metadata)),
            )
            row = self._conn.execute(
                "SELECT event_hash FROM events WHERE trace_id=? ORDER BY seq DESC LIMIT 1",
                (trace.trace_id,),
            ).fetchone()
            self._tail_hash_cache[trace.trace_id] = row[0] if row and row[0] else ""
            self._secure_database_files()

    def append_event(self, trace_id: str, event: TraceEvent) -> None:
        payload = json.dumps(event.payload, ensure_ascii=False, default=str)
        with self._lock:
            prev_hash = self._tail_hash_cache.get(trace_id)
            if prev_hash is None:
                row = self._conn.execute(
                    "SELECT event_hash FROM events WHERE trace_id=? ORDER BY seq DESC LIMIT 1",
                    (trace_id,),
                ).fetchone()
                prev_hash = row[0] if row and row[0] else ""
            event_hash = hashlib.sha256(
                _canonical_event(trace_id, event.seq, event.kind.value, event.ts, payload, prev_hash)
            ).hexdigest()
            event_hmac = (
                hmac.new(self.hmac_key, event_hash.encode("ascii"), hashlib.sha256).hexdigest()
                if self.hmac_key is not None
                else None
            )
            self._conn.execute(
                """
                INSERT INTO events(
                    trace_id,seq,kind,ts,payload,prev_hash,event_hash,event_hmac,key_id
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id, event.seq, event.kind.value, event.ts, payload,
                    prev_hash, event_hash, event_hmac, self.key_id,
                ),
            )
            event.prev_hash = prev_hash
            event.event_hash = event_hash
            event.event_hmac = event_hmac
            event.key_id = self.key_id
            self._tail_hash_cache[trace_id] = event_hash

    def close_trace(self, trace: Trace) -> None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(seq), 0), COALESCE(
                    (SELECT event_hash FROM events
                     WHERE trace_id=? ORDER BY seq DESC LIMIT 1), ''
                )
                FROM events WHERE trace_id=?
                """,
                (trace.trace_id, trace.trace_id),
            ).fetchone()
            event_count, last_seq, tail_hash = row or (0, 0, "")
            trace.metadata["audit_integrity"] = {
                "event_count": event_count,
                "last_seq": last_seq,
                "tail_hash": tail_hash,
            }
            self._conn.execute(
                "UPDATE traces SET metadata=? WHERE trace_id=?",
                (json.dumps(trace.metadata, ensure_ascii=False, default=str), trace.trace_id),
            )
            self._tail_hash_cache.pop(trace.trace_id, None)
            self._secure_database_files()

    def list_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT trace_id,user,started_at,metadata FROM traces ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "trace_id": trace_id,
                "user": user,
                "started_at": started_at,
                "metadata": json.loads(metadata) if metadata else {},
            }
            for trace_id, user, started_at, metadata in cur.fetchall()
        ]

    def get_events(self, trace_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT seq,kind,ts,payload,prev_hash,event_hash,event_hmac,key_id
            FROM events WHERE trace_id=? ORDER BY seq
            """,
            (trace_id,),
        )
        return [
            {
                "seq": seq,
                "kind": kind,
                "ts": ts,
                "payload": json.loads(payload),
                "prev_hash": prev_hash,
                "event_hash": event_hash,
                "event_hmac": event_hmac_value,
                "key_id": row_key_id,
                "integrity_status": "legacy-unsealed" if event_hash is None else "sealed",
            }
            for seq, kind, ts, payload, prev_hash, event_hash, event_hmac_value, row_key_id
            in cur.fetchall()
        ]

    def verify_trace(self, trace_id: str) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT seq,kind,ts,payload,prev_hash,event_hash,event_hmac,key_id
            FROM events WHERE trace_id=? ORDER BY seq
            """,
            (trace_id,),
        ).fetchall()
        legacy = False
        expected_prev = ""
        expected_seq = 1
        for seq, kind, ts, payload, prev_hash, event_hash, event_hmac_value, row_key_id in rows:
            if seq != expected_seq:
                return {"ok": False, "status": "invalid", "failed_seq": seq}
            expected_seq += 1
            if event_hash is None:
                legacy = True
                expected_prev = ""
                continue
            if (prev_hash or "") != expected_prev:
                return {"ok": False, "status": "invalid", "failed_seq": seq}
            calculated = hashlib.sha256(
                _canonical_event(trace_id, seq, kind, ts, payload, prev_hash or "")
            ).hexdigest()
            if not hmac.compare_digest(calculated, event_hash):
                return {"ok": False, "status": "invalid", "failed_seq": seq}
            if event_hmac_value is not None:
                if self.hmac_key is None:
                    return {"ok": False, "status": "key-unavailable", "failed_seq": seq}
                calculated_hmac = hmac.new(
                    self.hmac_key, event_hash.encode("ascii"), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(calculated_hmac, event_hmac_value):
                    return {"ok": False, "status": "invalid", "failed_seq": seq}
            expected_prev = event_hash
        trace_row = self._conn.execute(
            "SELECT metadata FROM traces WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
        metadata = json.loads(trace_row[0]) if trace_row and trace_row[0] else {}
        seal = metadata.get("audit_integrity")
        if isinstance(seal, dict):
            if (
                seal.get("event_count") != len(rows)
                or seal.get("last_seq") != (rows[-1][0] if rows else 0)
                or seal.get("tail_hash") != expected_prev
            ):
                return {"ok": False, "status": "tail-mismatch"}
        status = "legacy-unsealed" if legacy else "verified"
        return {"ok": True, "status": status, "event_count": len(rows)}

    def purge_before(self, cutoff: float) -> int:
        with self._lock:
            trace_ids = [
                row[0] for row in self._conn.execute(
                    "SELECT trace_id FROM traces WHERE started_at < ?", (cutoff,)
                ).fetchall()
            ]
            if not trace_ids:
                return 0
            placeholders = ",".join("?" for _ in trace_ids)
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    f"DELETE FROM events WHERE trace_id IN ({placeholders})", trace_ids
                )
                self._conn.execute(
                    f"DELETE FROM traces WHERE trace_id IN ({placeholders})", trace_ids
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            for trace_id in trace_ids:
                self._tail_hash_cache.pop(trace_id, None)
            return len(trace_ids)

    def find_events_by_kind(self, kind: EventKind, limit: int = 100) -> Iterable[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT trace_id,seq,ts,payload FROM events WHERE kind=? ORDER BY ts DESC LIMIT ?",
            (kind.value, limit),
        )
        for trace_id, seq, ts, payload in cur.fetchall():
            yield {"trace_id": trace_id, "seq": seq, "ts": ts, "payload": json.loads(payload)}

    def close(self) -> None:
        with self._lock:
            self._tail_hash_cache.clear()
            self._conn.close()
