# kyagent

kyagent 是面向麒麟高级服务器版与 LoongArch Linux 的安全智能运维 Agent。正式部署以 LoongArch64 Linux 为目标，提供自然语言运维、受控工具调用、安全拦截、最小权限执行和完整审计链路。

## 一键 Web

LoongArch Linux 生产安装：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

生产模式建议固定部署到 `/opt/kyagent`。不要从 `/home/<user>/...` 这类私有目录里用 `sudo -u kyagent` 启动，否则 `kyagent` 账户可能没有目录穿透或脚本读取权限。

离线演示：

```bash
bash scripts/kyagent.sh web --install-web --mock
```

`web` 会同时启动 FastAPI 后端和浏览器前端，并自动打开浏览器。没有桌面环境或找不到 opener 时，后端继续运行并打印手工访问地址。默认只监听 `127.0.0.1:8000`；局域网模式会 fail closed，必须显式开启认证并配置角色 token。完整边界见 [Web 控制台部署](docs/deployment/web.md)。

## 分开启动

需要调试或交给服务管理器编排时，使用分开的脚本：

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

前端是后端同源托管的静态页面，不需要额外 Node.js 进程。

## LoongArch 部署

先审阅命令，再执行安装：

```bash
sudo bash scripts/install-loongarch.sh --dry-run --yes
sudo bash scripts/install-loongarch.sh --yes --with-web
```

默认路径使用 `deepseek_httpx`、`pydantic v1` 纯 Python 安装方式和标准版 `uvicorn`，不安装 OpenAI/Anthropic SDK、`jiter`、`pydantic-core` 或 `uvicorn[standard]`。Old World / New World 边界、系统包范围和验收命令见 [LoongArch Linux 部署审查](docs/deployment/loongarch.md)。

## 常用入口

| 入口 | 命令 |
| --- | --- |
| 本地开发安装 | `bash scripts/kyagent.sh install` |
| 工具清单 | `bash scripts/kyagent.sh tools` |
| Chat | `bash scripts/kyagent.sh chat` |
| TUI | `bash scripts/kyagent.sh tui` |
| Web 一键启动 | `bash scripts/kyagent.sh web --mock` |
| 仅 Web 后端 | `bash scripts/kyagent.sh web-backend --mock` |
| 仅打开 Web 页面 | `bash scripts/kyagent.sh web-open` |

激活虚拟环境后也可以直接执行 `kyagent tui`、`kyagent chat` 和 `kyagent tools list`。

真实调用需要 `DEEPSEEK_API_KEY`；离线演示请显式使用 `--mock`。

## 配置 DeepSeek Key

key **只从环境变量 `DEEPSEEK_API_KEY` 读取**，不从 YAML、也不从项目根 `kyagent.json` 读取（`kyagent.json` 只接受 `llm_backend` 字段，残留的 key 字段会被忽略）。`configs/deepseek.yaml` 里的 `api_key_env: DEEPSEEK_API_KEY` 存的是“环境变量名”，不是 key 本体。缺 key 时直接报错，不会静默降级；离线演示请显式 `--mock`。

key 申请：<https://platform.deepseek.com>。

### 本地开发 / 临时

```bash
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
kyagent ask "80 端口被谁占了？"
```

### LoongArch 生产

龙芯 Old World 必须走纯 httpx 路径（绕开 openai SDK 和 jiter）。把 key 写进 `/etc/kyagent/env`（`0600`，属主 `kyagent`），由 systemd 或 `source` 注入：

```bash
sudo install -m 0600 -o kyagent -g kyagent /dev/null /etc/kyagent/env
sudo sh -c 'cat > /etc/kyagent/env' <<'EOF'
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
DEEPSEEK_API_KEY=sk-...
EOF
sudo chown kyagent:kyagent /etc/kyagent/env

# 自检
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "查 80 端口被谁占了"'
```

`install-loongarch.sh` 会自动生成 `/etc/kyagent/env` 模板（含上面前两行），拿到 key 后补 `DEEPSEEK_API_KEY=sk-...` 即可。

### 相关环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | key 本体，必需 | 无，缺则报错 |
| `KYAGENT_CONFIG` | 指向 `configs/deepseek.yaml` | 自动查找 `default.yaml` |
| `KYAGENT_DEEPSEEK_TRANSPORT` | `deepseek`（SDK）/ `deepseek_httpx`（龙芯） | `deepseek` |
| `KYAGENT_DEEPSEEK_MODEL` | 模型 ID | `deepseek-v4-flash` |
| `KYAGENT_DEEPSEEK_BASE_URL` | 反代地址 | 空 → `https://api.deepseek.com` |

完整的龙芯部署、env 写入和验收命令见 [LoongArch Linux 部署审查](docs/deployment/loongarch.md)。

## 最小权限

LoongArch 生产模式必须安装 `kyagent` 受限账户和 `/etc/sudoers.d/kyagent` 免密白名单。否则工具需要 root 只读信息时会失败，并在对话里看到 `sudo: a password is required`。

```bash
sudo bash scripts/kyagent.sh permissions
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

不要在生产安装里使用 `--skip-sudoers`；它只适合本地静态检查或自己手工接管权限配置的场景。

默认 sudoers 不允许重启或 reload 任意 systemd unit。确需服务变更时，在部署阶段显式配置 allowlist：

```bash
sudo env KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
  bash scripts/kyagent.sh permissions
```

完整边界、验证和排障见 [最小权限部署](docs/deployment/permissions.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [LoongArch Linux 部署审查](docs/deployment/loongarch.md) | LoongArch64 安装、依赖边界、验收和故障排查 |
| [Web 控制台部署](docs/deployment/web.md) | 一键启动、分开脚本、自动弹页和局域网访问 |
| [最小权限部署](docs/deployment/permissions.md) | sudoers 安装、服务 allowlist 和验证 |
| [完整项目说明](docs/kyagent/README.md) | 工具集、安全护栏、TUI、Web 和测试 |
| [架构文档](docs/kyagent/architecture.md) | 模块和数据流 |
| [安全模型](docs/kyagent/safety-model.md) | 意图过滤、Guardrail、沙箱和 sudoers |
| [当前状态](docs/status/current.md) | 当前实现状态 |
