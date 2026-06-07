"""Whitelisted project validation commands."""
from __future__ import annotations

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PYTEST_SUITES = {
    "unit": ["tests"],
    "mcp": ["tests/test_mcp.py", "tests/test_mcp_protocol.py", "tests/test_mcp_hardening.py"],
    "agent": ["tests/test_agent_parallel.py", "tests/test_intent.py", "tests/test_safety.py"],
    "web": ["tests/test_web_server.py", "tests/test_web_security.py"],
    "frontend": ["tests/test_web_frontend_playwright.py"],
    "loongarch": [
        "tests/test_loongarch_deploy_docs.py",
        "tests/test_loongarch_hardening.py",
        "tests/test_loongarch_installer_behavior.py",
    ],
}

_SCRIPTS = {
    "developer_quick_test": "scripts/developer-quick-test.sh",
    "install_syntax": "scripts/install.sh",
    "start_web_syntax": "scripts/start-web.sh",
}


class VerifyPytestTool(Tool):
    name = "verify_pytest"
    description = "Run a fixed pytest suite from the project validation allowlist."
    risk_level = RiskLevel.MEDIUM
    requires_root = False
    read_only = False
    input_schema = {
        "type": "object",
        "properties": {
            "suite": {"type": "string", "enum": sorted(_PYTEST_SUITES)},
        },
        "required": ["suite"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        suite = args.get("suite")
        paths = _PYTEST_SUITES.get(suite)
        if not paths:
            raise ToolError(f"unknown pytest suite: {suite}")
        return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]


class VerifyRuffTool(Tool):
    name = "verify_ruff"
    description = "Run ruff check with fixed project arguments."
    risk_level = RiskLevel.MEDIUM
    requires_root = False
    read_only = False
    input_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        return ["python", "-m", "ruff", "check", "kyagent", "tests"]


class VerifyScriptSyntaxTool(Tool):
    name = "verify_script_syntax"
    description = "Run bash -n on a fixed script from the validation allowlist."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "script": {"type": "string", "enum": sorted(_SCRIPTS)},
        },
        "required": ["script"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        script = _SCRIPTS.get(args.get("script"))
        if not script:
            raise ToolError("unknown script")
        return ["bash", "-n", script]


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(VerifyPytestTool())
    registry.register(VerifyRuffTool())
    registry.register(VerifyScriptSyntaxTool())
    return registry
