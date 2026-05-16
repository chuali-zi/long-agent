"""kyagent MCP stdio 服务器。

实现要点：
  - JSON-RPC 2.0 over stdio，newline-delimited（与 MCP 规范一致）
  - 暴露的方法：initialize / initialized / ping / tools/list / tools/call
  - 每次 tools/call 内部都走 Guardrail + ExecutionProxy + AuditLogger
  - 即便上游 LLM 跳过自家的安全层，工具调用仍受保护
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from kyagent.audit.logger import AuditLogger
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config, load_config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.executor.sandbox import SandboxConfig
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolError, ToolRegistry
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.policy import Decision


# ---- JSON-RPC 帮助 ---------------------------------------------------------


def _resp(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


# ---- 服务器 ----------------------------------------------------------------


class McpServer:
    """所有方法都同步执行，单线程；stdio 已天然串行。"""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        cfg: Config,
        registry: ToolRegistry,
        guardrail: Guardrail,
        executor: ExecutionProxy,
        audit: AuditLogger,
    ):
        self.cfg = cfg
        self.registry = registry
        self.guardrail = guardrail
        self.executor = executor
        self.audit = audit
        self._initialized = False

    # ---- 入口 ----------------------------------------------------------

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write(_err(None, -32700, "parse error"))
                continue

            try:
                response = self._dispatch(msg)
            except Exception as e:  # noqa: BLE001
                response = _err(msg.get("id"), -32603, "internal error",
                                {"exc": str(e), "traceback": traceback.format_exc()[-2000:]})
            if response is not None:
                self._write(response)

    def _write(self, msg: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    # ---- 路由 ----------------------------------------------------------

    def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # 通知（无 id）：不需要回复
        is_notification = "id" not in msg

        if method == "initialize":
            return _resp(req_id, self._initialize(params))
        if method == "initialized":
            self._initialized = True
            return None if is_notification else _resp(req_id, {})
        if method == "ping":
            return _resp(req_id, {})
        if method == "tools/list":
            return _resp(req_id, {"tools": self.registry.to_mcp_list()})
        if method == "tools/call":
            return _resp(req_id, self._call_tool(params))

        return _err(req_id, -32601, f"method not found: {method}")

    # ---- 处理器 --------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self.cfg.mcp.server_name,
                "version": self.cfg.mcp.server_version,
            },
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self.registry.get(name) if isinstance(name, str) else None

        # 为每次 tool 调用建一条 trace（外部 LLM 调进来时这是最完整的审计单元）
        trace = Trace(user="mcp-client")
        self.audit.open(trace)
        trace.metadata.update({"channel": "mcp", "tool": name})

        if tool is None:
            self.audit.event(trace, EventKind.ERROR, {"reason": "unknown_tool", "name": name})
            self.audit.close(trace)
            return {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            }

        try:
            cleaned = tool.validate(args)
        except ToolError as e:
            self.audit.event(trace, EventKind.ERROR, {"reason": "invalid_args", "detail": str(e)})
            self.audit.close(trace)
            return {"content": [{"type": "text", "text": f"参数错误: {e}"}], "isError": True}

        try:
            argv = tool.build_argv(cleaned)
        except ToolError as e:
            self.audit.event(trace, EventKind.ERROR, {"reason": "build_argv", "detail": str(e)})
            self.audit.close(trace)
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}

        self.audit.event(trace, EventKind.TOOL_REQUEST,
                         {"tool": tool.name, "argv": argv, "args": cleaned,
                          "risk": tool.risk_level.value, "requires_root": tool.requires_root})

        verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
        self.audit.event(trace, EventKind.SAFETY_CHECK, verdict.to_dict())

        if verdict.decision is Decision.DENY:
            self.audit.close(trace)
            return {
                "content": [{
                    "type": "text",
                    "text": f"已拦截：{verdict.risk.value}\n" + "\n".join(verdict.rationale),
                }],
                "isError": True,
            }
        if verdict.decision is Decision.CONFIRM:
            # 通过 MCP 协议无法发起用户确认，按"拒绝"返回，并提示需走交互模式
            self.audit.event(trace, EventKind.ERROR,
                             {"reason": "needs_confirm_via_mcp"})
            self.audit.close(trace)
            return {
                "content": [{
                    "type": "text",
                    "text": (f"需用户确认才能执行（risk={verdict.risk.value}）；"
                             f"通过 MCP 通道默认不发起确认。请改走 kyagent chat。"),
                }],
                "isError": True,
            }

        self.audit.event(trace, EventKind.EXECUTION,
                         {"argv": argv, "requires_root": tool.requires_root})
        result = self.executor.run(argv, requires_root=tool.requires_root)
        self.audit.event(trace, EventKind.EXECUTION_RESULT, result.to_dict())

        out = tool.format_result(result)
        self.audit.event(trace, EventKind.AGENT_REPLY,
                         {"ok": out.ok, "len": len(out.content), "error": out.error})
        self.audit.close(trace)

        return {
            "content": [{"type": "text", "text": out.content or (out.error or "")}],
            "isError": not out.ok,
        }


# ---- 入口函数（被 pyproject.toml 的 script 引用） --------------------------


def main() -> None:
    cfg = load_config()
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
    audit = AuditLogger(store, jsonl_file=cfg.resolve(cfg.audit.jsonl_file) if cfg.audit.jsonl_file else None)

    registry = default_registry()
    # 白名单过滤
    if cfg.mcp.enable_tools:
        keep = set(cfg.mcp.enable_tools)
        registry._tools = {n: t for n, t in registry._tools.items() if n in keep}

    server = McpServer(cfg, registry, guardrail, executor, audit)
    server.serve()


if __name__ == "__main__":
    main()
