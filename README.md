# kyagent

kyagent 是面向 A2 赛题的安全智能运维 Agent：在 LoongArch Linux + 麒麟高级服务器版 V11 上，通过自然语言调用受控 Tools，完成系统感知、安全校验、最小权限执行和审计追踪。

这份 README 只回答一个问题：**从 Gitee 拉下代码后，怎样一路启动 Web，并问出 `which process used the most cpu`，让 Agent 返回真实系统结果。** 详细原理和排障见 `docs/`。

## 开发者快速测试
本地开发/评测环境先安装开发依赖；这个入口默认会安装 `.[dev]`（含 `pytest`、`pytest-asyncio`、`ruff`），然后用项目推荐参数跑测试。等价的直接 pytest 命令也列在下面：

```bash
bash scripts/kyagent.sh install
bash scripts/kyagent.sh test
python -m pytest -q --basetemp pytest_tmp_run -p no:cacheprovider
```

## Benchmarks Web 实测

要在 Web 里逐个真实测试 RealOps benchmark，可以先一次性布置所有场景，打开 Web 后按 `benchmarks/*/manifest.yaml` 里的 `prompt` 逐个提问，最后统一验收并拆除：

```bash
sudo bash benchmarks/setup-all.sh
bash benchmarks/verify-all.sh --pre

# 另开终端启动 Web，然后在页面里逐个提问/修复
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env

# Web 测完后统一看结果，PERFECT 才算严格通过
bash benchmarks/verify-all.sh

# 测试结束清场
sudo bash benchmarks/teardown-all.sh
```

三个一键脚本分别是：`benchmarks/setup-all.sh` 只布置场景，`benchmarks/verify-all.sh` 只验收并输出 TSV 汇总，`benchmarks/teardown-all.sh` 只拆除场景。默认会使用真实感路径如 `/var/log/web-app01`、`/var/log/auth-api01`、`/tmp/shop-ops`，因此布置和拆除需要 root；脚本会给各场景显式隔离 runtime/root，避免同时打开所有场景时互相覆盖。

在 LoongArch/Kylin 目标机上，想把下面 README 链路一次性顺完，可以直接运行新增脚本。默认只交互输入一次 DeepSeek API key；脚本会复制到 `/opt/kyagent`、安装 Web 依赖、切到最大测试 sudoers、重写 `/etc/kyagent/env`，并把 `KYAGENT_WEB_ADMIN_TOKEN=admin123` 写在 `DEEPSEEK_API_KEY` 同一个环境文件里，随后执行快速验收和真实 CLI 提问，最后启动 Web。

```bash
sudo bash scripts/developer-quick-test.sh
```

这是最大测试权限入口，只用于开发验收。测试结束后请恢复正常权限：

```bash
sudo bash /opt/kyagent/scripts/setup-sudoers.sh
```

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

默认 sudoers 只放行只读查询。需要"清理日志、装包、显式白名单卸包、终止进程、重启常见服务"等写能力时，用一键生产预设 `sudo bash scripts/kyagent.sh permissions-prod --yes` 一次配好（危险动作仍被层层拦截）；授权范围与裁剪见 [最小权限配置](docs/deployment/permissions.md)。

## sudoers 权限档位

默认 `scripts/setup-sudoers.sh` 只放行必要 root 只读查询；`scripts/setup-sudoers-prod.sh --yes` 预开常见生产写操作；`scripts/setup-sudoers-max-test.sh --yes` 仅用于极限测试，等价于完整免密 root。三者都会覆盖 `/etc/sudoers.d/kyagent`，测试后请切回最小权限或生产预设，并用 `sudo visudo -cf /etc/sudoers.d/kyagent`、`sudo -l -U kyagent` 验证。

## 配置 API Key

推荐把 DeepSeek key 放进 root 可读的临时密钥文件，再让脚本写入 `/etc/kyagent/env`：

```bash
sudo sh -c 'printf "%s\n" "your-deepseek-api-key" > /root/deepseek.key'
sudo chmod 600 /root/deepseek.key
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

也可以手工编辑：

```bash
sudo editor /etc/kyagent/env
```

至少确认里面有这些值。这里不要只写 DeepSeek key，Web 的 admin access token 也必须写进去：

```bash
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_EXECUTOR_ACCOUNT=kyagent
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
KYAGENT_PLAN_DB=/var/lib/kyagent/plans.db
DEEPSEEK_API_KEY=your-deepseek-api-key
KYAGENT_WEB_ADMIN_TOKEN=admin123
```

`DEEPSEEK_API_KEY` 和 `KYAGENT_WEB_ADMIN_TOKEN` 都只从环境变量读取，不从 YAML 或 `kyagent.json` 读取。Web 页面调用 API 时会把页面里填写的 token 作为 `Authorization: Bearer <token>` 发送；如果 `/etc/kyagent/env` 里没有 `KYAGENT_WEB_ADMIN_TOKEN`，或者页面里没有填写同一个值，就拿不到 `admin` 角色，管理类命令、审核、审计等需要权限的接口会被拒绝。

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

有桌面环境时脚本会自动打开浏览器；没有桌面 opener 时，终端会打印手工访问地址。打开页面后，先在右侧/浮层里的 `Bearer token` 输入框填入 `/etc/kyagent/env` 中的 `KYAGENT_WEB_ADMIN_TOKEN`（上面的示例是 `admin123`）。这个 token 是 Web access token，不是 DeepSeek API key；不填它只能走本地开发放行的少数接口，不能以 admin 身份执行需要权限的 Web 操作。

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
