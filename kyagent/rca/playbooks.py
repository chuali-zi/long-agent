"""YAML-backed RCA playbook loading."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PlaybookError(ValueError):
    """Raised when an RCA playbook file is malformed."""


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    evidence: tuple[str, ...]
    checks: tuple[str, ...]


def _non_empty_strings(value: Any, *, field: str, playbook: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PlaybookError(f"{playbook}.{field} must be a non-empty string list")
    return tuple(value)


def load_playbooks(path: str | Path) -> dict[str, Playbook]:
    """Load validated playbooks keyed by their stable RCA category."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("playbooks"), dict):
        raise PlaybookError("playbooks root mapping is required")

    playbooks: dict[str, Playbook] = {}
    for name, value in raw["playbooks"].items():
        if not isinstance(name, str) or not name or not isinstance(value, dict):
            raise PlaybookError("each playbook must be a named mapping")
        description = value.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PlaybookError(f"{name}.description must be a non-empty string")
        playbooks[name] = Playbook(
            name=name,
            description=description,
            evidence=_non_empty_strings(value.get("evidence"), field="evidence", playbook=name),
            checks=_non_empty_strings(value.get("checks"), field="checks", playbook=name),
        )
    return playbooks
