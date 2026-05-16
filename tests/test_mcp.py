"""MCP 工具注册与协议层测试。"""
from __future__ import annotations

import json
from io import StringIO

from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import Tool, ToolRegistry


def test_default_registry_has_core_tools():
    reg = default_registry()
    names = set(reg.names())
    must_have = {
        "process_list", "lsof_port", "net_listen", "log_journal",
        "svc_status", "svc_restart", "fs_df", "pkg_info",
    }
    assert must_have.issubset(names), f"缺工具: {must_have - names}"


def test_to_mcp_list_shape():
    reg = default_registry()
    items = reg.to_mcp_list()
    assert isinstance(items, list)
    for it in items:
        assert "name" in it
        assert "description" in it
        assert "inputSchema" in it
        assert it["inputSchema"]["type"] == "object"


def test_anthropic_tools_shape():
    reg = default_registry()
    items = reg.to_anthropic_tools()
    for it in items:
        assert set(it.keys()) == {"name", "description", "input_schema"}


def test_validate_required_args_missing():
    reg = default_registry()
    tool = reg.get("lsof_port")
    from kyagent.mcp.tools.base import ToolError
    import pytest
    with pytest.raises(ToolError):
        tool.validate({})


def test_validate_coerces_string_to_int():
    reg = default_registry()
    tool = reg.get("lsof_port")
    cleaned = tool.validate({"port": "80"})
    assert cleaned["port"] == 80
    argv = tool.build_argv(cleaned)
    assert argv == ["lsof", "-nP", "-i", "TCP:80"]


def test_svc_restart_rejects_shell_metacharacters():
    reg = default_registry()
    tool = reg.get("svc_restart")
    from kyagent.mcp.tools.base import ToolError
    import pytest
    with pytest.raises(ToolError):
        tool.build_argv({"unit": "sshd; rm -rf /"})


def test_svc_restart_rejects_forbidden_unit():
    reg = default_registry()
    tool = reg.get("svc_restart")
    from kyagent.mcp.tools.base import ToolError
    import pytest
    with pytest.raises(ToolError):
        tool.build_argv({"unit": "systemd-logind"})


def test_find_rejects_shell_meta_in_name():
    reg = default_registry()
    tool = reg.get("fs_find")
    from kyagent.mcp.tools.base import ToolError
    import pytest
    with pytest.raises(ToolError):
        tool.build_argv({"path": "/var/log", "name": "*.log; rm -rf /"})


def test_filesystem_blocks_shadow_read():
    reg = default_registry()
    tool = reg.get("fs_ls")
    from kyagent.mcp.tools.base import ToolError
    import pytest
    with pytest.raises(ToolError):
        tool.build_argv({"path": "/etc/shadow"})
