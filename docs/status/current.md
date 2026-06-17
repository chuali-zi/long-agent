# 仓库当前状态 - A2 麒麟安全智能运维 Agent

更新时间：2026-06-17 +08:00

## 当前定位

本仓库对应 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”。正式交付目标是 LoongArch64 Linux + 麒麟高级服务器版 V11；默认部署路径是 `/opt/kyagent`，运行账户是 `kyagent`，生产启动配置是 `/etc/kyagent/env`。

## 当前文档结构

文档已按读者路径重构：

| 文档 | 职责 |
| --- | --- |
| `README.md` | 最短启动入口、场景选择、比赛交付形式 |
| `docs/deployment/loongarch.md` | LoongArch/Kylin 正式部署、依赖边界、验收 |
| `docs/deployment/web.md` | Web 控制台、分开启动、局域网认证、审核 API |
| `docs/deployment/permissions.md` | 文件权限、sudoers 权限、审计目录权限和排障 |
| `docs/kyagent/README.md` | 赛题贴合、架构、工具、安全、审计、交付说明 |
| `docs/kyagent/architecture.md` | 模块和数据流细节 |
| `docs/kyagent/safety-model.md` | 安全模型细节 |

## 推荐部署入口

正式 Web 部署：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

只重写生产配置：

```bash
cd /opt/kyagent
sudo bash scripts/kyagent.sh prod-env
```

离线演示：

```bash
bash scripts/kyagent.sh install
bash scripts/kyagent.sh web --install-web --mock
```

TUI：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

## LoongArch 边界

- 默认路径零 Rust，使用 `deepseek_httpx`、`pydantic v1`、`httpx`、`prompt_toolkit + rich`。
- 默认不安装 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
- Web extra 独立放在 `requirements-loongarch-web.txt`，不安装 `uvicorn[standard]`。
- `install-loongarch.sh` 支持 dry-run、离线 wheelhouse、命令库存检查、sudoers/env/selfcheck。
- `scripts/kyagent.sh prod-env` 可单独重写 `/etc/kyagent/env`，不用重跑完整安装器。

## 当前验证状态

- Python 3.10+ 是运行与测试硬要求；若系统默认 `python` 为 3.9，应使用项目 `.venv` 或显式 Python 3.10+。
- 最新全量 pytest：`834 passed, 3 skipped, 10 warnings, 0 failed`。
- 最新真实 DeepSeek + `/opt/kyagent` 生产前缀 RealOps：9 个 bench 全部 `PERFECT`。
- 最新性能微基准：`overall_pass=false`；仅 `ask_p50` 未达到 25% 降幅目标，`ask_p95`、`guardrail_p50`、`audit_total` 均达标。

## 权限边界

- 文件权限：`kyagent` 必须能读 `/opt/kyagent` 和 `/etc/kyagent/env`。
- sudoers 权限：`/etc/sudoers.d/kyagent` 只放行少量 root 查询和显式业务服务变更。
- 审计权限：生产审计写 `/var/lib/kyagent/audit.db` 和 `/var/log/kyagent/audit.jsonl`。
- 业务服务 restart/reload 必须通过 `KYAGENT_SERVICE_ALLOWLIST` 显式生成固定 sudoers 命令。

## 交付建议

比赛交付不建议做 Windows `.exe`。推荐交付：

```text
kyagent-release.tar.gz
源码压缩包
部署文档
演示 PPT
7 分钟以内演示视频
```

安装包内应包含源码、`scripts/`、`configs/`、requirements、README 和 docs，评委可按 LoongArch 部署文档一键安装。
