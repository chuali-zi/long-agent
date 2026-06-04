# kyagent

kyagent 是面向 A2 赛题的安全智能运维 Agent：在 LoongArch Linux + 麒麟高级服务器版 V11 上，通过自然语言调用受控 Tools，完成系统感知、安全校验、最小权限执行和审计追踪。

这份根 README 只回答一个问题：**我现在该怎么跑起来？** 详细原理和排障放在 `docs/` 下。

## 先选你的场景

| 场景 | 你该看哪里 | 适合谁 |
| --- | --- | --- |
| 比赛实机 Web 演示 | 本文“正式演示” | 接 DeepSeek key、给评委演示 |
| 在 LoongArch/Kylin 正式部署 | [LoongArch 部署](docs/deployment/loongarch.md) | 安装、验收、实机排障 |
| Web 控制台启动和局域网访问 | [Web 控制台部署](docs/deployment/web.md) | B/S 演示、浏览器审核 |
| 离线冒烟测试 | 本文“离线冒烟测试” | 本地开发、没 key 时确认页面能开 |
| 权限、sudoers、`Permission denied` | [最小权限部署](docs/deployment/permissions.md) | 排查工具执行失败 |
| 项目能力和架构说明 | [完整项目说明](docs/kyagent/README.md) | 写文档、答辩、二次开发 |

## 正式演示

比赛实机演示建议接真实 DeepSeek key。Web 端不是默认离线；只要不传 `--mock`，它会读取 `/etc/kyagent/env` 并使用 `deepseek_httpx`。

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent

sudo bash scripts/install-loongarch.sh --yes --with-web
sudo bash scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

换 key 或重写生产环境变量时，只重跑：

```bash
sudo bash scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

Web 会自动打开浏览器；没有桌面环境或找不到 opener 时，会继续运行并打印手工访问地址。

## LoongArch Linux 正式部署

正式部署建议固定到 `/opt/kyagent`，不要从 `/home/<user>/...` 私有目录里用 `sudo -u kyagent` 启动。

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent

sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

安装器会完成 LoongArch/Kylin 依赖、`kyagent` 账户、sudoers 白名单、审计目录和 `/etc/kyagent/env`。

安装器 dry-run：`sudo bash scripts/install-loongarch.sh --dry-run --yes`；只重写生产配置：`sudo bash scripts/kyagent.sh prod-env`

## 离线冒烟测试

不需要 DeepSeek key、不依赖真实 LLM，只适合确认 Web 页面、SSE 和 Agent 链路能启动：

```bash
bash scripts/kyagent.sh install
bash scripts/kyagent.sh web --install-web --mock
```

Web 依赖已装好时：`bash scripts/kyagent.sh web --mock`

分开启动时使用：

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

正式演示不要传 `--mock`。

## TUI 和 CLI

安装后可以使用 TUI：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

开发态也可以直接：

```bash
bash scripts/kyagent.sh chat
bash scripts/kyagent.sh tui
kyagent tui
kyagent tools list
```

`kyagent tui` 支持 `/tools`、`/audit`、`/reset`、`/exit` 和高风险操作确认。

## DeepSeek Key

真实后端需要 `DEEPSEEK_API_KEY`。key 只从环境变量读取，不从 YAML 或 `kyagent.json` 读取。

开发态：

```bash
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
kyagent ask "80 端口被谁占了？"
```

LoongArch 生产态：

```bash
sudo editor /etc/kyagent/env
```

补入：

```bash
DEEPSEEK_API_KEY=sk-...
```

安装器默认写入 `KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml` 和 `KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx`。LoongArch Old World 默认走 `deepseek_httpx`，避免 OpenAI SDK、`jiter`、`pydantic-core` 等 Rust/native 依赖。

## 权限快速判断

如果看到：

```text
sudo: a password is required
```

说明 `/etc/sudoers.d/kyagent` 免密白名单没有安装好，或安装时用了 `--skip-sudoers`。先在 `/opt/kyagent` 下验证：

```bash
sudo bash scripts/kyagent.sh permissions
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

如果看到：

```text
Permission denied
```

先判断是不是从私人目录用 `sudo -u kyagent` 启动。正式测试请把项目复制到 `/opt/kyagent`。

更多排障见 [最小权限部署](docs/deployment/permissions.md)。

## 比赛交付建议

赛题要求“软件安装包及部署文档”和“软件源代码文件（压缩包）”。本项目不建议交付 Windows `.exe`，而应交付 LoongArch/Kylin 可部署包：

```text
kyagent-release.tar.gz
  kyagent/
  configs/
  scripts/
  requirements-loongarch.txt
  requirements-loongarch-web.txt
  pyproject.toml
  README.md
  docs/
```

评委拿到后应能按本文 LoongArch Linux 部署命令安装并启动 Web 控制台。

## 文档导航

| 文档 | 作用 |
| --- | --- |
| [docs/deployment/loongarch.md](docs/deployment/loongarch.md) | LoongArch/Kylin 正式部署、依赖边界、验收命令 |
| [docs/deployment/web.md](docs/deployment/web.md) | Web 一键启动、分开启动、局域网认证 |
| [docs/deployment/permissions.md](docs/deployment/permissions.md) | 运行账户、sudoers、审计目录、权限排障 |
| [docs/kyagent/README.md](docs/kyagent/README.md) | 功能、架构、安全链路和工具说明 |
| [docs/kyagent/architecture.md](docs/kyagent/architecture.md) | 模块和数据流 |
| [docs/kyagent/safety-model.md](docs/kyagent/safety-model.md) | 意图过滤、Guardrail、执行沙箱 |
| [docs/status/current.md](docs/status/current.md) | 当前交付状态 |
