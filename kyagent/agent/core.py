"""Agent 主循环：接收 → 感知 → 推理 → 校验 → 执行 → 审计 闭环。

把所有子系统组合起来：LLM 决策 → Guardrail 二次过滤 → ExecutionProxy 落地 → AuditLogger 全链路。
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

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
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config, load_config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.executor.sandbox import SandboxConfig
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolError, ToolRegistry
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.patterns import RiskLevel
from kyagent.safety.policy import Decision


# Confirm 回调：tool name + argv + verdict → True 表示用户同意继续
ConfirmFn = Callable[[str, list[str], dict], bool]


def _auto_deny(name: str, argv: list[str], verdict: dict) -> bool:
    """默认 confirm 回调：直接拒绝。CLI 会注入交互式回调覆盖它。"""
    return False


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
    ):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.guardrail = guardrail
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
        sandbox = SandboxConfig(
            account=cfg.executor.account,
            timeout=cfg.executor.timeout,
            output_cap=cfg.executor.output_cap,
            path_whitelist=tuple(cfg.executor.path) if cfg.executor.path else (
                "/usr/local/bin", "/usr/bin", "/bin",
            ),
            forbid_root=cfg.executor.forbid_root,
        )
        executor = ExecutionProxy(sandbox)
        guardrail = Guardrail.from_config(cfg)
        store = AuditStore(cfg.resolve(cfg.audit.database))
        jsonl = cfg.resolve(cfg.audit.jsonl_file) if cfg.audit.jsonl_file else None
        audit = AuditLogger(store, jsonl_file=jsonl)
        registry = default_registry()
        if cfg.mcp.enable_tools:
            keep = set(cfg.mcp.enable_tools)
            registry._tools = {n: t for n, t in registry._tools.items() if n in keep}
        llm = build_backend(cfg)
        return cls(cfg, llm, registry, guardrail, executor, audit, confirm)

    # ---- 主入口 --------------------------------------------------------

    def ask(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        trace = Trace(user=user)
        self.audit.open(trace)
        trace.metadata.update({"backend": self.llm.name})

        self.audit.event(trace, EventKind.USER_INPUT, {"text": user_input})
        self.messages.append({"role": "user", "content": user_input})

        notes: list[str] = []
        iterations = 0
        denied = False

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

            # 选择串行 / 并行：本轮内任一工具被声明为 HIGH/CRITICAL 时
            # 全部回退到串行，便于审计顺序与 confirm 交互；否则并行执行。
            # 注意：guardrail 校验仍在 _handle_tool_use 内部完成，并行只影响执行
            # 阶段——任何 deny/confirm 决策不依赖兄弟工具的执行顺序。
            #
            # Windows mock 执行器无真实 I/O，线程化只会增加锁竞争，按平台旁路。
            tool_results: list[dict | None] = [None] * len(tool_uses)
            run_parallel = (
                sys.platform != "win32"
                and len(tool_uses) >= 2
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

    def _is_parallel_safe(self, tu: ToolUseBlock) -> bool:
        """声明 HIGH/CRITICAL 的工具走串行，便于审计 + confirm 交互。

        未知工具也按"不可并行"处理；后续 _handle_tool_use 内会以 ERROR 兜底，
        保留原有错误信息。
        """
        tool = self.registry.get(tu.name)
        if tool is None:
            return False
        return tool.risk_level.order < RiskLevel.HIGH.order

    def _handle_tool_use(self, trace: Trace, tu: ToolUseBlock,
                        notes: list[str]) -> ToolResultBlock:
        tool = self.registry.get(tu.name)
        if tool is None:
            self.audit.event(trace, EventKind.ERROR,
                             {"reason": "unknown_tool", "tool": tu.name})
            return ToolResultBlock(tool_use_id=tu.id, is_error=True,
                                   content=f"未知工具：{tu.name}")

        # 1. 参数校验 + argv 构造
        try:
            cleaned = tool.validate(tu.input or {})
            argv = tool.build_argv(cleaned)
        except ToolError as e:
            self.audit.event(trace, EventKind.ERROR,
                             {"reason": "tool_arg_error", "tool": tu.name, "detail": str(e)})
            return ToolResultBlock(tool_use_id=tu.id, is_error=True,
                                   content=f"工具参数非法：{e}")

        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": tu.name, "argv": argv, "args": cleaned,
            "risk": tool.risk_level.value, "requires_root": tool.requires_root,
        })

        # 2. 安全护栏（即便是 read_only 工具也过一遍，防止参数注入）
        verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
        self.audit.event(trace, EventKind.SAFETY_CHECK, verdict.to_dict())

        if verdict.decision is Decision.DENY:
            notes.append(f"已拦截 {tu.name}: {verdict.risk.value}")
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content=("[denied] 工具调用被安全护栏拒绝。\n"
                         f"风险等级: {verdict.risk.value}\n"
                         + "\n".join(verdict.rationale)),
            )

        if verdict.decision is Decision.CONFIRM:
            approved = False
            try:
                approved = self.confirm(tu.name, argv, verdict.to_dict())
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

        # 3. 落地执行
        self.audit.event(trace, EventKind.EXECUTION,
                         {"argv": argv, "requires_root": tool.requires_root})
        exec_result = self.executor.run(argv, requires_root=tool.requires_root)
        self.audit.event(trace, EventKind.EXECUTION_RESULT, exec_result.to_dict())

        # 4. 格式化
        out = tool.format_result(exec_result)
        content = out.content if out.ok else f"{out.content}\n---\n[stderr]\n{out.error or ''}"
        return ToolResultBlock(tool_use_id=tu.id, is_error=not out.ok, content=content[:6000])

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
