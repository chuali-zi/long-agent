"""AuditLogger：把 Trace 写入 SQLite 并可选追加 JSONL。

约定：所有运维相关组件都不直接写日志文件，而是通过 AuditLogger 把事件挂到 trace 上。

JSONL 通道用一个常驻 line-buffered 句柄，免去每事件 open/close 的开销。
Line buffering 保证 '\\n' 触发 flush，相当于每条事件都已持久到 OS buffer；
SQLite 仍是真正的"权威审计源"（WAL + 即时 fsync 由 SQLite 内部负责）。
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
import weakref
from pathlib import Path
from typing import Any, IO

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
        self._jsonl_path: Path | None = Path(jsonl_file) if jsonl_file else None
        self._jsonl_fp: IO[str] | None = None
        self._jsonl_lock = threading.Lock()
        if self._jsonl_path is not None:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            # line-buffered append handle，'\n' 触发 flush 到 OS。
            self._jsonl_fp = self._jsonl_path.open(
                "a", encoding="utf-8", buffering=1
            )
            # 进程退出时兜底 flush+close（弱引用避免阻止 GC）。
            atexit.register(_atexit_close, weakref.ref(self))

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
        with trace._lock:
            ev = trace.add(kind, payload)
            self.store.append_event(trace.trace_id, ev)
            if self._jsonl_fp is not None:
                # 仅作 fast-path 短路，避免 JSONL 关闭时仍构造 json.dumps。
                # 真正的判空 + 写入在 _jsonl_lock 内统一完成，规避
                # close_file() 把 _jsonl_fp 置 None 后第二次解引用炸 None 的 TOCTOU。
                line = json.dumps(
                    {"trace_id": trace.trace_id, **ev.to_dict()},
                    ensure_ascii=False, default=str,
                )
                with self._jsonl_lock:
                    fp = self._jsonl_fp
                    if fp is not None:
                        fp.write(line + "\n")
            if self.verbose:
                _logger.info("[%s] %s payload=%s", trace.trace_id[:8], kind.value, payload)

    def close(self, trace: Trace) -> None:
        self.store.close_trace(trace)
        if self.verbose:
            _logger.info("trace closed: %s", trace.summary())

    def close_file(self) -> None:
        """关闭 JSONL 句柄（atexit / 测试 teardown 使用）。"""
        with self._jsonl_lock:
            fp = self._jsonl_fp
            self._jsonl_fp = None
        if fp is not None:
            try:
                fp.flush()
            finally:
                fp.close()


def _atexit_close(ref: "weakref.ref[AuditLogger]") -> None:
    inst = ref()
    if inst is not None:
        try:
            inst.close_file()
        except Exception:  # noqa: BLE001
            pass
