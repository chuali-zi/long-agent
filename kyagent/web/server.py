"""FastAPI B/S server for kyagent.

Routes:
    GET  /                     单页前端（静态 index.html）
    GET  /api/health           健康检查
    GET  /api/tools            工具清单
    POST /api/ask              单轮提问（同步返回 final_text + trace_id）
    POST /api/ask/stream       SSE 流式（推 ProgressEvent）
    POST /api/safety/check     安全护栏裁决（不真正执行）
    GET  /api/audit/traces     trace 列表
    GET  /api/audit/traces/{id} trace 详情

Design notes
------------
* Agent.ask() 是同步 + 阻塞（含 HTTP + subprocess），通过 ``run_in_threadpool``
  扔到线程池，避免拖住事件循环。
* 流式：在请求线程里跑 ask，progress 回调把事件塞进一个 ``queue.Queue``；
  EventSource generator 从 queue 拉，转 SSE 格式吐出。这样不需要 asyncio
  的复杂 ContextVar 倒灌，逻辑清楚。
* 一个 session_id → 一个 Agent 实例缓存。无 session_id 时每次新建（无上下文）。
* 所有安全护栏（intent + argv + executor 账户隔离）走的还是 Agent.ask()
  原路径，HTTP 层不绕过任何检查。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kyagent.agent.core import Agent
from kyagent.audit.store import AuditStore
from kyagent.config import Config, load_config
from kyagent.confirm import auto_deny
from kyagent.mcp.tools import default_registry
from kyagent.progress import ProgressEvent
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.intent import IntentGuard
from kyagent.safety.policy import Decision
from kyagent.web import schemas as S

_VERSION = "0.1.0"
_STATIC_DIR = Path(__file__).parent / "static"


# ---- session 缓存 ---------------------------------------------------------


class _AgentSessionRegistry:
    """单进程内多 session 的 Agent 实例缓存。

    线程安全（FastAPI 在 threadpool 里跑同步处理，多个请求并发）。
    每个 session_id 一个 Agent；无 session_id 的请求每次新建（一次性）。
    """

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._agents: dict[str, Agent] = {}

    def get_or_create(self, session_id: Optional[str]) -> Agent:
        if not session_id:
            return self._fresh()
        with self._lock:
            agent = self._agents.get(session_id)
            if agent is None:
                agent = self._fresh()
                self._agents[session_id] = agent
            return agent

    def reset(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._agents:
                self._agents[session_id].messages.clear()
                return True
            return False

    def _fresh(self) -> Agent:
        # web 通道默认拒绝所有 confirm（无人值守路径）；
        # 高风险动作会被 deny，不会偷偷 confirm-pass。
        return Agent.from_config(self._cfg, confirm=auto_deny)


# ---- app 工厂 -------------------------------------------------------------


def build_app(cfg: Optional[Config] = None) -> FastAPI:
    """构造 FastAPI 应用。

    `cfg=None` 时走默认配置加载链（与 CLI 一致）。在测试 / 嵌入式场景里
    可显式注入预构建的 Config。
    """
    cfg = cfg or load_config(None)
    sessions = _AgentSessionRegistry(cfg)

    app = FastAPI(
        title="kyagent",
        version=_VERSION,
        description="麒麟安全运维 Agent — B/S 接入层",
    )
    # 同源部署时不需要 CORS，但本地前端开发常跑在不同端口；放宽到 *
    # 注意：所有写入路径仍走安全护栏，CORS 放宽并不放宽授权
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ---- 路由：health -----------------------------------------------------

    @app.get("/api/health", response_model=S.HealthResponse)
    async def health():
        return S.HealthResponse(
            status="ok",
            version=_VERSION,
            backend=cfg.agent.llm_backend,
            tools=len(default_registry().all()),
            audit_db=str(cfg.resolve(cfg.audit.database)),
        )

    # ---- 路由：tools ------------------------------------------------------

    @app.get("/api/tools", response_model=S.ToolListResponse)
    async def list_tools():
        reg = default_registry()
        items = []
        enable = set(cfg.mcp.enable_tools or [])
        for t in reg.all():
            if enable and t.name not in enable:
                continue
            items.append(S.ToolDescriptor(
                name=t.name,
                description=t.description,
                risk=t.risk_level.value,
                requires_root=t.requires_root,
                read_only=t.read_only,
                input_schema=t.input_schema,
            ))
        return S.ToolListResponse(count=len(items), tools=items)

    # ---- 路由：ask（同步） -------------------------------------------------

    @app.post("/api/ask", response_model=S.AskResponse)
    async def ask(req: S.AskRequest):
        agent = sessions.get_or_create(req.session_id)
        result = await run_in_threadpool(agent.ask, req.text, req.user)
        return S.AskResponse(
            trace_id=result.trace.trace_id,
            text=result.final_text,
            tool_iterations=result.tool_iterations,
            denied=result.denied,
            notes=result.notes,
            backend=agent.llm.name,
        )

    @app.post("/api/sessions/{session_id}/reset")
    async def reset_session(session_id: str):
        ok = sessions.reset(session_id)
        return {"reset": ok}

    # ---- 路由：ask/stream（SSE） ------------------------------------------

    @app.post("/api/ask/stream")
    async def ask_stream(req: S.AskRequest):
        agent = sessions.get_or_create(req.session_id)
        q: queue.Queue = queue.Queue(maxsize=512)
        _SENTINEL = object()

        def on_progress(ev: ProgressEvent) -> None:
            # 把 progress 事件塞进 queue；满了就丢（避免拖垮 worker）
            try:
                q.put_nowait({
                    "kind": ev.kind,
                    "text": ev.text,
                    "tool": ev.tool,
                    "argv": ev.argv,
                    "delta": ev.delta,
                    "meta": ev.meta,
                })
            except queue.Full:
                pass

        # 临时挂上 progress 回调；这是当前会话的一次性流式订阅，
        # 不影响其他 session 或同 session 的下次非流式调用
        prev_cb = agent.on_progress
        agent.on_progress = on_progress

        def worker() -> dict[str, Any]:
            try:
                result = agent.ask(req.text, user=req.user)
                return {
                    "trace_id": result.trace.trace_id,
                    "text": result.final_text,
                    "tool_iterations": result.tool_iterations,
                    "denied": result.denied,
                    "notes": result.notes,
                }
            finally:
                # 不管成败都恢复回调并投毒终止 generator
                agent.on_progress = prev_cb
                q.put(_SENTINEL)

        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, worker)

        async def event_stream():
            try:
                while True:
                    # 非阻塞 poll：避免在 asyncio 里直接 q.get() 卡住
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    if item is _SENTINEL:
                        break
                    yield _sse_pack("progress", item)
                # worker 收尾后再吐一次 final
                result = await fut
                yield _sse_pack("final", result)
            except asyncio.CancelledError:
                # 客户端断开 — 不阻止后台 worker 跑完（审计要完整落库）
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 反向代理别缓冲
            },
        )

    # ---- 路由：safety/check -----------------------------------------------

    @app.post("/api/safety/check", response_model=S.SafetyCheckResponse)
    async def safety_check(req: S.SafetyCheckRequest):
        intent_block: Optional[S.SafetyVerdictBlock] = None
        argv_block: Optional[S.SafetyVerdictBlock] = None

        if req.layer in ("intent", "both"):
            ig = IntentGuard.from_config(cfg)
            v = await run_in_threadpool(ig.evaluate, req.text)
            intent_block = S.SafetyVerdictBlock(
                layer="intent",
                risk=v.risk.value,
                decision=v.decision.value,
                hits=[S.SafetyHit(
                    rule_id=h.rule_id,
                    description=getattr(h, "description", None),
                    risk=h.risk.value,
                    matched=str(h.matched),
                    category=getattr(h, "category", None),
                ) for h in v.hits],
                rationale=v.rationale,
            )

        if req.layer in ("argv", "both"):
            gr = Guardrail.from_config(cfg)
            v = await run_in_threadpool(gr.check_cmdline, req.text)
            argv_block = S.SafetyVerdictBlock(
                layer="argv",
                risk=v.risk.value,
                decision=v.decision.value,
                hits=[S.SafetyHit(
                    rule_id=h.rule_id,
                    description=getattr(h, "description", None),
                    risk=h.risk.value,
                    matched=str(h.matched),
                ) for h in v.hits],
                rationale=v.rationale,
            )

        final_risk = None
        final_decision = None
        if intent_block and argv_block:
            from kyagent.safety.policy import Decision as _D
            from kyagent.safety.patterns import RiskLevel as _R
            ds = [_D(intent_block.decision), _D(argv_block.decision)]
            rs = [_R(intent_block.risk), _R(argv_block.risk)]
            final_decision = max(ds, key=lambda d: d.order).value
            final_risk = max(rs, key=lambda r: r.order).value

        return S.SafetyCheckResponse(
            intent=intent_block,
            argv=argv_block,
            final_risk=final_risk,
            final_decision=final_decision,
        )

    # ---- 路由：audit ------------------------------------------------------

    def _store() -> AuditStore:
        return AuditStore(cfg.resolve(cfg.audit.database))

    @app.get("/api/audit/traces", response_model=S.TraceListResponse)
    async def audit_traces(limit: int = Query(20, ge=1, le=200)):
        rows = await run_in_threadpool(_store().list_traces, limit)
        summaries = []
        for r in rows:
            meta = r.get("metadata") or {}
            summaries.append(S.TraceSummary(
                trace_id=r["trace_id"],
                user=r.get("user", ""),
                started_at=float(r.get("started_at", 0.0)),
                channel=str(meta.get("channel", meta.get("backend", ""))),
            ))
        return S.TraceListResponse(count=len(summaries), traces=summaries)

    @app.get("/api/audit/traces/{trace_id}", response_model=S.TraceDetailResponse)
    async def audit_trace_detail(trace_id: str):
        events = await run_in_threadpool(_store().get_events, trace_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
        return S.TraceDetailResponse(
            trace_id=trace_id,
            events=[S.TraceEvent(
                seq=int(ev["seq"]),
                kind=ev["kind"],
                payload=ev.get("payload") or {},
            ) for ev in events],
        )

    # ---- 静态前端 ---------------------------------------------------------

    if _STATIC_DIR.exists():
        # 把 / 直接指向 index.html，其它静态文件挂在 /static
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(str(_STATIC_DIR / "index.html"))

    return app


# ---- SSE 工具 -------------------------------------------------------------


def _sse_pack(event: str, data: Any) -> str:
    """SSE 协议帧：event:<name>\\ndata:<json>\\n\\n"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"
