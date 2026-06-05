from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    return f"/mnt/{drive}{resolved.as_posix()[2:]}"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


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


def _fake_runtime(tmp_path: Path, *, with_opener: bool = True) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backend_args = tmp_path / "backend-args"
    backend_env = tmp_path / "backend-env"
    opener_url = tmp_path / "opener-url"

    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-" ]]; then
  exit 0
fi
printf '%s\n' "$*" >"$BACKEND_ARGS_FILE"
printf '%s\n' "${KYAGENT_LLM_BACKEND:-}" >"$BACKEND_ENV_FILE"
sleep 0.05
""",
    )
    if with_opener:
        _write_executable(
            bin_dir / "xdg-open",
            """#!/usr/bin/env bash
printf '%s\n' "$1" >"$OPENER_URL_FILE"
""",
        )

    env = os.environ.copy()
    env["BACKEND_ARGS_FILE"] = _bash_path(backend_args)
    env["BACKEND_ENV_FILE"] = _bash_path(backend_env)
    env["OPENER_URL_FILE"] = _bash_path(opener_url)
    env["KYAGENT_WEB_OPENERS"] = _bash_path(bin_dir / "xdg-open") if with_opener else "missing-opener"
    env["KYAGENT_WEB_PYTHON"] = _bash_path(bin_dir / "python3")
    inherited = env.get("WSLENV", "")
    names = (
        "BACKEND_ARGS_FILE",
        "BACKEND_ENV_FILE",
        "OPENER_URL_FILE",
        "KYAGENT_WEB_OPENERS",
        "KYAGENT_WEB_PYTHON",
        "KYAGENT_WEB_ALLOW_NON_LOOPBACK",
        "KYAGENT_WEB_REQUIRE_AUTH",
        "KYAGENT_WEB_OPERATOR_TOKEN",
        "KYAGENT_WEB_REVIEWER_TOKEN",
        "KYAGENT_WEB_AUDITOR_TOKEN",
        "KYAGENT_WEB_ADMIN_TOKEN",
    )
    env["WSLENV"] = ":".join(filter(None, (inherited, *names)))
    return env


def test_open_web_suppresses_transient_health_probe_tracebacks() -> None:
    script = (ROOT / "scripts" / "open-web.sh").read_text(encoding="utf-8")

    assert 'if "$PYTHON" - "$HEALTH_URL" >/dev/null 2>&1 <<\'PY\'' in script


def test_one_click_launcher_rejects_non_loopback_without_explicit_authenticated_mode(tmp_path: Path) -> None:
    require_usable_bash()
    env = _fake_runtime(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/start-web.sh", "--mock", "--host", "0.0.0.0", "--port", "8123"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )

    assert result.returncode != 0
    assert "non-loopback" in result.stderr


def test_one_click_launcher_starts_authenticated_non_loopback_backend(tmp_path: Path) -> None:
    require_usable_bash()
    env = _fake_runtime(tmp_path)
    env.update({
        "KYAGENT_WEB_ALLOW_NON_LOOPBACK": "1",
        "KYAGENT_WEB_REQUIRE_AUTH": "1",
        "KYAGENT_WEB_OPERATOR_TOKEN": "operator",
        "KYAGENT_WEB_REVIEWER_TOKEN": "reviewer",
        "KYAGENT_WEB_AUDITOR_TOKEN": "auditor",
        "KYAGENT_WEB_ADMIN_TOKEN": "admin",
    })

    result = subprocess.run(
        ["bash", "scripts/start-web.sh", "--mock", "--host", "0.0.0.0", "--port", "8123"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "backend-args").read_text(encoding="utf-8").strip() == (
        "-m kyagent web serve --host 0.0.0.0 --port 8123"
    )
    assert (tmp_path / "backend-env").read_text(encoding="utf-8").strip() == "mock"
    assert (tmp_path / "opener-url").read_text(encoding="utf-8").strip() == "http://127.0.0.1:8123"


def test_one_click_launcher_keeps_running_when_browser_opener_is_missing(tmp_path: Path) -> None:
    require_usable_bash()
    env = _fake_runtime(tmp_path, with_opener=False)

    result = subprocess.run(
        ["bash", "scripts/start-web.sh", "--mock"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "[kyagent-web][WARN] browser opener unavailable" in result.stderr
    assert "open manually: http://127.0.0.1:8000" in result.stderr


def test_one_click_launcher_can_skip_browser_open(tmp_path: Path) -> None:
    require_usable_bash()
    env = _fake_runtime(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/start-web.sh", "--mock", "--no-open-browser"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "opener-url").exists()


def test_separate_web_scripts_are_available() -> None:
    assert (ROOT / "scripts" / "start-web-backend.sh").is_file()
    assert (ROOT / "scripts" / "open-web.sh").is_file()
