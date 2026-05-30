# kyagent TUI Shell Design

## 目标

为 kyagent 增加一个 LoongArch 友好的 TUI demo 壳，使现场演示从“单条命令输出”升级为“可持续交互、可视化、可确认、可回放”的闭环体验。

这个 TUI 的首要价值是把 kyagent 已经具备的安全运维能力展示清楚：用户连续输入自然语言，Agent 展示本轮回复、工具调用、风险确认、审计 trace timeline，并允许随时查看最近 trace 或回放某条 trace。它应优先服务麒麟 / LoongArch64 实机演示，因此默认依赖必须保持轻量、纯 Python、零 Rust，避免把部署风险转移到 UI 层。

MVP 成功标准：

- 在 LoongArch Old World 默认部署路径上可安装、可启动、可持续多轮输入。
- 不改变现有 `kyagent ask`、`kyagent chat`、`kyagent audit`、`kyagent tools` 的自动化语义。
- 高风险意图或工具调用必须在 TUI 内以明确确认面板呈现，并且默认拒绝。
- 每个 turn 的 trace 能以 timeline 形式查看，且能复用现有 SQLite 审计数据回放。

## 非目标

- 不做完整 IDE / Codex CLI 克隆，不实现文件树、diff 编辑器、patch 应用或终端 multiplexer。
- 不在 MVP 中引入 WebSocket、HTTP server、browser UI 或远程多人协作。
- 不改变 kyagent 的安全策略、sudoers 白名单、工具 schema 或 executor 沙箱模型。
- 不为 TUI 单独实现一套 Agent、Tool 或 Audit 数据模型。
- 不把 Textual 作为 LoongArch 默认安装依赖；Textual 只作为后续 optional extra 评估。
- 不在无人值守通道打开确认能力；`ask --json`、MCP stdio 等非交互通道继续默认拒绝 confirm。

## LoongArch 依赖策略

默认技术路线采用 `prompt_toolkit + rich`：

- `rich` 已经是主依赖，现有 CLI 正在使用它渲染 panel、table 和 prompt。
- `prompt_toolkit` 是纯 Python TTY 交互库，适合做多行输入、历史记录、快捷键、底部状态栏和可滚动布局；它不依赖 Rust，也不要求现代 manylinux LoongArch wheel。
- TUI 默认安装路径只新增 `prompt_toolkit`，并继续沿用 `pydantic v1`、`PyYAML`、`typer`、`rich`、`httpx` 的 LoongArch 轻量集合。
- 文档和安装脚本应明确：LoongArch Old World 默认仍使用 `deepseek_httpx` 或 mock，不能因为 TUI 拉入 `openai`、`anthropic`、`mcp`、`pydantic-core`、`jiter`。

Textual 仅作为 optional：

- 可在 x86_64/aarch64 或 LoongArch New World 上提供 `.[tui-textual]` 之类 extra，用于后续更复杂布局试验。
- 不能让 Textual 进入 `requirements-loongarch.txt` 或默认 `pip install -e .` 的必需路径。
- optional 路径不得启用 tree-sitter、native syntax highlighter 或其他需要平台 wheel / Rust / C 扩展的功能。
- MVP 文档、测试和演示命令均以 `prompt_toolkit + rich` 为准。

## 现有模块集成点

TUI 应作为新的交互通道接入现有 composition root，而不是复制业务逻辑。

- CLI 入口：`kyagent/cli.py`
  - 当前 `chat()` 已经提供多轮输入、`/reset`、`/audit`、Rich panel 输出和 `_cli_confirm()`。
  - TUI 可新增 `kyagent tui` 子命令，保留 `kyagent chat` 作为简单 Rich prompt 版本。
  - 默认 callback 不应被改成强制 TUI，避免破坏脚本和已有演示习惯。

- Runtime 装配：`kyagent/runtime.py`
  - TUI 通过 `load_config()` + `Agent.from_config()` 使用现有 `build_runtime()`。
  - 不直接 new `AuditStore`、`ExecutionProxy`、`Guardrail` 或 registry，避免通道行为漂移。

- Agent 主循环：`kyagent/agent/core.py`
  - 当前 `Agent.ask()` 是同步阻塞调用，维护 `self.messages` 上下文。
  - MVP 可以先在 TUI 中以“提交后等待结果”的方式调用 `ask()`，保证功能可落地。
  - 为了更好的可视化，后续应增加轻量事件 sink / run observer，让 TUI 在 `USER_INPUT`、`LLM_THOUGHT`、`TOOL_REQUEST`、`SAFETY_CHECK`、`EXECUTION_RESULT` 等事件写入时即时刷新，而不是只能在 turn 结束后读库。

- 确认契约：`kyagent/confirm.py` 与 `kyagent/agent/confirm_adapter.py`
  - TUI confirm 面板只消费 `ConfirmRequest`，不依赖 `Verdict` 或 `IntentVerdict` 内部字段。
  - confirm 回调必须由主交互循环处理。若后续引入后台 worker，worker 不得直接读键盘或弹确认。

- 审计回放：`kyagent/audit/{trace.py,logger.py,store.py}`
  - timeline 数据以 `AuditStore.get_events(trace_id)` 为权威来源。
  - 最近 trace 列表使用 `AuditStore.list_traces(limit)`。
  - TUI 展示层只读审计库，不修改 trace schema。

- 工具清单：`kyagent/mcp/tools/__init__.py` 与 `ToolRegistry`
  - `/tools` 显示 `agent.registry.all()` 经过配置白名单后的工具。
  - 字段至少包括 name、risk、requires_root、read_only、description。

## MVP 功能

### 1. 多轮输入

界面分为四个稳定区域：

- 顶部状态栏：后端名称、执行账户、工具数量、当前 trace id、配置状态。
- 主 transcript：按 turn 展示用户输入、kyagent 回复、notes 和错误。
- 右侧或下方 timeline：展示当前 trace 事件摘要。
- 底部输入区：支持单行 / 多行输入、历史记录、快捷键提交。

输入提交后，TUI 调用现有 `Agent.ask(text, user=...)`。MVP 不强求 token streaming；执行中可以显示“running”状态和最后一个已知阶段。turn 完成后更新 transcript、last_trace_id 和 timeline。

### 2. `/reset`

`/reset` 清空 `agent.messages`，重置当前会话上下文，但不删除审计库、不清空屏幕历史。界面应追加一条系统状态消息，说明上下文已清空。

### 3. `/audit`

无参数时显示当前 `last_trace_id` 的 timeline；没有 trace 时显示空状态。

建议支持扩展形式：

- `/audit recent`：展示最近 20 条 trace。
- `/audit <trace-id>`：回放指定 trace。

MVP 至少实现无参数行为，扩展形式可在同一设计下后续补齐。

### 4. `/tools`

显示当前配置实际启用的工具表。表格字段：

- `name`
- `risk`
- `root?`
- `read-only?`
- `description`

工具列表必须来自 Agent 当前 registry，不能绕过配置白名单直接调用 `default_registry()`。

### 5. 确认面板

当意图层或 argv 层返回 confirm 时，TUI 渲染阻塞式确认面板：

- 标题来自 `ConfirmRequest.title`。
- 风险等级醒目显示。
- 正文展示 `body`，工具调用通常是 argv，意图确认通常是 rationale。
- 命中规则逐行展示 `summary_lines`。
- 操作只提供 allow / deny，默认焦点在 deny。

确认面板关闭前，主输入区不可接受新的用户指令。超时、异常、窗口中断、未知按键都按 deny 处理。

### 6. Trace timeline

timeline 使用现有 event kind 顺序渲染：

`USER_INPUT → INTENT_CHECK → PERCEPTION → LLM_THOUGHT → TOOL_REQUEST → SAFETY_CHECK → EXECUTION → EXECUTION_RESULT → AGENT_REPLY / ERROR`

每个事件显示：

- `seq`
- 相对时间或本地时间
- `kind`
- 简短 payload 摘要

payload 摘要规则应保守：

- `USER_INPUT` 显示用户文本前 120 字。
- `LLM_THOUGHT` 显示 stop_reason、tool_calls 和文本前 160 字。
- `TOOL_REQUEST` 显示 tool、risk、requires_root、argv 摘要。
- `SAFETY_CHECK` 显示 decision、risk、user_confirmed。
- `EXECUTION_RESULT` 显示 returncode、ok、stdout/stderr 截断摘要。
- 超长 JSON 不在 timeline 直接展开，提供“详情面板”或 `/audit <trace-id>` 完整回放。

## 安全不变量

- TUI 不得绕过 `IntentGuard`、`Guardrail`、`ExecutionProxy`、`AuditLogger` 或 `Tool.validate()`。
- 所有实际运维动作仍必须从 `Agent.ask()` 的工具调用链路进入，不允许 TUI 直接执行 shell 命令。
- confirm 默认为拒绝；异常、取消、EOF、Ctrl-C、超时、非主线程确认都必须拒绝。
- confirm 决策必须发生在主交互循环，避免多线程 worker 抢 stdin 或产生授权错位。
- `/tools` 只展示当前配置启用工具，不泄露被白名单禁用的工具作为可用能力。
- `/reset` 只能清空对话上下文，不能删除审计数据或重置安全策略。
- `/audit` 和 timeline 只读审计库，不修改 trace、event 或 payload。
- TUI 不能改变非交互通道行为；`ask --json` 与 MCP confirm 继续默认拒绝。
- 输出中展示命令、argv、stderr 时必须截断，避免大输出撑爆终端或泄露过量敏感内容。
- LoongArch 默认依赖保持零 Rust；TUI 不能引入会默认触发 Rust / native extension 构建的包。

## 测试 / 验证策略

### 单元测试

- 命令解析：验证普通输入、空输入、`/reset`、`/audit`、`/tools` 的路由结果。
- timeline 摘要：用构造的 `TraceEvent` 覆盖每个关键 `EventKind` 的摘要渲染和截断。
- confirm 面板：用假的 `ConfirmRequest` 验证默认 deny、显式 allow、显式 deny、异常 deny。
- 工具表：验证 `/tools` 使用当前 Agent registry，尊重 `cfg.mcp.enable_tools`。

### 集成测试

- 使用 mock LLM 后端启动 TUI 会话对象，提交多轮输入后确认 `agent.messages` 持续增长。
- 执行 `/reset` 后确认 `agent.messages` 被清空，审计库仍保留历史 trace。
- 运行会触发 confirm 的请求，注入测试输入选择 deny，确认不会执行工具且 trace 中记录拒绝。
- 对 `AuditStore` 写入测试 trace，验证 `/audit` timeline 按 seq 顺序回放。

### LoongArch 验证

- 在 LoongArch / Kylin 默认安装路径执行 `pip install -r requirements-loongarch.txt`，其中已包含默认 TUI 依赖 `prompt_toolkit`。
- 验证不会安装 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。
- 在真实 TTY 中运行：
  - `kyagent tui`
  - 输入普通只读问题，例如“80 端口被谁占了？”
  - 输入 `/tools`
  - 输入 `/audit`
  - 输入 `/reset`
  - 输入高风险意图并选择 deny
- 保留现有回归：
  - `python -m kyagent ask "哪个进程 CPU 占用最高？"`
  - `python -m kyagent chat`
  - `python -m kyagent tools list`
  - `python -m kyagent audit list`
  - `python -m pytest -q`

### 手工演示验收

演示脚本建议按以下顺序：

1. 启动 TUI，展示顶部状态栏中的后端、执行账户、工具数和审计 DB。
2. 提问“哪个进程 CPU 占用最高？”，展示 transcript 和 trace timeline。
3. 输入 `/tools`，展示工具风险等级和 root 标记。
4. 提问“重启 sshd”，触发确认面板，先 deny，展示 trace 中的拒绝记录。
5. 输入 `/audit`，回放刚才 trace 的完整安全闭环。
6. 输入 `/reset`，展示上下文清空但审计仍可查。

## 后续缺口

- `Agent.ask()` 当前是同步阻塞接口，MVP 只能在 turn 结束后完整刷新 timeline；若要做到真正“执行中可视化”，需要新增事件 sink / observer。
- LLM token streaming 尚未统一到 `LlmBackend` 契约，MVP 不展示逐 token 输出。
- confirm 当前是简单 bool 返回，后续若要审计“谁在什么界面点了 allow/deny”，可扩展确认事件 payload，但不能破坏默认拒绝语义。
- TUI 布局需要适配窄终端、串口控制台、无真彩环境和中文宽字符，MVP 应保守使用 Rich 表格与纯文本符号。
- `prompt_toolkit` 历史记录文件位置、权限和清理策略需要单独设计，避免把敏感运维输入写到不合适的位置。
- Textual optional 版本可探索更强布局，但必须在 LoongArch 默认路径之外，并经过依赖树审查。
- 后续可增加 trace 搜索、按 kind 过滤、payload 展开、JSON 导出和 replay demo fixture，但这些不进入第一版 MVP。
