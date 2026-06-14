from __future__ import annotations

from kyagent.agent.core import _FileRemediationChecklist
from kyagent.agent.completion import assess_file_cleanup_completion
from kyagent.agent.scope import RemediationScope


def test_unknown_candidate_blocks_delete() -> None:
    checklist = _FileRemediationChecklist(
        scope=RemediationScope.from_user_text("cleanup auth-api01"),
        required_roots=("/var/log/auth-api01",),
    )
    checklist.scanned_roots.add("/var/log/auth-api01")
    target = "/var/log/auth-api01/app/current.log"
    checklist.candidate_paths.add(target)
    checklist.candidate_labels[target] = "unknown"

    err = checklist.pre_write_error(target, "cleanup auth-api01")

    assert "unknown" in err


def test_protect_candidate_blocks_delete() -> None:
    checklist = _FileRemediationChecklist(
        scope=RemediationScope.from_user_text("cleanup auth-api01"),
        required_roots=("/var/log/auth-api01",),
    )
    checklist.scanned_roots.add("/var/log/auth-api01")
    target = "/var/log/auth-api01/audit/incident-review.log.1"
    checklist._label_candidate(target, "protect")

    err = checklist.pre_write_error(target, "cleanup auth-api01")

    assert "protect" in err


def test_out_of_scope_candidate_still_blocks_delete() -> None:
    checklist = _FileRemediationChecklist(
        scope=RemediationScope.from_user_text("cleanup old files for web-app01"),
        required_roots=("/var/log/web-app01", "/var/cache/web-app01"),
    )
    checklist.scanned_roots.update({"/var/log/web-app01", "/var/cache/web-app01"})
    target = "/var/cache/dnf/packages/pkg.rpm"
    checklist.candidate_paths.add(target)
    checklist.candidate_labels[target] = "delete"

    err = checklist.pre_write_error(target, "cleanup old files for web-app01")

    assert "not in current scope" in err
    assert target in err


def test_explicit_path_can_override_scope_candidate_block() -> None:
    checklist = _FileRemediationChecklist(
        scope=RemediationScope.from_user_text("delete /var/cache/dnf/packages/pkg.rpm"),
        required_roots=(),
    )
    target = "/var/cache/dnf/packages/pkg.rpm"
    checklist.candidate_paths.add(target)
    checklist.candidate_labels[target] = "delete"

    err = checklist.pre_write_error(target, f"delete {target}")

    assert err == ""


def test_completion_report_marks_unchecked_roots() -> None:
    scope = RemediationScope.from_user_text("cleanup auth-api01 logs and cache")
    report = assess_file_cleanup_completion(
        scope,
        required_roots=("/var/log/auth-api01", "/var/cache/auth-api01"),
        scanned_roots={"/var/log/auth-api01"},
        candidate_paths=set(),
        executed_paths=[],
        verified_paths=set(),
    )

    assert not report.done
    assert any("/var/cache/auth-api01" in item for item in report.unchecked)
