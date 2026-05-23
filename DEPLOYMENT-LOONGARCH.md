# 麒麟 LoongArch64 部署指南

> 在**龙芯架构（loongarch64）的麒麟操作系统**上部署 kyagent 的步骤、坑点与排查手册。
> 鲲鹏 / 飞腾（aarch64）、海光 / 兆芯（x86_64）的麒麟**不需要看本文**，按 `README.kyagent.md` 走即可。

---

## 0. 在动手之前必须知道的事

### 0.1 文档诚实声明

本文档**未在真实龙芯麒麟硬件上端到端验证过**。所有事实在 2026-05-22 通过公开资料核对，但生态变化快、各发行版细节有差异。**强烈建议先在测试虚拟机上跑通，再上生产**。文末有"实测 checklist"，请边做边记录。

### 0.2 Old World vs New World — 最关键的一件事

龙芯有两套互不兼容的 ABI 生态：

| 特征 | Old World（旧世界） | New World（新世界） |
|---|---|---|
| ABI 版本 | LoongArch ABI 1.0 | LoongArch ABI 2.0 |
| 最低 kernel | 4.19 | 5.19+ |
| 最低 glibc | 2.28 | 2.36+ |
| 谁在用 | **银河麒麟 V10 / UOS / Loongnix** | 上游主线、社区发行版、Debian/Fedora 官方移植 |
| PyPI/manylinux 兼容性 | **不兼容** | 未来兼容（标签尚未正式接纳） |

**麒麟 V10 SP3（含 2403 版）是 Old World**。这意味着：

- PyPI 上即便将来出现 `loongarch64` wheel，**在你的麒麟上也跑不起来**（除非装 libLoL 兼容层，这超出本文档范围）
- 所有 C 扩展、Rust 扩展都必须**在你的机器上现编译**
- 麒麟 V11 计划基于 kernel 6.6 切到 New World（社区预期 2025 年）。本文档撰写时（2026-05）V11 在 LoongArch 上的正式发布状态请以官方为准；本文档**只覆盖 V10 Old World 部署**

### 0.3 龙芯部署方案（2026-05-22 起：零编译路径）

> **2026-05-22 重大变更**：经依赖侦察 + 替换方案 A，本部署**已不再需要现场编译任何 Rust / C 扩展**。
> 设计决策详见 `implementation-notes.html`（"2026-05-22 龙芯依赖替换方案"）。
> 配套清单文件：`requirements-loongarch.txt`。

| 包 | 类型 | 龙芯部署方案 |
|---|---|---|
| `pydantic` | 纯 Python（降至 v1.10） | 直接 pip 装 `py3-none-any` wheel，零编译 |
| `anthropic` | 纯 Python（锁 0.39.0） | `pip install --no-deps`，绕开 jiter Rust 编译 |
| `PyYAML` | 纯 Python fallback | `pip install --no-binary PyYAML`，跳过 libyaml C 扩展 |

**结论：本部署方案已不需要 Rust 工具链；libyaml-devel 也可不装**。

其他依赖（`typer` / `rich` / `openai`）是纯 Python，**无需编译**。
（`mcp` SDK 依赖 pydantic v2 即 pydantic-core Rust，**不建议在龙芯老世界安装**。）

---

## 1. 环境确认（10 分钟）

进入麒麟虚拟机，按下面四步确认你的环境长什么样：

```bash
# 1.1 看发行版（应该看到 "Kylin Linux Advanced Server V10"）
cat /etc/os-release

# 1.2 看 CPU 架构（应该输出 loongarch64）
uname -m

# 1.3 看内核版本（4.19.x 表示 Old World）
uname -r

# 1.4 看 glibc 版本（2.28 表示 Old World）
ldd --version | head -1
```

**判断**：
- 输出 `loongarch64` + kernel 4.19.x + glibc 2.28 → **Old World，按本文档走**
- 输出 `loongarch64` + kernel ≥5.19 + glibc ≥2.36 → New World（本文档大部分仍适用，但你可以直接装 rustup）
- 输出 `aarch64` / `x86_64` → **走错文档了，回去看 `README.kyagent.md`**

---

## 2. 装 Python 3.10+（kyagent 最低要求）

麒麟 V10 默认带 Python 3.7，**版本太低**（`pyproject.toml` 要求 `>=3.10`）。

```bash
# 2.1 先看系统源里有没有 python3.10/3.11
sudo dnf list available | grep -E '^python3\.(10|11|12)'

# 2.2 如果有，直接装（替换成实际版本号）
sudo dnf install python3.11 python3.11-pip python3.11-devel

# 2.3 校验
python3.11 --version  # 应输出 Python 3.11.x
```

**如果系统源里没有 Python 3.10+**：

选项 A（推荐）：用龙芯社区 mirror 的 RPM。麒麟社区在 `mirrors.loong64.com` 维护了向后移植的包，**注意要选 Old World 版本**（带 `ow` 或 `kylin` 后缀的，**不要用 `nw` 后缀**）。具体怎么配源问麒麟官方支持。

选项 B：自己源码编译 CPython（耗时 30 分钟+，需要 `dnf groupinstall "Development Tools"` + 一堆 devel 包）。本文档不展开，参考 [CPython 官方编译文档](https://docs.python.org/3/using/unix.html)。

> **谨慎使用** `bjia56/portable-python` 之类的社区预编译版。这些通常按 New World ABI 构建（kernel ≥5.19、glibc ≥2.36），**在 Kylin V10 Old World 上大概率跑不起来或直接段错误**。如果非要用，先 `ldd python3.11` 看依赖的 glibc 版本，>2.28 就别用。

---

## 3. 装 Rust 工具链 — ⚠️ 已弃用（2026-05-22 起）

> **本节已不再需要执行**。当前部署方案（见第 0.3 节）通过：
> - 降级 pydantic 至 v1.10（纯 Python），绕开 `pydantic-core` Rust 编译
> - 锁 anthropic 至 0.39.0 + `--no-deps` 安装，绕开 `jiter` Rust 编译
>
> 已**完全消除对 Rust 工具链的依赖**。直接跳到 [第 4 节](#4-装系统编译依赖)。
>
> 本节内容保留作为历史参考 —— 仅当未来需要升级 pydantic v2（pydantic-core）或启用 anthropic 流式 API（jiter）时再回头查阅。

### 3.1 不要用 rustup 官方脚本

```bash
# ❌ 不要这么干，麒麟 V10 上会失败
curl https://sh.rustup.rs -sSf | sh
# 报错：error: command failed: 'cargo': "kernel too old" / "GLIBC_2.36 not found"
```

**原因**：rustup 默认下载的二进制要求 kernel ≥5.19 + glibc ≥2.36，而麒麟 V10 是 4.19 + 2.28。

### 3.2 用麒麟系统源里的 Rust

```bash
# 装系统打包的 Rust（已经针对 Old World 编译）
sudo dnf install rust cargo

# 校验
cargo --version
rustc --version
```

`pydantic-core` 和 `jiter` 的 MSRV（最低 Rust 版本要求）会随版本变化，**没有固定值**。
原则：拿到 `cargo --version` 输出后，等第 6 步真去 `pip install -e .` 时如果报 `error: package 'pydantic-core' requires rustc X.Y.Z or newer` 再处理。

**如果系统源里的 Rust 版本太老编不动 pydantic-core**：

- 选项 A：换 `pydantic-core` 的旧版本，找一个 MSRV 低于你 Rust 版本的（在 `pyproject.toml` 里把 `pydantic>=2.5` 改成 `pydantic>=2.5,<2.X` 这种约束）
- 选项 B：从龙芯社区 mirror 找新版本 Rust 的 RPM
- 选项 C：源码编译 Rust（极慢，不建议）

---

## 4. 装系统编译依赖

```bash
sudo dnf install -y \
    gcc gcc-c++ make \
    openssl-devel libffi-devel \
    git lsof

# 可选（功能不依赖，但能消除一行编译告警）：libyaml C 扩展头文件
# sudo dnf install -y libyaml-devel
```

说明：
- `gcc gcc-c++ make` → PyYAML 的 `_yaml` libyaml C 扩展会**尝试编译**（见 6.4 步注释），以及某些 PyPI 传递依赖兜底（如 `cryptography` 在 wheel 缺失时回落源码）。注意：PyYAML sdist 编译失败时 setup.py 会自动回落纯 Python，install 仍成功
- `openssl-devel libffi-devel` → 编 `cryptography` 等可能的传递依赖
- `lsof` → kyagent 工具用，麒麟最小化安装可能没装
- `libyaml-devel`（可选） → kyagent 仅用 `yaml.safe_load`，纯 Python `SafeLoader` 足够。**装了**：PyYAML `_yaml` C 扩展编译成功，`yaml.__with_libyaml__=True`，加载略快。**不装**：PyYAML sdist build 时尝试编译 `_yaml` 失败，setup.py 捕获 `CompileError` 自动回落纯 Python，install 仍成功，但 pip 日志会有一行 `fatal error: yaml.h: No such file or directory`（这是 fallback 路径的标志日志，**不是终止性错误**）

---

## 5. 配 pip 镜像（龙芯专用）

```bash
mkdir -p ~/.pip
cat > ~/.pip/pip.conf <<'EOF'
[global]
index-url = https://mirrors.loong64.com/pypi/simple
extra-index-url = https://pypi.org/simple
trusted-host = mirrors.loong64.com
timeout = 120
EOF
```

**为什么用龙芯 mirror**（⚠️ **2026-05-22 起：已非必需**）：

当前部署方案的依赖**全部是 `py3-none-any` 纯 Python wheel**，PyPI 官方源已能满足，**不需要预编译 wheel**。
mirror 反而可能引入旧版本污染（mirror 上可能保留了针对旧 ABI 的同名包，pip 优先级高于 PyPI）。

**建议**：除非 PyPI 官方源在你的网络环境不通，否则**跳过本节**，直接走默认 PyPI。

- 仍想用 mirror 的场景：网络访问 PyPI 不畅；或未来升级 pydantic v2（届时需要预编译 wheel）
- 注意：mirror 上 wheel 的 ABI 兼容性（Old / New World）**未经本文档统一验证**
- 找不到的包会自动 fallback 到官方 PyPI

---

## 6. 装 kyagent

```bash
# 6.1 把项目搬上去（用 git 或者 scp）
sudo mkdir -p /opt/kyagent
sudo chown $USER:$USER /opt/kyagent
git clone <你的仓库地址> /opt/kyagent
cd /opt/kyagent

# 6.2 创建 venv
python3.11 -m venv .venv
source .venv/bin/activate

# 6.3 升级 pip / setuptools / wheel（pip 必须 >=23，老 pip 不识别新 wheel 标签）
pip install --upgrade "pip>=23" setuptools wheel

# 6.4 装项目依赖（2026-05-23 校对：无 Rust 编译；PyYAML 的 _yaml C 扩展会尝试编译，
#                 失败时 setup.py 自动回落纯 Python，install 仍成功，详见步骤 1）
#
# 2026-05-23 起，pyproject.toml 主依赖已经过 LoongArch 安全审计：
#   - 主依赖 = pydantic v1 + PyYAML + typer + rich + httpx (HttpxBackend 用)
#   - anthropic / openai SDK 已移到 [project.optional-dependencies]，不会被
#     `pip install -e .` 默认拉取 → 这是"普通入口"在 LoongArch 上的核心防护
#
# 推荐部署：默认 4 步装完，用 HttpxBackend (llm_backend=deepseek_httpx) 跑 DeepSeek，
# 完全零 Rust 编译依赖。Anthropic backend 是可选附加项（含 jiter Rust，详见末尾）。

# 1) PyYAML：显式强制走 sdist（LoongArch 上 PyPI 没有预编译 wheel，pip 默认也会回落
#    sdist；--no-binary 让这个事实显式化）。最低版本 6.0.1 —— 6.0.0 的 pyproject.toml
#    没给 Cython 上限，Cython 3.x 下 sdist 构建会失败（PyYAML issue #736）。
#    sdist build 依赖 setuptools + wheel + Cython，全是纯 Python（无 C/Rust 编译）。
#    安装时 setup.py 默认会**尝试**编译 _yaml libyaml C 扩展：
#      装了 libyaml-devel → 编译成功，yaml.__with_libyaml__=True
#      没装 libyaml-devel → CompileError 被 setup.py 捕获，自动 fallback 到纯 Python，
#                            yaml.__with_libyaml__=False，install 仍成功
#    （pip 日志会看到一行 'fatal error: yaml.h: No such file or directory'，这是
#     fallback 路径的标志日志，不是终止错误。详见第 4 节 libyaml-devel 说明。）
pip install --no-binary PyYAML "PyYAML>=6.0.1,<7"

# 2) pydantic v1：纯 Python wheel（py3-none-any）
pip install "pydantic>=1.10.13,<2"

# 3) 其余主依赖：typer / rich / httpx —— httpx 是 HttpxBackend 用的（DeepSeek/Qwen 走它）
pip install "typer>=0.12" "rich>=13.7" "httpx>=0.23.0,<1"

# 4) 装 kyagent 本身（默认入口，pyproject 主依赖已审计过 → 不会拉 jiter / openai / anthropic）
pip install -e .

# ---- 可选：仅当你需要 Anthropic Claude 后端时才做（默认推 DeepSeek 走 HttpxBackend）----
# anthropic 0.39 的 Requires-Dist 含 jiter(Rust)；用 --no-deps 跳过 + 手补纯 Python 依赖
# pip install --no-deps "anthropic==0.39.0"
# pip install \
#     "anyio>=3.5.0,<5" \
#     "distro>=1.7.0,<2" \
#     sniffio \
#     "typing-extensions>=4.7,<5"
# # （httpx 已在主依赖步骤 3，不重复装）
#
# 不要用 pip install -e '.[anthropic]'：那会让 pip 解析 anthropic 完整依赖图 → 拉 jiter
# → 在 LoongArch Old World 上触发 Rust 现场编译。必须走上面的 --no-deps 路径。

# 6.5 自检（确认最终 import 路径正确；libyaml=True/False 都合法）
python -c "import pydantic, yaml, httpx, kyagent; \
print('pydantic', pydantic.VERSION); \
print('yaml libyaml=', yaml.__with_libyaml__); \
print('httpx', httpx.__version__); \
print('kyagent OK')"
# 如果装了 anthropic 可选项，加测：
# python -c "import anthropic; print('anthropic', anthropic.__version__)"
```

**预期**：步骤 1-4 **无 Rust 编译**。PyYAML 步骤（步骤 1）的 `_yaml` C 扩展若 libyaml-devel
缺失会**尝试编译并失败**，setup.py 捕获后自动回落纯 Python（install 仍成功，pip 日志含
一行 `fatal error: yaml.h: No such file or directory` —— **这是 fallback 路径的标志，不是
终止错误**）。整体 < 2 分钟（取决于网络）。

可选 anthropic 步骤额外耗时 < 30 秒，仍无 Rust 编译（因为走 `--no-deps`）。

**安装日志中可能看到的"看似报错实则正常"的行**：
- `fatal error: yaml.h: No such file or directory` 后跟 `compilation terminated.` → PyYAML
  尝试编译 `_yaml` C 扩展失败，setup.py 自动 fallback 到纯 Python；只要最终 pip 显示
  `Successfully installed PyYAML-6.0.x` 就是正常的。想消除该日志：第 4 节装 libyaml-devel。

**真正会让安装中断的报错**：
- `error: can't find Rust compiler` 或 `error: Microsoft Visual C++ ... is required` →
  你大概率走了 `pip install -e '.[anthropic]'` 或 `.[openai]` 让 pip 解析了完整依赖图，
  把 jiter 拉了进来。**回到上面"可选 anthropic 步骤"，必须用 `pip install --no-deps`。**
- `AttributeError: cython_sources` 或 `Cython` 相关 sdist 构建错误 → PyYAML 锁版本不对，
  6.0.0 sdist 在 Cython 3.x 下直接构建失败（PyYAML issue #736）。必须用 `PyYAML>=6.0.1`，
  检查步骤 1 命令。
- `Could not find a version that satisfies the requirement pydantic-core` → 不应该出现；
  如果出现说明 pydantic 没正确锁到 <2，检查步骤 2。
- `ModuleNotFoundError: No module named 'anthropic'` 当你跑 `llm_backend=anthropic` → 你
  没装可选 anthropic 步骤。要么走上面 --no-deps 路径装，要么改用 `llm_backend=deepseek_httpx`
  （HttpxBackend，零 anthropic 依赖）。

---

## 7. 配运行账户和 sudoers

仓库自带了部署脚本，**直接用**：

```bash
sudo bash scripts/setup-sudoers.sh
```

这个脚本会做：
1. 创建 `kyagent` 系统账户（不能登陆，只能 `sudo -u kyagent` 切过去）
2. **如果系统存在 `systemd-journal` 组**，把 `kyagent` 加入该组（让它能读 journalctl）；不存在则跳过
3. 安装 `configs/sudoers.kyagent` 到 `/etc/sudoers.d/kyagent`
4. 用 `visudo -cf` 校验语法（失败会自动回滚）
5. 创建 `/var/lib/kyagent` 和 `/var/log/kyagent` 数据/日志目录

**校验**：
```bash
id kyagent  # 应该看到 uid=xxx(kyagent) ...
sudo -u kyagent -n /usr/bin/systemctl is-active sshd  # 应该输出 active/inactive，不应该报 sudoers 错
```

---

## 8. 跑 mock 验证整条链路

**强烈建议先跑 mock**——它不调外部 API，能快速暴露环境问题（Python、依赖、sudoers、工具命令）。

```bash
cd /opt/kyagent
source .venv/bin/activate

# 把审计目录指向第 7 步 setup-sudoers.sh 创建的可写目录。
# 默认配置写 ./var/audit.* 会解析到 /opt/kyagent/var/，
# 该目录归 root/部署用户所有，kyagent 账户写不进去 → 启动即 PermissionError。
export KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
export KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl

# 默认就是 mock 后端
sudo -u kyagent \
    --preserve-env=PATH,KYAGENT_AUDIT_DB,KYAGENT_AUDIT_JSONL \
    .venv/bin/kyagent ask "查 80 端口被谁占了"
```

**预期输出**：会看到 mock LLM 路由 + 真的 `lsof -nP -i TCP:80` 命令执行（不是 `[mock][win32]` 提示，那是 Windows 才会有的）。

**如果看到 `PermissionError: [Errno 13] ... '/opt/kyagent/var'`** → 上面两个 `KYAGENT_AUDIT_*` 环境变量没设置或没穿透给 kyagent 账户。检查 `--preserve-env` 列表。

**如果看到 `ImportError: cannot import name 'resource'`** → 你的 Python 是不是用了奇怪的精简版？`resource` 是 Python 标准库 POSIX 部分，正常应该有。

**如果看到 `command not in whitelist PATH: lsof`** → 第 4 步没装 lsof。

---

## 9. 接真实 LLM 后端

> ✅ **P-OPENAI-DEPS 已解决（2026-05-22）**：龙芯 Old World 部署可通过 `HttpxBackend`（纯 httpx 实现、零 openai SDK 依赖、不触发 jiter Rust 编译）对接 DeepSeek。
>
> 切换开关：环境变量 `KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx`（仅在 deepseek.yaml 生效，其它 yaml 不受影响）。
>
> 详细决策：`implementation-notes.html` "2026-05-22 HttpxBackend 实现 P-OPENAI-DEPS resolved" 条目。
>
> 其他真实后端的状态：
> - **anthropic** 已在第 6.4 节装好，可立即用，但 Anthropic API 在国内访问受限，仅参考对照
> - **Qwen / 智谱 GLM / vLLM / Ollama / Azure OpenAI** 等 OpenAI 协议兼容服务：代码层 `OpenAIBackend` / `HttpxBackend` 都能跑，**当前阶段不在推广范围**；如需启用请参考 `configs/qwen.yaml` 顶部注释，把 `llm_backend` 改成 `qwen_httpx` 即可（同样零 openai SDK 依赖）

当前阶段唯一推广的国产真实后端是 **DeepSeek**（OpenAI 协议兼容，国内可访问）：

```bash
# 9.1 申请 key 后导出（https://platform.deepseek.com）
export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
export KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml

# 龙芯 Old World 关键开关：把 DeepSeek 切到 httpx 传输路径，避开 openai SDK + jiter Rust 编译
export KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx

# audit 路径：默认相对路径会落到 /opt/kyagent/var/，kyagent 账户无写权限；必须显式指向可写目录
export KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
export KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl

# 9.2 跑（注意要把环境变量穿透给 kyagent 账户）
sudo -u kyagent \
    --preserve-env=DEEPSEEK_API_KEY,KYAGENT_CONFIG,KYAGENT_DEEPSEEK_TRANSPORT,KYAGENT_AUDIT_DB,KYAGENT_AUDIT_JSONL,PATH \
    .venv/bin/kyagent ask "查 80 端口被谁占了"
```

**自检**（启动 banner 应显示 backend = `httpx(DeepSeek (V4 Flash))` 而非 `openai(...)`）：

```bash
sudo -u kyagent \
    --preserve-env=DEEPSEEK_API_KEY,KYAGENT_CONFIG,KYAGENT_DEEPSEEK_TRANSPORT,KYAGENT_AUDIT_DB,KYAGENT_AUDIT_JSONL,PATH \
    .venv/bin/kyagent ask "ping"
# 若 banner 显示 backend=openai(...) → 说明 KYAGENT_DEEPSEEK_TRANSPORT 没穿透给 kyagent 账户，
# 检查 --preserve-env 列表。
```

**API key 的安全存放**（生产环境）：

不要写在 shell history 里。两种推荐姿势：

**方式 A：受限权限的环境文件 + 手动加载**

```bash
sudo mkdir -p /etc/kyagent
sudo install -m 0600 -o kyagent -g kyagent /dev/null /etc/kyagent/env
sudo tee /etc/kyagent/env > /dev/null <<'EOF'
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
# 龙芯 Old World 必须显式切到 httpx 传输；不写会回落到默认 deepseek（用 openai SDK）→ 启动即 ModuleNotFoundError
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
# audit 路径必须显式指向 setup-sudoers.sh 创建的可写目录；
# 不写会落到 /opt/kyagent/var/，kyagent 账户无权限，首次启动即报错
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
EOF
```

调用时：
```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; \
    /opt/kyagent/.venv/bin/kyagent ask "查 80 端口"'
```

**方式 B：systemd 服务 + EnvironmentFile（如果要常驻 MCP server）**

把下面这份保存为 `/etc/systemd/system/kyagent-mcp.service`：

```ini
[Unit]
Description=kyagent MCP server
After=network.target

[Service]
Type=simple
User=kyagent
Group=kyagent
WorkingDirectory=/opt/kyagent
EnvironmentFile=/etc/kyagent/env
ExecStart=/opt/kyagent/.venv/bin/kyagent-mcp
Restart=on-failure
RestartSec=5

# 沙箱加固（systemd 自带的，跟 kyagent 内部 rlimit 互补）
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/kyagent /var/log/kyagent

[Install]
WantedBy=multi-user.target
```

启用：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kyagent-mcp
sudo systemctl status kyagent-mcp
sudo journalctl -u kyagent-mcp -f
```

> 注意：MCP 默认用 stdio 通信（同机器进程间）。把它做成 systemd 服务的前提是你要改 `kyagent/mcp/server.py` 用 TCP/Unix Socket 监听——仓库当前**没有 TCP 模式**，所以方式 B 的 unit file 仅供以后改造时用。当下只用方式 A。

---

## 10. 已知问题 & 未验证项

按风险等级倒序：

| # | 风险点 | 说明 | 怎么应对 |
|---|---|---|---|
| 1 | **`resource.setrlimit` 在 Kylin 4.19 内核上未经端到端验证** | 龙芯 + Kylin 内核是 fork 版，POSIX RLIMIT 理论支持但无权威确认 | 跑 `kyagent ask "..."` 跑成功就说明 OK；失败时看 `journalctl` 是否有 `setrlimit failed` |
| 2 | ~~pydantic-core MSRV 与系统 Rust 版本可能不匹配~~ | **2026-05-22 起已规避**：通过降级 pydantic 至 v1.10（纯 Python） | 无需处理；若未来必须升 v2，参考第 3 节历史版本恢复 Rust 路径 |
| 3 | **`mirrors.loong64.com` 上的 wheel 是否覆盖 Old World 未全部确认** | 部分包可能只有 New World wheel | 失败就 fallback 到源码编译，pip 自动会做 |
| 4 | **CPython 3.10+ 在麒麟 V10 上没有官方包** | 上游 CPython 不把 loongarch64 列入 Tier | 用麒麟社区移植版或自己编译 |
| 5 | **lsof 在最小化安装上缺失** | systemd / journalctl 标配，lsof 不一定 | `dnf install lsof` |

---

## 11. 故障排查速查

| 现象 | 可能原因 | 验证命令 |
|---|---|---|
| `pip install` 卡在 pydantic-core | Rust 没装 / 版本太老 | `cargo --version` |
| `pip install` 卡在 PyYAML | libyaml-devel 没装 | `rpm -q libyaml-devel` |
| `kyagent` 命令不存在 | venv 没激活 | `which kyagent` |
| `kyagent ask` 报 "no module named anthropic" | 依赖装失败但没注意到 | `pip list | grep anthropic` |
| `sudo: kyagent: command not found` | PATH 没穿透 | 加 `--preserve-env=PATH` |
| `Permission denied (lsof)` | sudoers 配错 | `sudo visudo -cf /etc/sudoers.d/kyagent` |
| 工具报 `[mock][win32]` | 你在 Windows 上跑而不是麒麟 | `uname -m` |
| `cannot import name 'jiter'` | jiter 编译失败 | 重跑 `pip install -e . -v` 看完整 log |

---

## 12. 实测 checklist（小白照做版）

完成一项打一个勾，记下用时和报错，方便后续优化：

- [ ] 0. 确认是 Old World：`uname -r` 显示 4.19 + `ldd --version` 显示 2.28 — _____ 分钟
- [ ] 1. Python 3.10+ 装上：`python3.11 --version` ok — _____ 分钟
- [ ] 2. Rust 装上：`cargo --version` ok — _____ 分钟
- [ ] 3. 编译依赖装上：`rpm -q gcc libyaml-devel openssl-devel` ok — _____ 分钟
- [ ] 4. pip mirror 配上：`pip config list` 显示 loong64 mirror — _____ 分钟
- [ ] 5. `pip install -e .` 成功：耗时 _____ 分钟（重点关注 pydantic-core / jiter 编译耗时）
- [ ] 6. `setup-sudoers.sh` 跑过：`id kyagent` ok — _____ 分钟
- [ ] 7. mock 跑通：`kyagent ask "..."` 看到合理输出 — _____ 分钟
- [ ] 8. 真实 LLM 跑通：DeepSeek 实际调用成功（`KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx`） — _____ 分钟

**遇到本文档没覆盖的问题**：把完整命令 + 完整报错贴回来，我帮你诊断并更新本文档。

---

## 附录 A：参考资料

- [areweloongyet.com Old/New World 说明](https://areweloongyet.com/en/docs/old-and-new-worlds/)（最权威的 ABI 解读）
- [龙芯社区 PyPI 镜像](https://mirrors.loong64.com/pypi/simple)
- [Python discuss: manylinux/PyPI support for loongarch64](https://discuss.python.org/t/path-towards-manylinux-pypi-support-for-loongarch64/105548)
- [PEP 600 – 永久 manylinux 平台标签](https://peps.python.org/pep-0600/)
- [Rust loongarch 平台支持](https://doc.rust-lang.org/stable/rustc/platform-support/loongarch-linux.html)
- 本仓库内部：`README.kyagent.md`（通用部署）、`scripts/setup-sudoers.sh`（账户脚本）

## 附录 B：本文档维护

事实核查日期：**2026-05-22**。如果你部署时是 6 个月之后，龙芯生态可能已经有变（特别是 PyPI 官方接纳 loongarch64 标签、麒麟 V11 切到 New World）。**请重新核查 0.2 节的 ABI 现状**。
