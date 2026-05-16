#!/usr/bin/env bash
# 一键安装 kyagent（推荐 Python 3.10+）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip
pip install -e .

mkdir -p var

echo "[OK] kyagent 已安装。激活：source .venv/bin/activate"
echo "    入门：kyagent tools list"
echo "    交互：kyagent chat"
echo "    部署受限账户（root 执行）：sudo bash scripts/setup-sudoers.sh"
