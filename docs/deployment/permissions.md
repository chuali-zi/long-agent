# kyagent 最小权限部署

kyagent 不应以 root 身份持续运行。推荐创建独立的受限系统账户，并只通过 sudoers 白名单放行确实需要 root 的查询或服务操作。

## 一键配置

在仓库根目录执行：

```bash
sudo bash scripts/kyagent.sh permissions
```

该入口会调用 `scripts/setup-sudoers.sh`，依次完成：

1. 要求 root 权限，并检查 `visudo` 和 `sudo`。
2. 使用 `LC_ALL=C sudo -V` 获取稳定的英文版本输出。
3. 要求 `sudo >= 1.9.10`，因为动态参数白名单使用 sudoers 锚定正则。
4. 创建默认运行账户 `kyagent`，并在系统存在 `systemd-journal` 组时加入该组。
5. 用 `visudo -cf` 校验临时文件，再安装 `/etc/sudoers.d/kyagent`。
6. 对安装后的文件再次校验；失败时自动回滚。
7. 创建 `/var/lib/kyagent`、`/var/log/kyagent` 和 `/var/log/sudo-io`。

这不是任意 root 提权。模板只允许 [configs/sudoers.kyagent](../../configs/sudoers.kyagent) 中列出的命令和参数。

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

统一入口会为 `chat`、`tui` 和 `tools` 自动加载可读的 `/etc/kyagent/env`；Web 入口由 `start-web.sh` 加载同一文件。

## 自定义账户

默认账户名是 `kyagent`。需要改名时：

```bash
sudo env KYAGENT_USER=opsagent bash scripts/kyagent.sh permissions
```

脚本会同步改写 sudoers 模板中的目标账户和自审计规则。

## sudo 版本排障

查看脚本实际使用的版本输出：

```bash
LC_ALL=C sudo -V | head -1
```

动态参数正则要求 `sudo >= 1.9.10`。旧版本 sudo 无法可靠表达当前白名单边界，脚本会在写入 `/etc/sudoers.d/kyagent` 前停止。

如果仍提示无法识别版本，错误信息会带上原始首行。先确认系统中的 `sudo` 不是包装脚本，再升级发行版 sudo 包。
