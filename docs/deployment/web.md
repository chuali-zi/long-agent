# kyagent Web 控制台部署

Web 控制台是可选入口。它复用与 CLI/TUI 相同的 Agent、Guardrail、ExecutionProxy 和 Audit 链路，不会绕过安全检查。

## 离线演示

首次启动时安装 Web extra，并切换到 mock LLM：

```bash
bash scripts/start-web.sh --install-web --mock
```

统一入口也可以直接使用：

```bash
bash scripts/kyagent.sh web --install-web --mock
```

如果 Web extra 已安装，后续只需：

```bash
bash scripts/kyagent.sh web --mock
```

浏览器打开 `http://127.0.0.1:8000`。服务默认监听 `0.0.0.0:8000`；局域网访问请使用服务器 IP。

## 生产启动

LoongArch/Kylin 安装器可以提前安装 Web extra：

```bash
sudo bash scripts/install-loongarch.sh --yes --with-web
```

再用受限账户加载生产配置：

```bash
sudo -u kyagent bash scripts/kyagent.sh web \
  --env-file /etc/kyagent/env \
  --host 0.0.0.0 \
  --port 8000
```

## 参数

```text
--host HOST       监听地址，默认 0.0.0.0
--port PORT       监听端口，默认 8000
--config PATH     YAML 配置文件
--env-file PATH   启动前加载环境变量
--install-web     安装 FastAPI/uvicorn extra
--mock            强制使用离线 mock LLM
```

`.[web]` 只安装兼容 pydantic v1 的 FastAPI 和标准版 uvicorn。不要安装 `uvicorn[standard]`，避免在 LoongArch Old World 上引入额外 native/Rust 扩展。

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

提示缺少 FastAPI/uvicorn 时，执行：

```bash
bash scripts/kyagent.sh web --install-web --mock
```

受限账户无法读取配置时，检查：

```bash
sudo -u kyagent test -r /etc/kyagent/env
```
