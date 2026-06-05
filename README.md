# kyagent

kyagent 是面向 A2 赛题的安全智能运维 Agent：在 LoongArch Linux + 麒麟高级服务器版 V11 上，通过自然语言调用受控 Tools，完成系统感知、安全校验、最小权限执行和审计追踪。

这份 README 只回答一个问题：**从 Gitee 拉下代码后，怎样一路启动 Web，并问出 `which process used the most cpu`，让 Agent 返回真实系统结果。** 详细原理和排障见 `docs/`。

## 一条正式演示链路

以下命令假设你在 LoongArch/Kylin 目标机上操作，并且要接真实 DeepSeek key。请把 Gitee 地址替换成实际仓库地址。

```bash
git clone https://gitee.com/<your-org>/kyagent.git kyagent
cd kyagent

sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent

sudo bash scripts/install-loongarch.sh --yes --with-web
```

安装器会创建受限运行用户 `kyagent`、虚拟环境 `/opt/kyagent/.venv`、sudoers 最小权限白名单 `/etc/sudoers.d/kyagent`、审计目录 `/var/lib/kyagent` 和 `/var/log/kyagent`，并写入生产环境文件 `/etc/kyagent/env`。

## 配置 API Key

推荐把 DeepSeek key 放进 root 可读的临时密钥文件，再让脚本写入 `/etc/kyagent/env`：

```bash
sudo sh -c 'printf "%s\n" "sk-your-deepseek-key" > /root/deepseek.key'
sudo chmod 600 /root/deepseek.key
sudo bash scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

也可以手工编辑：

```bash
sudo editor /etc/kyagent/env
```

至少确认里面有这些值：

```bash
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_EXECUTOR_ACCOUNT=kyagent
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
DEEPSEEK_API_KEY=sk-your-deepseek-key
```

key 只从环境变量读取，不从 YAML 或 `kyagent.json` 读取。

## 激活虚拟环境

需要手工跑 CLI 或排障时，可以激活项目虚拟环境：

```bash
source /opt/kyagent/.venv/bin/activate
kyagent tools list
deactivate
```

正式以受限用户运行时，一般不用手工 activate，直接加载 `/etc/kyagent/env` 并调用 venv 里的入口：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
```

## 启动 Web

生产演示不要传 `--mock`。下面这条命令会读取 `/etc/kyagent/env`，使用真实 DeepSeek 后端和受限执行账户：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

默认监听：

```text
http://127.0.0.1:8000
```

有桌面环境时脚本会自动打开浏览器；没有桌面 opener 时，终端会打印手工访问地址。

打开页面后，在输入框里问：

```text
which process used the most cpu
```

Agent 应该会调用进程相关工具读取当前系统进程信息，并返回 CPU 占用最高的进程名称、PID、CPU 占比等摘要。返回内容取决于机器当时的 `ps/top` 数据，不是固定答案。

## 快速验收

在 `/opt/kyagent` 下依次检查：

```bash
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
sudo -u kyagent test -r /opt/kyagent/scripts/kyagent.sh
sudo -u kyagent test -w /var/lib/kyagent
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
```

也可以先用 CLI 直接问一次：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "which process used the most cpu"'
```

## 离线冒烟测试

没有 DeepSeek key 时，只能验证 Web 页面、SSE 和 Agent 链路能启动，不能证明真实问答能力：

```bash
bash scripts/kyagent.sh install
bash scripts/kyagent.sh web --install-web --mock
```

Web 依赖已装好时：

```bash
bash scripts/kyagent.sh web --mock
```

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

开发态常用入口：

```bash
bash scripts/kyagent.sh chat
bash scripts/kyagent.sh tui
kyagent tui
kyagent tools list
```

## 常见问题

### `Permission denied`

正式部署不要从 `/home/<user>/...` 私有目录里用 `sudo -u kyagent` 启动。请复制到 `/opt/kyagent`：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
sudo -u kyagent test -r /opt/kyagent/scripts/kyagent.sh
```

### `sudo: a password is required`

说明 sudoers 免密白名单没有安装好，或目标命令不在白名单里：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh permissions
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

生产安装不要使用 `--skip-sudoers`，除非你明确要手工接管运行账户、sudoers 白名单和审计目录权限。

### Web 或 TUI 写审计失败

确认 `/etc/kyagent/env` 指向生产审计目录，并且目录归属 `kyagent`：

```bash
grep KYAGENT_AUDIT /etc/kyagent/env
sudo -u kyagent test -w /var/lib/kyagent
sudo -u kyagent test -w /var/log/kyagent
```

## 文档导航

详细部署见 [LoongArch/Kylin 部署](docs/deployment/loongarch.md)、[Web 控制台](docs/deployment/web.md) 和 [最小权限配置](docs/deployment/permissions.md)。项目能力、架构和安全模型见 [完整项目说明](docs/kyagent/README.md)、[架构](docs/kyagent/architecture.md)、[安全模型](docs/kyagent/safety-model.md) 和 [当前状态](docs/status/current.md)。
