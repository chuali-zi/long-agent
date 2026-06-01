"""kyagent MCP stdio 服务器。

实现要点：
  - JSON-RPC 2.0 over stdio，newline-delimited（与 MCP 规范一致）
  - 暴露的方法（client → server 请求）：initialize / ping / tools/list / tools/call
  - 暴露的通知（client → server，无 id 无回复）：
      notifications/initialized   ← MCP 2024-11-05 lifecycle 必需
      notifications/cancelled     ← 可选，目前 no-op
  - 每次 tools/call 内部都走 Guardrail + ExecutionProxy + AuditLogger
  - 即便上游 LLM 跳过自家的安全层，工具调用仍受保护

协议合规性（MCP 2024-11-05 + JSON-RPC 2.0）：
  - 通知 = 无 id 的请求。服务器对通知**不返回任何 response**（即使错误也不返回）
  - 工具执行错误用 `result.isError=true` 返回，不是 JSON-RPC error
  - 协议错误（未知工具 / 参数非法）用 JSON-RPC error 返回，错误内容不泄漏内部 traceback
"""
from __future__ import annotations

import json
import sys
from typing import Any

from kyagent.audit.logger import AuditLogger
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config, load_config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.mcp.protocol import (
    ProtocolError,
    validate_initialize,
    validate_request,
    validate_tool_call,
)
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.mcp.tools.pipeline import (
    PipelineError,
    check_safety,
    execute_and_format,
    prepare_call,
)
from kyagent.runtime import build_runtime
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
        self._state = "new"

    # ---- 入口 ----------------------------------------------------------

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # JSON-RPC 2.0：parse error 时 id 用 null
                self._write(_err(None, -32700, "parse error"))
                continue

            is_notification = isinstance(msg, dict) and "id" not in msg
            try:
                response = self._dispatch(msg)
            except Exception:  # noqa: BLE001
                # 通知出错绝不回复（JSON-RPC 2.0 强制）。
                # 请求出错只回简短摘要，不暴露 traceback / 内部路径。
                if is_notification:
                    continue
                req_id = msg.get("id") if isinstance(msg, dict) else None
                response = _err(req_id, -32603, "internal error")
            if response is not None:
                self._write(response)

    def _write(self, msg: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    # ---- 路由 ----------------------------------------------------------

    def _dispatch(self, msg: Any) -> dict[str, Any] | None:
        try:
            request = validate_request(msg)
        except ProtocolError as exc:
            if (
                isinstance(msg, dict)
                and "id" not in msg
                and msg.get("jsonrpc") == "2.0"
                and isinstance(msg.get("method"), str)
            ):
                return None
            return _err(exc.req_id, exc.code, exc.message)
        method = request.method
        req_id = request.req_id
        params = request.params

        # ---- 通知 ----（必须返回 None，禁止有任何输出，错的方法名也不报错）
        if request.is_notification:
            if method == "notifications/initialized" and self._state == "initializing":
                # MCP 2024-11-05 lifecycle：客户端就绪信号
                self._state = "initialized"
            elif method == "notifications/cancelled":
                # 可选：客户端取消请求；我们目前同步处理，no-op
                pass
            # 任何其它通知：静默忽略，绝不返回 error response
            return None

        # ---- 请求 ----（必有 id，必须返回 response）
        if method == "initialize":
            try:
                validate_initialize(params, self.PROTOCOL_VERSION)
            except ProtocolError as exc:
                return _err(req_id, exc.code, exc.message)
            self._state = "initializing"
            return _resp(req_id, self._initialize())
        if method == "ping":
            # MCP utilities/ping：用于探活，固定返回空对象
            return _resp(req_id, {})
        if method in {"tools/list", "tools/call"} and self._state != "initialized":
            return _err(req_id, -32600, "server is not initialized")
        if method == "tools/list":
            return _resp(req_id, {"tools": self.registry.to_mcp_list()})
        if method == "tools/call":
            try:
                name, args = validate_tool_call(params)
                tool = self.registry.get(name)
                if tool is None:
                    raise ProtocolError(-32602, f"unknown tool: {name}")
                if name in {"ask_user_choice", "submit_rca_report"}:
                    return _resp(req_id, {
                        "content": [{"type": "text", "text": "该逻辑工具仅供 Agent 交互闭环使用"}],
                        "isError": True,
                    })
                return _resp(req_id, self._call_tool(tool, name, args))
            except ProtocolError as exc:
                return _err(req_id, exc.code, exc.message)

        return _err(req_id, -32601, f"method not found: {method}")

    # ---- 处理器 --------------------------------------------------------

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self.cfg.mcp.server_name,
                "version": self.cfg.mcp.server_version,
            },
        }

    def _call_tool(self, tool: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        # 为每次 tool 调用建一条 trace（外部 LLM 调进来时这是最完整的审计单元）
        trace = Trace(user="mcp-client")
        self.audit.open(trace)
        trace.metadata.update({"channel": "mcp", "tool": name})

        try:
            # validate + build_argv + TOOL_REQUEST + PERCEPTION（共享流水线）
            prep = prepare_call(tool, args, trace=trace, audit=self.audit)
            if isinstance(prep, PipelineError):
                return {"content": [{"type": "text", "text": prep.detail}], "isError": True}

            verdict = check_safety(prep, trace=trace, audit=self.audit, guardrail=self.guardrail)

            if verdict.decision is Decision.DENY:
                return {
                    "content": [{
                        "type": "text",
                        "text": f"已拦截：{verdict.risk.value}\n" + "\n".join(verdict.rationale),
                    }],
                    "isError": True,
                }
            if verdict.decision is Decision.CONFIRM:
                # 通过 MCP 协议无法发起用户确认，按"拒绝"返回，并提示需走交互模式
                self.audit.event(trace, EventKind.ERROR, {"reason": "needs_confirm_via_mcp"})
                return {
                    "content": [{
                        "type": "text",
                        "text": (f"需用户确认才能执行（risk={verdict.risk.value}）；"
                                 f"通过 MCP 通道默认不发起确认。请改走 kyagent chat。"),
                    }],
                    "isError": True,
                }

            # 落地执行 + 格式化（共享流水线，含 stderr 拼接 + 长度截断）
            _, formatted, content = execute_and_format(
                prep, trace=trace, audit=self.audit, executor=self.executor,
            )
            self.audit.event(trace, EventKind.AGENT_REPLY,
                             {"ok": formatted.ok, "len": len(formatted.content),
                              "error": formatted.error})
            return {
                "content": [{"type": "text", "text": content or (formatted.error or "")}],
                "isError": not formatted.ok,
            }
        finally:
            self.audit.close(trace)


# ---- 入口函数（被 pyproject.toml 的 script 引用） --------------------------


def main() -> None:
    cfg = load_config()
    rt = build_runtime(cfg)
    server = McpServer(cfg, rt.registry, rt.guardrail, rt.executor, rt.audit)
    try:
        server.serve()
    finally:
        rt.audit.close_file()
        rt.audit.store.close()


if __name__ == "__main__":
    main()
