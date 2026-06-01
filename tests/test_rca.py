from __future__ import annotations

from pathlib import Path

import pytest

from kyagent.rca import (
    ReportValidationError,
    load_playbooks,
    validate_evidence_references,
    validate_report,
)
from kyagent.agent.core import Agent
from kyagent.agent.llm import ToolUseBlock
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config


PLAYBOOKS = Path(__file__).parent.parent / "configs" / "rca-playbooks.yaml"


def test_default_playbooks_cover_minimal_rca_categories():
    playbooks = load_playbooks(PLAYBOOKS)
    assert {"zombie_process", "disk_io", "config_drift", "large_logs"} <= playbooks.keys()
    assert all(playbook.evidence for playbook in playbooks.values())


def test_report_validator_accepts_structured_report_with_known_evidence():
    report = {
        "playbook": "disk_io",
        "summary": "Disk latency is elevated.",
        "root_cause": "A batch workload is saturating the data volume.",
        "confidence": 0.8,
        "evidence_ids": ["evidence-1", "evidence-2"],
        "recommendations": ["Throttle the batch workload."],
    }
    validated = validate_report(
        report,
        playbooks=load_playbooks(PLAYBOOKS),
        available_evidence={"evidence-1", "evidence-2"},
    )
    assert validated.playbook == "disk_io"
    assert validated.evidence_ids == ("evidence-1", "evidence-2")


def test_report_validator_rejects_unknown_evidence_reference():
    report = {
        "playbook": "large_logs",
        "summary": "Logs consume the volume.",
        "root_cause": "Unbounded application logs.",
        "confidence": 0.7,
        "evidence_ids": ["missing"],
        "recommendations": ["Rotate logs."],
    }
    with pytest.raises(ReportValidationError, match="missing"):
        validate_report(
            report,
            playbooks=load_playbooks(PLAYBOOKS),
            available_evidence={"evidence-1"},
        )


def test_evidence_reference_api_reports_missing_ids():
    result = validate_evidence_references(["evidence-1", "missing"], {"evidence-1"})
    assert result.ok is False
    assert result.missing == ("missing",)


def test_agent_accepts_evidence_backed_rca_report_and_audits_diagnosis(tmp_path):
    cfg = Config(base_dir=PLAYBOOKS.parent.parent)
    cfg.agent.llm_backend = "mock"
    cfg.audit.database = str(tmp_path / "audit.db")
    cfg.audit.jsonl_file = None
    agent = Agent.from_config(cfg)
    trace = Trace(user="tester")
    agent.audit.open(trace)
    agent.audit.event(trace, EventKind.PERCEPTION, {"evidence_id": "evidence-1"})

    result = agent._handle_tool_use_inner(
        trace,
        ToolUseBlock(
            id="rca-1",
            name="submit_rca_report",
            input={
                "playbook": "disk_io",
                "summary": "Disk latency is elevated.",
                "root_cause": "A batch workload saturates the volume.",
                "confidence": 0.8,
                "evidence_ids": ["evidence-1"],
                "recommendations": ["Throttle the batch workload."],
            },
        ),
        [],
    )

    try:
        assert result.is_error is False
        diagnosis = next(event for event in trace.events if event.kind is EventKind.DIAGNOSIS)
        assert diagnosis.payload["playbook"] == "disk_io"
        assert diagnosis.payload["evidence_ids"] == ["evidence-1"]
    finally:
        agent.audit.close(trace)
        agent.shutdown()
