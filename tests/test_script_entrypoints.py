import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "kyagent.sh"


def read_repo(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_unified_entrypoint_exposes_abstract_commands() -> None:
    script = ENTRYPOINT.read_text(encoding="utf-8")

    for command in ("install", "permissions", "chat", "tui", "web", "web-backend", "web-open", "tools"):
        assert f"{command})" in script

    assert 'exec bash "$SCRIPT_DIR/setup-sudoers.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/start-web.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/start-web-backend.sh"' in script
    assert 'exec bash "$SCRIPT_DIR/open-web.sh"' in script
    assert 'exec "$KYAGENT_BIN" chat' in script
    assert 'exec "$KYAGENT_BIN" tui' in script
    assert "load_runtime_env" in script
    assert "/etc/kyagent/env" in script


def test_root_readme_stays_high_level_and_links_detailed_guides() -> None:
    readme = read_repo("README.md")

    for command in (
        "bash scripts/kyagent.sh install",
        "sudo bash scripts/kyagent.sh permissions",
        "bash scripts/kyagent.sh chat",
        "bash scripts/kyagent.sh tui",
        "bash scripts/kyagent.sh web --mock",
    ):
        assert command in readme

    assert "docs/deployment/permissions.md" in readme
    assert "docs/deployment/web.md" in readme
    assert len(readme.splitlines()) < 180


def test_detailed_deployment_guides_exist() -> None:
    permissions = read_repo("docs/deployment/permissions.md")
    web = read_repo("docs/deployment/web.md")

    for phrase in (
        "sudo bash scripts/kyagent.sh permissions",
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
