from __future__ import annotations

import pytest

from kyagent.agent.scope import RemediationScope, file_cleanup_required_roots
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
