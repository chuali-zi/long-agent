"""通道无关的工具调用流水线。

Agent 主循环（kyagent.agent.core）与 MCP 服务器（kyagent.mcp.server）都需要执行
同一组步骤：validate → build_argv → audit(TOOL_REQUEST) → guardrail →
execute → audit(PERCEPTION?) → format → 输出包装。历史上两条路径各写了一份，结果出现了
行为漂移（PERCEPTION 只在 Agent 落，stderr 拼接和 6KB 截断只在 Agent 做）。

本模块抽出"真重复"的部分，差异点（CONFIRM 通道行为、trace 生命周期、返回类型
包装）仍由调用方各自决定，避免硬把 stdin/threading 这种 Agent-only 细节漏到 MCP。

划分原则：
  · prepare_call         参数校验 → TOOL_REQUEST
  · check_safety         guardrail → SAFETY_CHECK
  · execute_and_format   EXECUTION → format → 共享的 stderr 拼接 + 长度截断
  · 调用方负责：DENY/CONFIRM 处理、trace open/close、最终类型包装
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import uuid
from typing import Any

from kyagent.audit.logger import AuditLogger
from kyagent.audit.trace import EventKind, Trace
from kyagent.executor.proxy import ExecutionProxy, ExecutionResult
from kyagent.mcp.tools.base import Tool, ToolError, ToolResult
from kyagent.safety.guardrail import Guardrail, Verdict
from kyagent.safety.output import sanitize_tool_output_for_llm


OUTPUT_CAP = 6000  # tool 输出统一截断长度（两条通道共享）

_SNAPSHOT_PREFIXES = {
    "process_": "进程",
    "lsof_":    "进程/句柄",
    "net_":     "网络",
    "log_":     "日志",
    "svc_":     "服务",
    "fs_":      "文件系统",
    "pkg_":     "软件包",
}


def _snapshot_kind(tool_name: str) -> str:
    for prefix, kind in _SNAPSHOT_PREFIXES.items():
        if tool_name.startswith(prefix):
            return kind
    return "其它"


def _bench_execution_result(prepared: "PreparedCall") -> ExecutionResult | None:
    if os.environ.get("KYAGENT_BENCH") != "1" or not prepared.tool.read_only:
        return None
    tool_name = prepared.tool.name
    stdout_by_tool = {
        "process_list": "USER PID %CPU %MEM ELAPSED STAT COMMAND COMMAND\nroot 1 0.1 0.2 1-00:00:00 Ss systemd /usr/lib/systemd/systemd\nkyagent 4242 3.2 1.1 00:00:01 R python kyagent-bench\n",
        "lsof_port": "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nsshd 222 root 3u IPv4 12345 0t0 TCP *:22 (LISTEN)\n",
        "net_listen": "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=222,fd=3))\n",
        "fs_df": "Filesystem Size Used Avail Use% Mounted on\n/dev/root 50G 20G 30G 40% /\n/dev/data 100G 10G 90G 10% /data\n",
        "fs_du": "4.0K\t/var/log\n",
        "log_journal": "Jun 17 12:00:00 host sshd[222]: bench journal sample\n",
        "svc_status": "sshd.service - OpenSSH server daemon\n   Loaded: loaded (/usr/lib/systemd/system/sshd.service; enabled)\n   Active: active (running) since Wed 2026-06-17 12:00:00 CST\n",
        "pkg_info": "Name        : bash\nVersion     : 5.2\nSummary     : The GNU Bourne Again shell\n",
        "pkg_installed": "bash-5.2-1.x86_64\npython-3.11-1.x86_64\n",
    }
    stdout = stdout_by_tool.get(tool_name, f"{tool_name}: benchmark read-only output\n")
    return ExecutionResult(
        argv=prepared.argv,
        returncode=0,
        stdout=stdout,
        stderr="",
        truncated=False,
        duration=0.0,
    )


@dataclass
class PreparedCall:
    """validate + build_argv 之后、guardrail 之前的中间形态。"""
    tool: Tool
    cleaned: dict[str, Any]
    argv: list[str]


@dataclass
class PipelineError:
    """validate / build_argv 阶段早期失败的统一返回。

    调用方拿到它就知道：审计 ERROR 已落、可以直接把 detail 包成通道自己的错误返回。
    """
    reason: str   # "invalid_args" / "write_preflight_denied" / "build_argv"
    detail: str   # 给用户/客户端的文字


# ---- 各阶段 ----------------------------------------------------------------


def _write_preflight_denied(exc: ToolError) -> bool:
    return "preflight denied (" in str(exc)


def prepare_call(
    tool: Tool,
    args: dict[str, Any],
    *,
    trace: Trace,
    audit: AuditLogger,
) -> PreparedCall | PipelineError:
    """参数校验 + argv 构造 + 落 TOOL_REQUEST。

    返回 PreparedCall 表示可以进入 guardrail；
    返回 PipelineError 表示已经早停（审计已记 ERROR，调用方直接打包返回即可）。
    """
    try:
        cleaned = tool.validate(args)
    except ToolError as e:
        reason = "write_preflight_denied" if _write_preflight_denied(e) else "invalid_args"
        audit.event(trace, EventKind.ERROR,
                    {"reason": reason, "tool": tool.name, "detail": str(e)})
        if reason == "write_preflight_denied":
            return PipelineError(reason, f"[denied] 写入预检拒绝：{e}")
        return PipelineError(reason, f"工具参数非法：{e}")

    try:
        argv = tool.build_argv(cleaned)
    except ToolError as e:
        audit.event(trace, EventKind.ERROR,
                    {"reason": "build_argv", "tool": tool.name, "detail": str(e)})
        return PipelineError("build_argv", str(e))

    audit.event(trace, EventKind.TOOL_REQUEST, {
        "tool": tool.name,
        "argv": argv,
        "args": cleaned,
        "risk": tool.risk_level.value,
        "requires_root": tool.requires_root,
    })

    return PreparedCall(tool=tool, cleaned=cleaned, argv=argv)


def check_safety(
    prepared: PreparedCall,
    *,
    trace: Trace,
    audit: AuditLogger,
    guardrail: Guardrail,
) -> Verdict:
    """guardrail 检查 + 落 SAFETY_CHECK。

    本函数不处理 DENY/CONFIRM 决策——调用方根据通道特性自行处理：
      · Agent 通道：CONFIRM → 调 self.confirm（含 threading 兜底）
      · MCP 通道  ：CONFIRM → 直接 deny（无用户交互通道）
    """
    verdict = guardrail.check_argv(prepared.argv, declared_risk=prepared.tool.risk_level)
    audit.event(trace, EventKind.SAFETY_CHECK, verdict.to_dict())
    return verdict


def execute_and_format(
    prepared: PreparedCall,
    *,
    trace: Trace,
    audit: AuditLogger,
    executor: ExecutionProxy,
    parallel_read_only: bool = False,
) -> tuple[ExecutionResult, ToolResult, str]:
    """落地执行 + 工具格式化 + 共享 content 拼接（stderr + 长度截断）。

    Returns:
      (exec_result, formatted, ready_content)
        exec_result : 原始执行结果（要落 EXECUTION_RESULT 已在本函数内做完）
        formatted   : Tool.format_result 的产物（ok/content/error）
        ready_content : 已拼好 stderr 并截断到 OUTPUT_CAP 的字符串；
                        两条通道都用这个，避免再各自实现截断/拼接。
    """
    audit.event(trace, EventKind.EXECUTION, {
        "argv": prepared.argv,
        "requires_root": prepared.tool.requires_root,
    })
    bench_result = _bench_execution_result(prepared)
    if bench_result is not None:
        exec_result = bench_result
    elif parallel_read_only:
        exec_result = executor.run(
            prepared.argv,
            requires_root=prepared.tool.requires_root,
            parallel_read_only=True,
        )
    else:
        exec_result = executor.run(
            prepared.argv,
            requires_root=prepared.tool.requires_root,
        )
    exec_result.extra["tool_args"] = prepared.cleaned
    execution_result = audit.event(trace, EventKind.EXECUTION_RESULT, exec_result.to_dict())

    formatted = prepared.tool.format_result(exec_result)
    if prepared.tool.read_only:
        audit.event(trace, EventKind.PERCEPTION, {
            "evidence_id": f"evidence-{uuid.uuid4().hex}",
            "tool": prepared.tool.name,
            "snapshot_kind": _snapshot_kind(prepared.tool.name),
            "execution_result_seq": getattr(execution_result, "seq", None),
            "ok": formatted.ok,
            "truncated": exec_result.truncated,
            "stdout_sha256": hashlib.sha256(exec_result.stdout.encode("utf-8")).hexdigest(),
        })
    if formatted.ok:
        content = formatted.content
    else:
        content = f"{formatted.content}\n---\n[stderr]\n{formatted.error or ''}"
    sanitized = sanitize_tool_output_for_llm(content[:OUTPUT_CAP], tool_name=prepared.tool.name)
    if sanitized.hits:
        audit.event(trace, EventKind.ERROR, {
            "reason": "tool_output_prompt_injection",
            "tool": prepared.tool.name,
            "redacted_lines": sanitized.hits,
        })
    content = sanitized.text[:OUTPUT_CAP]
    return exec_result, formatted, content
