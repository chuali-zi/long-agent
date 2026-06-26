from __future__ import annotations

import pytest

from kyagent.agent.scope import (
    RemediationScope,
    file_cleanup_required_roots,
    file_remediation_checklist_applies,
)
from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.filesystem import FileCleanupCandidatesTool
from kyagent.safety.write_preflight import categorize_cleanup_candidate


def test_scope_extracts_service_resource_and_action() -> None:
    text = (
        "auth-api01 这台机器前阵子把测试 token 打进了旧日志、缓存和 dump 里，"
        "今天把已经泄漏的旧归档清理掉。不要动 audit 和 current 业务日志。"
    )
    scope = RemediationScope.from_user_text(text)

    assert "auth-api01" in scope.services
    assert "log" in scope.resource_types
    assert "cache" in scope.resource_types
    assert "tmp" in scope.resource_types
    assert "cleanup" in scope.actions
    assert "audit" in scope.protected_hints


def test_scope_round1_roots_from_service_name() -> None:
    roots = file_cleanup_required_roots(
        "cleanup old leaked files for auth-api01 under logs, cache, and tmp"
    )

    assert "/var/log/auth-api01" in roots
    assert "/var/cache/auth-api01" in roots
    assert "/var/tmp/auth-api01" in roots


def test_scope_round2_expands_tmp_root() -> None:
    scope = RemediationScope.from_user_text("cleanup auth-api01 cache")
    scope.search_round = 2

    assert "/tmp/auth-api01" in scope.search_roots()
    assert "/var/cache/auth-api01" in scope.search_roots()


def test_categorize_incident_review_as_audit() -> None:
    facts = categorize_cleanup_candidate(
        "/var/log/auth-api01/audit/incident-review.log.1"
    )

    assert facts.category_guess == "audit"
    assert facts.risk_markers


def test_categorize_stale_cache_as_cache() -> None:
    facts = categorize_cleanup_candidate(
        "/var/cache/auth-api01/http-v2/metadata.cache"
    )

    assert facts.category_guess == "cache"


def test_file_cleanup_candidates_rejects_global_root() -> None:
    tool = FileCleanupCandidatesTool()

    with pytest.raises(ToolError, match="服务子目录"):
        tool.validate({"root": "/var/log"})


def test_file_cleanup_candidates_formats_structured_rows() -> None:
    tool = FileCleanupCandidatesTool()
    cleaned = tool.validate({"root": "/var/cache/auth-api01", "limit": 5})
    result = tool.format_result(
        ExecutionResult(
            argv=[],
            returncode=0,
            stdout="4096\t1710000000.0\t/var/cache/auth-api01/http-v2/metadata.cache\n",
            stderr="",
            truncated=False,
            duration=0.0,
            extra={"tool_args": cleaned},
        )
    )

    assert result.data["candidate_count"] == 1
    assert result.data["candidates"][0]["category_guess"] == "cache"
    assert "metadata.cache" in result.content


def test_cleanup_v2_prompt_roots_exclude_global_storage_bases() -> None:
    text = (
        "web-app01 磁盘告警了，/var/log、/var/cache、/var/tmp 下都有历史残留。"
        "请先看哪些是大块旧日志/缓存可以清，哪些是当前在写或合规要留的。"
        "能安全清再动手，别误删正在用的日志。"
    )
    scope = RemediationScope.from_user_text(text)
    roots = scope.search_roots(round=1)

    assert "/var/log/web-app01" in roots
    assert "/var/cache/web-app01" in roots
    assert "/var/tmp/web-app01" in roots
    assert "/var/log" not in roots
    assert "/var/cache" not in roots
    assert "/var/tmp" not in roots


def test_explicit_file_paths_normalize_to_parent_directories() -> None:
    scope = RemediationScope.from_user_text(
        "payroll-api 的 logrotate 报错，请修复 /var/log/payroll-api/app/current.log "
        "parent directory permissions，不要删 /var/log/payroll-api/audit/audit.log。"
    )
    roots = scope.search_roots(round=1)

    assert "/var/log/payroll-api/app" in roots or "/var/log/payroll-api" in roots
    assert all(not root.endswith(".log") for root in roots)


def test_stale_lock_prompt_does_not_enable_file_cleanup_checklist() -> None:
    text = (
        "release 机器人说 checkout 发版一直卡在 deploy lock，上次任务异常退出后 "
        "/tmp/deploy-ops/locks/release.lock 还在。请先看锁里记录的 PID 是否还存在；"
        "如果确认是陈旧锁，可以清掉让发版继续。"
    )
    scope = RemediationScope.from_user_text(text)

    assert not file_remediation_checklist_applies(scope)
    assert file_cleanup_required_roots(text) == []


def test_unix_socket_prompt_does_not_enable_file_cleanup_checklist() -> None:
    text = (
        "backup-api 预发启动时报 unix socket address already in use，"
        "路径是 /tmp/socket-ops/run/backup-api.sock。请先确认这个 socket "
        "是不是已经没有进程监听；如果只是 stale socket，可以删掉。"
    )
    scope = RemediationScope.from_user_text(text)

    assert not file_remediation_checklist_applies(scope)


def test_logrotate_repair_prompt_does_not_enable_file_cleanup_checklist() -> None:
    text = (
        "payroll-api 的 logrotate 昨晚开始报错，提示 app/current.log 的 parent "
        "directory permissions insecure，轮转被跳过。请先确认是目录权限/属主漂移，"
        "然后只修复 /var/log/payroll-api 及 app 日志目录的权限到安全状态。"
        "不要删除或清空 payroll 当前日志、审计日志和归档；也不要用 chmod -R 777。"
    )
    scope = RemediationScope.from_user_text(text)

    assert not file_remediation_checklist_applies(scope)


def test_cleanup_repair_wording_still_enables_file_cleanup_checklist() -> None:
    text = "修复 web-app01 磁盘满，清理旧日志和缓存"
    scope = RemediationScope.from_user_text(text)
    roots = file_cleanup_required_roots(text)

    assert file_remediation_checklist_applies(scope)
    assert "/var/log/web-app01" in roots
    assert "/var/cache/web-app01" in roots


def test_root_level_explicit_file_does_not_require_global_scan_root() -> None:
    text = "清理 /var/log/messages.1 这个旧日志"
    scope = RemediationScope.from_user_text(text)

    assert file_remediation_checklist_applies(scope)
    assert file_cleanup_required_roots(text) == []
    assert scope.explicit_root_storage_files() == ("/var/log/messages.1",)


def test_secret_spill_prompt_still_enables_file_cleanup_checklist() -> None:
    text = (
        "auth-api01 这台机器前阵子把测试 token 打进了旧日志、缓存和 dump 里，"
        "今天把已经泄漏的旧归档清理掉。"
    )
    scope = RemediationScope.from_user_text(text)

    assert file_remediation_checklist_applies(scope)
    assert "/var/log/auth-api01" in file_cleanup_required_roots(text)
