"""Agent 主循环：接收 → 感知 → 推理 → 校验 → 执行 → 审计 闭环。

把所有子系统组合起来：LLM 决策 → Guardrail 二次过滤 → ExecutionProxy 落地 → AuditLogger 全链路。
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
from kyagent.mcp.tools.pipeline import (
    PipelineError,
    check_safety,
    execute_and_format,
    prepare_call,
)
from kyagent.agent import confirm_adapter
from kyagent.confirm import ConfirmFn, auto_deny
from kyagent.runtime import build_runtime
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
    ):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.guardrail = guardrail
        self.intent_guard = intent_guard  # 赛题第 3 条：NL 意图层（None 则跳过）
        self.executor = executor
        self.audit = audit
        self.confirm = confirm
        self.messages: list[dict] = []
        self.system_prompt = SYSTEM_PROMPT
        # 持久线程池：避免每多工具回合都付一次 thread spawn 的固定开销，
        # 在 Windows mock 后端这一开销会盖过并行带来的收益。
        self._tool_pool: ThreadPoolExecutor | None = None

    def _ensure_pool(self) -> ThreadPoolExecutor:
        if self._tool_pool is None:
            self._tool_pool = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="ky-tool"
            )
        return self._tool_pool

    def shutdown(self) -> None:
        if self._tool_pool is not None:
            self._tool_pool.shutdown(wait=False)
            self._tool_pool = None

    @classmethod
    def from_config(cls, cfg: Config, confirm: ConfirmFn = _auto_deny) -> "Agent":
        # 通道无关基础设施统一从 composition root 装配
        rt = build_runtime(cfg)
        # 通道特定（LLM 后端、NL 意图层）在这里组合
        llm = build_backend(cfg)
        intent_guard = IntentGuard.from_config(cfg) if cfg.safety.intent_check else None
        return cls(cfg, llm, rt.registry, rt.guardrail, rt.executor, rt.audit, confirm,
                   intent_guard=intent_guard)

    # ---- 主入口 --------------------------------------------------------

    def ask(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        trace = Trace(user=user)
        self.audit.open(trace)
        trace.metadata.update({"backend": self.llm.name})

        self.audit.event(trace, EventKind.USER_INPUT, {"text": user_input})

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
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=reply,
                                      tool_iterations=0, denied=True, notes=notes)

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
                    self.audit.close(trace)
                    return AgentRunResult(trace=trace, final_text=reply,
                                          tool_iterations=0, denied=True, notes=notes)

            # 净化 stealth injection（零宽字符）：把净化后的文本送进 LLM，
            # 保留原文在 USER_INPUT 事件里以便审计追溯
            if intent_verdict.sanitized_text is not None:
                effective_input = intent_verdict.sanitized_text
                notes.append("已剥离零宽字符送入 LLM")

        self.messages.append({"role": "user", "content": effective_input})

        tools_for_llm = self.registry.to_anthropic_tools()

        while iterations < self.cfg.agent.max_iterations:
            iterations += 1

            try:
                assistant = self.llm.chat(self.system_prompt, self.messages, tools_for_llm)
            except Exception as e:  # noqa: BLE001
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "llm_error", "detail": str(e)})
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=f"LLM 调用失败：{e}",
                                      tool_iterations=iterations, notes=notes)

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
                self.audit.event(trace, EventKind.AGENT_REPLY, {"text": final})
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=final,
                                      tool_iterations=iterations, denied=denied, notes=notes)

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
                pool = self._ensure_pool()
                futures = [
                    pool.submit(self._handle_tool_use, trace, tu, notes)
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
                    result_block = self._handle_tool_use(trace, tu, notes)
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
        self.audit.close(trace)
        return AgentRunResult(trace=trace,
                              final_text="达到最大工具调用次数，已中止。",
                              tool_iterations=iterations, denied=denied, notes=notes)

    # ---- 工具调用处理 --------------------------------------------------

    def _executor_supports_parallel_tools(self) -> bool:
        """Whether this executor can safely run tool calls from worker threads."""
        return bool(getattr(self.executor, "supports_parallel_tool_execution", False))

    def _is_parallel_safe(self, tu: ToolUseBlock) -> bool:
        """Return True only for preflighted allow-only read-only tool calls.

        未知工具也按"不可并行"处理；后续 _handle_tool_use 内会以 ERROR 兜底，
        保留原有错误信息。
        """
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
                        notes: list[str]) -> ToolResultBlock:
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
            # 兜底：confirm() 必然在主线程执行。_is_parallel_safe 已经在
            # 主线程拦掉这条路径，这里防御非确定性 reviewer 让 worker 线程
            # 意外拿到 CONFIRM 的极端情况——直接按 deny 处理，绝不让 stdin
            # 被多个 worker 抢，确保审计链上"谁授权了什么"不会错位。
            if threading.current_thread() is not threading.main_thread():
                notes.append(f"非主线程下 CONFIRM 默认拒绝 {tu.name}")
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
        )
        return ToolResultBlock(tool_use_id=tu.id, is_error=not formatted.ok, content=content)

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


def build_agent(config_path: str | None = None, confirm: ConfirmFn = _auto_deny) -> Agent:
    cfg = load_config(config_path)
    return Agent.from_config(cfg, confirm=confirm)
