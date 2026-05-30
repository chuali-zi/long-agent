"""Regression tests for external strings passed to command-line tools."""
from __future__ import annotations

import pytest

from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.logs import DmesgTool, JournalctlTool
from kyagent.mcp.tools.network import NetLinkStatsTool, PingTool
from kyagent.mcp.tools.service import SvcReloadTool, SvcRestartTool, SvcStatusTool


@pytest.mark.parametrize("tool_cls", [SvcStatusTool, SvcRestartTool, SvcReloadTool])
def test_service_tools_reject_option_like_unit(tool_cls):
    with pytest.raises(ToolError):
        tool_cls().validate({"unit": "--help"})


def test_service_tools_accept_normal_unit():
    cleaned = SvcStatusTool().validate({"unit": "nginx.service"})
    assert cleaned["unit"] == "nginx.service"


def test_net_ping_rejects_option_like_host():
    with pytest.raises(ToolError):
        PingTool().validate({"host": "-f"})


@pytest.mark.parametrize("host", ["127.0.0.1", "kylinos.cn", "2001:db8::1"])
def test_net_ping_accepts_normal_host(host):
    cleaned = PingTool().validate({"host": host})
    assert cleaned["host"] == host


def test_net_link_stats_rejects_option_like_iface():
    with pytest.raises(ToolError):
        NetLinkStatsTool().validate({"iface": "--help"})


def test_dmesg_rejects_option_like_level():
    with pytest.raises(ToolError):
        DmesgTool().validate({"level": "--help"})


def test_dmesg_accepts_comma_separated_levels():
    cleaned = DmesgTool().validate({"level": "err,warn"})
    assert cleaned["level"] == "err,warn"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit", "--all"),
        ("since", "--system"),
        ("grep", "--help"),
        ("unit", "sshd.service;id"),
        ("since", "1 hour ago;id"),
        ("grep", "error;id"),
    ],
)
def test_log_journal_rejects_unsafe_external_strings(field, value):
    with pytest.raises(ToolError):
        JournalctlTool().validate({field: value})


def test_log_journal_accepts_normal_external_strings():
    cleaned = JournalctlTool().validate(
        {
            "unit": "sshd.service",
            "since": "2026-05-30 10:00:00",
            "grep": r"Failed password|invalid user",
        }
    )
    assert cleaned == {
        "unit": "sshd.service",
        "since": "2026-05-30 10:00:00",
        "grep": r"Failed password|invalid user",
    }
