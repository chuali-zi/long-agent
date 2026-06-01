from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time

import pytest

from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config
from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.base import Tool
from kyagent.mcp.tools.pipeline import execute_and_format, prepare_call
from kyagent.safety.patterns import RiskLevel
from kyagent.runtime import build_audit_store


def _mode(path) -> int:  # noqa: ANN001
    return stat.S_IMODE(path.stat().st_mode)


def test_audit_files_are_owner_only(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not enforceable on Windows")
    audit_dir = tmp_path / "private" / "audit"
    store = AuditStore(audit_dir / "audit.db")
    logger = AuditLogger(store, jsonl_file=audit_dir / "audit.jsonl")
    trace = Trace()
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "hello"})
    logger.close_file()

    assert _mode(audit_dir) == 0o700
    assert _mode(audit_dir / "audit.db") == 0o600
    assert _mode(audit_dir / "audit.jsonl") == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = audit_dir / f"audit.db{suffix}"
        if sidecar.exists():
            assert _mode(sidecar) == 0o600


def test_store_migrates_legacy_events_and_marks_them_unsealed(tmp_path):
    db = tmp_path / "audit.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, user TEXT NOT NULL,
            started_at REAL NOT NULL, metadata TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,
            seq INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
            payload TEXT NOT NULL
        );
        INSERT INTO traces VALUES ('old', 'tester', 1.0, '{}');
        INSERT INTO events(trace_id,seq,kind,ts,payload)
        VALUES ('old', 1, 'user_input', 1.0, '{"text":"old"}');
        """
    )
    conn.commit()
    conn.close()

    store = AuditStore(db, hmac_key=b"secret", key_id="k1")
    events = store.get_events("old")
    assert events[0]["integrity_status"] == "legacy-unsealed"
    assert store.verify_trace("old")["status"] == "legacy-unsealed"


def test_sealed_trace_detects_database_tampering_and_jsonl_has_integrity_fields(tmp_path):
    store = AuditStore(tmp_path / "audit.db", hmac_key=b"secret", key_id="k1")
    logger = AuditLogger(store, jsonl_file=tmp_path / "audit.jsonl")
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "hello"})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "world"})
    logger.close_file()

    assert store.verify_trace(trace.trace_id)["ok"] is True
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert {"prev_hash", "event_hash", "event_hmac", "key_id"} <= record.keys()

    store._conn.execute(
        "UPDATE events SET payload=? WHERE trace_id=? AND seq=2",
        ('{"text":"tampered"}', trace.trace_id),
    )
    result = store.verify_trace(trace.trace_id)
    assert result["ok"] is False
    assert result["failed_seq"] == 2


def test_sealed_trace_detects_deleted_middle_event(tmp_path):
    store = AuditStore(tmp_path / "audit.db", hmac_key=b"secret", key_id="k1")
    logger = AuditLogger(store)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "one"})
    logger.event(trace, EventKind.LLM_THOUGHT, {"text": "two"})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "three"})

    store._conn.execute(
        "DELETE FROM events WHERE trace_id=? AND seq=2",
        (trace.trace_id,),
    )

    result = store.verify_trace(trace.trace_id)
    assert result["ok"] is False
    assert result["failed_seq"] == 3


def test_closed_sealed_trace_detects_deleted_tail_event(tmp_path):
    store = AuditStore(tmp_path / "audit.db", hmac_key=b"secret", key_id="k1")
    logger = AuditLogger(store)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "one"})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "two"})
    logger.close(trace)

    store._conn.execute(
        "DELETE FROM events WHERE trace_id=? AND seq=2",
        (trace.trace_id,),
    )

    result = store.verify_trace(trace.trace_id)
    assert result["ok"] is False
    assert result["status"] == "tail-mismatch"


def test_store_enforces_unique_trace_sequence_and_purges_old_traces(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    old = Trace(trace_id="old", started_at=10.0)
    new = Trace(trace_id="new", started_at=20.0)
    for trace in (old, new):
        store.open_trace(trace)
        event = trace.add(EventKind.USER_INPUT, {"trace": trace.trace_id})
        store.append_event(trace.trace_id, event)

    with pytest.raises(sqlite3.IntegrityError):
        store.append_event("new", new.events[0])

    assert store.purge_before(15.0) == 1
    assert store.get_events("old") == []
    assert store.get_events("new")


class _ReadOnlyMediumTool(Tool):
    name = "disk_probe"
    risk_level = RiskLevel.MEDIUM
    read_only = True

    def build_argv(self, args):  # noqa: ANN001, ARG002
        return ["probe"]


class _Executor:
    def run(self, argv, *, requires_root=False):  # noqa: ANN001, ARG002
        return ExecutionResult(
            argv=argv,
            returncode=1,
            stdout="partial output",
            stderr="failed",
            truncated=True,
            duration=0.1,
        )


def test_read_only_perception_is_result_evidence_after_execution_result(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    audit = AuditLogger(store)
    trace = Trace()
    audit.open(trace)
    prepared = prepare_call(_ReadOnlyMediumTool(), {}, trace=trace, audit=audit)
    execute_and_format(prepared, trace=trace, audit=audit, executor=_Executor())

    assert [event.kind for event in trace.events] == [
        EventKind.TOOL_REQUEST,
        EventKind.EXECUTION,
        EventKind.EXECUTION_RESULT,
        EventKind.PERCEPTION,
    ]
    evidence = trace.events[-1].payload
    assert evidence["evidence_id"].startswith("evidence-")
    assert evidence["tool"] == "disk_probe"
    assert evidence["snapshot_kind"] == "其它"
    assert evidence["execution_result_seq"] == trace.events[-2].seq
    assert evidence["ok"] is False
    assert evidence["truncated"] is True
    assert evidence["stdout_sha256"] == hashlib.sha256(b"partial output").hexdigest()


def test_runtime_integrity_mode_requires_hmac_material(tmp_path, monkeypatch):
    cfg = Config()
    cfg.audit.database = str(tmp_path / "audit.db")
    cfg.audit.integrity_enabled = True
    monkeypatch.delenv("KYAGENT_AUDIT_HMAC_KEY", raising=False)

    with pytest.raises(ValueError, match="HMAC"):
        build_audit_store(cfg)


def test_runtime_loads_hmac_key_file_and_purges_expired_traces(tmp_path):
    db = tmp_path / "audit.db"
    old_store = AuditStore(db)
    old_trace = Trace(user="old", started_at=time.time() - 3 * 86400)
    old_store.open_trace(old_trace)
    old_store.close_trace(old_trace)
    old_store.close()

    key_file = tmp_path / "audit-hmac.key"
    key_file.write_text("runtime-secret\n", encoding="utf-8")
    cfg = Config()
    cfg.audit.database = str(db)
    cfg.audit.integrity_enabled = True
    cfg.audit.hmac_key_file = str(key_file)
    cfg.audit.retain_days = 1

    store = build_audit_store(cfg)
    try:
        assert store.hmac_key == b"runtime-secret"
        assert store.list_traces() == []
    finally:
        store.close()
