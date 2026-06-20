"""Pytest-wide defaults for isolated test runs."""
from __future__ import annotations

import os
import tempfile
import inspect
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

# The repository supports Python >=3.10, but developer smoke commands may still
# hit a system Python 3.9 where pathlib.Path.write_text lacks newline=.
_path_classes = {Path, type(Path())}
if any("newline" not in inspect.signature(cls.write_text).parameters for cls in _path_classes):
    _write_text_by_class = {cls: cls.write_text for cls in _path_classes}

    def _write_text_with_newline(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if newline is None:
            return _write_text_by_class[type(self)](self, data, encoding=encoding, errors=errors)
        with self.open("w", encoding=encoding, errors=errors, newline=newline) as f:
            return f.write(data)

    for _path_class in _path_classes:
        _path_class.write_text = _write_text_with_newline  # type: ignore[method-assign]
