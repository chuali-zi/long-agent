#!/usr/bin/env bash
# Back-compat wrapper — prefer: sudo bash benchmarks/run-suite.sh
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run-suite.sh" "$@"
