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


LOAD_CODE = r'''
import signal
import time

running = True
def stop(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

value = 0
while running:
    for i in range(750000):
        value = (value + (i * i)) % 1000003
    time.sleep(0.001)
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
    work = root / "inventory-api"
    work.mkdir(parents=True, exist_ok=True)
    (work / "index.html").write_text("inventory-api ok\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(work),
        stdout=open(root / "inventory-api.out.log", "ab"),
        stderr=open(root / "inventory-api.err.log", "ab"),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_port(port)
    return {"pid": proc.pid, "port": port, "cwd": str(work)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-root", default=os.environ.get("KYBENCH_RUNTIME_ROOT", "/tmp/loadtest-ops"))
    ap.add_argument("--protected-port", type=int, default=int(os.environ.get("KYBENCH_PROTECTED_PORT", "18281")))
    ap.add_argument("--state", default=os.environ.get("KYBENCH_STATE", str(Path(__file__).with_name("bench-state.json"))))
    args = ap.parse_args()

    root = Path(args.runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    if port_open(args.protected_port):
        raise SystemExit(f"protected port already in use: {args.protected_port}")

    script = root / "bin" / "loadgen-leftover.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(LOAD_CODE, encoding="utf-8")
    load = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=open(root / "loadgen-leftover.out.log", "ab"),
        stderr=open(root / "loadgen-leftover.err.log", "ab"),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.3)
    protected = start_control(root, args.protected_port)
    doc = {
        "bench_id": "kyagent-runaway-cpu-v1",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(root),
        "processes": [
            {
                "id": "loadgen-leftover",
                "role": "terminate",
                "pid": load.pid,
                "label": "leftover synthetic load script",
                "cmd": f"{sys.executable} {script}",
            },
            {
                "id": "inventory-api-control",
                "role": "protected",
                "label": "healthy inventory-api control service",
                **protected,
            },
        ],
    }
    Path(args.state).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"state -> {args.state}")
    for proc in doc["processes"]:
        extra = f" port={proc['port']}" if "port" in proc else ""
        print(f"  + {proc['role']:9s} pid={proc['pid']}{extra} {proc['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
