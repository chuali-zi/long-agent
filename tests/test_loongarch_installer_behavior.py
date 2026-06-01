from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    return f"/mnt/{drive}{resolved.as_posix()[2:]}"


def _run_bash(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )


def test_non_loongarch_override_requires_dry_run() -> None:
    result = _run_bash(
        "bash scripts/install-loongarch.sh --yes --allow-non-loongarch "
        "--skip-system-packages --skip-sudoers"
    )

    assert result.returncode != 0
    assert "--allow-non-loongarch requires --dry-run" in result.stderr


def test_shell_assignment_roundtrips_metacharacters_without_execution(tmp_path: Path) -> None:
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
