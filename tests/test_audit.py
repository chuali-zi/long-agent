"""审计链路完整性测试。"""
from __future__ import annotations

import json
import threading
import tempfile
from pathlib import Path

from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind, Trace


def _make_logger(tmpdir: Path) -> tuple[AuditLogger, AuditStore]:
    store = AuditStore(tmpdir / "audit.db")
    logger = AuditLogger(store, jsonl_file=tmpdir / "audit.jsonl")
    return logger, store


def test_full_reasoning_chain_persisted(tmp_path):
    logger, store = _make_logger(tmp_path)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "查 80 端口"})
    logger.event(trace, EventKind.LLM_THOUGHT, {"text": "我先调用 lsof_port"})
    logger.event(trace, EventKind.TOOL_REQUEST,
                 {"tool": "lsof_port", "argv": ["lsof", "-nP", "-i", "TCP:80"]})
    logger.event(trace, EventKind.SAFETY_CHECK,
                 {"decision": "allow", "risk": "low", "hits": []})
    logger.event(trace, EventKind.EXECUTION, {"argv": ["lsof", "-nP", "-i", "TCP:80"]})
    logger.event(trace, EventKind.EXECUTION_RESULT, {"returncode": 0, "stdout": "..."})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "80 端口由 nginx 占用"})
    logger.close(trace)

    events = store.get_events(trace.trace_id)
    # 7 个事件，并按 seq 严格递增
    assert len(events) == 7
    assert [e["seq"] for e in events] == list(range(1, 8))
    kinds = [e["kind"] for e in events]
    assert kinds[0] == EventKind.USER_INPUT.value
    assert kinds[-1] == EventKind.AGENT_REPLY.value
    # 必须包含安全校验事件
    assert EventKind.SAFETY_CHECK.value in kinds


def test_jsonl_appended(tmp_path):
    logger, _ = _make_logger(tmp_path)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "hello"})
    logger.close(trace)
    jsonl = tmp_path / "audit.jsonl"
    content = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    import json
    rec = json.loads(content[0])
    assert rec["trace_id"] == trace.trace_id
    assert rec["kind"] == "user_input"


def test_list_traces_orders_by_recency(tmp_path):
    logger, store = _make_logger(tmp_path)
    t1 = Trace(user="a")
    logger.open(t1); logger.event(t1, EventKind.USER_INPUT, {"text": "1"}); logger.close(t1)
    import time
    time.sleep(0.01)
    t2 = Trace(user="b")
    logger.open(t2); logger.event(t2, EventKind.USER_INPUT, {"text": "2"}); logger.close(t2)
    rows = store.list_traces(limit=10)
    assert rows[0]["trace_id"] == t2.trace_id
    assert rows[1]["trace_id"] == t1.trace_id


def test_filter_events_by_kind(tmp_path):
    logger, store = _make_logger(tmp_path)
    trace = Trace(user="x")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text": "1"})
    logger.event(trace, EventKind.SAFETY_CHECK, {"decision": "deny"})
    logger.event(trace, EventKind.AGENT_REPLY, {"text": "no"})
    logger.close(trace)
    hits = list(store.find_events_by_kind(EventKind.SAFETY_CHECK))
    assert len(hits) == 1
    assert hits[0]["payload"]["decision"] == "deny"


class _BlockingStore:
    """Store test double that exposes event interleaving deterministically."""

    def __init__(self):
        self.seqs: list[int] = []
        self.first_entered = threading.Event()
        self.second_appended = threading.Event()
        self.release_first = threading.Event()

    def open_trace(self, trace):  # noqa: ANN001, ARG002
        return None

    def append_event(self, trace_id, event):  # noqa: ANN001, ARG002
        if event.seq == 1:
            self.first_entered.set()
            assert self.release_first.wait(timeout=2)
        self.seqs.append(event.seq)
        if event.seq == 2:
            self.second_appended.set()

    def close_trace(self, trace):  # noqa: ANN001, ARG002
        return None


def test_audit_event_serializes_shared_trace_updates(tmp_path):
    store = _BlockingStore()
    logger = AuditLogger(store, jsonl_file=tmp_path / "audit.jsonl")
    trace = Trace(user="tester")

    def add_first():
        logger.event(trace, EventKind.USER_INPUT, {"label": "first"})

    def add_second():
        assert store.first_entered.wait(timeout=1)
        logger.event(trace, EventKind.LLM_THOUGHT, {"label": "second"})

    first = threading.Thread(target=add_first, name="first-audit-thread")
    second = threading.Thread(target=add_second, name="second-audit-thread")

    first.start()
    second.start()
    interleaved_before_first_completed = store.second_appended.wait(timeout=0.2)
    store.release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    logger.close_file()

    assert not first.is_alive()
    assert not second.is_alive()
    assert not interleaved_before_first_completed
    assert store.seqs == [1, 2]

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
