#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cat >"$TMP_DIR/kyagent" <<'EOF'
#!/usr/bin/env bash
printf '%s|%s\n' "${TEST_RUNTIME_ENV:-missing}" "$*"
EOF
chmod +x "$TMP_DIR/kyagent"
printf 'TEST_RUNTIME_ENV=loaded\n' >"$TMP_DIR/env"

KYAGENT_BIN="$TMP_DIR/kyagent" \
KYAGENT_ENV_FILE="$TMP_DIR/env" \
bash scripts/kyagent.sh chat
