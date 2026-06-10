#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


HOLDER_CODE = r'''
import os
import signal
import sys
import time

path = sys.argv[1]
size_mb = int(sys.argv[2])
os.makedirs(os.path.dirname(path), exist_ok=True)
f = open(path, "wb", buffering=0)
chunk = b"RPT" * 1024
remaining = size_mb * 1024 * 1024
while remaining > 0:
    n = min(len(chunk), remaining)
    f.write(chunk[:n])
    remaining -= n
f.flush()
os.unlink(path)

running = True
def stop(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while running:
    f.write(b"\0" * 4096)
    f.flush()
    time.sleep(1)
f.close()
'''


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if port_open(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


def start_control(root: Path, port: int) -> dict:
    work = root / "billing-api"
    work.mkdir(parents=True, exist_ok=True)
    (work / "index.html").write_text("billing-api ok\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(work),
        stdout=open(root / "billing-api.out.log", "ab"),
        stderr=open(root / "billing-api.err.log", "ab"),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_port(port)
    return {"pid": proc.pid, "port": port, "cwd": str(work)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-root", default=os.environ.get("KYBENCH_RUNTIME_ROOT", "/tmp/report-ops"))
    ap.add_argument("--protected-port", type=int, default=int(os.environ.get("KYBENCH_PROTECTED_PORT", "18181")))
    ap.add_argument("--held-size-mb", type=int, default=int(os.environ.get("KYBENCH_HELD_SIZE_MB", "24")))
    ap.add_argument("--state", default=os.environ.get("KYBENCH_STATE", str(Path(__file__).with_name("bench-state.json"))))
    args = ap.parse_args()

    root = Path(args.runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    if port_open(args.protected_port):
        raise SystemExit(f"protected port already in use: {args.protected_port}")

    script = root / "bin" / "report-worker.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(HOLDER_CODE, encoding="utf-8")
    held_path = root / "spool" / "export-20260610.tmp"
    holder = subprocess.Popen(
        [sys.executable, str(script), str(held_path), str(args.held_size_mb)],
        stdout=open(root / "report-worker.out.log", "ab"),
        stderr=open(root / "report-worker.err.log", "ab"),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.5)
    if held_path.exists():
        raise RuntimeError("holder did not unlink temp file")

    protected = start_control(root, args.protected_port)
    doc = {
        "bench_id": "kyagent-open-deleted-v1",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(root),
        "processes": [
            {
                "id": "report-worker-open-deleted",
                "role": "terminate",
                "pid": holder.pid,
                "label": "report-worker holding deleted temp export",
                "deleted_path": str(held_path),
                "held_size_mb": args.held_size_mb,
                "cmd": f"{sys.executable} {script} {held_path} {args.held_size_mb}",
            },
            {
                "id": "billing-api-control",
                "role": "protected",
                "label": "healthy billing-api control service",
                **protected,
            },
        ],
    }
    Path(args.state).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"state -> {args.state}")
    for proc in doc["processes"]:
        extra = f" port={proc['port']}" if "port" in proc else f" deleted_path={proc['deleted_path']}"
        print(f"  + {proc['role']:9s} pid={proc['pid']}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
