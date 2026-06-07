"""Agent 主循环：接收 → 感知 → 推理 → 校验 → 执行 → 审计 闭环。

把所有子系统组合起来：LLM 决策 → Guardrail 二次过滤 → ExecutionProxy 落地 → AuditLogger 全链路。
"""
from __future__ import annotations

import sys
import threading
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from kyagent.agent.llm import (
    AssistantMessage,
    LlmBackend,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    build_backend,
)
from kyagent.agent.prompt import SYSTEM_PROMPT
from kyagent.audit.logger import AuditLogger
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config, load_config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.mcp.tools.base import ToolError, ToolRegistry
from kyagent.progress import ProgressCallback, ProgressEvent, noop_progress
from kyagent.planner import PlanSnapshot, PlanStore
from kyagent.mcp.tools.pipeline import (
    PipelineError,
    check_safety,
    execute_and_format,
    prepare_call,
)
from kyagent.agent import confirm_adapter
from kyagent.confirm import ConfirmFn, auto_deny
from kyagent.interactive import (
    UserChoice,
    UserChoiceFn,
    UserChoiceOption,
    auto_cancel_choice,
)
from kyagent.runtime import build_runtime
from kyagent.rca import load_playbooks, validate_report
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.intent import IntentGuard, IntentVerdict
from kyagent.safety.policy import Decision


# Confirm 回调：单参 ConfirmRequest → True 表示用户同意继续。
# 具体的 verdict → ConfirmRequest 翻译由各 Verdict 自己负责（to_confirm_request），
# Agent 与 UI 都不依赖具体 verdict 类型。
_auto_deny = auto_deny  # backward-compat 别名，旧引用不破


@dataclass
class AgentRunResult:
    trace: Trace
    final_text: str
    tool_iterations: int = 0
    denied: bool = False
    notes: list[str] = field(default_factory=list)
    plan_id: str | None = None


class Agent:
    """单轮 turn 由 .ask() 完成；维护 messages 上下文。"""

    def __init__(
        self,
        cfg: Config,
        llm: LlmBackend,
        registry: ToolRegistry,
        guardrail: Guardrail,
        executor: ExecutionProxy,
        audit: AuditLogger,
        confirm: ConfirmFn = _auto_deny,
        intent_guard: IntentGuard | None = None,
        on_progress: ProgressCallback | None = None,
        on_user_choice: UserChoiceFn | None = None,
        plan_store: PlanStore | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.guardrail = guardrail
        self.intent_guard = intent_guard  # 赛题第 3 条：NL 意图层（None 则跳过）
        self.executor = executor
        self.audit = audit
        self.confirm = confirm
        # on_progress 一旦赋值不再变更：worker 线程读到的总是同一个 callable
        self.on_progress: ProgressCallback = on_progress or noop_progress
        # ask_user_choice 工具的回调；UI 未注入时默认拒绝（保守）
        self.on_user_choice: UserChoiceFn = on_user_choice or auto_cancel_choice
        self.plan_store = plan_store
        self.messages: list[dict] = []
        self.system_prompt = SYSTEM_PROMPT
        self._run_lock = threading.RLock()
        self._active_run_thread_id: int | None = None
        # 持久线程池：避免每多工具回合都付一次 thread spawn 的固定开销，
        # 在 Windows mock 后端这一开销会盖过并行带来的收益。
        self._tool_pool: ThreadPoolExecutor | None = None
        self._shutdown = False

    def _ensure_pool(self) -> ThreadPoolExecutor:
        if self._tool_pool is None:
            self._tool_pool = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="ky-tool"
            )
        return self._tool_pool

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._tool_pool is not None:
            self._tool_pool.shutdown(wait=False)
            self._tool_pool = None
        self.audit.close_file()
        self.audit.store.close()
        if self.plan_store is not None:
            self.plan_store.close()

    def _emit(self, event: ProgressEvent) -> None:
        """防御式包装：TUI/外部回调抛异常不应影响 Agent 主循环。

        审计照常走，进度静默丢弃。回调可能从 worker 线程被触发；并发安全由
        UI 端自己负责（progress.py 注释里写了 callback 必须不 raise）。
        """
        try:
            self.on_progress(event)
        except Exception:
            pass

    @classmethod
    def from_config(cls, cfg: Config, confirm: ConfirmFn = _auto_deny,
                    on_progress: ProgressCallback | None = None,
                    on_user_choice: UserChoiceFn | None = None) -> "Agent":
        # 通道无关基础设施统一从 composition root 装配
        rt = build_runtime(cfg)
        # 通道特定（LLM 后端、NL 意图层）在这里组合
        llm = build_backend(cfg)
        intent_guard = IntentGuard.from_config(cfg) if cfg.safety.intent_check else None
        plan_store = None
        if getattr(cfg.planning, "enabled", True):
            plan_store = PlanStore(cfg.resolve(cfg.planning.database))
        return cls(cfg, llm, rt.registry, rt.guardrail, rt.executor, rt.audit, confirm,
                   intent_guard=intent_guard, on_progress=on_progress,
                   on_user_choice=on_user_choice, plan_store=plan_store)

    # ---- 主入口 --------------------------------------------------------

    def ask(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        with self._run_lock:
            previous_thread_id = self._active_run_thread_id
            self._active_run_thread_id = threading.get_ident()
            try:
                return self._ask_impl(user_input, user=user)
            finally:
                self._active_run_thread_id = previous_thread_id

    def _ask_impl(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        trace = Trace(user=user)
        self.audit.open(trace)
        trace.metadata.update({"backend": self.llm.name})
        plan: PlanSnapshot | None = None
        self._emit(ProgressEvent(kind="agent_start", text=user_input))

        self.audit.event(trace, EventKind.USER_INPUT, {"text": user_input})
        if self.plan_store is not None:
            plan = self.plan_store.create_run_plan(
                trace_id=trace.trace_id,
                user=user,
                title=user_input,
                metadata={"backend": self.llm.name},
            )
            trace.metadata["plan_id"] = plan.plan_id
            self._emit_plan(trace, plan, kind="plan_start", text=plan.title)

        notes: list[str] = []
        iterations = 0
        denied = False

        # ===== 赛题第 3 条：NL 意图层 + 抗 Prompt Injection =====
        # 这是 LLM 看到 user_input 之前的"一次过滤"。argv 层 Guardrail 是 LLM 输出
        # 之后的"二次过滤"，两者互补缺一不可。
        effective_input = user_input
        if self.intent_guard is not None:
            intent_verdict: IntentVerdict = self.intent_guard.evaluate(
                user_input, context={"user": user}
            )
            self.audit.event(trace, EventKind.INTENT_CHECK, intent_verdict.to_dict())

            if intent_verdict.decision is Decision.DENY:
                denied = True
                notes.append(
                    f"已在意图层拦截（risk={intent_verdict.risk.value}）：" +
                    ",".join(h.rule_id for h in intent_verdict.hits[:3])
                )
                reply = (
                    f"[blocked] 你的请求被自然语言意图风险过滤器拦截。\n"
                    f"风险等级：{intent_verdict.risk.value}\n"
                    + "\n".join(intent_verdict.rationale)
                )
                self.audit.event(trace, EventKind.AGENT_REPLY,
                                 {"text": reply, "blocked_at": "intent"})
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "receive", "failed", "Intent layer denied request",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "failed", current_step="receive")
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=reply,
                                      tool_iterations=0, denied=True, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            if intent_verdict.decision is Decision.CONFIRM:
                approved = False
                try:
                    approved = self.confirm(confirm_adapter.for_intent(intent_verdict))
                except Exception:
                    approved = False
                if not approved:
                    denied = True
                    notes.append(f"用户拒绝意图层 confirm（risk={intent_verdict.risk.value}）")
                    reply = (
                        f"[denied] 用户拒绝高风险意图请求（risk={intent_verdict.risk.value}）"
                    )
                    self.audit.event(trace, EventKind.AGENT_REPLY,
                                     {"text": reply, "blocked_at": "intent_confirm"})
                    if plan is not None:
                        plan = self._set_plan_step(
                            trace, plan, "receive", "failed",
                            "User rejected intent confirmation",
                            event_kind="plan_step_end",
                        )
                        plan = self._set_plan_status(trace, plan, "failed", current_step="receive")
                    self.audit.close(trace)
                    return AgentRunResult(trace=trace, final_text=reply,
                                          tool_iterations=0, denied=True, notes=notes,
                                          plan_id=plan.plan_id if plan else None)

            # 净化 stealth injection（零宽字符）：把净化后的文本送进 LLM，
            # 保留原文在 USER_INPUT 事件里以便审计追溯
            if intent_verdict.sanitized_text is not None:
                effective_input = intent_verdict.sanitized_text
                notes.append("已剥离零宽字符送入 LLM")

        self.messages.append({"role": "user", "content": effective_input})

        tools_for_llm = self.registry.to_anthropic_tools()
        if plan is not None:
            plan = self._set_plan_step(
                trace, plan, "receive", "complete", "Request accepted",
                event_kind="plan_step_end",
            )
            plan = self._set_plan_step(
                trace, plan, "reason", "running", "Starting reasoning/tool loop",
                event_kind="plan_step_start",
            )

        while iterations < self.cfg.agent.max_iterations:
            iterations += 1
            budget_payload = {
                "iteration": iterations,
                "max_iterations": self.cfg.agent.max_iterations,
                "reason": "loop",
                "plan_id": plan.plan_id if plan else None,
            }
            self.audit.event(trace, EventKind.BUDGET, budget_payload)
            self._emit(ProgressEvent(kind="budget_update", meta=budget_payload))
            self._emit(ProgressEvent(
                kind="thinking_start",
                meta={"iteration": iterations},
            ))

            def _on_delta(chunk: str) -> None:
                # 闭包捕获 self，给 TUI 推 token 级增量；空块直接跳过
                if chunk:
                    self._emit(ProgressEvent(kind="thinking_delta", delta=chunk))

            try:
                # 所有后端要么实现 chat_stream（基类默认调 chat 再发一次完整 delta），
                # 要么暂未升级（并行子代理在 llm.py 加）。getattr 兜底保证两侧都能跑。
                stream_fn = getattr(self.llm, "chat_stream", None)
                if callable(stream_fn):
                    assistant = stream_fn(
                        self.system_prompt, self.messages, tools_for_llm, _on_delta
                    )
                else:
                    assistant = self.llm.chat(
                        self.system_prompt, self.messages, tools_for_llm
                    )
                    # fallback：一次性把全部文本作为单条 delta 喷出，保持事件契约
                    for _t in assistant.texts():
                        _on_delta(_t)
            except Exception as e:  # noqa: BLE001
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "llm_error", "detail": str(e)})
                self._emit(ProgressEvent(
                    kind="error",
                    text=str(e),
                    meta={"reason": "llm_error"},
                ))
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "failed", str(e),
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "failed", current_step="reason")
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=f"LLM 调用失败：{e}",
                                      tool_iterations=iterations, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            self._emit(ProgressEvent(
                kind="thinking_end",
                text="\n".join(assistant.texts())[:200],
                meta={"tool_calls": [t.name for t in assistant.tool_uses()]},
            ))
            self.audit.event(trace, EventKind.LLM_THOUGHT,
                             {"stop_reason": assistant.stop_reason,
                              "text": "\n".join(assistant.texts())[:4000],
                              "tool_calls": [t.name for t in assistant.tool_uses()]})

            tool_uses = assistant.tool_uses()

            # 没有工具调用：终结
            if not tool_uses:
                final = "\n".join(assistant.texts()).strip()
                self.messages.append({"role": "assistant",
                                      "content": [{"type": "text", "text": final}]})
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "complete", "No more tool calls",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_step(
                        trace, plan, "verify", "complete", "Final response prepared",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_step(
                        trace, plan, "respond", "complete", "Answer returned",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "complete", current_step="respond")
                self.audit.event(trace, EventKind.AGENT_REPLY, {"text": final})
                self.audit.close(trace)
                self._emit(ProgressEvent(kind="agent_final", text=final))
                return AgentRunResult(trace=trace, final_text=final,
                                      tool_iterations=iterations, denied=denied, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            # 把 assistant 消息原样追加（含 tool_use 块）
            self.messages.append({"role": "assistant",
                                  "content": self._blocks_to_dict(assistant)})

            # 选择串行 / 并行：只有执行器明确声明线程安全，且所有工具预检均为
            # allow-only 只读调用时才进入线程池。confirm/deny/未知/参数错误路径
            # 保持串行，避免交互提示与审计流并发交错。
            tool_results: list[dict | None] = [None] * len(tool_uses)
            run_parallel = (
                sys.platform != "win32"
                and len(tool_uses) >= 2
                and self._executor_supports_parallel_tools()
                and all(self._is_parallel_safe(tu) for tu in tool_uses)
            )

            if run_parallel:
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "running",
                        f"Running {len(tool_uses)} read-only tools in parallel",
                        event_kind="plan_step_update",
                    )
                pool = self._ensure_pool()
                futures = [
                    pool.submit(self._handle_tool_use, trace, tu, notes, True)
                    for tu in tool_uses
                ]
                for idx, (tu, fut) in enumerate(zip(tool_uses, futures)):
                    result_block = fut.result()
                    if result_block.is_error and result_block.content.startswith("[denied]"):
                        denied = True
                    tool_results[idx] = {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_block.content,
                        "is_error": result_block.is_error,
                    }
            else:
                for idx, tu in enumerate(tool_uses):
                    result_block = self._handle_tool_use(trace, tu, notes, False)
                    if result_block.is_error and result_block.content.startswith("[denied]"):
                        denied = True
                    tool_results[idx] = {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_block.content,
                        "is_error": result_block.is_error,
                    }

            # 把 tool_result 作为下一轮 user 消息送回（顺序与 tool_uses 一致）
            self.messages.append({"role": "user", "content": tool_results})

        # 超出最大迭代
        notes.append(f"达到最大迭代次数 {self.cfg.agent.max_iterations}")
        self.audit.event(trace, EventKind.ERROR, {"reason": "max_iterations"})
        if plan is not None:
            plan = self._set_plan_step(
                trace, plan, "reason", "failed", "Reached max_iterations",
                event_kind="plan_step_end",
            )
            plan = self._set_plan_status(trace, plan, "failed", current_step="reason")
        self.audit.close(trace)
        self._emit(ProgressEvent(
            kind="error",
            text="达到最大工具调用次数",
            meta={"reason": "max_iterations"},
        ))
        return AgentRunResult(trace=trace,
                              final_text="达到最大工具调用次数，已中止。",
                              tool_iterations=iterations, denied=denied, notes=notes)

    # ---- 工具调用处理 --------------------------------------------------

    def _emit_plan(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        *,
        kind: str,
        text: str = "",
        step_id: str = "",
    ) -> None:
        snapshot = plan.to_dict()
        payload = {"event": kind, "plan": snapshot, "plan_id": plan.plan_id}
        if step_id:
            payload["step_id"] = step_id
        self.audit.event(trace, EventKind.PLAN_UPDATE, payload)
        self._emit(ProgressEvent(kind=kind, text=text, meta=payload))  # type: ignore[arg-type]
        self._emit(ProgressEvent(
            kind="plan_snapshot",
            text=plan.title,
            meta={"plan": snapshot, "plan_id": plan.plan_id},
        ))

    def _set_plan_step(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        step_id: str,
        status: str,
        detail: str,
        *,
        event_kind: str,
    ) -> PlanSnapshot:
        if self.plan_store is None:
            return plan
        updated = self.plan_store.set_step(plan.plan_id, step_id, status, detail)
        self._emit_plan(trace, updated, kind=event_kind, text=detail, step_id=step_id)
        return updated

    def _set_plan_status(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        status: str,
        *,
        current_step: str | None = None,
    ) -> PlanSnapshot:
        if self.plan_store is None:
            return plan
        updated = self.plan_store.set_status(
            plan.plan_id, status, current_step=current_step
        )
        self._emit_plan(trace, updated, kind="plan_step_update", text=status)
        return updated

    def _executor_supports_parallel_tools(self) -> bool:
        """Whether this executor can safely run tool calls from worker threads."""
        return bool(getattr(self.executor, "supports_parallel_tool_execution", False))

    def _is_parallel_safe(self, tu: ToolUseBlock) -> bool:
        """Return True only for preflighted allow-only read-only tool calls.

        未知工具也按"不可并行"处理；后续 _handle_tool_use 内会以 ERROR 兜底，
        保留原有错误信息。
        """
        if tu.name in {"ask_user_choice", "submit_rca_report"}:
            return False
        tool = self.registry.get(tu.name)
        if tool is None:
            return False
        if not tool.read_only:
            return False
        # 预检的唯一前提：guardrail 必须是确定性的。一旦 llm_reviewer 介入
        # （guardrail.py 明确捕获 reviewer 异常并把结果置 None），主线程预检
        # 拿到的 verdict 与 worker 线程内的 verdict 可能不一致，导致 worker
        # 走到 CONFIRM 分支并触发 self.confirm() 读 stdin。直接拒绝并行。
        if self.guardrail.llm_reviewer is not None:
            return False
        try:
            cleaned = tool.validate(tu.input or {})
            argv = tool.build_argv(cleaned)
        except ToolError:
            return False
        verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
        return verdict.decision is Decision.ALLOW

    def _handle_tool_use(self, trace: Trace, tu: ToolUseBlock,
                        notes: list[str], parallel_read_only: bool = False) -> ToolResultBlock:
        """对外入口：包一层 try/finally，确保 tool_call_end 一定发出。

        入口先发一次只含 tool 名的 tool_call_start；prepare_call 成功后
        inner 会再发一次带 argv 的 tool_call_start 补充信息。
        """
        self._emit(ProgressEvent(kind="tool_call_start", tool=tu.name))
        ok = False
        result_block: ToolResultBlock | None = None
        try:
            result_block = self._handle_tool_use_inner(
                trace, tu, notes, parallel_read_only=parallel_read_only
            )
            ok = (not result_block.is_error)
            return result_block
        finally:
            self._emit(ProgressEvent(
                kind="tool_call_end",
                tool=tu.name,
                text=(result_block.content[:200] if result_block else ""),
                meta={"ok": ok},
            ))

    def _handle_tool_use_inner(self, trace: Trace, tu: ToolUseBlock,
                               notes: list[str],
                               parallel_read_only: bool = False) -> ToolResultBlock:
        # 特判：ask_user_choice 不走 ExecutionProxy / 安全护栏流水线，
        # 它是纯逻辑工具（UI 交互），单独路由。
        if tu.name == "ask_user_choice":
            tool = self.registry.get(tu.name)
            if tool is None:
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content="未知工具：ask_user_choice"
                )
            try:
                cleaned = tool.validate(tu.input or {})
            except ToolError as e:
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "invalid_args", "tool": tu.name, "detail": str(e),
                })
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{e}"
                )
            tu = ToolUseBlock(id=tu.id, name=tu.name, input=cleaned)
            return self._handle_user_choice(trace, tu)
        if tu.name == "submit_rca_report":
            tool = self.registry.get(tu.name)
            if tool is None:
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content="未知工具：submit_rca_report"
                )
            try:
                cleaned = tool.validate(tu.input or {})
            except ToolError as e:
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "invalid_args", "tool": tu.name, "detail": str(e),
                })
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{e}"
                )
            return self._handle_rca_report(trace, tu.id, cleaned)

        tool = self.registry.get(tu.name)
        if tool is None:
            self.audit.event(trace, EventKind.ERROR,
                             {"reason": "unknown_tool", "tool": tu.name})
            return ToolResultBlock(tool_use_id=tu.id, is_error=True,
                                   content=f"未知工具：{tu.name}")

        # 1. validate + build_argv + TOOL_REQUEST + PERCEPTION（共享流水线）
        prep = prepare_call(tool, tu.input or {}, trace=trace, audit=self.audit)
        if isinstance(prep, PipelineError):
            return ToolResultBlock(tool_use_id=tu.id, is_error=True, content=prep.detail)

        # argv 已就绪：补一次 tool_call_start，让 TUI 看到具体命令行
        self._emit(ProgressEvent(
            kind="tool_call_start",
            tool=tu.name,
            argv=list(prep.argv),
        ))

        # 2. 安全护栏（即便是 read_only 工具也过一遍，防止参数注入）
        verdict = check_safety(prep, trace=trace, audit=self.audit, guardrail=self.guardrail)

        if verdict.decision is Decision.DENY:
            notes.append(f"已拦截 {tu.name}: {verdict.risk.value}")
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content=("[denied] 工具调用被安全护栏拒绝。\n"
                         f"风险等级: {verdict.risk.value}\n"
                         + "\n".join(verdict.rationale)),
            )

        if verdict.decision is Decision.CONFIRM:
            # 兜底：confirm() 只能在当前 Agent.ask turn 的拥有线程执行。
            # TUI/CLI 通常是 MainThread；Web/FastAPI 会把 ask 放进 worker
            # 线程。并行工具池线程即使意外拿到 CONFIRM 也必须拒绝，
            # 防止多个 tool worker 争抢同一个交互通道。
            if threading.get_ident() != self._active_run_thread_id:
                notes.append(f"非主线程/非运行线程下 CONFIRM 默认拒绝 {tu.name}")
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "confirm_in_worker_denied", "tool": tu.name})
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True,
                    content=("[denied] 工具需要二次确认，但当前调度在非主线程，"
                             f"已自动拒绝（risk={verdict.risk.value}）"),
                )
            approved = False
            try:
                approved = self.confirm(
                    confirm_adapter.for_tool_call(verdict, tu.name, prep.argv)
                )
            except Exception:
                approved = False
            if not approved:
                notes.append(f"用户拒绝 {tu.name}")
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "user_denied_confirm", "tool": tu.name})
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True,
                    content=f"[denied] 用户拒绝执行（risk={verdict.risk.value}）",
                )
            self.audit.event(trace, EventKind.SAFETY_CHECK,
                             {"user_confirmed": True, "tool": tu.name})

        # 3. 落地执行 + 格式化（共享流水线，含 stderr 拼接 + 长度截断）
        _, formatted, content = execute_and_format(
            prep, trace=trace, audit=self.audit, executor=self.executor,
            parallel_read_only=parallel_read_only,
        )
        return ToolResultBlock(tool_use_id=tu.id, is_error=not formatted.ok, content=content)

    def _handle_user_choice(self, trace: Trace, tu: ToolUseBlock) -> ToolResultBlock:
        """处理 ask_user_choice：解析选项 → 审计 → 通过 progress 通知 UI →
        同步等 on_user_choice 回调 → 校验返回值合法 → 转 ToolResultBlock。

        没有 ExecutionProxy 介入：不构 argv、不过 Guardrail。审计照样记，
        以便事后能复盘"agent 问过用户什么 / 用户选了什么"。
        """
        args = tu.input or {}
        question = str(args.get("question", "")).strip()
        raw_options = args.get("options") or []
        options: list[UserChoiceOption] = [
            UserChoiceOption(
                label=str(o.get("label", o.get("value", ""))),
                value=str(o.get("value", "")),
                description=str(o.get("description", "")),
            )
            for o in raw_options
            if isinstance(o, dict) and o.get("value")
        ]

        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "ask_user_choice",
            "question": question,
            "options": [{"value": o.value, "label": o.label} for o in options],
        })

        # 推送给 UI；TUI 可据此渲染选项卡片，但用户作答仍走 on_user_choice 回调
        self._emit(ProgressEvent(
            kind="user_choice",
            text=question,
            meta={"options": [
                {"label": o.label, "value": o.value, "description": o.description}
                for o in options
            ]},
        ))

        # 同步阻塞拿用户结果；on_user_choice 可能由 UI 弹窗驱动
        try:
            chosen = self.on_user_choice(
                UserChoice(question=question, options=options)
            )
        except Exception:
            chosen = ""
        chosen = (chosen or "").strip()
        valid_values = {o.value for o in options}
        if chosen and chosen not in valid_values:
            # 拒绝非法值：UI 不应给 LLM 编造选项的机会
            chosen = ""

        self.audit.event(trace, EventKind.EXECUTION_RESULT, {
            "tool": "ask_user_choice",
            "chosen": chosen,
            "stdout": chosen,
        })

        if not chosen:
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content="[user_choice] 用户未做出选择或选项无效。",
            )
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=False,
            content=f"用户选择: {chosen}",
        )

    def _handle_rca_report(
        self, trace: Trace, tool_use_id: str, report_args: dict
    ) -> ToolResultBlock:
        """Validate and persist an evidence-backed RCA diagnosis."""
        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "submit_rca_report",
            "playbook": report_args.get("playbook"),
            "evidence_ids": report_args.get("evidence_ids", []),
        })
        available = {
            str(event.payload["evidence_id"])
            for event in trace.events
            if event.kind is EventKind.PERCEPTION and event.payload.get("evidence_id")
        }
        try:
            report = validate_report(
                report_args,
                playbooks=load_playbooks(self.cfg.resolve(self.cfg.rca.playbooks_file)),
                available_evidence=available,
            )
        except ValueError as exc:
            self.audit.event(trace, EventKind.ERROR, {
                "reason": "invalid_rca_report", "detail": str(exc),
            })
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content=f"RCA 报告非法：{exc}",
            )
        payload = asdict(report)
        payload["evidence_ids"] = list(report.evidence_ids)
        payload["recommendations"] = list(report.recommendations)
        self.audit.event(trace, EventKind.DIAGNOSIS, payload)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            is_error=False,
            content="RCA 报告已记录：" + json.dumps(payload, ensure_ascii=False),
        )

    @staticmethod
    def _blocks_to_dict(am: AssistantMessage) -> list[dict]:
        out: list[dict] = []
        for b in am.blocks:
            if isinstance(b, TextBlock):
                out.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return out


# ---- 便捷入口 -------------------------------------------------------------


def build_agent(config_path: str | None = None, confirm: ConfirmFn = _auto_deny,
                on_progress: ProgressCallback | None = None,
                on_user_choice: UserChoiceFn | None = None) -> Agent:
    cfg = load_config(config_path)
    return Agent.from_config(cfg, confirm=confirm, on_progress=on_progress,
                             on_user_choice=on_user_choice)
