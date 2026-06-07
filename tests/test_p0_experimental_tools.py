from __future__ import annotations

import pytest

from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.gitinspect import GitDiffTool, GitShowTool, GitStatusTool
from kyagent.mcp.tools.validation import VerifyPytestTool, VerifyRuffTool, VerifyScriptSyntaxTool
from kyagent.mcp.tools.webfacts import GithubIssueSearchTool, OsvQueryTool, WebFetchUrlTool
from kyagent.mcp.tools.docintake import DocxExtractTextTool, PdfExtractTextTool, XlsxListSheetsTool
from kyagent.mcp.tools.planstate import PlanGetTool, PlanListTool


def _argv(tool, args):
    return tool.build_argv(tool.validate(args))


def test_git_status_uses_safe_no_pager_prefix():
    argv = _argv(GitStatusTool(), {"repo": "."})
    assert argv[:2] == ["git", "--no-pager"]
    assert "core.pager=cat" in argv
    assert "protocol.file.allow=never" in argv
    assert argv[-3:] == ["status", "--short", "--branch"]


def test_git_show_rejects_option_like_revision():
    with pytest.raises(ToolError):
        _argv(GitShowTool(), {"rev": "-c"})


def test_git_diff_rejects_path_traversal_and_shell_meta():
    tool = GitDiffTool()
    for path in ("../secret", "/etc/passwd", "ok;rm"):
        with pytest.raises(ToolError):
            _argv(tool, {"path": path})


def test_verify_pytest_is_fixed_suite_and_not_read_only():
    tool = VerifyPytestTool()
    argv = _argv(tool, {"suite": "mcp"})
    assert argv[:5] == ["python", "-m", "pytest", "-q", "-p"]
    assert "tests/test_mcp.py" in argv
    assert tool.read_only is False
    with pytest.raises(ToolError):
        tool.validate({"suite": "tests/test_secret.py"})


def test_verify_pytest_frontend_suite_exposes_playwright_checks():
    argv = _argv(VerifyPytestTool(), {"suite": "frontend"})
    assert "tests/test_web_frontend_playwright.py" in argv


def test_verify_ruff_fixed_argv():
    tool = VerifyRuffTool()
    assert _argv(tool, {}) == ["python", "-m", "ruff", "check", "kyagent", "tests"]
    assert tool.read_only is False


def test_verify_script_syntax_allowlist():
    tool = VerifyScriptSyntaxTool()
    assert _argv(tool, {"script": "start_web_syntax"}) == ["bash", "-n", "scripts/start-web.sh"]
    with pytest.raises(ToolError):
        tool.validate({"script": "../../evil.sh"})


def test_web_fetch_rejects_non_https_and_non_allowlisted_domains():
    tool = WebFetchUrlTool()
    for url in ("http://docs.python.org/3/", "https://example.com/", "https://127.0.0.1/"):
        with pytest.raises(ToolError):
            _argv(tool, {"url": url})


def test_web_fetch_allows_official_docs_url():
    argv = _argv(WebFetchUrlTool(), {"url": "https://docs.python.org/3/library/json.html"})
    assert argv == [
        "python", "-m", "kyagent.web_knowledge",
        "fetch", "https://docs.python.org/3/library/json.html",
    ]


def test_osv_query_package_rejects_shell_meta_package():
    tool = OsvQueryTool()
    with pytest.raises(ToolError):
        _argv(tool, {"ecosystem": "PyPI", "package": "django;rm"})


def test_github_issue_search_rejects_shell_meta_query():
    tool = GithubIssueSearchTool()
    with pytest.raises(ToolError):
        _argv(tool, {"query": "repo:openai/foo $(id)"})


def test_document_intake_tools_use_local_module_and_reject_traversal():
    assert _argv(DocxExtractTextTool(), {"path": "samples/report.docx"}) == [
        "python", "-m", "kyagent.document_intake", "docx", "samples/report.docx",
    ]
    assert _argv(XlsxListSheetsTool(), {"path": "samples/book.xlsx"})[:4] == [
        "python", "-m", "kyagent.document_intake", "xlsx",
    ]
    with pytest.raises(ToolError):
        _argv(PdfExtractTextTool(), {"path": "../secret.pdf"})


def test_plan_state_tools_are_read_only_and_validate_ids():
    assert PlanListTool().read_only is True
    assert _argv(PlanListTool(), {"limit": 3}) == [
        "python", "-m", "kyagent.plan_cli", "list", "--limit", "3",
    ]
    assert _argv(PlanGetTool(), {"plan_id": "plan-abc123"}) == [
        "python", "-m", "kyagent.plan_cli", "get", "plan-abc123",
    ]
    with pytest.raises(ToolError):
        _argv(PlanGetTool(), {"plan_id": "../plan"})
