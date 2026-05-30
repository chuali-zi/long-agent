#!/usr/bin/env bash
# 一键安装 kyagent（推荐 Python 3.10+）。
# 默认主依赖全部纯 Python / 零 Rust 编译，所有架构（含 LoongArch64）安全。
# 真实 LLM 后端（anthropic / openai SDK）含 jiter(Rust)，移到可选依赖，按需装。
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

echo ""
echo "[OK] kyagent 已安装（默认 deepseek_httpx；缺 DEEPSEEK_API_KEY 会直接报错；HttpxBackend 可用，零 Rust 编译依赖）。"
echo "    激活：source .venv/bin/activate"
echo "    入门：kyagent tools list"
echo "    交互：kyagent chat"
echo "    Web ：bash scripts/start-web.sh --install-web --mock"
echo "    部署受限账户（root 执行）：sudo bash scripts/setup-sudoers.sh"
echo ""
echo "可选 LLM 后端（默认不装；按需 pip install）："
echo "  - DeepSeek / Qwen / OpenAI 兼容服务（推荐 — HttpxBackend，无需装 openai SDK）："
echo "      llm_backend=deepseek_httpx  # 默认主依赖里的 httpx 即可用，所有架构（含 LoongArch）零 Rust"
echo "  - DeepSeek / Qwen / OpenAI 通过官方 openai SDK（备选 — OpenAIBackend）："
echo "      pip install -e '.[openai]'  # ≥1.40 含 jiter(Rust)；LoongArch 别走这个"
echo "  - Anthropic Claude（海外参考对照）："
echo "      pip install -e '.[anthropic]'  # 含 jiter(Rust)；LoongArch 先看 docs/deployment/loongarch.md"
