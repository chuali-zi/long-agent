import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "kyagent.sh"


def read_repo(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require_usable_bash() -> None:
    result = subprocess.run(
        ["bash", "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        pytest.skip("bash is not usable in this test environment")


def test_unified_entrypoint_exposes_abstract_commands() -> None:
    script = ENTRYPOINT.read_text(encoding="utf-8")

    for command in ("install", "permissions", "prod-env", "chat", "tui", "web", "web-backend", "web-open", "tools"):
        assert f"{command})" in script

    assert 'exec bash "$SCRIPT_DIR/setup-sudoers.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/write-prod-env.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/start-web.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/start-web-backend.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/open-web.sh"' in script
    assert 'exec "$KYAGENT_BIN" chat' in script
    assert 'exec "$KYAGENT_BIN" tui' in script
    assert "load_runtime_env" in script
    assert "/etc/kyagent/env" in script


def test_unified_entrypoint_reports_unreadable_delegated_scripts() -> None:
    script = ENTRYPOINT.read_text(encoding="utf-8")

    assert "require_script_readable" in script
    assert "cannot read delegated script" in script
    assert "/opt/kyagent" in script
    assert 'require_script_readable "$SCRIPT_DIR/start-web.sh"' in script


def test_root_readme_stays_high_level_and_links_detailed_guides() -> None:
    readme = read_repo("README.md")

    for command in (
        "sudo bash scripts/developer-quick-test.sh",
        "bash scripts/kyagent.sh install",
        "sudo bash scripts/kyagent.sh permissions",
        "sudo bash /opt/kyagent/scripts/kyagent.sh prod-env",
        "bash scripts/kyagent.sh chat",
        "bash scripts/kyagent.sh tui",
        "bash scripts/kyagent.sh web --mock",
    ):
        assert command in readme

    assert "docs/deployment/permissions.md" in readme
    assert "docs/deployment/web.md" in readme
    assert len(readme.splitlines()) < 230


def test_developer_quick_test_script_chains_readme_flow() -> None:
    script = read_repo("scripts/developer-quick-test.sh")
    readme = read_repo("README.md")

    for phrase in (
        "install-loongarch.sh",
        "--yes --with-web",
        "setup-sudoers-max-test.sh",
        "prod-env --deepseek-key-file",
        "KYAGENT_WEB_ADMIN_TOKEN=admin123",
        "visudo -cf /etc/sudoers.d/kyagent",
        "sudo -l -U",
        "tools list",
        "which process used the most cpu",
        "web --env-file",
    ):
        assert phrase in script

    assert "sudo bash scripts/developer-quick-test.sh" in readme
    assert "KYAGENT_WEB_ADMIN_TOKEN=admin123" in readme


def test_prod_env_script_writes_only_the_minimal_runtime_env() -> None:
    script = read_repo("scripts/write-prod-env.sh")

    for phrase in (
        "set -euo pipefail",
        "sudo bash scripts/kyagent.sh prod-env",
        "id \"$KYAGENT_USER\"",
        "write_shell_assignment",
        "install -d -m 0750 -o root -g \"$KYAGENT_USER\" \"$ENV_DIR\"",
        "install -m 0640 -o root -g \"$KYAGENT_USER\" \"$tmp\" \"$ENV_FILE\"",
        "KYAGENT_CONFIG",
        "KYAGENT_DEEPSEEK_TRANSPORT",
        "deepseek_httpx",
        "KYAGENT_EXECUTOR_ACCOUNT",
        "KYAGENT_AUDIT_DB",
        "/var/lib/kyagent/audit.db",
        "KYAGENT_AUDIT_JSONL",
        "/var/log/kyagent/audit.jsonl",
        "KYAGENT_PLAN_DB",
        "/var/lib/kyagent/plans.db",
        "KYAGENT_AUDIT_INTEGRITY_ENABLED",
        "KYAGENT_AUDIT_HMAC_KEY_FILE",
        "KYAGENT_AUDIT_HMAC_KEY_ID",
        "local-v1",
        "DEEPSEEK_API_KEY",
        "--deepseek-key-file",
    ):
        assert phrase in script

    assert "NOPASSWD" not in script
    assert "/etc/sudoers.d/kyagent" not in script


def test_production_web_commands_use_absolute_opt_prefix() -> None:
    readme = read_repo("README.md")
    web = read_repo("docs/deployment/web.md")

    assert "sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web" in readme
    assert "sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web" in web
    assert "sudo -u kyagent bash scripts/kyagent.sh web --env-file /etc/kyagent/env" not in readme


def test_production_key_setup_uses_absolute_opt_entrypoint() -> None:
    readme = read_repo("README.md")
    loongarch = read_repo("docs/deployment/loongarch.md")
    permissions = read_repo("docs/deployment/permissions.md")

    for doc in (readme, loongarch, permissions):
        assert "sudo bash /opt/kyagent/scripts/kyagent.sh prod-env" in doc

    assert "sudo bash scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key" not in readme


def test_detailed_deployment_guides_exist() -> None:
    permissions = read_repo("docs/deployment/permissions.md")
    web = read_repo("docs/deployment/web.md")

    for phrase in (
        "sudo bash scripts/kyagent.sh permissions",
        "sudo bash /opt/kyagent/scripts/kyagent.sh prod-env",
        "最小权限",
        "/etc/sudoers.d/kyagent",
        "sudo >= 1.9.10",
        "LC_ALL=C sudo -V",
        "sudo visudo -cf /etc/sudoers.d/kyagent",
    ):
        assert phrase in permissions

    for phrase in (
        "bash scripts/kyagent.sh web --mock",
        "http://127.0.0.1:8000",
        "--install-web",
        "--env-file /etc/kyagent/env",
    ):
        assert phrase in web


def test_unified_entrypoint_loads_runtime_env_before_chat() -> None:
    require_usable_bash()

    result = subprocess.run(
        ["bash", "tests/fixtures/kyagent_entrypoint_env.sh"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "loaded|chat"
