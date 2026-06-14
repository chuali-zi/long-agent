from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.path_helpers import bash_path as _bash_path


ROOT = Path(__file__).resolve().parents[1]


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


def _run_bash(command: str) -> subprocess.CompletedProcess[str]:
    require_usable_bash()
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )


def test_non_loongarch_linux_arch_check_continues_for_wsl_testing() -> None:
    result = _run_bash(
        "source scripts/install-loongarch.sh\n"
        "DRY_RUN=0\n"
        "ALLOW_NON_LOONGARCH=0\n"
        "detect_arch"
    )

    assert result.returncode == 0, result.stderr
    assert "architecture:" in result.stdout


def test_shell_assignment_roundtrips_metacharacters_without_execution(tmp_path: Path) -> None:
    require_usable_bash()
    marker = tmp_path / "must-not-exist"
    value = f"sk-$(touch {marker.as_posix()}) with spaces"
    script = tmp_path / "roundtrip.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "source scripts/install-loongarch.sh\n"
        f"line=\"$(write_shell_assignment DEEPSEEK_API_KEY '{value}')\"\n"
        "eval \"$line\"\n"
        "printf '%s' \"$DEEPSEEK_API_KEY\"\n",
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        ["bash", _bash_path(script)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == value
    assert not marker.exists()
