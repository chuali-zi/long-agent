#!/usr/bin/env bash
# 一键安装 kyagent（推荐 Python 3.10+）。
# 开发机便捷安装。LoongArch64 正式部署必须使用 install-loongarch.sh，
# 由专用脚本固定纯 Python / 零 Rust 默认路径。
# 真实 LLM 后端（anthropic / openai SDK）含 jiter(Rust)，移到可选依赖，按需装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INSTALL_DEV_DEPS="${KYAGENT_INSTALL_DEV_DEPS:-1}"

usage() {
  cat <<'EOF'
Usage: bash scripts/install.sh [options]

Options:
  --runtime-only   Install only kyagent runtime dependencies, without dev/test tools.
  --dev            Install developer/test tools. This is the default.
  -h, --help       Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-only)
      INSTALL_DEV_DEPS=0
      shift
      ;;
    --dev)
      INSTALL_DEV_DEPS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip
if [[ "$INSTALL_DEV_DEPS" == "1" ]]; then
  pip install -e ".[dev]"
else
  pip install -e .
fi

mkdir -p var

echo ""
echo "[NOTE] LoongArch64 正式部署请使用：sudo bash scripts/install-loongarch.sh --yes"
echo "[OK] kyagent 已安装（默认 deepseek_httpx；缺 DEEPSEEK_API_KEY 会直接报错；HttpxBackend 可用，零 Rust 编译依赖）。"
echo "    激活：source .venv/bin/activate"
echo "    入门：kyagent tools list"
echo "    交互：kyagent chat"
echo "    Chat：bash scripts/kyagent.sh chat"
echo "    TUI ：bash scripts/kyagent.sh tui"
echo "    Web ：bash scripts/kyagent.sh web --install-web --mock"
if [[ "$INSTALL_DEV_DEPS" == "1" ]]; then
  echo "    测试：bash scripts/kyagent.sh test"
  echo "          python -m pytest -q --basetemp pytest_tmp_run -p no:cacheprovider"
else
  echo "    测试依赖未安装；需要时执行：pip install -e '.[dev]' 或 pip install -r requirements-dev.txt"
fi
echo "    部署受限账户（root 执行）：sudo bash scripts/kyagent.sh permissions"
echo ""
echo "可选 LLM 后端（默认不装；按需 pip install）："
echo "  - DeepSeek / Qwen / OpenAI 兼容服务（推荐 — HttpxBackend，无需装 openai SDK）："
echo "      llm_backend=deepseek_httpx  # 默认主依赖里的 httpx 即可用，所有架构（含 LoongArch）零 Rust"
echo "  - DeepSeek / Qwen / OpenAI 通过官方 openai SDK（备选 — OpenAIBackend）："
echo "      pip install -e '.[openai]'  # ≥1.40 含 jiter(Rust)；LoongArch 别走这个"
echo "  - Anthropic Claude（海外参考对照）："
echo "      pip install -e '.[anthropic]'  # 含 jiter(Rust)；LoongArch 先看 docs/deployment/loongarch.md"
