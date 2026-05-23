"""Composition root：装配通道无关的共享基础设施。

历史上 Agent.from_config 与 McpServer main() 各自手动构造 SandboxConfig /
ExecutionProxy / Guardrail / AuditLogger / ToolRegistry，字段一字不差地复制了
两份。每次新增执行器/审计/规则配置都要记得改两处，否则两条通道行为漂移。

本模块把"通道无关"的基础设施装配收敛到一处。
通道特定的对象（LLM 后端、IntentGuard、confirm 回调）不在 Runtime 里 —
它们属于"通道用户层"，混进来会让 Runtime 语义变模糊（MCP 不需要 LLM，
HTTP/WebSocket 通道可能不需要 intent 层）。
"""
from __future__ import annotations

from dataclasses import dataclass

from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.config import Config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.executor.sandbox import SandboxConfig
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.safety.guardrail import Guardrail


@dataclass
class Runtime:
    """通道无关的共享基础设施。"""
    sandbox: SandboxConfig
    executor: ExecutionProxy
    guardrail: Guardrail
    audit: AuditLogger
    registry: ToolRegistry


def build_runtime(cfg: Config) -> Runtime:
    """根据 Config 装配出一组共享基础设施。

    通道差异（LLM / intent_guard / confirm）由调用方自行处理。
    """
    sandbox = SandboxConfig(
        account=cfg.executor.account,
        timeout=cfg.executor.timeout,
        output_cap=cfg.executor.output_cap,
        path_whitelist=tuple(cfg.executor.path) if cfg.executor.path else (
            "/usr/local/bin", "/usr/bin", "/bin",
        ),
        forbid_root=cfg.executor.forbid_root,
        forbid_root_strict=cfg.executor.forbid_root_strict,
    )
    executor = ExecutionProxy(sandbox)
    guardrail = Guardrail.from_config(cfg)

    store = AuditStore(cfg.resolve(cfg.audit.database))
    jsonl = cfg.resolve(cfg.audit.jsonl_file) if cfg.audit.jsonl_file else None
    audit = AuditLogger(store, jsonl_file=jsonl)

    registry = default_registry()
    if cfg.mcp.enable_tools:
        keep = set(cfg.mcp.enable_tools)
        # NOTE: 此处直接动私有 _tools。后续可引入 ToolRegistry.with_whitelist(keep)
        # 公共方法把这层 hack 收掉，但本次重构范围只统一装配位置，不动签名。
        registry._tools = {n: t for n, t in registry._tools.items() if n in keep}

    return Runtime(
        sandbox=sandbox,
        executor=executor,
        guardrail=guardrail,
        audit=audit,
        registry=registry,
    )
