#!/usr/bin/env bash
set -euo pipefail

source scripts/setup-sudoers.sh

sudo() {
  if [[ "${LC_ALL:-}" != "C" ]]; then
    printf 'Version de sudo 1.9.13p3\n'
    return 0
  fi
  printf 'Sudo version 1.9.13p3\n'
}

detect_sudo_version
