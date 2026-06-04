# 最小权限部署

这一页只讲权限。先记住一句话：

```text
kyagent 用户负责长期运行；sudoers 只给它少量 root 查询能力；/opt 和 /var 目录权限保证它能读代码、写审计。
```

## 三种权限不要混

| 名称 | 控制什么 | 常见错误 |
| --- | --- | --- |
| 文件权限 | `kyagent` 能不能读 `/opt/kyagent`、脚本、venv、`/etc/kyagent/env` | `Permission denied` |
| sudoers 权限 | `kyagent` 能不能免密执行少数 root 命令 | `sudo: a password is required` |
| 审计目录权限 | `kyagent` 能不能写审计 DB 和 JSONL | `unable to open database file` |

不要把这三类问题都当成“tool 白名单问题”。它们发生在不同层。

## 一键安装权限

在项目部署目录执行：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh permissions
```

它会做这些事：

1. 检查 `sudo`、`visudo` 和 `sudo >= 1.9.10`。
2. 创建受限运行账户 `kyagent`。
3. 尽量把 `kyagent` 加入 `systemd-journal` 组，方便读 journal。
4. 用 `visudo -cf` 校验并安装 `/etc/sudoers.d/kyagent`。
5. 创建 `/var/lib/kyagent`、`/var/log/kyagent`、`/var/log/sudo-io`。

验证：

```bash
LC_ALL=C sudo -V | head -n 1
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
sudo -u kyagent test -r /opt/kyagent/scripts/kyagent.sh
sudo -u kyagent test -w /var/lib/kyagent
```

## 为什么要放在 `/opt/kyagent`

生产模式不要从 `/home/<user>/project` 里启动：

```bash
sudo -u kyagent bash scripts/kyagent.sh tui
```

`sudo -u kyagent` 会把当前进程切换成 `kyagent` 用户。它通常不能进入你的私人 home 目录，所以会被 Linux 文件权限拒绝。

推荐：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh tools
```

## `/etc/kyagent/env` 是什么

`/etc/kyagent/env` 是生产启动配置。它不是代码，不是 YAML，而是一组启动环境变量：

```bash
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_EXECUTOR_ACCOUNT=kyagent
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
DEEPSEEK_API_KEY=sk-...
```

安装器会生成模板。拿到 key 后补 `DEEPSEEK_API_KEY` 即可。

启动时加载：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
```

`scripts/kyagent.sh` 也会自动尝试读取 `/etc/kyagent/env`。

## 快速重写生产配置

如果账户和 sudoers 已经装好，只是想重新生成 `/etc/kyagent/env`，不要重跑完整安装器：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh prod-env
```

如果 key 放在临时文件里：

```bash
sudo bash scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

这条命令只负责生产启动配置、审计 HMAC key 和审计目录。它不会改 tool 白名单，也不会安装 Python 依赖。

## sudoers 放行什么

默认 sudoers 只放行确实需要 root 的只读查询，例如：

- `dmidecode -s system-product-name`
- `iptables -L -n -v --line-numbers`
- `nft list ruleset`
- `smartctl -H -A -- /dev/...`
- `sudo -l -U kyagent`

不要为了省事写：

```text
kyagent ALL=(ALL) NOPASSWD: ALL
```

也不要粗放放行整个 `systemctl`。这会破坏赛题要求的“最小权限代理执行”。

## 服务变更 allowlist

默认不允许重启或 reload 任意 systemd unit。确实需要给业务服务开放时，在部署阶段显式列出：

```bash
sudo env KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
  bash scripts/kyagent.sh permissions
```

脚本只接受普通 service unit，并拒绝核心系统服务、`.target`、`.socket` 和 shell 元字符。生成的 sudoers 是固定命令列表，不是通配所有服务。

## 自定义运行账户

默认账户是 `kyagent`。需要换名时：

```bash
sudo env KYAGENT_USER=opsagent bash scripts/kyagent.sh permissions
```

同时要让 `/etc/kyagent/env` 里的账户保持一致：

```bash
KYAGENT_EXECUTOR_ACCOUNT=opsagent
```

`install-loongarch.sh` 会自动同步这个变量。

## 常见错误

### `Permission denied`

先问：是不是从私人目录启动？

处理：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
sudo -u kyagent test -r /opt/kyagent/scripts/kyagent.sh
```

### `sudo: a password is required`

说明 sudoers 免密白名单没装好，或目标命令不在白名单里。

处理：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh permissions
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

### Web 或 TUI 写审计失败

确认 `/etc/kyagent/env` 指向生产审计目录：

```bash
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
```

确认目录归属：

```bash
sudo -u kyagent test -w /var/lib/kyagent
sudo -u kyagent test -w /var/log/kyagent
```
