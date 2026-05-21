# 01 · 架构总览：模块与依赖

## 1. 一张图看完整体

```
┌────────────────────────────────────────────────────────────────────────┐
│                              入口层                                     │
│                                                                         │
│  python -m kyagent ask "查 80 端口"      Claude Desktop（MCP host）    │
│         │                                          │                    │
│         ▼                                          ▼                    │
│  ┌─────────────────┐                  ┌──────────────────────────┐    │
│  │ kyagent.cli     │                  │ kyagent.mcp.server       │    │
│  │ (typer + rich)  │                  │ (stdio JSON-RPC 2.0)     │    │
│  └────────┬────────┘                  └─────────────┬────────────┘    │
└───────────┼─────────────────────────────────────────┼──────────────────┘
            │                                         │
            │  build_agent / Agent.from_config        │  对每次 tools/call
            │                                         │  独立组装组件
            ▼                                         ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          编排层（Agent）                                │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │ kyagent.agent.core.Agent.ask(text)                              │  │
│  │                                                                 │  │
│  │ 主循环：messages → LLM → tool_use → guardrail → executor → ...  │  │
│  │                                                                 │  │
│  │ 持久 ThreadPoolExecutor + ConfirmFn 回调                        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───┬─────────────┬─────────────┬─────────────┬─────────────┬──────────┘
    │             │             │             │             │
    ▼             ▼             ▼             ▼             ▼
┌────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ agent  │  │ mcp.tools│  │ safety   │  │ executor │  │ audit    │
│ .llm   │  │          │  │          │  │          │  │          │
│        │  │ Tool +   │  │ Guardrail│  │ Execution│  │ Trace +  │
│ 三套   │  │ Registry │  │ + Rule + │  │ Proxy +  │  │ Logger + │
│ 后端   │  │          │  │ Policy   │  │ Sandbox  │  │ Store    │
└────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
   ▼            ▼              ▼             ▼             ▼
 Claude    process,        safety-      sudo +         SQLite
 OpenAI    network,        rules.yaml   preexec_fn +  (WAL)
 Mock      logs, svc,      (27 rules)   RLIMIT +      + JSONL
           fs, package                  PATH 白名单   stream
```

---

## 2. 模块清单（按依赖深度）

按"被依赖"程度从下往上排：底层模块是上层的基石。

### 2.1 底层基础设施（无内部依赖，给所有人用）
- `kyagent.config` — 配置 schema 和加载器
- `kyagent.safety.patterns` — `RiskLevel` enum 和 `Rule` dataclass
- `kyagent.audit.trace` — `Trace` / `TraceEvent` / `EventKind` 数据结构

### 2.2 服务层（依赖底层）
- `kyagent.safety.rules` — `RuleEngine`（依赖 patterns）
- `kyagent.safety.policy` — `Policy.decide(risk) → Decision`
- `kyagent.safety.guardrail` — `Guardrail` 主流水线（依赖 rules + policy）
- `kyagent.executor.sandbox` — `SandboxConfig` + `make_preexec_fn` + `build_clean_env`
- `kyagent.executor.proxy` — `ExecutionProxy.run()`（依赖 sandbox）
- `kyagent.audit.store` — `AuditStore` SQLite 封装
- `kyagent.audit.logger` — `AuditLogger`（依赖 trace + store）

### 2.3 MCP 工具集（独立的服务层，但接口和 LLM tools 对齐）
- `kyagent.mcp.tools.base` — `Tool` 基类 + `ToolRegistry`
- `kyagent.mcp.tools.{process,network,logs,service,filesystem,package}` — 内置工具
- `kyagent.mcp.tools.__init__.default_registry()` — 一行注册全部内置工具

### 2.4 LLM 层（与 OS 完全无关）
- `kyagent.agent.prompt` — `SYSTEM_PROMPT` 文本常量
- `kyagent.agent.llm` — `LlmBackend` 抽象 + `AnthropicBackend` / `OpenAIBackend` / `MockBackend` + `build_backend()`

### 2.5 编排层
- `kyagent.agent.core` — `Agent.ask()` 把上面所有人串起来

### 2.6 入口层
- `kyagent.cli` — typer 子命令树（chat / ask / tools / safety / audit / mcp）
- `kyagent.mcp.server` — JSON-RPC 2.0 over stdio
- `kyagent.__main__` — `python -m kyagent` 转发到 `cli.app()`

---

## 3. 依赖图（精确到模块）

```
                 ┌──────────────┐
                 │   cli.py     │  (typer + rich)
                 └──────┬───────┘
                        │ uses
                        ▼
                 ┌──────────────┐
                 │ agent.core   │  ← Agent.ask() 主入口
                 └──────┬───────┘
        ┌───────┬───────┼────────┬──────────┬──────────┐
        ▼       ▼       ▼        ▼          ▼          ▼
   ┌────────┐┌──────┐┌──────┐┌──────────┐┌────────┐┌──────────┐
   │agent.  ││mcp.  ││safety││executor. ││audit.  ││config    │
   │llm     ││tools ││.     ││proxy     ││logger  │└──────────┘
   │        ││      ││guard ││          ││        │
   └────────┘└──┬───┘│rail  │└──┬───────┘└──┬─────┘
                │    └──┬───┘   │           │
                ▼       │       ▼           ▼
           ┌──────┐     │  ┌────────┐  ┌────────┐
           │tools.│     │  │executor│  │audit.  │
           │base  │     │  │.sandbox│  │store + │
           └──────┘     │  └────────┘  │trace   │
                        ▼              └────────┘
                 ┌──────────┐
                 │safety.   │
                 │rules +   │
                 │policy +  │
                 │patterns  │
                 └──────────┘

mcp.server 独立平行：直接依赖 mcp.tools + safety.guardrail + executor.proxy + audit.logger
benchmarks/bench_ask.py 也是平行入口，只 import 顶层模块测性能
```

**关键不变量**：

1. `executor` 不知道 `safety` —— 任何防护都是上层（Agent / McpServer）调用 `guardrail.check_argv` 后再交给 executor。这避免了"执行器自己短路安全检查"的反模式。
2. `safety` 不知道 `executor` —— 它只接收 cmdline / argv，不真正运行。意味着 `kyagent safety test` 子命令完全离线、零副作用。
3. `audit` 是所有人都可以 `event()`，但不反向调用其它模块（无回调、无 hook）。审计是纯下游。
4. `mcp.tools` 模块层完全独立于 `agent.llm`：工具的 `input_schema` 是 JSON Schema，`to_anthropic_tools()` / `to_mcp_list()` 是简单序列化，未来加新后端不需要改工具。

---

## 4. 三类入口（同一个核心）

kyagent 有三个互相独立的入口，但都复用同一套核心（`Agent` 主循环或 `Guardrail+Executor+Audit` 三件套）：

### 4.1 CLI 交互模式
```
用户 ──▶ kyagent chat ──▶ cli._cli_confirm（注入回调）
                              │
                              ▼
                          Agent.ask()
```
确认场景：tty 弹 Rich Panel + Prompt.ask("是否放行")。

### 4.2 CLI 单次模式
```
用户 ──▶ kyagent ask "..." ──▶ confirm=lambda *a: False
                                    │
                                    ▼
                                Agent.ask()
```
没有人交互，confirm 路径一律拒绝。

### 4.3 MCP stdio 模式（被外部 LLM host 当工具集挂载）
```
Claude Desktop ──▶ stdin/stdout JSON-RPC
                       │
                       ▼
              McpServer.serve()
                       │
                       ▼
              每条 tools/call：
              Guardrail → Executor → Audit
              （不走 Agent 主循环，因为 LLM 决策在 Claude Desktop 那边）
```
MCP 通道里 CONFIRM 默认按 deny 处理（无人交互），这点写在 `mcp/server.py:174-186`。

---

## 5. 数据所有权

| 数据 | 所有者 | 生命周期 | 持久化吗？ |
|---|---|---|---|
| `Config` | `cli` / `mcp.server.main` 加载 | 进程级 | YAML 文件 |
| `messages: list[dict]` | `Agent` 实例 | 一次对话 session | 否（重启清空） |
| `Trace` | `Agent.ask()` / `McpServer._call_tool()` 各自起一条 | 单次 turn | SQLite + JSONL |
| `ToolRegistry` | `default_registry()` 进程单例 | 进程级 | 否（代码定义） |
| `ExecutionProxy` | `Agent` / `McpServer` 各自一个 | 进程级 | 否 |
| `Guardrail` | 同上 | 进程级 | 规则在 YAML |
| `_MANUAL_CACHE` (rules.py) | `RuleEngine` 进程级 | 进程级 LRU 1024 | 否 |
| `_jsonl_fp` | `AuditLogger` 实例 | 进程级 line-buffered | 否（atexit 关） |
| `_tool_pool` | `Agent` 实例 | 懒创建到 `shutdown()` | 否 |

**没有"全局可变状态"** —— 除了 RuleEngine 的进程级 LRU，但它由 `_version` 指纹 + `engine_id` 双重隔离，规则一变就失效。

---

## 6. 模块边界 = 安全边界

这点是这个项目的灵魂。每一条模块边界都对应一条安全断言：

| 边界 | 强制约束 | 在哪里实现 |
|---|---|---|
| LLM → Agent | LLM 只能返回 `tool_use` 块，不能塞 raw shell | `agent.llm.AssistantMessage.tool_uses()` 只解析 tool_use 块 |
| Agent → Tool | LLM 给的 args 必须过 `Tool.validate()` 才进 `build_argv` | `agent/core.py:247-249` |
| Tool → Executor | executor 只接 `list[str] argv`，不接字符串 | `Tool.build_argv() -> list[str]`；executor 全程不调 shell |
| Agent/MCP → Executor | 必须经过 `Guardrail.check_argv` 才能落地 | `agent/core.py:261` + `mcp/server.py:162` |
| Executor → 子进程 | 没有 `shell=True`，PATH 白名单，env scrub | `executor/proxy.py:140-149` |
| 工具层 → 路径 | 黑名单（`/etc/shadow` / `systemd-logind`）在 Tool 自己的 `build_argv` 里 | `service.py:_validate_unit`, `filesystem.py:_safe_path` |
| 所有阶段 → audit | 每一步 `audit.event` 写入 trace | 主循环 + MCP 调用都有 7 段事件 |

---

## 7. 一个最小心智模型

**"自然语言进来，argv 出去，结果回去"**：

```
自然语言  ─[LLM]→  tool_use(name + JSON args)
                            │
                            ▼
                    Tool.validate(args)     ← JSON Schema 校验
                            │
                            ▼
                    Tool.build_argv(args)   ← 受控的 argv 构造
                            │
                            ▼
                    Guardrail.check_argv(argv)  ← 安全二次过滤
                            │
                            ▼
                    ExecutionProxy.run(argv)    ← sudo + preexec + RLIMIT
                            │
                            ▼
                    ExecutionResult (stdout/stderr/rc/duration)
                            │
                            ▼
                    Tool.format_result(exec) → ToolResult
                            │
                            ▼
                    返回给 LLM 作为 tool_result → LLM 总结 → AGENT_REPLY
```

七个变换、每一步都有审计事件。这个 pipeline 就是赛题"完整闭环日志"的物理实现。

---

## 8. 下一步

继续 → [02-data-flow.md](./02-data-flow.md) 看一次 `ask("查 80 端口")` 在这套架构里走的完整时序。
