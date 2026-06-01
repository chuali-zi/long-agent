"""Structured RCA report validation without runtime integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence

from kyagent.rca.playbooks import Playbook


class ReportValidationError(ValueError):
    """Raised when an RCA report does not meet the public schema."""


@dataclass(frozen=True)
class EvidenceReferenceValidation:
    ok: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class RCAReport:
    playbook: str
    summary: str
    root_cause: str
    confidence: float
    evidence_ids: tuple[str, ...]
    recommendations: tuple[str, ...]


def validate_evidence_references(
    evidence_ids: Sequence[str],
    available_evidence: Collection[str],
) -> EvidenceReferenceValidation:
    """Return missing evidence IDs while preserving report order."""
    available = set(available_evidence)
    missing = tuple(dict.fromkeys(item for item in evidence_ids if item not in available))
    return EvidenceReferenceValidation(ok=not missing, missing=missing)


def _required_text(report: Mapping[str, Any], field: str) -> str:
    value = report.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReportValidationError(f"{field} must be a non-empty string")
    return value


def _string_list(report: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = report.get(field)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ReportValidationError(f"{field} must be a non-empty string list")
    return tuple(value)


def validate_report(
    report: Mapping[str, Any],
    *,
    playbooks: Mapping[str, Playbook],
    available_evidence: Collection[str],
) -> RCAReport:
    """Validate and normalize a structured RCA report."""
    if not isinstance(report, Mapping):
        raise ReportValidationError("report must be a mapping")
    playbook = _required_text(report, "playbook")
    if playbook not in playbooks:
        raise ReportValidationError(f"unknown playbook: {playbook}")
    confidence = report.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ReportValidationError("confidence must be a number")
    if not 0 <= float(confidence) <= 1:
        raise ReportValidationError("confidence must be between 0 and 1")
    evidence_ids = _string_list(report, "evidence_ids")
    references = validate_evidence_references(evidence_ids, available_evidence)
    if not references.ok:
        raise ReportValidationError(
            f"unknown evidence references: {', '.join(references.missing)}"
        )
    return RCAReport(
        playbook=playbook,
        summary=_required_text(report, "summary"),
        root_cause=_required_text(report, "root_cause"),
        confidence=float(confidence),
        evidence_ids=evidence_ids,
        recommendations=_string_list(report, "recommendations"),
    )
