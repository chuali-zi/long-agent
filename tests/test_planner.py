from __future__ import annotations

import sqlite3

import pytest

from kyagent.planner import PlanStore, PlanTodoItem


def test_plan_store_persists_steps(tmp_path):
    db = tmp_path / "plans.db"
    store = PlanStore(db)
    plan = store.create_run_plan(trace_id="trace-1", user="tester", title="Investigate disk")
    assert plan.status == "running"
    assert plan.current_step == "receive"
    assert [s.step_id for s in plan.steps] == ["receive", "reason", "verify", "respond"]

    updated = store.set_step(plan.plan_id, "reason", "running", "Checking tools")
    assert updated.current_step == "reason"
    assert updated.steps[1].status == "running"
    with_todos = store.replace_todos(plan.plan_id, [
        PlanTodoItem("todo-1", "Inspect current state", status="in_progress", priority="high"),
        PlanTodoItem("todo-2", "Report result", status="pending", priority="medium"),
    ])
    assert [t.content for t in with_todos.todos] == ["Inspect current state", "Report result"]
    assert with_todos.todo_revision == 1
    original_ids = {todo.content: todo.todo_id for todo in with_todos.todos}
    reordered = store.replace_todos(plan.plan_id, [
        PlanTodoItem("", "Report result", status="in_progress", priority="medium"),
        PlanTodoItem("", "Inspect current state", status="completed", priority="high"),
    ])
    assert reordered.todo_revision == 2
    assert {todo.content: todo.todo_id for todo in reordered.todos} == original_ids
    done = store.set_status(plan.plan_id, "complete", current_step="respond")
    assert done.status == "complete"
    store.close()

    reopened = PlanStore(db)
    loaded = reopened.get(plan.plan_id)
    assert loaded.status == "complete"
    assert loaded.steps[1].detail == "Checking tools"
    assert loaded.todos[0].status == "in_progress"
    assert loaded.todos[0].priority == "medium"
    assert loaded.todo_revision == 2
    assert reopened.latest(1)[0].plan_id == plan.plan_id
    reopened.close()


def test_plan_store_migrates_legacy_database_with_todo_revision(tmp_path):
    db = tmp_path / "legacy-plans.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE plans (
      plan_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, user TEXT NOT NULL,
      title TEXT NOT NULL, status TEXT NOT NULL, current_step TEXT NOT NULL DEFAULT '',
      metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE plan_steps (
      plan_id TEXT NOT NULL, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
      updated_at REAL NOT NULL, PRIMARY KEY (plan_id, step_id)
    );
    CREATE TABLE plan_todos (
      plan_id TEXT NOT NULL, todo_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      content TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL,
      updated_at REAL NOT NULL, PRIMARY KEY (plan_id, todo_id)
    );
    """)
    conn.close()

    store = PlanStore(db)
    plan = store.create_run_plan(trace_id="trace-old", user="tester", title="migrated")
    assert plan.todo_revision == 0
    updated = store.replace_todos(plan.plan_id, [PlanTodoItem("", "first")])
    assert updated.todo_revision == 1
    assert updated.todos[0].todo_id.startswith("todo-")
    store.close()


def test_replace_todos_rolls_back_entire_snapshot_on_invalid_item(tmp_path):
    store = PlanStore(tmp_path / "plans.db")
    plan = store.create_run_plan(trace_id="trace-rollback", user="tester", title="rollback")
    original = store.replace_todos(plan.plan_id, [PlanTodoItem("", "keep me")])

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_todos(plan.plan_id, [
            PlanTodoItem("", "valid"),
            PlanTodoItem("", "invalid", status="not-a-status"),  # type: ignore[arg-type]
        ])

    loaded = store.get(plan.plan_id)
    assert [item.content for item in loaded.todos] == ["keep me"]
    assert loaded.todo_revision == original.todo_revision
    store.close()
