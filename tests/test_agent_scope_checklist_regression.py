"""Regression tests for RemediationScope + file-remediation checklist gating (P0).

Guards against:
  - global /var/log|cache|tmp roots in required_roots (tools reject them)
  - file/socket paths mistaken for directory scan roots
  - non-file-cleanup benches inheriting the delete checklist
  - Web multi-turn scope bleed from prior cleanup tickets
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kyagent.agent.core import Agent, _FileRemediationChecklist
from kyagent.agent.llm import AssistantMessage, LlmBackend, TextBlock
from kyagent.agent.scope import (
    RemediationScope,
    file_cleanup_required_roots,
    file_remediation_checklist_applies,
)
from kyagent.audit.trace import EventKind
from kyagent.config import Config

_REPO = Path(__file__).resolve().parent.parent
_BENCHES = _REPO / "benchmarks"
_GLOBAL_ROOTS = frozenset({"/var/log", "/var/cache", "/var/tmp", "/tmp"})


def _load_manifest_prompt(manifest: Path) -> str:
    lines = manifest.read_text(encoding="utf-8").splitlines()
    parts: list[str] = []
    collect = False
    for line in lines:
        if line.startswith("prompt:"):
            rest = line.split(":", 1)[1].strip()
            if rest == ">":
                collect = True
                continue
            if rest:
                return rest.strip().strip('"')
        if collect:
            if line and not line.startswith(" ") and not line.startswith("\t"):
                break
            parts.append(line.strip())
    return " ".join(parts)


def _bench_prompts() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for manifest in sorted(_BENCHES.glob("*/manifest.yaml")):
        prompt = _load_manifest_prompt(manifest)
        assert prompt, f"missing prompt in {manifest}"
        out.append((manifest.parent.name, prompt))
    return out


class _ImmediateFinalBackend(LlmBackend):
    name = "immediate-final"

    def chat(self, system, messages, tools):  # noqa: ANN001, ARG002
        return AssistantMessage(
            blocks=[TextBlock(text="done without tools")],
            stop_reason="end_turn",
        )


@pytest.fixture
def agent(tmp_path):
    cfg = Config(base_dir=_REPO)
    cfg.audit.database = str(tmp_path / "audit.db")
    cfg.audit.jsonl_file = str(tmp_path / "audit.jsonl")
    cfg.safety.rules_file = "configs/safety-rules.yaml"
    cfg.agent.llm_backend = "mock"
    cfg.planning.enabled = False
    return Agent.from_config(cfg, confirm=lambda *a, **k: False)


@pytest.mark.parametrize("bench_id,prompt", _bench_prompts())
def test_bench_prompt_checklist_expectations(bench_id: str, prompt: str) -> None:
  scope = RemediationScope.from_user_text(prompt)
  applies = file_remediation_checklist_applies(scope)
  roots = scope.search_roots(round=1)

  assert all(root not in _GLOBAL_ROOTS for root in roots)
  assert all(not root.endswith((".sock", ".lock")) for root in roots)

  file_cleanup_benches = {"cleanup-v2", "secret-spill-v1"}
  if bench_id in file_cleanup_benches:
      assert applies, bench_id
      assert roots
      assert file_cleanup_required_roots(prompt)
  else:
      assert not applies, bench_id
      assert file_cleanup_required_roots(prompt) == []


def test_cleanup_v2_roots_are_service_scoped_only() -> None:
    _, prompt = next(p for p in _bench_prompts() if p[0] == "cleanup-v2")
    roots = RemediationScope.from_user_text(prompt).search_roots(round=1)

    assert set(roots) == {
        "/var/log/web-app01",
        "/var/cache/web-app01",
        "/var/tmp/web-app01",
    }


def test_nested_explicit_paths_collapse_to_minimal_roots() -> None:
    scope = RemediationScope.from_user_text(
        "修复 /var/log/payroll-api 及 /var/log/payroll-api/app 目录权限"
    )
    roots = scope.search_roots(round=1)

    assert roots == ("/var/log/payroll-api",)


def test_repair_cleanup_wording_does_not_disable_cleanup_checklist() -> None:
    prompt = "修复 web-app01 磁盘满，清理旧日志和缓存"
    scope = RemediationScope.from_user_text(prompt)

    assert file_remediation_checklist_applies(scope)
    assert set(file_cleanup_required_roots(prompt)) >= {
        "/var/log/web-app01",
        "/var/cache/web-app01",
    }


def test_root_level_explicit_file_cleanup_has_guarded_target_not_global_root() -> None:
    prompt = "清理 /var/log/messages.1 这个旧日志"
    scope = RemediationScope.from_user_text(prompt)
    checklist = _FileRemediationChecklist.from_scope(scope)

    assert file_remediation_checklist_applies(scope)
    assert checklist.required_roots == ()
    assert checklist.explicit_file_targets == {"/var/log/messages.1"}
    assert "/var/log" not in scope.search_roots(round=1)


def test_cron_turn_after_cleanup_does_not_inherit_web_app_checklist(agent) -> None:
    cleanup_prompt = _load_manifest_prompt(_BENCHES / "cleanup-v2" / "manifest.yaml")
    cron_prompt = _load_manifest_prompt(_BENCHES / "cron-injection-v1" / "manifest.yaml")

    agent.llm = _ImmediateFinalBackend()
    agent.cfg.agent.max_iterations = 5

    cleanup_result = agent.ask(cleanup_prompt)
    assert cleanup_result.tool_iterations >= 1
    cleanup_blocks = [
        e for e in cleanup_result.trace.events
        if e.kind is EventKind.PLAN_UPDATE
        and e.payload.get("event") == "file_remediation_checklist_required"
    ]
    assert cleanup_blocks

    cron_result = agent.ask(cron_prompt)
    cron_blocks = [
        e for e in cron_result.trace.events
        if e.kind is EventKind.PLAN_UPDATE
        and e.payload.get("event") == "file_remediation_checklist_required"
    ]
    assert not cron_blocks
    assert all(
        "web-app01" not in (e.payload.get("detail") or "")
        for e in cron_result.trace.events
        if e.kind is EventKind.PLAN_UPDATE
    )


def test_stale_lock_immediate_answer_skips_file_cleanup_checklist(agent) -> None:
    prompt = _load_manifest_prompt(_BENCHES / "stale-lock-v1" / "manifest.yaml")
    agent.llm = _ImmediateFinalBackend()
    agent.cfg.agent.max_iterations = 5

    result = agent.ask(prompt)

    checklist_blocks = [
        e for e in result.trace.events
        if e.kind is EventKind.PLAN_UPDATE
        and e.payload.get("event") == "file_remediation_checklist_required"
    ]
    assert not checklist_blocks
    assert result.tool_iterations < 5
    assert result.final_text == "done without tools"


def test_short_followup_cleanup_inherits_previous_discovery(agent) -> None:
    cleanup_prompt = _load_manifest_prompt(_BENCHES / "cleanup-v2" / "manifest.yaml")
    agent.llm = _ImmediateFinalBackend()
    agent.cfg.agent.max_iterations = 5
    agent.ask(cleanup_prompt)

    followup = agent.ask("好的，把刚才确认可删的都清理掉")
    assert agent._file_remediation_checklist is not None
    assert followup.tool_iterations <= 5
    assert not any(
        e.kind is EventKind.ERROR and e.payload.get("reason") == "max_iterations"
        for e in followup.trace.events
    )


def test_scope_context_text_uses_current_turn_only(agent) -> None:
    agent.messages = [
        {"role": "user", "content": "清理 web-app01 日志"},
        {"role": "assistant", "content": "ok"},
    ]
    cron_prompt = "排查 /etc/cron.d 恶意 cron"
    assert agent._scope_context_text(cron_prompt) == cron_prompt

    scope = RemediationScope.from_user_text(agent._scope_context_text(cron_prompt))
    assert not file_remediation_checklist_applies(scope)


def test_checklist_from_scope_respects_applies_gate() -> None:
    lock_scope = RemediationScope.from_user_text(
        _load_manifest_prompt(_BENCHES / "stale-lock-v1" / "manifest.yaml")
    )
    assert not file_remediation_checklist_applies(lock_scope)
    checklist = _FileRemediationChecklist.from_scope(lock_scope)
    assert checklist.required_roots == ("/tmp/deploy-ops/locks",)
    # Agent layer must skip attaching this checklist when applies is false.
