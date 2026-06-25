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
  local artifact_dir
  artifact_dir="$(dirname "$ask_json")"
  mkdir -p "$artifact_dir"
  chmod 0700 "$artifact_dir"

  # The caller (normally root) owns the redirections.  The agent itself still
  # runs as the restricted account, so it never needs write access to /opt.
  # Positional parameters keep paths/prompts out of shell source code.
  (
    umask 077
    sudo -u "$kyagent_user" bash -c '
      set -euo pipefail
      set -a
      source "$1"
      set +a
      export PYTHONPATH="$2"
      export KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS="$3"
      exec "$4" ask --json --auto-approve-safe-remediation "$5"
    ' _ "$env_file" "$install_prefix" \
      "${KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS:-}" \
      "$install_prefix/.venv/bin/kyagent" "$prompt" > "$ask_json"
  )

  local trace_id
  trace_id="$("$install_prefix/.venv/bin/python" - "$ask_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
trace_id = doc.get("trace_id")
if not isinstance(trace_id, str) or not trace_id:
    raise SystemExit("ask JSON is missing trace_id")
print(trace_id)
PY
)"

  (
    umask 077
    sudo -u "$kyagent_user" bash -c '
      set -euo pipefail
      set -a
      source "$1"
      set +a
      export PYTHONPATH="$2"
      exec "$3" "$4" "$5" -
    ' _ "$env_file" "$install_prefix" \
      "$install_prefix/.venv/bin/python" "$lib_dir/dump_trace.py" \
      "$trace_id" > "$trace_json"
  )

  "$install_prefix/.venv/bin/python" - "$ask_json" "$trace_json" <<'PY'
import json
import sys

ask = json.load(open(sys.argv[1], encoding="utf-8"))
trace = json.load(open(sys.argv[2], encoding="utf-8"))
if ask.get("trace_id") != trace.get("trace_id"):
    raise SystemExit("ask/trace IDs do not match")
events = trace.get("events")
if not isinstance(events, list):
    raise SystemExit("trace JSON is missing events")
if not events:
    raise SystemExit("trace JSON has no events")
kinds = {event.get("kind") for event in events if isinstance(event, dict)}
if "user_input" not in kinds:
    raise SystemExit("trace JSON is missing user_input event")
if not ({"agent_reply", "error"} & kinds):
    raise SystemExit("trace JSON is missing terminal agent_reply/error event")
PY
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
  local artifact_dir="${KYBENCH_ARTIFACT_DIR:-}"
  if [[ -z "$artifact_dir" ]]; then
    artifact_dir="$(mktemp -d "${TMPDIR:-/tmp}/kybench-${bench_id}-behavior.XXXXXX")"
  fi
  mkdir -p "$artifact_dir"
  chmod 0700 "$artifact_dir"
  local ask_json="${KYBENCH_ASK_JSON:-$artifact_dir/last-ask.json}"
  local trace_json="${KYBENCH_TRACE_JSON:-$artifact_dir/last-trace.json}"
  local score_json="${KYBENCH_SCORE_JSON:-$bench_dir/score.json}"
  local lib_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local py
  py="$(command -v python3 || command -v python || true)"
  [[ -n "$py" ]] || { echo "python required for grading" >&2; return 10; }

  echo "behavior artifacts: $artifact_dir"
  local capture_rc=0
  kybench_run_ask_capture "$bench_dir" "$prompt" "$ask_json" "$trace_json" || capture_rc=$?
  # Outcome grade writes score.json then exits with its own code; don't abort.
  bash "$bench_dir/verify.sh" post || true
  "$py" "$lib_dir/behavior_health.py" \
    "$ask_json" "$trace_json" "$score_json" "$bench_id" \
    --profile "${KYBENCH_BEHAVIOR_PROFILE:-standard}" \
    --capture-exit "$capture_rc"
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
