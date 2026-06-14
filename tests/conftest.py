"""Pytest-wide defaults for isolated test runs."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Web/Agent tests construct real Agent instances before monkeypatching ask().
# Force the mock LLM backend unless a test explicitly overrides it.
os.environ["KYAGENT_LLM_BACKEND"] = "mock"

# Keep pytest hermetic even when run from an /opt production install or from a
# shell that sourced /etc/kyagent/env. Individual tests can still monkeypatch
# these values when they assert production path behavior.
_state_dir = Path(tempfile.mkdtemp(prefix="kyagent-pytest-"))
os.environ["KYAGENT_AUDIT_DB"] = str(_state_dir / "audit.db")
os.environ["KYAGENT_AUDIT_JSONL"] = str(_state_dir / "audit.jsonl")
os.environ["KYAGENT_PLAN_DB"] = str(_state_dir / "plans.db")
