from __future__ import annotations

from kyagent.agent.core import _FileRemediationChecklist
from kyagent.agent.scope import RemediationScope, file_cleanup_required_roots


def _checklist(**kwargs) -> _FileRemediationChecklist:
    scope = kwargs.pop("scope", RemediationScope.from_user_text("cleanup auth-api01"))
    return _FileRemediationChecklist(scope=scope, **kwargs)


def test_required_roots_from_service_name_cover_log_cache_tmp() -> None:
    text = (
        "auth-api01 这台机器前阵子把测试 token 打进了旧日志、缓存和 dump 里，"
        "今天把已经泄漏的旧归档清理掉。"
    )

    roots = file_cleanup_required_roots(text)

    assert "/var/log/auth-api01" in roots
    assert "/var/cache/auth-api01" in roots
    assert "/var/tmp/auth-api01" in roots
    assert "/tmp/auth-api01" not in roots


def test_pre_scan_error_lists_missing_roots() -> None:
    checklist = _checklist(
        required_roots=("/var/log/auth-api01", "/var/cache/auth-api01"),
    )

    err = checklist.pre_scan_error()

    assert "not yet enumerated" in err
    assert "/var/log/auth-api01" in err
    assert "/var/cache/auth-api01" in err


def test_read_result_marks_scanned_root_from_path_arg() -> None:
    checklist = _checklist(required_roots=("/var/log/auth-api01",))

    checklist.record_read_result(
        "fs_ls",
        {"path": "/var/log/auth-api01"},
        "total 0\n-rw-r--r-- 1 root root 0 app/debug-20260412.log\n",
    )

    assert checklist.pre_scan_error() == ""


def test_write_blocked_until_root_scanned() -> None:
    checklist = _checklist(required_roots=("/var/log/auth-api01",))
    target = "/var/log/auth-api01/app/debug-20260412.log"

    err = checklist.pre_write_error(target, "清理 auth-api01 泄漏文件")

    assert "candidate roots are incomplete" in err
    assert "/var/log/auth-api01" in err


def test_final_error_requires_post_verify_after_delete() -> None:
    checklist = _checklist(required_roots=("/var/log/auth-api01",))
    target = "/var/log/auth-api01/app/debug-20260412.log"
    checklist.scanned_roots.add("/var/log/auth-api01")
    checklist.candidate_paths.add(target)
    checklist.candidate_labels[target] = "delete"
    checklist.record_write_result(target, ok=True)

    err = checklist.final_error()

    assert "unverified changes" in err
    assert "/var/log/auth-api01/app" in err


def test_rescan_ancestor_root_verifies_deleted_target() -> None:
    # 回归：删除成功后文件已不在结果里，agent 用 dir_largest_files 重扫祖先根目录
    # （而非精确父目录）应能复核 target，否则 final_error 永远拦截 → 死循环。
    checklist = _checklist(required_roots=("/var/cache/auth-api01",))
    target = "/var/cache/auth-api01/app/old.bin"
    checklist.scanned_roots.add("/var/cache/auth-api01")
    checklist.candidate_paths.add(target)
    checklist.candidate_labels[target] = "delete"
    checklist.record_write_result(target, ok=True)
    assert "unverified changes" in checklist.final_error()

    # 重扫根目录，结果里已不含被删文件（只剩其他保留文件）
    checklist.record_read_result(
        "dir_largest_files",
        {"path": "/var/cache/auth-api01"},
        "12M /var/cache/auth-api01/app/keep.bin\n",
    )

    assert checklist.final_error() == ""


def test_followup_cleanup_inherits_previous_discovery_candidates() -> None:
    previous = _checklist(required_roots=("/var/log/web-app01",))
    previous.scanned_roots.add("/var/log/web-app01")
    target = "/var/log/web-app01/app/portal.log.6"
    previous._label_candidate(target, "delete")

    followup = _checklist(
        scope=RemediationScope.from_user_text("帮我清理系统垃圾"),
        required_roots=(),
    )
    followup.inherit_discovery_from(previous)

    assert followup.pre_write_error(target, "帮我清理系统垃圾") == ""


def test_inherit_discovery_preserves_current_required_roots() -> None:
    previous = _checklist(required_roots=("/var/log/web-app01",))
    target = "/var/log/web-app01/app/portal.log.6"
    previous._label_candidate(target, "delete")

    followup = _checklist(required_roots=("/var/cache/web-app01",))
    followup.inherit_discovery_from(previous)

    assert followup.required_roots == ("/var/cache/web-app01",)
    assert followup.candidate_labels[target] == "delete"


def test_generic_cleanup_without_discovery_still_blocks_out_of_scope_write() -> None:
    checklist = _checklist(
        scope=RemediationScope.from_user_text("帮我清理系统垃圾"),
        required_roots=(),
    )

    err = checklist.pre_write_error(
        "/var/log/web-app01/app/portal.log.6",
        "帮我清理系统垃圾",
    )

    assert "not in current scope" in err


def test_root_level_explicit_file_blocks_until_read_only_discovery() -> None:
    user_text = "清理 /var/log/messages.1 这个旧日志"
    target = "/var/log/messages.1"
    checklist = _FileRemediationChecklist.from_scope(
        RemediationScope.from_user_text(user_text)
    )

    assert checklist.required_roots == ()
    assert checklist.explicit_file_targets == {target}
    err = checklist.pre_write_error(target, user_text)

    assert "explicit root-level target" in err
    assert "fs_ls" in err


def test_fs_ls_exact_root_level_file_allows_safe_delete_candidate() -> None:
    user_text = "清理 /var/log/messages.1 这个旧日志"
    target = "/var/log/messages.1"
    checklist = _FileRemediationChecklist.from_scope(
        RemediationScope.from_user_text(user_text)
    )

    checklist.record_read_result(
        "fs_ls",
        {"path": target},
        "-rw-r--r-- 1 root root 12 Jan 1 00:00 /var/log/messages.1\n",
    )

    assert checklist.candidate_labels[target] == "delete"
    assert checklist.pre_write_error(target, user_text) == ""
