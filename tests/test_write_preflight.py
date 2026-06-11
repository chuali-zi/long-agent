from __future__ import annotations

import pytest

from kyagent.safety.write_preflight import (
    PathMetadata,
    WritePreflightDecision,
    classify_write_preflight,
)


OLD = PathMetadata(exists=True, is_regular_file=True, mtime=0.0)
RECENT = PathMetadata(exists=True, is_regular_file=True, mtime=3_500.0)


@pytest.mark.parametrize(
    ("path", "rule_id"),
    [
        ("/var/log/audit/audit.log", "audit-log"),
        ("/var/log/audit/audit.log.1.gz", "audit-log"),
        ("/var/log/wtmp", "login-record-log"),
        ("/var/log/btmp.1", "login-record-log"),
        ("/var/log/secure.1", "auth-log"),
        ("/var/log/mysql-bin.000001", "database-log"),
        ("/var/log/mysql/error.log.2", "database-log"),
        ("/var/log/auth.log.1", "auth-log"),
        ("/var/log/sudo.log.2", "auth-log"),
        ("/var/log/sshd.log.gz", "auth-log"),
        ("/var/log/postgresql/server.log.3", "database-log"),
        ("/var/log/redis/redis.log.4", "database-log"),
    ],
)
def test_write_preflight_denies_audit_auth_and_database_logs(path, rule_id):
    result = classify_write_preflight(
        path,
        operation="delete",
        metadata=OLD,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == rule_id
    assert result.reason


def test_write_preflight_denies_active_log_files():
    result = classify_write_preflight(
        "/var/log/myapp.log",
        operation="truncate",
        metadata=OLD,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "active-log"


def test_write_preflight_denies_recently_modified_files_before_allow_rules():
    result = classify_write_preflight(
        "/var/log/myapp.log.1",
        operation="delete",
        metadata=RECENT,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "recently-modified"
    assert "minimum age" in result.reason


def test_write_preflight_allows_recent_disposable_cache_and_temp_spool():
    for path, rule_id in [
        ("/var/cache/web-app01/dnf/metadata.solv", "cache-target"),
        ("/var/tmp/web-app01/pip-build-3f9a/wheel.log", "temp-build-residual"),
    ]:
        result = classify_write_preflight(
            path,
            operation="truncate",
            metadata=RECENT,
            now=4_000.0,
        )
        assert result.decision is WritePreflightDecision.ALLOW_CONFIRM
        assert result.rule_id == rule_id


@pytest.mark.parametrize(
    ("path", "rule_id"),
    [
        ("/var/log/myapp.log.1", "old-rotated-log"),
        ("/var/log/myapp.log.gz", "old-rotated-log"),
        ("/var/log/auth-api01/app/debug-20260412.log", "dated-log"),
        ("/var/cache/dnf/pkg.tmp", "cache-target"),
        ("/tmp/build-wheel/output.tmp", "temp-build-residual"),
        ("/var/tmp/.pytest_cache/node", "temp-build-residual"),
        ("/var/tmp/web-app01/pip-build-3f9a/wheel.log", "temp-build-residual"),
        ("/var/tmp/auth-api01/core/auth-api.24891.core.txt", "temp-core-dump"),
        ("/var/log/web-app01/app/portal.log.6", "old-rotated-log"),
    ],
)
def test_write_preflight_allows_confirm_for_cleanup_targets(path, rule_id):
    result = classify_write_preflight(
        path,
        operation="delete",
        metadata=OLD,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.ALLOW_CONFIRM
    assert result.rule_id == rule_id
    assert result.allowed is True


def test_write_preflight_denies_unclassified_targets():
    result = classify_write_preflight(
        "/var/log/messages",
        operation="delete",
        metadata=OLD,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "unclassified-cleanup-target"


def test_write_preflight_denies_missing_rotated_targets():
    result = classify_write_preflight(
        "/var/log/myapp.log.1",
        operation="delete",
        metadata=PathMetadata(exists=False),
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "target-not-found"


def test_write_preflight_denies_recent_dated_log_before_allow():
    result = classify_write_preflight(
        "/var/log/auth-api01/app/debug-20260412.log",
        operation="delete",
        metadata=RECENT,
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "recently-modified"


def test_write_preflight_denies_symlink_targets():
    result = classify_write_preflight(
        "/var/log/myapp.log.1",
        operation="delete",
        metadata=PathMetadata(exists=True, is_regular_file=False, is_symlink=True),
        now=4_000.0,
    )
    assert result.decision is WritePreflightDecision.DENY
    assert result.rule_id == "symlink-target"
