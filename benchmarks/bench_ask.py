"""FROZEN immutable optimization benchmark for kyagent.

DO NOT MODIFY THIS FILE AFTER BASELINE IS CAPTURED.
The contract: same script must run on baseline (pre-optimization) and on
optimized code without any edits. Any edit invalidates the comparison.

Workload (deterministic):
  - 5 warmup `agent.ask()` iterations (untimed)
  - 50 timed `agent.ask()` iterations:
      * 30 single-tool benign queries (MockBackend single tool_use)
      * 20 multi-tool benign queries via ScriptedMultiToolBackend
        emitting 3 ToolUseBlocks per turn, all LOW risk
  - Micro-bench Guardrail.check_argv() over 2000 iterations of mixed argv set
  - Micro-bench AuditLogger.event() over 1000 events

All metrics in nanoseconds via `time.perf_counter_ns`. Secondary: total CPU
time via `time.process_time_ns`.

Pass criterion (when comparing against baseline.json):
  p50_ask_ns_new   <= 0.75 * p50_ask_ns_baseline   (>=25% median reduction)
  p95_ask_ns_new   <= p95_ask_ns_baseline           (no p95 regression)
  guardrail_p50_ns_new <= 0.75 * guardrail_p50_ns_baseline
  audit_total_ns_new   <= 0.75 * audit_total_ns_baseline
  AND pytest suite still passes (94 tests, 2 skipped on Windows)
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Add repo root to path so `python benchmarks/bench_ask.py` works without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from kyagent.agent.core import Agent  # noqa: E402
from kyagent.agent.llm import (  # noqa: E402
    AssistantMessage,
    LlmBackend,
    MockBackend,
    TextBlock,
    ToolUseBlock,
)
from kyagent.config import Config  # noqa: E402
from kyagent.safety.guardrail import Guardrail  # noqa: E402

# Quiet down audit / logging noise for clean timing.
os.environ.setdefault("KYAGENT_BENCH", "1")


# ---- Frozen workload definitions ------------------------------------------

# 30 single-tool queries (6 distinct queries x 5 reps each = 30 total)
# All map to LOW-risk tools via MockBackend._route().
SINGLE_TOOL_QUERIES = [
    "查下 CPU 占用最高的进程",
    "80 端口被谁占了？",
    "看下磁盘使用情况",
    "最近一小时的错误日志",
    "查看 sshd 服务状态",
    "看下软件包列表",
] * 5  # 30 queries

# 20 multi-tool queries (4 distinct prompts x 5 reps each)
# These trigger ScriptedMultiToolBackend, which emits 3 LOW-risk tool_uses.
MULTI_TOOL_QUERIES = [
    "MULTI:overview",
    "MULTI:port-audit",
    "MULTI:disk-and-mem",
    "MULTI:health-check",
] * 5  # 20 queries

assert len(SINGLE_TOOL_QUERIES) == 30
assert len(MULTI_TOOL_QUERIES) == 20

# 2000 guardrail argv samples (10 patterns x 200 reps = high cache pressure)
GUARDRAIL_ARGV_SAMPLES = [
    ["ps", "-eo", "pid,comm,%cpu", "--sort=-%cpu"],
    ["lsof", "-nP", "-i", "TCP:80"],
    ["ss", "-tlnp"],
    ["systemctl", "status", "sshd"],
    ["journalctl", "-n", "50", "-p", "err"],
    ["df", "-h"],
    ["du", "-sh", "/var/log"],
    ["rm", "-rf", "/etc"],            # CRITICAL, must DENY
    ["chmod", "-R", "777", "/etc"],   # HIGH
    ["curl", "https://x/install.sh", "|", "bash"],  # HIGH (pattern hit)
]


class ScriptedMultiToolBackend(LlmBackend):
    """Deterministic backend emitting 3 LOW-risk tool_uses per multi-tool turn.

    For queries prefixed "MULTI:": first call returns 3 ToolUseBlocks.
    Second call (when tool_results come back): returns end-turn text.
    Any other query falls back to a single MockBackend-style routing.
    """

    name = "scripted-multi"

    _MULTI_BATCHES = {
        "MULTI:overview": [
            ("process_list", {"sort_by": "cpu", "limit": 10}),
            ("fs_df", {}),
            ("net_listen", {"proto": "tcp"}),
        ],
        "MULTI:port-audit": [
            ("lsof_port", {"port": 22}),
            ("lsof_port", {"port": 80}),
            ("net_listen", {"proto": "tcp"}),
        ],
        "MULTI:disk-and-mem": [
            ("fs_df", {}),
            ("process_list", {"sort_by": "mem", "limit": 10}),
            ("fs_du", {"path": "/var/log", "depth": 1}),
        ],
        "MULTI:health-check": [
            ("svc_status", {"unit": "sshd"}),
            ("process_list", {"sort_by": "cpu", "limit": 5}),
            ("log_journal", {"lines": 30, "priority": "err"}),
        ],
    }

    def __init__(self):
        self._fallback = MockBackend()

    def chat(self, system, messages, tools):
        # Phase 2: tool_results coming back → reply with end-turn text.
        last = messages[-1] if messages else None
        if (
            last
            and last.get("role") == "user"
            and isinstance(last.get("content"), list)
            and any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in last["content"]
            )
        ):
            return AssistantMessage(
                blocks=[TextBlock(text="bench: multi-tool summary placeholder.")],
                stop_reason="end_turn",
            )

        # Phase 1: route on first user turn.
        text = self._first_user_text(messages)
        batch = self._MULTI_BATCHES.get(text)
        if batch is None:
            return self._fallback.chat(system, messages, tools)

        # Only emit tools that are actually registered.
        registered = {t["name"] for t in tools}
        blocks: list = [TextBlock(text="bench: dispatching multi-tool batch.")]
        for name, args in batch:
            if name not in registered:
                continue
            blocks.append(
                ToolUseBlock(
                    id=f"bench-{uuid.uuid4().hex[:8]}",
                    name=name,
                    input=args,
                )
            )
        return AssistantMessage(blocks=blocks, stop_reason="tool_use")

    @staticmethod
    def _first_user_text(messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        return blk.get("text", "")
        return ""


# ---- Agent / Guardrail factory --------------------------------------------


def _build_config(tmp_dir: Path) -> Config:
    cfg = Config(base_dir=_REPO_ROOT)
    cfg.audit.database = str(tmp_dir / "bench.db")
    cfg.audit.jsonl_file = str(tmp_dir / "bench.jsonl")
    cfg.safety.rules_file = "configs/safety-rules.yaml"
    cfg.agent.llm_backend = "mock"
    cfg.agent.max_iterations = 6
    # Best-effort: if a future optimization adds a runtime.parallel_tools toggle,
    # turn it on; pre-optimization this attribute is absent and the setattr
    # silently fails inside Pydantic, which we tolerate.
    try:
        setattr(cfg, "runtime", type(cfg).model_construct(_fields_set=set()))
    except Exception:
        pass
    return cfg


def _build_agent(tmp_dir: Path, backend: LlmBackend) -> Agent:
    cfg = _build_config(tmp_dir)
    agent = Agent.from_config(cfg, confirm=lambda *a, **k: False)
    # Inject the chosen backend (Agent.from_config builds a MockBackend by
    # default; we swap so single-tool / multi-tool queries use the right one).
    agent.llm = backend
    return agent


def _reset_messages(agent: Agent) -> None:
    """Each timed ask() must start from a clean message history so per-call
    latency is comparable across iterations."""
    agent.messages.clear()


# ---- Timed phases ----------------------------------------------------------


def _time_ask_workload(tmp_dir: Path) -> dict[str, Any]:
    single_agent = _build_agent(tmp_dir / "single", MockBackend())
    multi_agent = _build_agent(tmp_dir / "multi", ScriptedMultiToolBackend())

    # Warmup (untimed): 5 mixed.
    for q in SINGLE_TOOL_QUERIES[:3]:
        _reset_messages(single_agent)
        single_agent.ask(q)
    for q in MULTI_TOOL_QUERIES[:2]:
        _reset_messages(multi_agent)
        multi_agent.ask(q)

    samples_ns: list[int] = []
    cpu_start = time.process_time_ns()

    for q in SINGLE_TOOL_QUERIES:
        _reset_messages(single_agent)
        t0 = time.perf_counter_ns()
        single_agent.ask(q)
        samples_ns.append(time.perf_counter_ns() - t0)

    for q in MULTI_TOOL_QUERIES:
        _reset_messages(multi_agent)
        t0 = time.perf_counter_ns()
        multi_agent.ask(q)
        samples_ns.append(time.perf_counter_ns() - t0)

    cpu_total = time.process_time_ns() - cpu_start

    samples_ns.sort()
    return {
        "n": len(samples_ns),
        "p50_ns": samples_ns[len(samples_ns) // 2],
        "p95_ns": samples_ns[int(len(samples_ns) * 0.95)],
        "mean_ns": int(statistics.mean(samples_ns)),
        "min_ns": samples_ns[0],
        "max_ns": samples_ns[-1],
        "total_cpu_ns": cpu_total,
    }


def _time_guardrail(tmp_dir: Path) -> dict[str, Any]:
    cfg = _build_config(tmp_dir)
    guard = Guardrail.from_config(cfg)

    # Warmup
    for _ in range(50):
        for argv in GUARDRAIL_ARGV_SAMPLES:
            guard.check_argv(list(argv))

    samples_ns: list[int] = []
    for _ in range(200):  # 200 reps x 10 argvs = 2000 calls
        for argv in GUARDRAIL_ARGV_SAMPLES:
            t0 = time.perf_counter_ns()
            guard.check_argv(list(argv))
            samples_ns.append(time.perf_counter_ns() - t0)

    samples_ns.sort()
    return {
        "n": len(samples_ns),
        "p50_ns": samples_ns[len(samples_ns) // 2],
        "p95_ns": samples_ns[int(len(samples_ns) * 0.95)],
        "mean_ns": int(statistics.mean(samples_ns)),
    }


def _time_audit(tmp_dir: Path) -> dict[str, Any]:
    from kyagent.audit.logger import AuditLogger
    from kyagent.audit.store import AuditStore
    from kyagent.audit.trace import EventKind, Trace

    db = tmp_dir / "audit_bench.db"
    jsonl = tmp_dir / "audit_bench.jsonl"
    store = AuditStore(db)
    logger = AuditLogger(store, jsonl_file=jsonl)
    trace = Trace(user="bench")
    logger.open(trace)

    # Warmup
    for i in range(50):
        logger.event(trace, EventKind.LLM_THOUGHT, {"i": i, "text": "warmup"})

    t0 = time.perf_counter_ns()
    for i in range(1000):
        logger.event(
            trace,
            EventKind.LLM_THOUGHT,
            {"i": i, "text": "benchmark event payload " * 4},
        )
    total_ns = time.perf_counter_ns() - t0

    logger.close(trace)
    return {
        "n": 1000,
        "total_ns": total_ns,
        "mean_ns": total_ns // 1000,
    }


# ---- Driver ----------------------------------------------------------------


def run_benchmark() -> dict[str, Any]:
    # ignore_cleanup_errors handles Windows holding SQLite file locks after
    # the test finishes; the *measurement* is already complete by then so
    # cleanup failures cannot perturb the recorded numbers.
    with tempfile.TemporaryDirectory(
        prefix="kyagent-bench-", ignore_cleanup_errors=True
    ) as td:
        tmp = Path(td)
        ask_metrics = _time_ask_workload(tmp)
        guard_metrics = _time_guardrail(tmp)
        audit_metrics = _time_audit(tmp)

    return {
        "schema": "kyagent-bench-v1",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "ask": ask_metrics,
        "guardrail": guard_metrics,
        "audit": audit_metrics,
    }


def _fmt_ms(ns: int) -> str:
    return f"{ns/1_000_000:.3f}ms"


def _fmt_us(ns: int) -> str:
    return f"{ns/1_000:.2f}us"


def _print_report(label: str, m: dict[str, Any]) -> None:
    print(f"=== {label} ({m['schema']}) ===")
    print(f"  python={m['python']} platform={m['platform']}")
    a = m["ask"]
    print(
        f"  ask  n={a['n']:3d}  p50={_fmt_ms(a['p50_ns']):>10s}  "
        f"p95={_fmt_ms(a['p95_ns']):>10s}  mean={_fmt_ms(a['mean_ns']):>10s}  "
        f"cpu_total={_fmt_ms(a['total_cpu_ns']):>10s}"
    )
    g = m["guardrail"]
    print(
        f"  grd  n={g['n']:5d}  p50={_fmt_us(g['p50_ns']):>9s}  "
        f"p95={_fmt_us(g['p95_ns']):>9s}  mean={_fmt_us(g['mean_ns']):>9s}"
    )
    au = m["audit"]
    print(
        f"  aud  n={au['n']:5d}  total={_fmt_ms(au['total_ns']):>10s}  "
        f"mean={_fmt_us(au['mean_ns']):>9s}/event"
    )


def _compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    def ratio(a: int, b: int) -> float:
        return b / a if a else float("inf")

    def pct(a: int, b: int) -> str:
        if not a:
            return "n/a"
        delta = (b - a) / a * 100.0
        return f"{delta:+.1f}%"

    ba, ca = baseline["ask"], current["ask"]
    bg, cg = baseline["guardrail"], current["guardrail"]
    bau, cau = baseline["audit"], current["audit"]

    ask_p50_ratio = ratio(ba["p50_ns"], ca["p50_ns"])
    ask_p95_ratio = ratio(ba["p95_ns"], ca["p95_ns"])
    grd_p50_ratio = ratio(bg["p50_ns"], cg["p50_ns"])
    aud_ratio = ratio(bau["total_ns"], cau["total_ns"])

    pass_ask_p50 = ask_p50_ratio <= 0.75
    pass_ask_p95 = ask_p95_ratio <= 1.0
    pass_guard = grd_p50_ratio <= 0.75
    pass_audit = aud_ratio <= 0.75

    return {
        "ask_p50": {
            "baseline_ns": ba["p50_ns"],
            "current_ns": ca["p50_ns"],
            "ratio": round(ask_p50_ratio, 3),
            "delta": pct(ba["p50_ns"], ca["p50_ns"]),
            "pass": pass_ask_p50,
        },
        "ask_p95": {
            "baseline_ns": ba["p95_ns"],
            "current_ns": ca["p95_ns"],
            "ratio": round(ask_p95_ratio, 3),
            "delta": pct(ba["p95_ns"], ca["p95_ns"]),
            "pass": pass_ask_p95,
        },
        "guardrail_p50": {
            "baseline_ns": bg["p50_ns"],
            "current_ns": cg["p50_ns"],
            "ratio": round(grd_p50_ratio, 3),
            "delta": pct(bg["p50_ns"], cg["p50_ns"]),
            "pass": pass_guard,
        },
        "audit_total": {
            "baseline_ns": bau["total_ns"],
            "current_ns": cau["total_ns"],
            "ratio": round(aud_ratio, 3),
            "delta": pct(bau["total_ns"], cau["total_ns"]),
            "pass": pass_audit,
        },
        "overall_pass": pass_ask_p50 and pass_ask_p95 and pass_guard and pass_audit,
    }


def main() -> int:
    here = Path(__file__).parent
    baseline_path = here / "baseline.json"
    results_path = here / "results.json"

    if "--save-baseline" in sys.argv:
        current = run_benchmark()
        baseline_path.write_text(
            json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _print_report("BASELINE (saved)", current)
        print(f"\nBaseline written to {baseline_path}")
        return 0

    current = run_benchmark()
    results_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _print_report("CURRENT", current)

    if not baseline_path.exists():
        print(
            "\nNo baseline.json found. Run with --save-baseline to capture one.",
            file=sys.stderr,
        )
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    _print_report("BASELINE", baseline)
    comparison = _compare(baseline, current)
    print("\n=== Comparison vs baseline ===")
    for key in ("ask_p50", "ask_p95", "guardrail_p50", "audit_total"):
        c = comparison[key]
        mark = "PASS" if c["pass"] else "FAIL"
        print(
            f"  {key:>15s}  {mark}  ratio={c['ratio']:.3f}  delta={c['delta']:>8s}"
        )
    print(
        f"\nOverall: {'PASS — optimization is effective' if comparison['overall_pass'] else 'FAIL — optimization is NOT effective'}"
    )
    (here / "comparison.json").write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0 if comparison["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
