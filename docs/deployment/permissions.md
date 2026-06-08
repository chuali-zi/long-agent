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
KYAGENT_PLAN_DB=/var/lib/kyagent/plans.db
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
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env
```

如果 key 放在临时文件里：

```bash
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
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

## 写操作授权（opt-in）

默认部署只授予只读查询权限，不包含任何写操作。如果运维场景确实需要日志清理、包管理或进程终止能力，必须在重跑 `setup-sudoers.sh` 时**显式设置对应环境变量**，实现"最小权限、显式授权"的安全设计。

### 日志清理（KYAGENT_ENABLE_LOG_CLEAN=1）

```bash
sudo env KYAGENT_ENABLE_LOG_CLEAN=1 bash scripts/kyagent.sh permissions
```

放行命令范围：

- `journalctl --vacuum-size=<数字>[KMGT]`
- `journalctl --vacuum-time=<数字>(s|min|h|days|weeks|months|years)`
- `kyagent-log-clean <绝对路径>`（清空 `/var/log`、`/var/cache`、`/var/tmp`、`/tmp` 下的普通文件）
- `kyagent-file-delete <绝对路径>`（删除 `/var/log`、`/var/cache`、`/var/tmp`、`/tmp` 下的单个普通文件）

文件清空不再把裸 `truncate -s 0` 连同路径正则交给 sudoers——旧字符类
`/var/log/[A-Za-z0-9._/@-]+` 同时含 `.` 和 `/`，`..` 可匹配，配合 `truncate`
跟随符号链接可越界写任意文件（P1）。现改由 sudoers 只授权
`/usr/local/bin/kyagent-log-clean`（开关打开时随脚本以 root:root 0755 安装）+ 单个
绝对路径参数；该包装器在 OS 层做语义校验，而非"懒惰地禁掉 `..`"：

1. realpath 预检，明显越界的输入在打开前拒绝；
2. 以 `O_NOFOLLOW` 打开调用方原始路径，末端符号链接（含 TOCTOU 调包）即失败；
3. 用已打开 fd 的真实路径（`/proc/self/fd`）做权威越界判定，连中间组件是
   符号链接的越界也封死；
4. 必须是普通文件，再 `ftruncate` 到 0（只清空、不删除）。

sudoers 的参数正则 `^/[A-Za-z0-9._/@-]+$` 仅作粗粒度闸门（绝对路径、安全字符集、
无空白与 shell 元字符），**刻意允许 `..` 语法**，越界与否一律由包装器按解析后的真实
路径判定。工具层 `fs_truncate` 仍做 `posixpath.normpath` 归一化作为纵深防御。

文件删除同样不授权 `rm`、`find -delete` 或通配删除。`fs_delete_file` / `log_delete_file`
只会调用 `/usr/local/bin/kyagent-file-delete`，包装器用 realpath、`O_NOFOLLOW`、fd
真实路径和删除前 inode 复检确认目标仍是允许根目录下的同一个普通文件，然后才 `unlink`。
它不递归、不接受 glob、不删除目录；`log_delete_file` 还会在工具层额外限制 `/var/log`
和常见日志后缀。

### 包管理（KYAGENT_ENABLE_PKG_MGMT=1）

```bash
sudo env KYAGENT_ENABLE_PKG_MGMT=1 bash scripts/kyagent.sh permissions
```

放行命令范围：

- `dnf -y install <包名>`
- `yum -y install <包名>`
- `dnf -y update <包名>`
- `yum -y update <包名>`
- `dnf -y reinstall <包名>`
- `yum -y reinstall <包名>`
- `dnf -y update`
- `yum -y update`
- `dnf -y update --security`
- `yum -y update --security`
- `dnf clean all`
- `yum clean all`
- `rpm --rebuilddb`

包名仅允许 `[A-Za-z0-9._+-]+`，禁止空格、管道、引号等元字符，防止参数注入。
单包安装/更新/重装用锚定参数正则；全量更新、安全更新、清理缓存和 RPM 数据库重建是固定命令，不接受额外参数。

卸载软件包不使用通配授权；如确需卸载，必须额外配置固定 allowlist：

```bash
sudo env KYAGENT_PKG_REMOVE_ALLOWLIST=telnet,ftp \
  bash scripts/kyagent.sh permissions
```

脚本会把每个包渲染成固定 sudoers 命令，例如 `dnf -y remove telnet`，并拒绝把 `kernel`、`systemd`、`glibc`、`openssh-server`、`sudo` 等关键系统包加入卸载 allowlist。

### 进程终止（KYAGENT_ENABLE_PROC_KILL=1）

```bash
sudo env KYAGENT_ENABLE_PROC_KILL=1 bash scripts/kyagent.sh permissions
```

放行命令范围：

- `kill -(TERM|KILL|HUP|INT) <PID>`

信号仅限 TERM/KILL/HUP/INT，PID 仅允许 `>=2` 的纯数字，禁止 `0`、`1`、负数（进程组 kill）和 shell 展开。

### 同时开启多个

```bash
sudo env KYAGENT_ENABLE_LOG_CLEAN=1 KYAGENT_ENABLE_PKG_MGMT=1 \
  bash scripts/kyagent.sh permissions
```

未设置或设为非 `1` 的开关对应的 Cmnd_Alias 不会写入 sudoers，**默认部署不受影响**。

## 一键生产预设（permissions-prod）

手工拼装上面这些环境变量容易出错。`scripts/setup-sudoers-prod.sh`（入口 `kyagent.sh permissions-prod`）是 `setup-sudoers.sh` 的薄封装，把"真实工程上最常见的一组写操作"一次性配好：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh permissions-prod          # 打印授权摘要，交互确认后写入
sudo bash scripts/kyagent.sh permissions-prod --yes    # 跳过确认，非交互一键写入
```

它在默认只读基线之上，额外默认开启（全部是固定命令 + 锚定参数正则，不是通配放行）：

- **日志清理**（`KYAGENT_ENABLE_LOG_CLEAN=1`）：`journalctl --vacuum-size/--vacuum-time`、`kyagent-log-clean <绝对路径>`、`kyagent-file-delete <绝对路径>`（OS 层 realpath+O_NOFOLLOW 校验，限 `/var/log`、`/var/cache`、`/var/tmp`、`/tmp`，防 `..` 越界与符号链接）
- **包管理**（`KYAGENT_ENABLE_PKG_MGMT=1`）：`dnf/yum -y install <pkg>`、`dnf/yum -y update <pkg>`、`dnf/yum -y reinstall <pkg>`、`dnf/yum -y update`、`dnf/yum -y update --security`、`dnf/yum clean all`、`rpm --rebuilddb`；卸载仅在设置 `KYAGENT_PKG_REMOVE_ALLOWLIST` 时按固定包名授权
- **进程终止**（`KYAGENT_ENABLE_PROC_KILL=1`）：`kill -(TERM|KILL|HUP|INT) <pid>=2+`
- **重启常见服务**（`KYAGENT_SERVICE_ALLOWLIST` 默认值）：`systemctl restart/reload` 对 nginx、httpd、sshd、firewalld、chronyd、crond、rsyslog、mariadb、mysqld、postgresql、redis、docker、php-fpm（仅 restart/reload，不含 stop/disable/mask）

危险动作仍被层层拦截：删 `kernel/systemd/glibc/openssh-server` 等关键包、清空 `/etc`、`kill pid<2` 会在工具层 + 安全护栏被拒；最终 sudoers 还要过 `visudo` 校验、失败自动回滚。生产预设默认不授权通配卸载包。

按需裁剪——任意开关设为 `0` 关闭，`KYAGENT_SERVICE_ALLOWLIST` 覆盖服务清单：

```bash
sudo env KYAGENT_ENABLE_PROC_KILL=0 \
         KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
  bash scripts/kyagent.sh permissions-prod --yes
```

脚本会先打印将要授权的完整范围；非交互环境（无 tty）必须显式加 `--yes`，否则拒绝执行，避免无人值守时误授权。

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
KYAGENT_PLAN_DB=/var/lib/kyagent/plans.db
```

确认目录归属：

```bash
sudo -u kyagent test -w /var/lib/kyagent
sudo -u kyagent test -w /var/log/kyagent
```
