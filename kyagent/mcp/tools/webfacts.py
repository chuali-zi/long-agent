"""Allowlisted external fact lookup tools."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel
from kyagent.web_knowledge import DEFAULT_ALLOWED_DOMAINS, validate_url


_PKG = re.compile(r"^[A-Za-z0-9_.+:-]{1,160}$")
_QUERY = re.compile(r"^[A-Za-z0-9_./:@# +\\-]{1,240}$")


class WebFetchUrlTool(Tool):
    name = "web_fetch_url"
    description = "Fetch text from an HTTPS URL whose domain is on the knowledge allowlist."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 8, "maxLength": 1000},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        try:
            url = validate_url(args["url"], domains=set(DEFAULT_ALLOWED_DOMAINS))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise ToolError("URL credentials are not allowed")
        return ["python", "-m", "kyagent.web_knowledge", "fetch", url]


class OsvQueryTool(Tool):
    name = "osv_query_package"
    description = "Query OSV for vulnerabilities in one package/ecosystem."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "ecosystem": {
                "type": "string",
                "enum": ["PyPI", "npm", "Maven", "Go", "crates.io", "Packagist", "RubyGems"],
            },
            "package": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "required": ["ecosystem", "package"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        package = args["package"]
        if not _PKG.fullmatch(package):
            raise ToolError("unsafe package name")
        return [
            "python", "-m", "kyagent.web_knowledge",
            "osv-query", args["ecosystem"], package,
        ]


class GithubIssueSearchTool(Tool):
    name = "github_issue_search"
    description = "Search GitHub issues and PRs through the allowlisted GitHub API."
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 240},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def build_argv(self, args: dict) -> list[str]:
        query = args["query"]
        if not _QUERY.fullmatch(query) or any(x in query for x in (";", "`", "$(", "|")):
            raise ToolError("unsafe GitHub search query")
        limit = str(int(args.get("limit", 5)))
        return [
            "python", "-m", "kyagent.web_knowledge",
            "github-issues", query, "--limit", limit,
        ]


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(WebFetchUrlTool())
    registry.register(OsvQueryTool())
    registry.register(GithubIssueSearchTool())
    return registry
