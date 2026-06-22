from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kyagent.executor.proxy import ExecutionResult
from kyagent.mcp.tools import package as package_mod
from kyagent.mcp.tools.base import ToolError
from kyagent.mcp.tools.pkg_family import PkgFamily, set_pkg_family_for_tests
from kyagent.mcp.tools.loongarch import LaWorldCheckTool
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
    env = os.environ.copy()
    # Installer tests must not consume a developer/CI secret from the host.
    # Tests that exercise key handling pass an explicit key file instead.
    env.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )


def _exec_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> ExecutionResult:
    return ExecutionResult(
        argv=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=False,
        duration=0.0,
    )


def test_offline_dry_run_applies_wheelhouse_to_every_pip_install(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    result = _run_bash(
        "bash scripts/install-loongarch.sh --yes --dry-run --allow-non-loongarch "
        "--skip-system-packages --skip-sudoers --offline "
        f"--wheelhouse '{_bash_path(wheelhouse)}' --with-web"
    )

    assert result.returncode == 0, result.stderr
    pip_installs = [
        line for line in result.stdout.splitlines() if "-m pip install" in line
    ]
    assert pip_installs
    assert all("--no-index" in line for line in pip_installs)
    assert all("--find-links" in line for line in pip_installs)
    assert all(_bash_path(wheelhouse) in line for line in pip_installs)
    assert all("--upgrade" not in line for line in pip_installs)


def test_offline_requires_wheelhouse() -> None:
    result = _run_bash(
        "bash scripts/install-loongarch.sh --yes --dry-run --allow-non-loongarch "
        "--skip-system-packages --skip-sudoers --offline"
    )

    assert result.returncode != 0
    assert "--offline requires --wheelhouse DIR" in result.stderr


def test_deepseek_key_file_is_loaded_without_trailing_newline(tmp_path: Path) -> None:
    require_usable_bash()
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("sk-file-secret\n", encoding="utf-8")
    script = tmp_path / "read-key.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "source scripts/install-loongarch.sh\n"
        f"load_deepseek_key_file '{_bash_path(key_file)}'\n"
        "printf '%s' \"$DEEPSEEK_KEY\"\n",
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
    assert result.stdout == "sk-file-secret"


def test_help_marks_inline_deepseek_key_as_not_recommended() -> None:
    result = _run_bash("bash scripts/install-loongarch.sh --help")

    assert result.returncode == 0
    assert "--deepseek-key-file FILE" in result.stdout
    assert "--deepseek-key KEY" in result.stdout
    assert "not recommended" in result.stdout.lower()


def test_strict_command_inventory_fails_when_commands_are_missing() -> None:
    result = _run_bash(
        "source scripts/install-loongarch.sh; PATH=''; report_optional_commands strict"
    )

    assert result.returncode != 0
    assert "strict command inventory missing" in result.stderr


def test_package_manager_detection_prefers_dnf_then_yum_then_rpm(monkeypatch) -> None:
    available = {"dnf", "yum", "rpm"}
    monkeypatch.setattr(package_mod.shutil, "which", lambda name: name if name in available else None)
    assert package_mod._detect_pm() == "dnf"

    available.remove("dnf")
    assert package_mod._detect_pm() == "yum"

    available.remove("yum")
    assert package_mod._detect_pm() == "rpm"


def test_package_info_fails_clearly_without_rpm_package_commands(monkeypatch) -> None:
    monkeypatch.setattr(package_mod.shutil, "which", lambda name: None)

    with pytest.raises(ToolError, match="dnf/yum/rpm"):
        package_mod.PkgInfoTool().build_argv({"name": "bash"})


def test_rpm_family_extensions_use_yum_when_dnf_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        package_mod.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"yum", "rpm"} else None,
    )
    set_pkg_family_for_tests(PkgFamily.RPM)
    try:
        assert package_mod.PkgUpdatesTool().build_argv({})[0] == "yum"
        assert package_mod.PkgSecurityUpdatesTool().build_argv({})[0] == "yum"
        assert package_mod.PkgRepoListTool().build_argv({})[0] == "yum"
        assert package_mod.PkgHistoryTool().build_argv({})[0] == "yum"
    finally:
        set_pkg_family_for_tests(None)


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "expected"),
    [
        (
            "/lib64/ld.so.1\n/lib64/ld-linux-loongarch-lp64d.so.1\n",
            "",
            0,
            "mixed",
        ),
        ("/lib64/ld.so.1\n", "", 2, "old"),
        ("/lib64/ld-linux-loongarch-lp64d.so.1\n", "", 2, "new"),
        ("", "ls: cannot access loaders\n", 2, "unknown"),
    ],
)
def test_world_check_reports_four_states(
    stdout: str, stderr: str, returncode: int, expected: str
) -> None:
    result = LaWorldCheckTool().format_result(_exec_result(stdout, stderr, returncode))

    assert result.ok
    assert result.data["world"] == expected
    assert f"verdict: {expected}" in result.content


def test_world_check_does_not_treat_unrelated_nonzero_as_old() -> None:
    result = LaWorldCheckTool().format_result(
        _exec_result(stderr="ls: permission denied\n", returncode=13)
    )

    assert result.ok
    assert result.data["world"] == "unknown"
