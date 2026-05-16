"""ExecutionProxy 行为测试。"""
from __future__ import annotations

import sys

import pytest

from kyagent.executor.proxy import ExecutionProxy
from kyagent.executor.sandbox import SandboxConfig, build_clean_env


def _proxy() -> ExecutionProxy:
    return ExecutionProxy(SandboxConfig(
        account="kyagent",
        timeout=2.0,
        output_cap=2048,
    ))


def test_empty_argv_rejected():
    r = _proxy().run([])
    assert r.skipped_reason == "empty_argv"
    assert r.returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mock-only branch")
def test_windows_mock_mode():
    """Windows 上不真正执行 Linux 命令。"""
    r = _proxy().run(["ps", "aux"])
    assert r.skipped_reason == "windows_mock"
    assert "mock" in r.stdout.lower()
    assert r.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execution test")
def test_posix_echo():
    r = _proxy().run(["echo", "hello"])
    assert r.returncode == 0
    assert "hello" in r.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX timeout")
def test_posix_timeout():
    r = _proxy().run(["sleep", "10"])
    assert r.timed_out
    assert r.returncode != 0


def test_clean_env_blocks_ld_preload():
    cfg = SandboxConfig()
    env = build_clean_env(cfg, extra={"LD_PRELOAD": "/tmp/x.so", "FOO": "bar"})
    assert "LD_PRELOAD" not in env
    assert env["FOO"] == "bar"


def test_clean_env_restricts_path():
    import os as _os
    cfg = SandboxConfig()
    env = build_clean_env(cfg)
    parts = env["PATH"].split(_os.pathsep)
    # 只允许白名单里的 path 段
    for p in parts:
        assert p in cfg.path_whitelist


def test_sudo_wrap_for_root_when_allowed():
    cfg = SandboxConfig(account="kyagent", forbid_root=False)
    proxy = ExecutionProxy(cfg)
    final, sudo_used, run_as = proxy._wrap_privilege(["systemctl", "restart", "nginx"],
                                                     requires_root=True)
    assert sudo_used
    assert run_as == "root"
    assert final[:5] == ["sudo", "-n", "-u", "root", "--"]


def test_forbid_root_returns_false_command():
    cfg = SandboxConfig(account="kyagent", forbid_root=True)
    proxy = ExecutionProxy(cfg)
    final, _, _ = proxy._wrap_privilege(["systemctl", "restart", "nginx"],
                                        requires_root=True)
    # forbid_root=True 且目标账户非 root：用 /bin/false 占位
    assert final == ["/bin/false"]
