"""MCP 协议级测试：lifecycle 握手 + JSON Schema 严格校验 + 错误处理。

固化 codex 报告的两个真问题：
  1. notifications/initialized 必须被识别且不返回 response
  2. JSON Schema 的 enum / minimum / maximum / pattern 必须真正生效
  3. 非法输入不得泄漏 Python traceback / 绝对路径
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolError
from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.logs import LogSizeSampleTool


# ---- JSON Schema 严格校验 -------------------------------------------------


def test_enum_rejected():
    """proto.enum=[tcp,udp,all] — 'bad' 必须被 ToolError 拦下，绝不能进 build_argv。"""
    tool = default_registry().get("net_listen")
    with pytest.raises(ToolError) as ei:
        tool.validate({"proto": "bad"})
    assert "enum" in str(ei.value).lower() or "允许集合" in str(ei.value)


def test_enum_accepted():
    tool = default_registry().get("net_listen")
    cleaned = tool.validate({"proto": "udp"})
    assert cleaned["proto"] == "udp"


def test_integer_minimum_enforced():
    """ping count minimum=1 — count=0 必须被拦。"""
    tool = default_registry().get("net_ping")
    with pytest.raises(ToolError) as ei:
        tool.validate({"host": "127.0.0.1", "count": 0})
    assert "minimum" in str(ei.value).lower() or "小于" in str(ei.value)


def test_integer_maximum_enforced():
    """ping count maximum=10 — count=999 必须被拦。"""
    tool = default_registry().get("net_ping")
    with pytest.raises(ToolError) as ei:
        tool.validate({"host": "127.0.0.1", "count": 999})
    assert "maximum" in str(ei.value).lower() or "大于" in str(ei.value)


def test_lsof_port_range():
    """lsof_port port minimum=1, maximum=65535。"""
    tool = default_registry().get("lsof_port")
    with pytest.raises(ToolError):
        tool.validate({"port": 0})
    with pytest.raises(ToolError):
        tool.validate({"port": 70000})
    cleaned = tool.validate({"port": 80})
    assert cleaned["port"] == 80


def test_lsof_port_no_match_is_successful_absence_evidence():
    tool = default_registry().get("lsof_port")
    result = tool.format_result(ExecutionResult(
        argv=["lsof", "-nP", "-i", "TCP:18080"],
        returncode=1,
        stdout="",
        stderr="",
        truncated=False,
        duration=0.01,
    ))
    assert result.ok is True
    assert result.data["no_match"] is True


def test_lsof_port_real_exit_one_error_stays_failure():
    tool = default_registry().get("lsof_port")
    result = tool.format_result(ExecutionResult(
        argv=["lsof", "-nP", "-i", "TCP:18080"],
        returncode=1,
        stdout="",
        stderr="permission denied",
        truncated=False,
        duration=0.01,
    ))
    assert result.ok is False
    assert result.error == "permission denied"


def test_process_list_sort_enum():
    tool = default_registry().get("process_list")
    with pytest.raises(ToolError):
        tool.validate({"sort_by": "unknown"})
    cleaned = tool.validate({"sort_by": "cpu"})
    assert cleaned["sort_by"] == "cpu"


def test_svc_list_state_enum():
    tool = default_registry().get("svc_list")
    with pytest.raises(ToolError):
        tool.validate({"state": "garbage"})
    tool.validate({"state": "running"})


def test_string_coerce_still_works():
    """对回归保险：LLM 偶尔以字符串回传数字，仍应转换成功。"""
    tool = default_registry().get("lsof_port")
    cleaned = tool.validate({"port": "80"})
    assert cleaned["port"] == 80


@pytest.mark.parametrize("paths", [["relative"], ["--help"], [123]])
def test_array_items_are_validated_recursively(paths):
    with pytest.raises(ToolError):
        LogSizeSampleTool().validate({"paths": paths})


# ---- MCP stdio 协议层 ----------------------------------------------------


def _run_mcp_session(messages: list[str]) -> tuple[list[dict], str]:
    """启动 stdio MCP server 跑一次会话，返回 (按行解析的 response, stderr)。"""
    env = dict(os.environ)
    env["KYAGENT_CONFIG"] = str(Path(__file__).parent.parent / "configs" / "default.yaml")
    p = subprocess.Popen(
        [sys.executable, "-m", "kyagent.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    inp = ("\n".join(messages) + "\n").encode("utf-8")
    out, err = p.communicate(input=inp, timeout=10)
    resps: list[dict] = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            resps.append(json.loads(line))
    return resps, err.decode("utf-8", errors="replace")


def test_initialize_returns_protocol_version():
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}}})
    resps, _ = _run_mcp_session([init])
    assert len(resps) == 1
    assert resps[0]["id"] == 1
    assert resps[0]["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resps[0]["result"]["capabilities"]


def test_notifications_initialized_no_response():
    """MCP 2024-11-05 lifecycle：notifications/initialized 是通知，服务端 MUST NOT 回复。

    这是 codex 指控 #4 的修复点：原代码会回 method-not-found error，破坏协议。
    """
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}}})
    notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    resps, _ = _run_mcp_session([init, notif, ping])
    # 应得 2 条响应：initialize + ping。notifications/initialized 必须无响应
    assert len(resps) == 2, f"应得 2 条响应（init + ping），实得 {len(resps)}: {resps}"
    ids = [r.get("id") for r in resps]
    assert ids == [1, 2]
    # 任何响应都不应是 method-not-found（-32601）
    for r in resps:
        assert "error" not in r or r["error"]["code"] != -32601, f"不应有 method-not-found: {r}"


def test_unknown_notification_silently_ignored():
    """JSON-RPC 2.0：任何通知（无 id），无论方法名是否认识，服务端 MUST NOT 回复。"""
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}}})
    fake = json.dumps({"jsonrpc": "2.0", "method": "notifications/totally-made-up"})
    resps, _ = _run_mcp_session([init, fake])
    assert len(resps) == 1
    assert resps[0]["id"] == 1


def test_tools_call_invalid_enum_returns_clean_error_not_traceback():
    """codex 指控 #5：发非法 enum 会泄漏 Python traceback / 绝对路径。修复后应得干净的参数错误。"""
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}}})
    notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "net_listen", "arguments": {"proto": "bad"}}})
    resps, _ = _run_mcp_session([init, notif, call])
    assert len(resps) == 2
    # 工具执行错误用 result.isError=true 返回（MCP 标准），不应进 jsonrpc error 兜底
    call_resp = resps[1]
    assert call_resp.get("id") == 2
    # 关键断言：不能泄漏 traceback / Windows 绝对路径
    raw = json.dumps(call_resp, ensure_ascii=False)
    assert "Traceback" not in raw
    assert "D:\\race" not in raw and "/D/race" not in raw
    assert "kyagent\\mcp" not in raw and "kyagent/mcp/tools/network" not in raw
    # 应该有人类可读的错误（"参数错误" 或 "enum"）
    body = ""
    if "result" in call_resp:
        body = json.dumps(call_resp["result"], ensure_ascii=False)
    elif "error" in call_resp:
        body = json.dumps(call_resp["error"], ensure_ascii=False)
    assert "proto" in body.lower() or "enum" in body.lower() or "参数" in body or "允许" in body
