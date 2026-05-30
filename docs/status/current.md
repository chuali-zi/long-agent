# 仓库当前状态 - A2 麒麟安全智能运维 Agent

生成时间：2026-05-29 10:17:06 +08:00

## 1. 当前定位

本仓库对应 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”。交付重点是在麒麟/Linux 上实现可控运维闭环：自然语言输入、意图过滤、LLM 或 mock 推理、MCP 风格工具调用、argv 二次安全过滤、最小权限执行、SQLite/JSONL 审计和最终回复。

## 2. 文档布局

根目录现在只保留项目入口和必要根文件：

```text
D:\race\long
├── README.md               # 使用手册入口
├── AGENT.md                # agent 协作约束
├── kyagent.json            # 项目根 LLM backend 轻量配置
├── pyproject.toml
├── requirements*.txt
├── configs/
├── scripts/
├── kyagent/
├── tests/
└── docs/
```

主要文档都在 `docs/`：

```text
docs/
├── kyagent/
│   ├── README.md           # 完整项目说明
│   ├── architecture.md     # 架构文档
│   └── safety-model.md     # 安全模型
├── deployment/
│   └── loongarch.md        # LoongArch/Kylin 部署审查
├── status/
│   ├── current.md          # 当前状态
│   └── log.md              # 工作日志
└── superpowers/
```

## 3. 当前默认运行路径

- 默认 `llm_backend` 是 `deepseek_httpx`。
- `DEEPSEEK_API_KEY` 或项目根 `kyagent.json` 的 `deepseek_api_key` / `deepseek.api_key` 存在时走真实 DeepSeek OpenAI-compatible HTTP 接口。
- 两处都未配置 key 时直接报错，避免生产环境静默使用 mock；离线演示需显式设置 `llm_backend=mock`。
- LoongArch Old World 默认不安装 `.[openai]`、`.[anthropic]`、`.[mcp]`，避免 `jiter`、`pydantic-core` 等 Rust 扩展风险。

## 4. 赛题要求贴合度

| 赛题要求 | 当前实现状态 | 关键位置 |
| --- | --- | --- |
| OS 环境深度感知 | 已覆盖进程、端口、网络、日志、服务、文件系统、包管理工具 | `kyagent/mcp/tools/*.py` |
| MCP 运维插件化 | 自研 `Tool`/`ToolRegistry` + stdio JSON-RPC MCP server | `kyagent/mcp/` |
| 安全意图校验器 | 自然语言意图层 + LLM 输出 argv 层 | `kyagent/safety/`、`configs/*rules.yaml` |
| 最小权限代理执行 | PATH 白名单、clean env、timeout、rlimit、sudoers 白名单 | `kyagent/executor/`、`configs/sudoers.kyagent` |
| 推理链路溯源 | SQLite + JSONL 双通道 | `kyagent/audit/` |
| 国产模型路径 | DeepSeek + `deepseek_httpx` | `kyagent/agent/llm.py`、`configs/deepseek.yaml` |

## 5. 本轮文档整理结果

- 根 `README.md` 已重写为使用手册入口：部署、脚本、LLM key、启动、使用、CLI、配置和验收命令都在同一个入口里。
- 根目录长文档已迁移：
  - `docs/kyagent/README.md`
  - `docs/deployment/loongarch.md`
  - `docs/status/log.md`
  - `docs/status/current.md`
- `AGENT.md` 已改为指向 `docs/status/`。
- LoongArch/Kylin 部署说明不再放在根目录，根 README 只链接它。

## 6. 验证重点

本轮变更完成后应至少验证：

```powershell
python -m pytest tests -q
Get-ChildItem -File
```

历史测试临时目录 `pytest_tmp_run/` 在当前 Windows 环境下仍可能提示 permission denied；它是测试产物，不属于文档整理范围。
`.gitignore` 已忽略 `pytest_tmp*/`，避免新的 pytest 临时目录进入未跟踪文件列表。
