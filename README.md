# kyagent

kyagent 是面向麒麟/Linux 的安全智能运维 Agent。它通过自然语言查询系统状态、调用受控运维工具、执行安全拦截，并把完整链路写入审计日志。

默认 LLM backend 是 `deepseek_httpx`。真实调用需要 `DEEPSEEK_API_KEY`；离线演示请显式使用 `--mock`。

## 快速开始

普通 Linux 开发或演示环境：

```bash
bash scripts/kyagent.sh install
bash scripts/kyagent.sh tools
bash scripts/kyagent.sh chat
```

安装完成后，按需要单独启动交互入口：

| 入口 | 命令 | 用途 |
| --- | --- | --- |
| Chat | `bash scripts/kyagent.sh chat` | 简洁的持续对话 |
| TUI | `bash scripts/kyagent.sh tui` | 流式终端界面，适合现场演示 |
| Web | `bash scripts/kyagent.sh web --mock` | 启动浏览器前端离线演示 |
| CLI | `kyagent ask "80 端口被谁占了？"` | 单轮查询 |

Web 启动后，在浏览器打开 `http://127.0.0.1:8000`。安装 Web 依赖、生产配置和局域网访问方式见 [Web 控制台部署](docs/deployment/web.md)。

## 最小权限

生产环境建议让 Agent 使用受限系统账户运行：

```bash
sudo bash scripts/kyagent.sh permissions
```

这个命令会创建 `kyagent` 账户，校验并安装 `/etc/sudoers.d/kyagent` 最小权限白名单，再准备审计目录。它不会授予任意 root 权限。执行内容、验证命令和 sudo 版本排障见 [最小权限部署](docs/deployment/permissions.md)。

配置完成后，可使用受限账户启动：

```bash
sudo -u kyagent bash scripts/kyagent.sh chat
sudo -u kyagent bash scripts/kyagent.sh tui
sudo -u kyagent bash scripts/kyagent.sh web --env-file /etc/kyagent/env
```

统一入口会为 `chat`、`tui` 和 `tools` 自动加载可读的 `/etc/kyagent/env`。

## LoongArch/Kylin

LoongArch/Kylin 推荐使用专用部署脚本。默认路径使用 `deepseek_httpx`、`pydantic v1` 和纯 httpx，不安装 OpenAI/Anthropic SDK：

```bash
sudo bash scripts/install-loongarch.sh --dry-run --yes
sudo bash scripts/install-loongarch.sh --yes
```

安装 Web 依赖时显式增加：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

Old World 依赖边界、完整参数和验收步骤见 [LoongArch/Kylin 部署审查](docs/deployment/loongarch.md)。

## 常用命令

```bash
kyagent tools list
kyagent safety test "rm -rf /"
kyagent ask "查下 CPU 占用最高的进程"
kyagent chat
kyagent tui
kyagent audit list
kyagent audit show <trace-id>
kyagent mcp serve
```

TUI 基于 `prompt_toolkit + rich`。内置 `/tools`、`/audit`、`/reset`、`/exit` 命令，适合观察流式思考、工具调用和审计回放。

## 配置 LLM

开发环境可以直接导出 DeepSeek key：

```bash
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
export KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
kyagent ask "80 端口被谁占了？"
```

生产环境建议把相同变量写入 `/etc/kyagent/env`，再通过受限账户加载。完整模板见 [LoongArch/Kylin 部署审查](docs/deployment/loongarch.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [最小权限部署](docs/deployment/permissions.md) | sudoers 安装、边界、验证和故障排查 |
| [Web 控制台部署](docs/deployment/web.md) | 浏览器前端安装、启动、生产参数和审核接口 |
| [LoongArch/Kylin 部署审查](docs/deployment/loongarch.md) | Old World 依赖边界和一键部署 |
| [完整项目说明](docs/kyagent/README.md) | 工具集、安全护栏、TUI、Web 和测试 |
| [架构文档](docs/kyagent/architecture.md) | 模块和数据流 |
| [安全模型](docs/kyagent/safety-model.md) | 意图过滤、Guardrail、沙箱和 sudoers |
| [当前状态](docs/status/current.md) | 当前实现状态 |

## Windows

Windows 可以验证 Python、LLM、CLI、TUI 和 Web 路径；Linux 系统工具执行会返回 mock 占位结果：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
$env:KYAGENT_LLM_BACKEND = "mock"
python -m kyagent tui
```

`scripts/kyagent.sh`、sudoers 和 LoongArch 部署脚本面向 Linux。Windows 上请使用 `python -m kyagent ...`。
