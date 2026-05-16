"""AuditLogger：把 Trace 写入 SQLite 并可选追加 JSONL。

约定：所有运维相关组件都不直接写日志文件，而是通过 AuditLogger 把事件挂到 trace 上。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind, Trace


_logger = logging.getLogger("kyagent.audit")


class AuditLogger:
    """协调 SQLite 持久化、JSONL 追加和 stderr 调试输出。"""

    def __init__(
        self,
        store: AuditStore,
        jsonl_file: str | Path | None = None,
        verbose: bool = False,
    ):
        self.store = store
        self.verbose = verbose
        self._jsonl = Path(jsonl_file) if jsonl_file else None
        if self._jsonl:
            self._jsonl.parent.mkdir(parents=True, exist_ok=True)

    def open(self, trace: Trace) -> None:
        self.store.open_trace(trace)
        if self.verbose:
            _logger.info("trace opened: %s by %s", trace.trace_id, trace.user)

    def event(
        self,
        trace: Trace,
        kind: EventKind,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ev = trace.add(kind, payload)
        self.store.append_event(trace.trace_id, ev)
        if self._jsonl is not None:
            line = json.dumps(
                {"trace_id": trace.trace_id, **ev.to_dict()},
                ensure_ascii=False, default=str,
            )
            with self._jsonl.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.verbose:
            _logger.info("[%s] %s payload=%s", trace.trace_id[:8], kind.value, payload)

    def close(self, trace: Trace) -> None:
        self.store.close_trace(trace)
        if self.verbose:
            _logger.info("trace closed: %s", trace.summary())
