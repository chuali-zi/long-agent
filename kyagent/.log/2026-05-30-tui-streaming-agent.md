# 2026-05-30 TUI streaming: Agent 进度回调接入

分支: `experimental/tui-streaming`，目标文件: `kyagent/agent/core.py`（只改一个文件）。

## 改动
1. import `ProgressCallback, ProgressEvent, noop_progress`。
2. `Agent.__init__` 新增 `on_progress` 参数（默认 `noop_progress`）；赋值后不再变更，保证 worker 线程读到的 callable 不变。
3. `from_config` / `build_agent` 透传 `on_progress`。
4. 加 `_emit(event)`：`try/except` 吞掉回调异常，审计照常。
5. 6 个点位 emit：
   - `agent_start` — ask 开头（L127）
   - `thinking_start` — 每轮 LLM 前（L192）
   - `thinking_end` — LLM 返回后（L211）
   - `error(llm_error)` — LLM 异常分支（L202）
   - `tool_call_start` — `_handle_tool_use` 入口（仅 tool 名）+ prepare_call 后补带 argv 的一次
   - `tool_call_end` — try/finally 兜底（一定发出）
   - `agent_final` — 无 tool_uses 终结分支
   - `error(max_iterations)` — 迭代超限
6. 把原 `_handle_tool_use` 重命名为 `_handle_tool_use_inner`；新 `_handle_tool_use` 是 try/finally 外壳。

## 不改
audit、messages、并行选路、tests/、token 级流。

## 后续
CLI `--tui` 接线、TUI 端订阅事件（task #6/#8）。
