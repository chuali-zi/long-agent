"""Dump one audit trace's events to JSON for behavioral grading.

Usage:
    python dump_trace.py <trace_id> <out.json> [latest]

Opens the audit store using the *same* config resolution the agent used
(``load_config(None)`` + ``build_audit_store``), so it reads exactly the DB the
just-finished ``kyagent ask`` wrote to. Must run as the same user/env as the run.

If ``trace_id`` is the literal ``latest`` (or empty), the most recent trace is
used — a fallback for when the run did not surface its trace_id.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: dump_trace.py <trace_id> <out.json>", file=sys.stderr)
        return 2
    trace_id = argv[0].strip()
    out_path = Path(argv[1])

    from kyagent.config import load_config
    from kyagent.runtime import build_audit_store

    cfg = load_config(None)
    store = build_audit_store(cfg)

    if not trace_id or trace_id == "latest":
        traces = store.list_traces(limit=1)
        if not traces:
            print("no traces found in audit store", file=sys.stderr)
            return 1
        trace_id = traces[0]["trace_id"]

    events = store.get_events(trace_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"trace_id": trace_id, "events": events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"dumped {len(events)} events for trace {trace_id} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
