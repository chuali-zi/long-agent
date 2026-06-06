# Web 控制台部署

Web 控制台是本项目的 B/S 演示入口。前端由 FastAPI 后端同源托管，是零外部依赖的单文件静态页面，不需要额外 Node.js 进程。Web 复用 CLI/TUI 的 Agent、Guardrail、ExecutionProxy 和 Audit 链路，不绕过安全层。

## 离线演示

本地或录屏前快速验证：

```bash
bash scripts/start-web.sh --install-web --mock
```

等价入口：

```bash
bash scripts/kyagent.sh web --install-web --mock
```

Web 依赖已经装好时，用更短的入口：

```bash
bash scripts/kyagent.sh web --mock
```

这会：

1. 必要时安装 Web extra。
2. 设置 `KYAGENT_LLM_BACKEND=mock`。
3. 启动 `kyagent web serve`。
4. 等待 `/api/health` 就绪。
5. 自动打开浏览器。

没有桌面环境或 opener 不可用时，后端不会退出，而是打印：

```text
open manually: http://127.0.0.1:8000
```

## 分开启动

调试或交给服务管理器编排时，可以分开运行：

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

后端脚本负责启动服务；打开脚本只负责等待健康检查和打开页面。

## LoongArch 生产启动

生产路径建议固定为 `/opt/kyagent`：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent

sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web \
  --env-file /etc/kyagent/env
```

不要从 `/home/<user>/...` 私有目录里切换到 `kyagent` 账户启动，否则可能没有目录穿透或脚本读取权限。

## 非回环监听

默认只监听：

```text
127.0.0.1:8000
```

如果要让局域网访问，必须显式开启认证并配置四类角色 token。缺少任一开关或 token 都会 fail closed。

示例 `/etc/kyagent/env`：

```bash
KYAGENT_WEB_ALLOW_NON_LOOPBACK=1
KYAGENT_WEB_REQUIRE_AUTH=1
KYAGENT_WEB_OPERATOR_TOKEN=replace-operator-token
KYAGENT_WEB_REVIEWER_TOKEN=replace-reviewer-token
KYAGENT_WEB_AUDITOR_TOKEN=replace-auditor-token
KYAGENT_WEB_ADMIN_TOKEN=replace-admin-token
```

启动：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web \
  --env-file /etc/kyagent/env \
  --host 0.0.0.0 \
  --port 8000
```

角色含义：

| 角色 | 能力 |
| --- | --- |
| `operator` | 发起 Agent 问答 |
| `reviewer` | 处理审核和用户选择 |
| `auditor` | 查看 trace |
| `admin` | 拥有全部权限 |

即使开启认证，也不要把控制面暴露到不受信任网络。

## 高风险审核流程

高风险命令不会被浏览器直接执行。流程是：

```text
用户提问
  -> Agent 生成 tool call
  -> Guardrail 判断需要确认
  -> SSE 推送 approval_required
  -> 浏览器展示审核卡片
  -> reviewer 调 approve/reject API
  -> SSE 推送 approval_resolved
  -> Agent 继续或终止
```

接口：

```text
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
```

对应 SSE 事件：

```text
approval_required
approval_resolved
```

## 参数速查

组合入口 `scripts/start-web.sh`：

| 参数 | 作用 |
| --- | --- |
| `--mock` | 使用 mock LLM，适合离线演示 |
| `--install-web` | 启动前安装 Web extra |
| `--host HOST` | 后端监听地址，默认 `127.0.0.1` |
| `--port PORT` | 后端端口，默认 `8000` |
| `--no-open-browser` | 只启动后端，不打开浏览器 |
| `--env-file PATH` | 启动前加载环境变量文件 |

`scripts/start-web-backend.sh` 接受后端参数。`scripts/open-web.sh` 支持 `--url`、`--health-url` 和 `--wait-seconds`。

## 依赖边界

LoongArch Web extra 来自 `requirements-loongarch-web.txt`：

- `fastapi>=0.95,<0.100`
- `uvicorn>=0.23,<0.30`

不要安装 `uvicorn[standard]`，避免在 LoongArch Old World 上引入额外 native/Rust 扩展。

## 排障

### 提示缺少 FastAPI 或 uvicorn

处理：

```bash
bash scripts/start-web.sh --install-web --mock
```

或在生产安装时加：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

### `Permission denied`

先确认是不是从私人目录用 `sudo -u kyagent` 启动。正式部署请放到 `/opt/kyagent`。

```bash
sudo -u kyagent test -r /opt/kyagent/scripts/start-web.sh
sudo -u kyagent test -r /etc/kyagent/env
```

### 局域网启动失败

确认已设置：

```bash
KYAGENT_WEB_ALLOW_NON_LOOPBACK=1
KYAGENT_WEB_REQUIRE_AUTH=1
KYAGENT_WEB_OPERATOR_TOKEN=...
KYAGENT_WEB_REVIEWER_TOKEN=...
KYAGENT_WEB_AUDITOR_TOKEN=...
KYAGENT_WEB_ADMIN_TOKEN=...
```
