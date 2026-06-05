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
import os
import queue
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kyagent.agent.core import Agent
from kyagent.config import Config, load_config
from kyagent.confirm import auto_deny
from kyagent.mcp.plugins import configured_registry
from kyagent.progress import ProgressEvent
from kyagent.runtime import build_audit_store
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.intent import IntentGuard
from kyagent.web.approvals import ApprovalBroker
from kyagent.web.choices import ChoiceBroker
from kyagent.web.security import WebSecurity
from kyagent.web import schemas as S

_VERSION = "0.1.0"
_STATIC_DIR = Path(__file__).parent / "static"


# ---- session 缓存 ---------------------------------------------------------


class _AgentSessionRegistry:
    """单进程内多 session 的 Agent 实例缓存。

    线程安全（FastAPI 在 threadpool 里跑同步处理，多个请求并发）。
    每个 session_id 一个 Agent；无 session_id 的请求每次新建（一次性）。
    """

    def __init__(self, cfg: Config, *, max_sessions: int = 128, ttl_seconds: float = 1800.0):
        self._cfg = cfg
        self._max_sessions = max(1, max_sessions)
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._lock = threading.Lock()
        self._agents: OrderedDict[str, tuple[Agent, float]] = OrderedDict()

    def get_or_create(self, session_id: Optional[str], *, owner: str = "local-web") -> Agent:
        if not session_id:
            return self._fresh()
        cache_key = f"{owner}:{session_id}"
        evicted: list[Agent] = []
        with self._lock:
            now = time.monotonic()
            evicted.extend(self._expire_locked(now))
            entry = self._agents.pop(cache_key, None)
            if entry is None:
                agent = self._fresh()
            else:
                agent = entry[0]
            self._agents[cache_key] = (agent, now)
            while len(self._agents) > self._max_sessions:
                _, (old_agent, _) = self._agents.popitem(last=False)
                evicted.append(old_agent)
        self._shutdown_all(evicted)
        return agent

    def reset(self, session_id: str, *, owner: str = "local-web") -> bool | str:
        with self._lock:
            entry = self._agents.get(f"{owner}:{session_id}")
            if entry is None:
                return False
            agent = entry[0]
        run_lock = getattr(agent, "_run_lock", None)
        if run_lock is not None and not run_lock.acquire(blocking=False):
            return "busy"
        try:
            agent.messages.clear()
            return True
        finally:
            if run_lock is not None:
                run_lock.release()

    def shutdown(self) -> None:
        with self._lock:
            agents = [entry[0] for entry in self._agents.values()]
            self._agents.clear()
        self._shutdown_all(agents)

    def _expire_locked(self, now: float) -> list[Agent]:
        expired = []
        for session_id, (agent, touched_at) in list(self._agents.items()):
            if now - touched_at >= self._ttl_seconds:
                self._agents.pop(session_id)
                expired.append(agent)
        return expired

    @staticmethod
    def _shutdown_all(agents: list[Agent]) -> None:
        for agent in agents:
            agent.shutdown()

    def _fresh(self) -> Agent:
        # web 通道默认拒绝所有 confirm（无人值守路径）；
        # 高风险动作会被 deny，不会偷偷 confirm-pass。
        return Agent.from_config(self._cfg, confirm=auto_deny)


def _preflight_audit_store(cfg: Config) -> None:
    """Fail at startup if the configured audit store cannot be opened."""
    store = None
    try:
        store = build_audit_store(cfg)
    except Exception as exc:  # noqa: BLE001
        db_path = cfg.resolve(cfg.audit.database)
        jsonl_path = cfg.resolve(cfg.audit.jsonl_file) if cfg.audit.jsonl_file else None
        detail = f"audit store is not writable: {db_path}"
        if jsonl_path is not None:
            detail += f" (jsonl: {jsonl_path})"
        raise RuntimeError(detail) from exc
    finally:
        if store is not None:
            store.close()


# ---- app 工厂 -------------------------------------------------------------


def build_app(cfg: Optional[Config] = None) -> FastAPI:
    """构造 FastAPI 应用。

    `cfg=None` 时走默认配置加载链（与 CLI 一致）。在测试 / 嵌入式场景里
    可显式注入预构建的 Config。
    """
    cfg = cfg or load_config(None)
    _preflight_audit_store(cfg)
    sessions = _AgentSessionRegistry(
        cfg,
        max_sessions=_env_int("KYAGENT_WEB_MAX_SESSIONS", 128),
        ttl_seconds=_env_float("KYAGENT_WEB_SESSION_TTL_SECONDS", 1800.0),
    )
    approvals = ApprovalBroker(max_records=_env_int("KYAGENT_WEB_MAX_APPROVALS", 256))
    choices = ChoiceBroker(max_records=_env_int("KYAGENT_WEB_MAX_CHOICES", 256))
    security = WebSecurity.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            sessions.shutdown()

    app = FastAPI(
        title="kyagent",
        version=_VERSION,
        description="麒麟安全运维 Agent — B/S 接入层",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(security.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def enforce_web_security(request: Request, call_next):
        request.state.web_principal = security.role_for_request(request) or "local-web"
        denied = security.check(request)
        return denied if denied is not None else await call_next(request)

    # ---- 路由：health -----------------------------------------------------

    @app.get("/api/health", response_model=S.HealthResponse)
    async def health():
        return S.HealthResponse(
            status="ok",
            version=_VERSION,
        )

    # ---- 路由：tools ------------------------------------------------------

    @app.get("/api/tools", response_model=S.ToolListResponse)
    async def list_tools():
        reg = configured_registry(cfg)
        items = []
        for t in reg.all():
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
    async def ask(req: S.AskRequest, request: Request):
        user = request.state.web_principal
        agent = sessions.get_or_create(req.session_id, owner=user)
        try:
            result = await run_in_threadpool(agent.ask, req.text, user)
            return S.AskResponse(
                trace_id=result.trace.trace_id,
                text=result.final_text,
                tool_iterations=result.tool_iterations,
                denied=result.denied,
                notes=result.notes,
                backend=agent.llm.name,
            )
        finally:
            if not req.session_id:
                agent.shutdown()

    @app.post("/api/sessions/{session_id}/reset")
    async def reset_session(session_id: str, request: Request):
        try:
            S.validate_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ok = sessions.reset(session_id, owner=request.state.web_principal)
        if ok == "busy":
            raise HTTPException(status_code=409, detail="session is busy")
        return {"reset": ok}

    # ---- 路由：approvals --------------------------------------------------

    @app.get("/api/approvals", response_model=S.ApprovalListResponse)
    async def list_approvals(status: Optional[str] = Query(None)):
        records = approvals.list_records(status=status)
        return S.ApprovalListResponse(
            count=len(records),
            approvals=[S.ApprovalRecordResponse(**r.to_dict()) for r in records],
        )

    @app.get("/api/approvals/{approval_id}", response_model=S.ApprovalRecordResponse)
    async def get_approval(approval_id: str):
        rec = approvals.get(approval_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"approval {approval_id} not found")
        return S.ApprovalRecordResponse(**rec.to_dict())

    @app.post("/api/approvals/{approval_id}/approve", response_model=S.ApprovalRecordResponse)
    async def approve_approval(approval_id: str, req: S.ApprovalActionRequest, request: Request):
        rec = approvals.resolve(
            approval_id,
            approved=True,
            reviewer=request.state.web_principal,
            reason=req.reason,
        )
        if rec is None:
            raise HTTPException(status_code=404, detail=f"approval {approval_id} not found")
        return S.ApprovalRecordResponse(**rec.to_dict())

    @app.post("/api/approvals/{approval_id}/reject", response_model=S.ApprovalRecordResponse)
    async def reject_approval(approval_id: str, req: S.ApprovalActionRequest, request: Request):
        rec = approvals.resolve(
            approval_id,
            approved=False,
            reviewer=request.state.web_principal,
            reason=req.reason,
        )
        if rec is None:
            raise HTTPException(status_code=404, detail=f"approval {approval_id} not found")
        return S.ApprovalRecordResponse(**rec.to_dict())

    # ---- 路由：choices ----------------------------------------------------

    @app.get("/api/choices", response_model=S.ChoiceListResponse)
    async def list_choices(status: Optional[str] = Query(None)):
        records = choices.list_records(status=status)
        return S.ChoiceListResponse(
            count=len(records),
            choices=[S.ChoiceRecordResponse(**record.to_dict()) for record in records],
        )

    @app.post("/api/choices/{choice_id}/select", response_model=S.ChoiceRecordResponse)
    async def select_choice(choice_id: str, req: S.ChoiceActionRequest, request: Request):
        record = choices.resolve(
            choice_id, value=req.value, reviewer=request.state.web_principal
        )
        if record is None:
            raise HTTPException(status_code=404, detail=f"choice {choice_id} not found")
        if record.status != "selected":
            raise HTTPException(status_code=409, detail=f"choice {choice_id} is not pending")
        return S.ChoiceRecordResponse(**record.to_dict())

    # ---- 路由：ask/stream（SSE） ------------------------------------------

    @app.post("/api/ask/stream")
    async def ask_stream(req: S.AskRequest, request: Request):
        user = request.state.web_principal
        agent = sessions.get_or_create(req.session_id, owner=user)
        q: queue.Queue = queue.Queue(maxsize=512)

        def enqueue(event: str, data: dict[str, Any]) -> None:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

        def on_progress(ev: ProgressEvent) -> None:
            # 把 progress 事件塞进 queue；满了就丢（避免拖垮 worker）
            enqueue("progress", {
                "kind": ev.kind,
                "text": ev.text,
                "tool": ev.tool,
                "argv": ev.argv,
                "delta": ev.delta,
                "meta": ev.meta,
            })

        def web_confirm(confirm_req):
            rec = approvals.create(
                confirm_req,
                session_id=req.session_id,
                user=user,
                emit=enqueue,
            )
            enqueue("approval_required", rec.to_dict())
            return approvals.wait(rec.approval_id)

        def web_user_choice(choice):
            rec = choices.create(choice, session_id=req.session_id, user=user, emit=enqueue)
            enqueue("choice_required", rec.to_dict())
            return choices.wait(rec.choice_id)

        def worker() -> dict[str, Any]:
            run_lock = getattr(agent, "_run_lock", threading.RLock())
            with run_lock:
                # 临时挂上 progress / confirm 回调；整个 ask turn 和恢复过程
                # 在同一把 Agent 运行锁里，避免同 session 并发 stream 串事件。
                prev_cb = agent.on_progress
                prev_confirm = agent.confirm
                prev_choice = agent.on_user_choice
                agent.on_progress = on_progress
                agent.confirm = web_confirm
                agent.on_user_choice = web_user_choice
                try:
                    result = agent.ask(req.text, user=user)
                    return {
                        "trace_id": result.trace.trace_id,
                        "text": result.final_text,
                        "tool_iterations": result.tool_iterations,
                        "denied": result.denied,
                        "notes": result.notes,
                    }
                except Exception as exc:
                    payload = {"error": str(exc), "text": str(exc)}
                    return {**payload, "denied": True, "notes": ["worker exception"]}
                finally:
                    agent.on_progress = prev_cb
                    agent.confirm = prev_confirm
                    agent.on_user_choice = prev_choice
                    if not req.session_id:
                        agent.shutdown()

        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, worker)

        async def event_stream():
            try:
                while True:
                    # 非阻塞 poll：避免在 asyncio 里直接 q.get() 卡住
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        if fut.done():
                            result = await fut
                            if result.get("error"):
                                yield _sse_pack("error", {"text": result["error"]})
                            yield _sse_pack("final", result)
                            break
                        await asyncio.sleep(0.02)
                        continue
                    event_name, payload = item
                    yield _sse_pack(event_name, payload)
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

    def _list_traces(limit: int):
        store = build_audit_store(cfg)
        try:
            return store.list_traces(limit)
        finally:
            store.close()

    def _get_events(trace_id: str):
        store = build_audit_store(cfg)
        try:
            return store.get_events(trace_id)
        finally:
            store.close()

    @app.get("/api/audit/traces", response_model=S.TraceListResponse)
    async def audit_traces(limit: int = Query(20, ge=1, le=200)):
        rows = await run_in_threadpool(_list_traces, limit)
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
        events = await run_in_threadpool(_get_events, trace_id)
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
