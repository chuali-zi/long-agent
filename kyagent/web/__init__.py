"""FastAPI B/S adapter for kyagent.

The web layer is a thin wrapper around the same Agent / Guardrail /
ExecutionProxy stack used by the CLI / TUI / MCP server. It does **not**
introduce a parallel safety path — every HTTP request goes through
``Agent.ask()`` which means:

  * Intent layer (NL prompt-injection / NLP risk filter) — runs first
  * Argv layer (Guardrail shell scrub)                  — runs on every tool call
  * ExecutionProxy least-privilege account              — same as CLI

This module is deliberately optional. The default ``pip install -e .``
on LoongArch does NOT pull FastAPI/uvicorn — install with the ``[web]``
extra, e.g. ``pip install -e .[web]``.
"""
from kyagent.web.server import build_app

__all__ = ["build_app"]
