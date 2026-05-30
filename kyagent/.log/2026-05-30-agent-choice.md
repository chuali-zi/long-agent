# 2026-05-30 Agent streaming + ask_user_choice

分支 experimental/tui-streaming，独立完成 Agent 主循环侧两件事：

## 变更
1. **kyagent/agent/core.py**
   - `Agent.__init__` / `from_config` / `build_agent` 新增 `on_user_choice: UserChoiceFn | None`，默认 `auto_cancel_choice`（拒绝）。
   - `ask()` while 循环把 `self.llm.chat(...)` 换成 `chat_stream(..., _on_delta)`，闭包内对每段 chunk emit `thinking_delta`。`getattr` 兜底覆盖并行子代理还没合并 chat_stream 的窗口期。
   - `_handle_tool_use_inner` 开头按 name 特判 `ask_user_choice` → `_handle_user_choice`，绕过 prepare_call/check_safety/execute_and_format 流水线。新方法负责解析 options → 写 TOOL_REQUEST 审计 → emit `user_choice` ProgressEvent → 同步阻塞调 `on_user_choice` → 校验返回 value 在 options 集合内 → 写 EXECUTION_RESULT → 构造 ToolResultBlock（拒绝路径 is_error=True，"未做出选择或选项无效"）。

2. **kyagent/mcp/tools/interactive.py（新建）**
   - `AskUserChoiceTool(name="ask_user_choice", risk=LOW, read_only=True)`，schema 要求 options minItems=2，每项必备 value+label。description 明确"不用于开放式问题"。`build_argv` 返回 `["true"]` 占位（永不被消费）。
   - `register(reg)` 默认注册。

3. **kyagent/mcp/tools/__init__.py**：在 register_builtin 列表末尾加 `interactive.register(reg)`。

## 测试 tests/test_user_choice.py
六条用例：schema 通过 / 缺 options 报错 / options=1 报错；Agent 路由用户同意（"y" 喂回 LLM，第二轮结束）；用户拒绝（is_error+未做出选择）；非法 value 同样按拒绝处理；thinking_delta 走 LlmBackend 默认 chat_stream（一条 delta = 全文）。
全套 35 测试通过（test_user_choice + test_agent_parallel + test_intent）。
