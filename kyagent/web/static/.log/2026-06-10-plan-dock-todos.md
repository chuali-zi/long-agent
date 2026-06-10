# 2026-06-10 Plan 区域改为自动打印动态 todo

## 背景
前端 Plan 区域原本只渲染固定 4 阶段 steps（receive/reason/verify/respond），
每次都一样。需求：该区域应自动显示模型每轮生成的 todo，每次都不同。

## 钩子定位
后端钩子已存在，无需新增：
- `kyagent/agent/core.py` `_extract_todo_plan()` 解析模型文本中的 `TODO n:` /
  复选框行 → `PlanStore.replace_todos()` 写入 `plan.todos`（动态、每轮不同）。
- `_emit_plan()` 通过 `plan_snapshot` 事件把含 todos 的完整快照推送前端。
前端此前忽略 `plan.todos`，故只显示静态 steps。

## 改动（index.html）
1. `normalizePlanStatus` 兼容 todo 状态：completed/cancelled→done、in_progress→running。
2. 新增 `normalizePlanTodos()`，映射 {todo_id, content, status}。
3. `renderBackendPlan()` 优先渲染 `plan.todos`，无 todo 时回退到 steps 占位。

效果：任务开始先显示阶段占位，模型给出 TODO 计划后自动切换为动态 todo 列表。
