"""CLI helpers for read-only durable plan inspection."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kyagent.planner import PlanStore


def _db_path() -> Path:
    return Path(os.environ.get("KYAGENT_PLAN_DB", "./var/plans.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kyagent-plan")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=20)
    p_get = sub.add_parser("get")
    p_get.add_argument("plan_id")
    args = parser.parse_args(argv)
    store = PlanStore(_db_path())
    try:
        if args.cmd == "list":
            out = {"plans": [p.to_dict() for p in store.latest(max(1, min(args.limit, 100)))]}
        else:
            out = {"plan": store.get(args.plan_id).to_dict()}
    finally:
        store.close()
    print(json.dumps({"ok": True, **out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
