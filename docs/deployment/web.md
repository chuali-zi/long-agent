# kyagent Web 控制台部署

Web 控制台复用 CLI/TUI 的 Agent、Guardrail、ExecutionProxy 和 Audit 链路。前端由 FastAPI 同源托管，不需要额外 Node.js 服务。

## 一键启动

离线演示：

```bash
bash scripts/start-web.sh --install-web --mock
# 等价的统一入口
bash scripts/kyagent.sh web --install-web --mock
# Web extra 已安装时
bash scripts/kyagent.sh web --mock
```

该命令启动后端、等待 `/api/health` 就绪并自动打开浏览器。没有桌面环境或找不到 `xdg-open`、`gio`、`sensible-browser` 时，后端不会退出，而是打印：

```text
open manually: http://127.0.0.1:8000
```

默认监听 `127.0.0.1:8000`。

## 分开启动

调试或交给 systemd 编排时，可以分别启动后端和打开页面：

```bash
bash scripts/start-web-backend.sh --mock
bash scripts/open-web.sh --url http://127.0.0.1:8000
```

统一入口也暴露相同层次：

```bash
bash scripts/kyagent.sh web-backend --mock
bash scripts/kyagent.sh web-open
```

## LoongArch 生产启动

安装阶段显式打开 Web extra：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

仅本机访问：

```bash
sudo -u kyagent bash scripts/kyagent.sh web \
  --env-file /etc/kyagent/env
```

需要局域网访问时，先在受控的 `/etc/kyagent/env` 中开启非回环监听并配置四类角色 token：

```bash
KYAGENT_WEB_ALLOW_NON_LOOPBACK=1
KYAGENT_WEB_REQUIRE_AUTH=1
KYAGENT_WEB_OPERATOR_TOKEN=<random-operator-token>
KYAGENT_WEB_REVIEWER_TOKEN=<random-reviewer-token>
KYAGENT_WEB_AUDITOR_TOKEN=<random-auditor-token>
KYAGENT_WEB_ADMIN_TOKEN=<random-admin-token>
```

再显式监听所有网卡，并指定浏览器本机打开地址：

```bash
sudo -u kyagent bash scripts/kyagent.sh web \
  --env-file /etc/kyagent/env \
  --host 0.0.0.0 \
  --browser-url http://127.0.0.1:8000 \
  --port 8000
```

服务端会拒绝缺少上述任一开关或 token 的非回环监听。API 使用 Bearer token 做角色鉴权：`operator` 调用 Agent，`reviewer` 处理审核与选择，`auditor` 查看 trace，`admin` 拥有全部权限。请求体中的 `user` 和 `reviewer` 字段不作为身份依据。即便已启用认证，也不要把控制面直接暴露到不受信任网络。

## 参数

组合入口 `scripts/start-web.sh`：

```text
--host HOST           监听地址，默认 127.0.0.1
--port PORT           监听端口，默认 8000
--browser-url URL     自动打开的地址
--no-open-browser     只启动后端，不尝试打开页面
--config PATH         YAML 配置文件
--env-file PATH       启动前加载环境变量
--install-web         安装 FastAPI/uvicorn extra
--mock                强制使用离线 mock LLM
```

`scripts/start-web-backend.sh` 接受除 `--browser-url`、`--no-open-browser` 外的后端参数。`scripts/open-web.sh` 支持 `--url`、`--health-url` 和 `--wait-seconds`。

`requirements-loongarch-web.txt` 只安装兼容 pydantic v1 的 FastAPI 和标准版 uvicorn。不要安装 `uvicorn[standard]`，避免在 LoongArch Old World 上引入额外 native/Rust 扩展。

## 浏览器审核

高风险操作不会直接执行。服务端通过 SSE 发出：

```text
approval_required
approval_resolved
```

页面使用以下接口批准或拒绝：

```text
GET  /api/approvals
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
```

## 故障排查

缺少 FastAPI/uvicorn：

```bash
bash scripts/kyagent.sh web --install-web --mock
```

受限账户无法读取配置：

```bash
sudo -u kyagent test -r /etc/kyagent/env
```
