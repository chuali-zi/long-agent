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


def test_clean_env_blocks_identity_and_path_overrides():
    cfg = SandboxConfig()
    env = build_clean_env(cfg, extra={
        "PATH": "/tmp/evil",
        "HOME": "/tmp/evil-home",
        "SHELL": "/tmp/evil-shell",
        "USER": "attacker",
        "LOGNAME": "attacker",
        "FOO": "allowed",
    })
    assert env["PATH"] != "/tmp/evil"
    assert env["HOME"] != "/tmp/evil-home"
    assert env["SHELL"] == "/bin/sh"
    assert env["USER"] == cfg.account
    assert env["LOGNAME"] == cfg.account
    assert env["FOO"] == "allowed"


def test_absolute_command_outside_whitelist_is_rejected():
    proxy = _proxy()
    assert proxy._resolve_command("/tmp/evil") is None


def test_invalid_argv_is_rejected_before_platform_dispatch():
    result = _proxy().run(["echo", "bad\0arg"])
    assert result.skipped_reason == "invalid_argv"
    assert result.returncode != 0


def test_sudo_wrap_for_root_when_allowed():
    cfg = SandboxConfig(account="kyagent", forbid_root=False)
    proxy = ExecutionProxy(cfg)
    final, sudo_used, run_as = proxy._wrap_privilege(["systemctl", "restart", "nginx"],
                                                     requires_root=True)
    assert sudo_used
    assert run_as == "root"
    assert final[:5] == ["/usr/bin/sudo", "-n", "-u", "root", "--"]


def test_forbid_root_default_still_routes_through_sudoers():
    """赛题"非必要不用 root"修复（codex 指控 #1）：默认 forbid_root=True 不应
    把 requires_root 工具替换成 /bin/false；应通过 sudo 走 sudoers 白名单，由
    sudoers 决定是否真的能跑。这才让 configs/sudoers.kyagent 的 KY_SVC_MUTATE
    白名单不再是死代码。
    """
    cfg = SandboxConfig(account="kyagent", forbid_root=True)
    proxy = ExecutionProxy(cfg)
    final, sudo_used, run_as = proxy._wrap_privilege(
        ["systemctl", "restart", "nginx"], requires_root=True
    )
    assert sudo_used, "应通过 sudo 包裹"
    assert run_as == "root"
    assert final[:5] == ["/usr/bin/sudo", "-n", "-u", "root", "--"]
    assert final[5:] == ["systemctl", "restart", "nginx"]


def test_forbid_root_strict_rejects_with_false_command():
    """forbid_root_strict=True 是显式的"演示 / 合规"模式：彻底拒绝 root 提升，
    绕过 sudoers，requires_root 工具被替换成 /bin/false。"""
    cfg = SandboxConfig(account="kyagent", forbid_root=True, forbid_root_strict=True)
    proxy = ExecutionProxy(cfg)
    final, sudo_used, run_as = proxy._wrap_privilege(
        ["systemctl", "restart", "nginx"], requires_root=True
    )
    assert final == ["/bin/false"]
    assert sudo_used is False
    assert run_as == "kyagent"


def test_forbid_root_disabled_runs_root_directly_via_sudo():
    """显式 forbid_root=False：与未修复前完全一样的行为；用于环境必须直接 root 的极端场景。"""
    cfg = SandboxConfig(account="kyagent", forbid_root=False)
    proxy = ExecutionProxy(cfg)
    final, sudo_used, run_as = proxy._wrap_privilege(
        ["systemctl", "restart", "nginx"], requires_root=True
    )
    assert sudo_used
    assert run_as == "root"
    assert final[:5] == ["/usr/bin/sudo", "-n", "-u", "root", "--"]
