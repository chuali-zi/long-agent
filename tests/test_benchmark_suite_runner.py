from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_SUITE = ROOT / "benchmarks" / "run-suite.sh"
COMMON = ROOT / "benchmarks" / "lib" / "common.sh"


def test_run_suite_captures_expected_nonzero_bench_results() -> None:
    script = RUN_SUITE.read_text(encoding="utf-8")

    assert 'if bash "$run_script" --ask 2>&1 | tee "$log_file"; then' in script
    assert "rc=$?" in script
    assert "set +e" not in script


def test_run_suite_captures_nonperfect_score_exit_without_aborting() -> None:
    lines = RUN_SUITE.read_text(encoding="utf-8").splitlines()

    grade_exit_lines = [
        line.strip()
        for line in lines
        if '"$ROOT/lib/grade.py" exit "$score_copy"' in line
    ]

    assert grade_exit_lines == [
        'if "$py" "$ROOT/lib/grade.py" exit "$score_copy"; then'
    ]


def test_real_agent_uses_code_from_selected_install_prefix() -> None:
    script = COMMON.read_text(encoding="utf-8")

    assert "export PYTHONPATH=$install_prefix_q" in script
