# 仓库当前状态 - A2 麒麟安全智能运维 Agent

生成时间：2026-06-01 +08:00

## 当前定位

本仓库对应 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”。正式交付目标是 LoongArch64 Linux + 麒麟高级服务器版 V11；安装器同时保留面向 Old World 环境的保守路径。

## 部署入口

根 `README.md` 保持高度抽象，只呈现正式入口：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash scripts/kyagent.sh web --env-file /etc/kyagent/env
```

`web` 会启动 FastAPI 后端、等待健康检查并自动打开浏览器。无桌面环境时，服务继续运行并打印访问 URL。

分开脚本：

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

## LoongArch 边界

- 安装器只支持 LoongArch Linux；非龙芯 Linux 只允许 `--dry-run --allow-non-loongarch`。
- 默认依赖路径不安装 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
- `pydantic v1` 使用 `SKIP_CYTHON=1` 和 `--no-binary pydantic` 固定纯 Python 安装。
- Web extra 独立放在 `requirements-loongarch-web.txt`，不安装 `uvicorn[standard]`。
- editable 安装使用 `--no-deps`，避免重新解析未审计依赖。

## 最小权限

- Web 默认监听 `127.0.0.1`；显式使用 `0.0.0.0` 时必须开启认证并配置四类角色 token，否则启动失败。
- 默认 sudoers 不允许任意 systemd 服务变更。
- 业务服务重启或 reload 必须在部署阶段通过 `KYAGENT_SERVICE_ALLOWLIST` 显式列出。
- `/etc/kyagent/env` 使用 shell 安全转义格式写入，并同步 `KYAGENT_EXECUTOR_ACCOUNT`。
- 安装器生成 `/etc/kyagent/audit-hmac.key`，审计事件使用哈希链和 HMAC 封印；`kyagent audit verify <trace-id>` 可校验完整性。
- RCA 通过内置 playbook 和 `submit_rca_report` 逻辑工具落库，只接受当前 trace 已存在的 `PERCEPTION evidence_id`。

## 文档布局

| 文档 | 职责 |
| --- | --- |
| `README.md` | LoongArch Linux 高层入口 |
| `docs/deployment/loongarch.md` | 依赖边界、安装器、手工兜底、验收 |
| `docs/deployment/web.md` | Web 一键启动、分开脚本、局域网风险 |
| `docs/deployment/permissions.md` | sudoers、服务 allowlist、验证 |
| `docs/kyagent/README.md` | 完整能力说明 |
