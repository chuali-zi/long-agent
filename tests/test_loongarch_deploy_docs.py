from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


CHECKED_TEXT_FILES = [
    "README.md",
    "AGENT.md",
    "docs/kyagent/README.md",
    "docs/deployment/loongarch.md",
    "docs/status/current.md",
    "docs/status/log.md",
    "requirements.txt",
    "requirements-loongarch.txt",
    "pyproject.toml",
    "configs/default.yaml",
    "configs/deepseek.yaml",
    "configs/openai.yaml",
    "configs/qwen.yaml",
    "kyagent/config.py",
    "study/00-START-HERE.md",
    "study/04-llm-backends.md",
    "study/09-config.md",
    "study/13-testing-bench.md",
]


def read_repo(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_root_docs_are_migrated_to_docs_directory() -> None:
    root_docs_that_should_not_exist = [
        "README.kyagent.md",
        "DEPLOYMENT-LOONGARCH.md",
        "log.md",
        "staus.md",
    ]

    for path in root_docs_that_should_not_exist:
        assert not (ROOT / path).exists()

    required_docs = [
        "docs/kyagent/README.md",
        "docs/deployment/loongarch.md",
        "docs/status/log.md",
        "docs/status/current.md",
    ]

    for path in required_docs:
        assert (ROOT / path).exists()


def test_no_docs_reference_removed_implementation_notes() -> None:
    offenders = [
        path
        for path in CHECKED_TEXT_FILES
        if "implementation-notes.html" in read_repo(path)
    ]

    assert offenders == []


def test_loongarch_install_script_exists_and_has_safety_gates() -> None:
    script = read_repo("scripts/install-loongarch.sh")

    required_tokens = [
        "set -euo pipefail",
        "detect_arch",
        "detect_python",
        "KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx",
        "write_shell_assignment KYAGENT_AUDIT_DB /var/lib/kyagent/audit.db",
        "write_shell_assignment KYAGENT_AUDIT_JSONL /var/log/kyagent/audit.jsonl",
        "visudo -cf",
        "--dry-run",
        "--yes",
        "--with-web",
        "pip install --no-binary PyYAML,pydantic -r requirements-loongarch.txt",
        "verify_runtime_prefix_access",
        "runtime account cannot read install prefix",
    ]

    for token in required_tokens:
        assert token in script


def test_loongarch_installer_uses_linux_only_audited_dependency_path() -> None:
    script = read_repo("scripts/install-loongarch.sh")

    for token in (
        "uname -s",
        "--allow-non-loongarch requires --dry-run",
        "SKIP_CYTHON=1",
        "--no-binary PyYAML,pydantic",
        "requirements-loongarch-web.txt",
        "--no-deps -e .",
        "printf '%q'",
        "KYAGENT_EXECUTOR_ACCOUNT",
    ):
        assert token in script


def test_loongarch_installer_reports_optional_command_inventory() -> None:
    script = read_repo("scripts/install-loongarch.sh")
    deployment = read_repo("docs/deployment/loongarch.md")

    assert "report_optional_commands" in script
    for command in ("smartctl", "crontab", "aureport", "aide", "dmidecode", "iptables", "nft"):
        assert command in script
    assert "可选系统命令" in deployment


def test_loongarch_web_requirements_are_separate_and_sdk_free() -> None:
    requirements = read_repo("requirements-loongarch-web.txt")

    assert "fastapi>=0.95,<0.100" in requirements
    assert "uvicorn>=0.23,<0.30" in requirements
    for token in (
        "openai",
        "anthropic",
        "mcp",
        "jiter",
        "pydantic-core",
        "uvicorn[standard]",
        "markdown",
        "mistune",
        "markdown-it",
    ):
        assert token not in requirements


def test_loongarch_default_requirements_avoid_sdk_and_rust_extensions() -> None:
    requirements = read_repo("requirements-loongarch.txt")

    forbidden_runtime_deps = [
        "openai>=",
        "openai==",
        "anthropic>=",
        "anthropic==",
        "mcp>=",
        "mcp==",
        "jiter",
        "pydantic-core",
    ]

    for token in forbidden_runtime_deps:
        assert token not in requirements

    assert "pydantic>=1.10.13,<2" in requirements
    assert "PyYAML>=6.0.1,<7" in requirements
    assert "httpx>=0.23.0,<0.28" in requirements
    assert "prompt_toolkit>=3.0,<4" in requirements
    assert "textual" not in requirements
    assert "tree-sitter" not in requirements


def test_loongarch_docs_prefer_httpx_transport_over_sdk_extras() -> None:
    deployment = read_repo("docs/deployment/loongarch.md")

    required_phrases = [
        "deepseek_httpx",
        "不要在 LoongArch Old World 上安装 `.[openai]`",
        "默认路径零 Rust",
        "PyYAML",
        "fallback",
        "`--with-web`",
    ]

    for phrase in required_phrases:
        assert phrase in deployment


def test_backend_docs_list_httpx_variants() -> None:
    docs = [
        read_repo("configs/default.yaml"),
        read_repo("configs/deepseek.yaml"),
        read_repo("kyagent/config.py"),
        read_repo("docs/kyagent/README.md"),
    ]

    for doc in docs:
        assert "openai_httpx" in doc
        assert "deepseek_httpx" in doc
        assert "qwen_httpx" in doc


def test_tui_demo_documented_for_loongarch() -> None:
    readme = read_repo("README.md")
    project_docs = read_repo("docs/kyagent/README.md")
    deployment = read_repo("docs/deployment/loongarch.md")

    assert "kyagent tui" in readme
    assert "prompt_toolkit + rich" in project_docs
    assert "/tools" in project_docs
    assert "/audit" in project_docs
    assert "kyagent tui" in deployment
    assert "prompt_toolkit + rich" in deployment
    assert "tree-sitter" in deployment


def test_web_start_script_and_docs_are_present() -> None:
    script = read_repo("scripts/start-web.sh") + read_repo("scripts/start-web-backend.sh")
    readme = read_repo("README.md")
    web_docs = read_repo("docs/deployment/web.md")
    deployment = read_repo("docs/deployment/loongarch.md")

    for token in (
        "set -euo pipefail",
        "--install-web",
        "--mock",
        "--no-open-browser",
        "KYAGENT_LLM_BACKEND=mock",
        "kyagent web serve",
        "pip install -e .[web]",
    ):
        assert token in script

    for phrase in (
        "bash scripts/kyagent.sh web --mock",
        "docs/deployment/web.md",
    ):
        assert phrase in readme

    for phrase in (
        "bash scripts/start-web.sh --install-web --mock",
        "bash scripts/start-web-backend.sh",
        "bash scripts/open-web.sh",
        "approval_required",
        "approval_resolved",
        "POST /api/approvals/{approval_id}/approve",
        "POST /api/approvals/{approval_id}/reject",
    ):
        assert phrase in web_docs

    assert "`--with-web`" in deployment
    assert "FastAPI" in deployment
    assert "uvicorn" in deployment


def test_root_readme_is_loongarch_first_and_documents_web_layers() -> None:
    readme = read_repo("README.md")

    assert "LoongArch Linux" in readme
    assert "bash scripts/kyagent.sh web --mock" in readme
    assert "自动打开浏览器" in readme
    assert "bash scripts/start-web-backend.sh" in readme
    assert "bash scripts/open-web.sh" in readme
    assert "## Windows" not in readme


def test_loongarch_docs_explain_sudoers_password_failure() -> None:
    readme = read_repo("README.md")
    deployment = read_repo("docs/deployment/loongarch.md")

    for doc in (readme, deployment):
        assert "sudo: a password is required" in doc
        assert "sudo bash scripts/kyagent.sh permissions" in doc
        assert "sudo -l -U kyagent" in doc
        assert "--skip-sudoers" in doc


def test_shell_scripts_use_lf_line_endings() -> None:
    for script in (ROOT / "scripts").glob("*.sh"):
        assert b"\r\n" not in script.read_bytes(), f"{script.name} must use LF line endings"
