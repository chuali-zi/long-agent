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

import os
import time
from dataclasses import dataclass

from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.config import Config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.executor.sandbox import SandboxConfig
from kyagent.mcp.plugins import configured_registry
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


def build_audit_store(cfg: Config) -> AuditStore:
    """Build the configured audit store and apply the retention policy."""
    key: bytes | None = None
    key_id: str | None = None
    if getattr(cfg.audit, "integrity_enabled", False):
        key_env = getattr(cfg.audit, "hmac_key_env", "KYAGENT_AUDIT_HMAC_KEY")
        key_file = getattr(cfg.audit, "hmac_key_file", None)
        material = os.environ.get(key_env, "").strip()
        if not material and key_file:
            material = cfg.resolve(key_file).read_text(encoding="utf-8").strip()
        if not material:
            raise ValueError(
                "audit integrity is enabled but no HMAC key material is configured"
            )
        key = material.encode("utf-8")
        key_id = getattr(cfg.audit, "hmac_key_id", "local-v1")
    store = AuditStore(cfg.resolve(cfg.audit.database), hmac_key=key, key_id=key_id)
    retain_days = getattr(cfg.audit, "retain_days", 90)
    if retain_days > 0:
        store.purge_before(time.time() - retain_days * 86400)
    return store


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
        allow_parallel_read_only_tools=getattr(
            cfg.executor, "allow_parallel_read_only_tools", True
        ),
    )
    executor = ExecutionProxy(sandbox)
    guardrail = Guardrail.from_config(cfg)

    store = build_audit_store(cfg)
    jsonl = cfg.resolve(cfg.audit.jsonl_file) if cfg.audit.jsonl_file else None
    audit = AuditLogger(store, jsonl_file=jsonl)

    registry = configured_registry(cfg)

    return Runtime(
        sandbox=sandbox,
        executor=executor,
        guardrail=guardrail,
        audit=audit,
        registry=registry,
    )
