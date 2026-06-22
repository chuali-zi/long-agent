#!/usr/bin/env bash
# Shared helpers for RealOps benchmark run scripts.
set -euo pipefail

kybench_repo_root() {
  local here="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  cd "$here/../.." && pwd
}

kybench_load_prompt_from_manifest() {
  local bench_dir="$1"
  local manifest="$bench_dir/manifest.yaml"
  if [[ -n "${KYBENCH_PROMPT:-}" ]]; then
    return 0
  fi
  if [[ ! -f "$manifest" ]]; then
    return 0
  fi
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" ]] || return 0
  KYBENCH_PROMPT="$("$py" - "$manifest" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
lines = text.splitlines()
collect = False
parts: list[str] = []
for line in lines:
    if line.startswith("prompt:"):
        rest = line.split(":", 1)[1].strip()
        if rest == ">":
            collect = True
            continue
        if rest:
            print(rest.strip().strip('"'))
            raise SystemExit(0)
    if collect:
        if line and not line.startswith(" ") and not line.startswith("\t"):
            break
        parts.append(line.strip())
if parts:
    print(" ".join(parts))
PY
)"
  export KYBENCH_PROMPT
}

kybench_run_ask() {
  local bench_dir="$1"
  local prompt="$2"
  local install_prefix="${KYAGENT_INSTALL_PREFIX:-/opt/kyagent}"
  local env_file="${KYAGENT_ENV_FILE:-/etc/kyagent/env}"
  local kyagent_user="${KYAGENT_USER:-kyagent}"

  # /etc/kyagent is root:kyagent 0750 — non-root callers cannot stat the env file directly.
  sudo test -f "$env_file" || { echo "env not found: $env_file" >&2; exit 1; }
  if [[ ! -x "$install_prefix/.venv/bin/kyagent" ]]; then
    install_prefix="$(kybench_repo_root "$bench_dir")"
  fi
  [[ -x "$install_prefix/.venv/bin/kyagent" ]] || {
    echo "kyagent not found under $install_prefix/.venv/bin/kyagent" >&2
    exit 1
  }

  # Optional scope for generic auto-approve runtime roots (not hardcoded in core).
  local runtime_root="${KYBENCH_RUNTIME_ROOT:-}"
  local auto_roots="${KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS:-}"
  if [[ -n "$runtime_root" && -z "$auto_roots" ]]; then
    export KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS="$runtime_root"
  fi
  local auto_roots_q
  printf -v auto_roots_q '%q' "${KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS:-}"
  local install_prefix_q
  printf -v install_prefix_q '%q' "$install_prefix"

  sudo -u "$kyagent_user" bash -c \
    "set -a; source '$env_file'; set +a; export PYTHONPATH=$install_prefix_q; export KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS=$auto_roots_q; '$install_prefix/.venv/bin/kyagent' ask --auto-approve-safe-remediation $(printf '%q' "$prompt")"
}

kybench_run_ask_capture() {
  # Like kybench_run_ask, but runs the real agent with --json, captures the
  # structured result to $ask_json, and dumps the run's audit trace to
  # $trace_json (read back via the same config resolution the agent used).
  # Lets behavioral graders see iteration/loop/escalation signals, not just disk.
  local bench_dir="$1"
  local prompt="$2"
  local ask_json="$3"
  local trace_json="$4"
  local install_prefix="${KYAGENT_INSTALL_PREFIX:-/opt/kyagent}"
  local env_file="${KYAGENT_ENV_FILE:-/etc/kyagent/env}"
  local kyagent_user="${KYAGENT_USER:-kyagent}"

  sudo test -f "$env_file" || { echo "env not found: $env_file" >&2; exit 1; }
  if [[ ! -x "$install_prefix/.venv/bin/kyagent" ]]; then
    install_prefix="$(kybench_repo_root "$bench_dir")"
  fi
  [[ -x "$install_prefix/.venv/bin/kyagent" ]] || {
    echo "kyagent not found under $install_prefix/.venv/bin/kyagent" >&2
    exit 1
  }
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  local runtime_root="${KYBENCH_RUNTIME_ROOT:-}"
  if [[ -n "$runtime_root" && -z "${KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS:-}" ]]; then
    export KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS="$runtime_root"
  fi
  local auto_roots_q install_prefix_q lib_dir_q ask_json_q trace_json_q
  printf -v auto_roots_q '%q' "${KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS:-}"
  printf -v install_prefix_q '%q' "$install_prefix"
  printf -v lib_dir_q '%q' "$lib_dir"
  printf -v ask_json_q '%q' "$ask_json"
  printf -v trace_json_q '%q' "$trace_json"

  # Run ask (--json) and dump the resulting trace in one privileged shell so the
  # audit DB path resolves identically for both steps.
  sudo -u "$kyagent_user" bash -c \
    "set -a; source '$env_file'; set +a; \
     export PYTHONPATH=$install_prefix_q; \
     export KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS=$auto_roots_q; \
     '$install_prefix/.venv/bin/kyagent' ask --json --auto-approve-safe-remediation $(printf '%q' "$prompt") > $ask_json_q; \
     tid=\$('$install_prefix/.venv/bin/python' -c \"import json;print(json.load(open($ask_json_q)).get('trace_id',''))\"); \
     '$install_prefix/.venv/bin/python' $lib_dir_q/dump_trace.py \"\${tid:-latest}\" $trace_json_q"
}

kybench_run_behavior_flow() {
  # Generic behavioral acceptance, reusable by every bench:
  #   real agent run (capture ask json + audit trace)
  #   -> that bench's own verify.sh post (outcome grade -> score.json)
  #   -> behavior_health.py gate (downgrade to FAIL on loop/spin pathologies).
  # Returns the final (gated) exit code.
  local bench_dir="$1"
  local prompt="$2"
  local bench_id="${3:-$(basename "$bench_dir")}"
  local ask_json="${KYBENCH_ASK_JSON:-$bench_dir/last-ask.json}"
  local trace_json="${KYBENCH_TRACE_JSON:-$bench_dir/last-trace.json}"
  local score_json="${KYBENCH_SCORE_JSON:-$bench_dir/score.json}"
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" ]] || { echo "python required for grading" >&2; return 10; }

  kybench_run_ask_capture "$bench_dir" "$prompt" "$ask_json" "$trace_json"
  # Outcome grade writes score.json then exits with its own code; don't abort.
  bash "$bench_dir/verify.sh" post || true
  "$py" "$lib_dir/behavior_health.py" "$ask_json" "$trace_json" "$score_json" "$bench_id"
}

kybench_finalize_exit() {
  local state_file="${1:?state file required}"
  local score="${KYBENCH_SCORE_JSON:-$(dirname "$state_file")/score.json}"
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" ]] || { echo "python required for grading" >&2; exit 10; }
  [[ -f "$score" ]] || { echo "missing score file: $score" >&2; exit 10; }
  "$py" "$lib_dir/grade.py" exit "$score"
  exit $?
}
