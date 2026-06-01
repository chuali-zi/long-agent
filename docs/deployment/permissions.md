# kyagent 最小权限部署

kyagent 不应以 root 身份持续运行。推荐创建独立受限账户，并只通过 sudoers 白名单放行确实需要 root 的查询。

## 一键配置

```bash
sudo bash scripts/kyagent.sh permissions
```

脚本会：

1. 检查 `visudo`、`sudo` 和 `sudo >= 1.9.10`。
2. 使用 `LC_ALL=C sudo -V` 获取稳定版本输出。
3. 创建默认运行账户 `kyagent`，并在存在时加入 `systemd-journal` 组。
4. 用 `visudo -cf` 校验临时文件，再安装 `/etc/sudoers.d/kyagent`。
5. 对安装后的文件再次校验，失败时自动回滚。
6. 创建 `/var/lib/kyagent`、`/var/log/kyagent` 和 `/var/log/sudo-io`。

默认模板 [configs/sudoers.kyagent](../../configs/sudoers.kyagent) 不授予任意 systemd 服务变更能力。

## 服务 allowlist

确需让 Agent 重启或 reload 少量业务服务时，在安装阶段显式指定：

```bash
sudo env KYAGENT_SERVICE_ALLOWLIST=nginx.service,sshd.service \
  bash scripts/kyagent.sh permissions
```

脚本只接受普通服务名或 `.service` unit，并拒绝 `.target`、`.socket`、核心 systemd 服务和 shell 元字符。生成的 sudoers 规则逐项列出固定命令，不使用任意 unit 正则。

## 验证

```bash
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
sudo -u kyagent bash scripts/kyagent.sh tools
```

生产配置写入 `/etc/kyagent/env` 后，可使用受限账户启动：

```bash
sudo -u kyagent bash scripts/kyagent.sh chat
sudo -u kyagent bash scripts/kyagent.sh tui
sudo -u kyagent bash scripts/kyagent.sh web --env-file /etc/kyagent/env
```

## 自定义账户

默认账户名是 `kyagent`。需要改名时：

```bash
sudo env KYAGENT_USER=opsagent bash scripts/kyagent.sh permissions
```

LoongArch 安装器会把同一个账户写入 `/etc/kyagent/env` 的 `KYAGENT_EXECUTOR_ACCOUNT`。手工配置自定义账户时也要设置该变量。

## sudo 版本排障

```bash
LC_ALL=C sudo -V | head -1
```

动态查询参数正则要求 `sudo >= 1.9.10`。旧版本 sudo 无法可靠表达当前白名单边界，脚本会在覆盖 `/etc/sudoers.d/kyagent` 前停止。
