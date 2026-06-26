from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_SUITE = ROOT / "benchmarks" / "run-suite.sh"
VERIFY_ALL = ROOT / "benchmarks" / "verify-all.sh"
WEB_MANUAL = ROOT / "benchmarks" / "WEB_MANUAL_TEST.md"
COMMON = ROOT / "benchmarks" / "lib" / "common.sh"


def test_run_suite_captures_expected_nonzero_bench_results() -> None:
    script = RUN_SUITE.read_text(encoding="utf-8")

    assert 'run_flag="--ask-behavior"' in script
    assert '--outcome-only) OUTCOME_ONLY=1' in script
    assert 'bash "$run_script" "$run_flag" 2>&1 | tee "$log_file"' in script
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

    assert 'export PYTHONPATH="$2"' in script
    assert 'KYBENCH_ARTIFACT_DIR' in script
    assert 'open($ask_json_q)' not in script


def test_verify_all_requires_root_and_prints_summary() -> None:
    script = VERIFY_ALL.read_text(encoding="utf-8")

    assert "sudo bash benchmarks/verify-all.sh" in script
    assert '[[ $EUID -ne 0 ]]' in script
    assert "must run as root" in script
    assert "WEB_MANUAL_TEST.md" in script
    assert "=== kybench verify" in script
    assert "PASSED (" in script
    assert "NOT PASSED (" in script
    assert "score_mode_mismatch" in script


def test_web_manual_test_doc_exists_and_covers_workflow() -> None:
    text = WEB_MANUAL.read_text(encoding="utf-8")

    assert "sudo bash benchmarks/setup-all.sh" in text
    assert "sudo bash benchmarks/verify-all.sh --pre" in text
    assert "sudo bash benchmarks/verify-all.sh --post" in text
    assert "PERFECT" in text
    assert "SETUP_OK" in text
