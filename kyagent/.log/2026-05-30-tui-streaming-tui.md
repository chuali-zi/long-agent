# 2026-05-30 · tui.py 流式 TUI 实现

## 范围
覆盖式 Write `kyagent/tui.py`。不动 agent、cli、tests。

## 屏幕布局
rich.layout.Layout 三段：header(3) / body(对话历史，flex) / status(3)。
Live 只在 `_run_turn()` 期间持有，prompt 在 Live 外执行——避免光标乱跳。
进度事件 → status 行映射严格按需求：thinking / tool_call_start /
tool_call_end / agent_final / error 都有对应 spinner+tone。

## 关键决策
* prompt_toolkit 用 try/except 包，缺包回退 input()；patch_stdout(raw=True)。
* 模块级 `_LIVE_LOCK` 包 `handle_progress` 的状态变更和 `live.update()`，
  worker 线程安全。
* markup 防穿透：history 正文和 confirm 用 `rich.text.Text()` 包裹。
* `run_tui` 内部用 forwarder dict 解决 "构造 app 前要把 on_progress
  喂给 Agent" 的鸡生蛋问题。

## 保留 / 删除
保留：`confirm_request_lines`, `tool_rows`, `run_tui`, `TuiApp`, `TuiSession`。
删除：`trace_event_summaries`, `_event_summary`（旧 timeline 视图）；`_clip` 留作内部工具。

## 偏离
无。Agent.from_config 已支持 on_progress kwarg（core.py:111），不需要等
另一个 agent。冒烟 import 通过。
