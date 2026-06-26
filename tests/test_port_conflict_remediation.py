"""Regression tests for the port-conflict-v1 fix.

Covers two defects that made the bench INCONCLUSIVE:
  1. Perception blindness — lsof_port/net_listen run unprivileged are blind to
     root-owned listeners, and lsof_port used to assert "port free" on the
     ambiguous empty result.  Now they request a one-off privileged retry.
  2. No closed-loop verification — terminate/port remediation now must confirm
     the target port is actually released before the final answer.
"""
from __future__ import annotations

from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools import network as network_mod
from kyagent.mcp.tools import process as process_mod
from kyagent.mcp.tools.pipeline import PreparedCall, execute_and_format
from kyagent.agent.core import _ProcessRemediationChecklist
from kyagent.agent.scope import (
    RemediationScope,
    parse_remediation_ports,
    process_remediation_checklist_applies,
)


def _exec(stdout="", stderr="", returncode=0, **kw):
    return ExecutionResult(
        argv=kw.pop("argv", []),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=False,
        duration=0.0,
        **kw,
    )


# ---- 1a. lsof_port perception ------------------------------------------------


class TestLsofPortPerception:
    def test_wants_privileged_retry_on_blinded_empty(self):
        t = process_mod.LsofPortTool()
        # exit 1 + empty + empty stderr == ambiguous (free OR blinded by root)
        assert t.wants_privileged_retry(_exec(returncode=1)) is True

    def test_no_retry_when_rows_present(self):
        t = process_mod.LsofPortTool()
        res = _exec(stdout="COMMAND PID USER ...\npython3 30728 root ... (LISTEN)\n")
        assert t.wants_privileged_retry(res) is False

    def test_no_retry_when_permission_error(self):
        t = process_mod.LsofPortTool()
        # non-empty stderr is a real error, not the ambiguous empty case
        assert t.wants_privileged_retry(_exec(returncode=1, stderr="permission denied")) is False

    def test_format_result_reports_free_only_when_truly_empty(self):
        t = process_mod.LsofPortTool()
        out = t.format_result(_exec(returncode=1))
        assert out.ok and out.data.get("no_match") is True
        assert "No process is using" in out.content

    def test_format_result_passes_through_rows(self):
        t = process_mod.LsofPortTool()
        out = t.format_result(_exec(stdout="python3 30728 root TCP 127.0.0.1:18080 (LISTEN)\n"))
        assert out.ok and not out.data.get("no_match")
        assert "18080" in out.content


# ---- 1c. net_listen perception ----------------------------------------------


class TestNetListenPerception:
    _BLIND = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        "LISTEN 0      5      127.0.0.1:18080    0.0.0.0:*\n"
    )
    _OWNED = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      128    0.0.0.0:22         0.0.0.0:*    users:(("sshd",pid=222,fd=3))\n'
    )

    def test_wants_privileged_retry_when_owner_hidden(self):
        t = network_mod.SsListenTool()
        assert t.wants_privileged_retry(_exec(stdout=self._BLIND)) is True

    def test_no_retry_when_all_owners_visible(self):
        t = network_mod.SsListenTool()
        assert t.wants_privileged_retry(_exec(stdout=self._OWNED)) is False

    def test_format_result_flags_hidden_owner(self):
        t = network_mod.SsListenTool()
        out = t.format_result(_exec(stdout=self._BLIND))
        assert out.data.get("owner_hidden") is True
        assert "无法解析其属主" in out.content


# ---- 1b. pipeline privileged retry ------------------------------------------


class _FakeAuditEvent:
    seq = 0


class _FakeAudit:
    def __init__(self):
        self.events = []

    def event(self, trace, kind, payload):
        self.events.append((kind, payload))
        return _FakeAuditEvent()


class _ScriptedExecutor:
    """Returns scripted results keyed by requires_root; records call privileges."""

    def __init__(self, unpriv, priv, current_user="kyagent"):
        self._unpriv = unpriv
        self._priv = priv
        self._user = current_user
        self.calls = []

    @property
    def current_user(self):
        return self._user

    def run(self, argv, *, requires_root=False, **kw):
        self.calls.append(requires_root)
        return self._priv if requires_root else self._unpriv


def _prepared(tool, argv):
    return PreparedCall(tool=tool, cleaned={}, argv=argv)


def test_pipeline_privileged_retry_adopts_root_visibility():
    tool = process_mod.LsofPortTool()
    unpriv = _exec(returncode=1)  # blinded empty
    priv = _exec(stdout="python3 30728 root TCP 127.0.0.1:18080 (LISTEN)\n")
    ex = _ScriptedExecutor(unpriv, priv)
    audit = _FakeAudit()
    _, formatted, content = execute_and_format(
        _prepared(tool, ["lsof", "-nP", "-i", "TCP:18080"]),
        trace=object(), audit=audit, executor=ex,
    )
    assert ex.calls == [False, True], "should retry once with root"
    assert "18080" in content and not formatted.data.get("no_match")


def test_pipeline_no_retry_when_already_root():
    tool = process_mod.LsofPortTool()
    unpriv = _exec(returncode=1)
    ex = _ScriptedExecutor(unpriv, unpriv, current_user="root")
    _, formatted, content = execute_and_format(
        _prepared(tool, ["lsof", "-nP", "-i", "TCP:18080"]),
        trace=object(), audit=_FakeAudit(), executor=ex,
    )
    assert ex.calls == [False], "root already sees everything; no retry"
    assert formatted.data.get("no_match") is True  # trustworthy free


def test_pipeline_retry_empty_keeps_free_verdict():
    tool = process_mod.LsofPortTool()
    unpriv = _exec(returncode=1)
    priv = _exec(returncode=1)  # root also empty => genuinely free
    ex = _ScriptedExecutor(unpriv, priv)
    _, formatted, _ = execute_and_format(
        _prepared(tool, ["lsof", "-nP", "-i", "TCP:18080"]),
        trace=object(), audit=_FakeAudit(), executor=ex,
    )
    assert ex.calls == [False, True]
    assert formatted.data.get("no_match") is True


def test_pipeline_retry_sudo_denied_does_not_fake_free():
    # sudoers not installed: privileged retry fails (rc!=0 + stderr). We keep the
    # unprivileged empty result and DON'T regress to a spurious error on dev boxes.
    tool = process_mod.LsofPortTool()
    unpriv = _exec(returncode=1)
    priv = _exec(returncode=1, stderr="sudo: a password is required")
    ex = _ScriptedExecutor(unpriv, priv)
    _, formatted, _ = execute_and_format(
        _prepared(tool, ["lsof", "-nP", "-i", "TCP:18080"]),
        trace=object(), audit=_FakeAudit(), executor=ex,
    )
    assert ex.calls == [False, True]
    # original unprivileged empty stands -> reported as free (best effort fallback)
    assert formatted.data.get("no_match") is True


# ---- 2a. scope predicates / port parsing ------------------------------------

_PORT_CONFLICT_PROMPT = (
    "checkout 预发说 18080 被占，新版本起不来。先查是谁占的；"
    "若确认是昨晚留下的旧 HTTP 预发实例，可以结束它。"
    "18081 上 orders-api 是对照环境，不要动，也别动系统服务。"
)


class TestScopeParsing:
    def test_parse_targets_and_protected(self):
        targets, protected = parse_remediation_ports(_PORT_CONFLICT_PROMPT)
        assert targets == {18080}
        assert 18081 in protected
        assert 18081 not in targets

    def test_no_target_ports_for_handle_release(self):
        targets, _ = parse_remediation_ports(
            "mysqld 删了旧日志但句柄没释放，请释放被删除文件占用的句柄。"
        )
        assert targets == set()

    def test_no_target_ports_for_stress_kill(self):
        targets, _ = parse_remediation_ports("把压测进程 stress-ng 杀掉，释放 CPU。")
        assert targets == set()

    def test_process_remediation_predicate(self):
        scope = RemediationScope.from_user_text("终止占用 18080 端口的进程")
        assert process_remediation_checklist_applies(scope) is True
        scope2 = RemediationScope.from_user_text("查看 18080 谁在监听")
        assert process_remediation_checklist_applies(scope2) is False


# ---- 2b/2c. process remediation checklist gate -------------------------------


class TestProcessRemediationChecklist:
    def test_blocks_until_port_released(self):
        cl = _ProcessRemediationChecklist.from_user_text(_PORT_CONFLICT_PROMPT)
        assert cl.target_ports == {18080}
        # before any verification: blocked
        assert "18080" in cl.final_error()

        # observe occupied (bound) -> still blocked
        cl.record_read_result("lsof_port", {"port": 18080},
                              "python3 30728 root ... (LISTEN)", {})
        assert cl.final_error() != ""

        # kill, then must re-verify
        cl.record_kill()
        assert cl.port_state[18080] == "needs_reverify"
        assert cl.final_error() != ""

        # post-kill lsof shows free -> released -> gate clears
        cl.record_read_result("lsof_port", {"port": 18080},
                              "No process is using the requested port.",
                              {"no_match": True})
        assert cl.final_error() == ""

    def test_net_listen_presence_marks_bound_not_released(self):
        cl = _ProcessRemediationChecklist.from_user_text(_PORT_CONFLICT_PROMPT)
        cl.record_read_result(
            "net_listen", {},
            "LISTEN 0 5 127.0.0.1:18080 0.0.0.0:*\n", {},
        )
        assert cl.port_state[18080] == "bound"
        assert cl.final_error() != ""

    def test_no_targets_never_blocks(self):
        cl = _ProcessRemediationChecklist.from_user_text(
            "把压测进程 stress-ng 杀掉，释放 CPU。"
        )
        cl.record_kill()
        assert cl.final_error() == ""

    def test_progress_advances_with_observations(self):
        cl = _ProcessRemediationChecklist.from_user_text(_PORT_CONFLICT_PROMPT)
        before = cl.progress()
        cl.record_read_result("lsof_port", {"port": 18080},
                              "No process is using the requested port.",
                              {"no_match": True})
        assert cl.progress() > before
