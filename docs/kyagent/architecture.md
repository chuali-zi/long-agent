# kyagent 架构

## 1. 数据流（一次 user turn）

```
┌─ user_input ────────────────────────────────────────────────────────────────┐
│ "重启 nginx"                                                                │
└─────────────────┬───────────────────────────────────────────────────────────┘
                  ▼
        ┌───────────────────────┐
        │ Agent.ask()           │   开 trace（uuid）→ AuditLogger.open
        └───────────┬───────────┘
                    ▼ messages.append(user)
        ┌───────────────────────┐
        │ LlmBackend.chat()     │   ① Anthropic / mock；带 tools schema
        └───────────┬───────────┘
                    ▼ AssistantMessage(blocks=[text, tool_use])
                ┌───┴────────┐
   text only ◀──┘            └──▶ tool_use(name, input)
        │                          │
        ▼                          ▼
   AGENT_REPLY              ┌───────────────┐
                            │ Tool.validate │ ② 参数校验
                            │ + build_argv  │   禁止 shell 元字符
                            └───────┬───────┘
                                    ▼ argv
                            ┌───────────────┐
                            │ Guardrail     │ ③ 安全裁决
                            │ check_argv()  │    declared_risk = tool.risk_level
                            └───────┬───────┘
                                    │ Verdict
                  ┌─────────────────┼──────────────────┐
                  ▼                 ▼                  ▼
                allow            confirm              deny
                  │                 │                  │
                  │   confirm_cb()──┤                  │
                  │      ▲ false ───┘                  │
                  ▼      │ true                        ▼
        ┌──────────────────┐                  ToolResult(is_error=True,
        │ ExecutionProxy   │ ④ sudo -n -u kyagent --   "[denied] ...")
        │ subprocess.Popen │    timeout / rlimit /
        │ clean_env, pgrp  │    output_cap / PATH 白名单
        └────────┬─────────┘
                 ▼ ExecutionResult
        ┌──────────────────┐
        │ Tool.format_result│
        └────────┬─────────┘
                 ▼ ToolResultBlock
        messages.append({role:user, content:[tool_result]})
                 │
                 └────▶ 回到 LlmBackend.chat()，多轮迭代直到 stop_reason=end_turn
                                                       │
                                                       ▼
                                                AGENT_REPLY → AuditLogger.close
```

## 2. 模块边界

| 模块 | 状态 | 边界契约 |
|---|---|---|
| `kyagent.agent.llm`     | 无状态 | 仅做 `chat(system, messages, tools) → AssistantMessage`；不感知工具语义 |
| `kyagent.agent.core`    | 有上下文 | 持有 messages 与各子系统句柄；负责 trace 串联与多轮迭代 |
| `kyagent.mcp.tools`     | 无状态 | `Tool.build_argv` 只产 argv，不真正执行；不做安全决策 |
| `kyagent.safety`        | 无状态 | 输入 cmdline / argv + declared_risk，输出 Verdict |
| `kyagent.executor`      | 无状态 | 接受 argv + requires_root，副作用仅限子进程 |
| `kyagent.audit`         | 持久化 | 唯一向 SQLite/JSONL 写入的入口 |
| `kyagent.mcp.server`    | 进程 | stdio JSON-RPC，把上述模块组合成 MCP host 端可见的工具 |
| `kyagent.web.server`    | 进程 | FastAPI B/S 接入层；把 ProgressEvent 转为 SSE，并把浏览器 approve/reject 接回 ConfirmFn |

模块之间靠数据结构（Verdict / ExecutionResult / ToolResult）通信，不互相 import 彼此的内部实现，便于替换。

### 2.1 v2 流式接口（TUI 通道使用）

`Agent.ask()` 在保留同步签名的前提下，额外暴露两个回调，供 TUI / 长时通道实时驱动 UI：

- `on_progress: ProgressCallback` — 主循环里每个阶段都会发一个事件，事件 `kind` 取值为 `agent_start / thinking_start / thinking_delta / thinking_end / tool_call_start / tool_call_end / user_choice / agent_final / error`，schema 见 `kyagent/progress.py`。CLI 的 `ask` / `chat` 子命令不接这个回调，行为与旧版一致；TUI 用它驱动思考流面板和底部状态行。
- `on_user_choice: UserChoiceFn` — 用于内置的 `ask_user_choice` 工具：LLM 主动让用户从选项里挑一个时，由该回调向 UI 提问并返回所选 value。schema 见 `kyagent/interactive.py`。

`LlmBackend` 同步新增 `chat_stream(system, messages, tools, on_delta)`：基类提供基于 `chat()` 的 fallback（一次性发完整 text），`HttpxBackend`（OpenAI SSE）、`OpenAIBackend`（SDK `stream=True`）、`MockBackend`（按空格切块模拟流）各自原生实现；`AnthropicBackend` 走基类 fallback，因为 SDK 的 `messages.stream()` 会触发 jiter Rust 编译，对 LoongArch 不友好。

### 2.2 Web 审核桥

`kyagent.web.server` 把相同的 `ProgressEvent` 序列通过 `POST /api/ask/stream` 推成 SSE。遇到 `ConfirmRequest` 时，`ApprovalBroker` 创建待审核记录并阻塞当前 Agent turn；浏览器调用 `/api/approvals/{approval_id}/approve` 或 `/reject` 后唤醒执行线程，并收到 `approval_resolved`。同步 `/api/ask` 保持无人值守默认拒绝，避免没有交互界面时意外放行。

FastAPI worker 线程可以成为一个 Agent turn 的拥有线程；并行 tool worker 仍不得触发 confirm。`Agent._active_run_thread_id` 区分这两类线程，保留原有并发安全约束。

## 3. 配置链

```
KYAGENT_CONFIG env ─┐
                    ▼
configs/default.yaml ──(YAML + ${VAR:-default} 展开)──▶ Pydantic Config
                                                          │
                                  ┌────────────────────────┤
                                  ▼                        ▼
                            SandboxConfig             SafetyConfig
                                  │                        │
                          ExecutionProxy             Guardrail.from_config
                                                          │
                                                  RuleEngine.from_yaml
                                                  (configs/safety-rules.yaml)
```

`safety-rules.yaml` 与 `default.yaml` 分开，规则库可独立热扩，不需要改代码。

## 4. 失败注入点（防御纵深）

| 攻击姿势 | 在哪一层被挡 |
|---|---|
| LLM 幻觉一条 `rm -rf /` 工具调用 | Guardrail Stage 1 命中正则 → DENY |
| LLM 给 `svc_restart` 喂 `sshd; rm -rf /` | Tool.build_argv 检测 shell 元字符 → ToolError |
| LLM 给 `svc_restart` 喂 `systemd-logind` | Tool 内 `_FORBIDDEN_UNITS` 列表 → ToolError |
| LLM 调 `process_list` 但你想让它读 /etc/shadow | 工具没有 cat 接口；fs_ls 也禁读 `/etc/shadow` |
| LLM 让我们用 `find -exec rm` 绕过 rm 规则 | `fs_find` 工具不传 `-exec` 参数 |
| 用户在 confirm 时被诱导按 yes | 仍要过 sudoers 白名单，不在白名单里直接失败 |
| 进程内核态提权 | rlimit + 干净 env + PATH 白名单 + 非 root 账户 |
| 输出体爆破 | sandbox output_cap 64K/流 + truncated 标记；tool 返回再统一截到 6KB（pipeline.OUTPUT_CAP） |
| 死循环消耗 CPU | timeout 30s + SIGTERM/SIGKILL pgrp |
| 删本机审计 | 审计落 SQLite + JSONL；JSONL 可外送 SIEM |

## 5. 工具集架构

92 个内置工具按 10 个域模块组织在 `kyagent/mcp/tools/` 下：`process` / `service` / `network` / `logs` / `filesystem` / `package` / `disk` / `system` / `security` / `compliance` / `loongarch`（外加 `interactive` 单独承载 `ask_user_choice`）。每个域文件用 `register(registry)` 接入，`default_registry()` 一次性串起，详见 `kyagent/mcp/tools/__init__.py`。

两个基础设施类约束工具实现边界：

- **`TrendTool`**（`base.py:263`）—— 采样型工具基类，约定 `build_argv` 必须把"间隔 / 次数"编入命令本身（例如 `iostat -dx 1 2`、`vmstat 1 2`），让被调程序自行跨时间采样。绝不在 Python 端 sleep，避免给 sudoers 放行 `sh -c "...; sleep N"` 这类拼接命令。子类目前只有 `DiskIoStatsTool`；没有原生 interval 支持的命令（`/proc/diskstats`、文件大小扫描）继续做"快照"普通 Tool，让 LLM 自行调两次比对——这是更老实的边界。
- **`PkgFamilyMixin`**（`pkg_family.py:107`）—— 按发行版透明切换包管理器 argv。一次性读 `/etc/os-release` 的 `ID/ID_LIKE/PRETTY_NAME` 判定 `PkgFamily.RPM`（麒麟服务器版 / RHEL 系）或 `PkgFamily.DPKG`（麒麟桌面版 / Debian 系），结果进程级缓存；`set_pkg_family_for_tests()` 让单测可注入。`PkgVerifyTool` 因此能在 RPM 下发 `rpm -V`、在 DPKG 下发 `debsums -c`，LLM 只看到统一的 `pkg_verify`，不必区分发行版。

工具与三大子系统的关系是严格的单向依赖：

```
Tool.build_argv()  ──argv──▶  Guardrail.check_argv()  ──verdict──▶  ExecutionProxy.run()  ──ExecutionResult──▶  Tool.format_result()
       ▲                              │                                       │                                       │
       └── input_schema 校验           └── audit: SAFETY_CHECK                  └── audit: EXECUTION / EXECUTION_RESULT
```

任何工具都**不绕过**这三层。`interactive.AskUserChoiceTool` 是唯一例外：它由 `Agent._handle_tool_use_inner` 按名拦截，hand-off 给 UI 回调而不走 ExecutionProxy，所以 `build_argv` 只返回 placeholder。

## 6. 可扩展点

- **新增工具**：在 `kyagent/mcp/tools/` 增模块，写 `Tool` 子类，`register()` 进 registry。
- **新增规则**：在 `configs/safety-rules.yaml` 加一条；支持 `pattern` / `command+flags+target_in` 两种范式。
- **接其他 LLM**：实现 `LlmBackend.chat()`；统一返回 `AssistantMessage`。
- **接其他 MCP host**：`kyagent mcp serve` 即标准 stdio，可被 Claude Desktop / Cursor / Continue 直接发现。
