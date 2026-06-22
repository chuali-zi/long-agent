from __future__ import annotations

import pytest

from kyagent.planner import PlanStore, PlanTodoItem
from kyagent.todos import TodoService


def _service(tmp_path):  # noqa: ANN001
    store = PlanStore(tmp_path / "plans.db")
    plan = store.create_run_plan(trace_id="trace", user="tester", title="todo contract")
    return store, plan, TodoService(store)


def test_todo_service_emits_monotonic_full_snapshots_and_stable_ids(tmp_path):  # noqa: ANN001
    store, plan, service = _service(tmp_path)
    try:
        plan, first = service.replace(plan.plan_id, [
            PlanTodoItem("", "inspect state", status="in_progress", priority="high"),
            PlanTodoItem("", "report result"),
        ])
        first_ids = [item["todo_id"] for item in first.items]

        plan, second = service.replace(plan.plan_id, [
            PlanTodoItem("", "inspect state", status="completed", priority="high"),
            PlanTodoItem("", "report result", status="in_progress"),
        ])

        assert second.revision == first.revision + 1
        assert [item["todo_id"] for item in second.items] == first_ids
        assert [item["status"] for item in second.items] == ["completed", "in_progress"]
        assert second.to_dict()["items"] == list(second.items)
    finally:
        store.close()


def test_todo_service_rejects_invalid_snapshot_atomically(tmp_path):  # noqa: ANN001
    store, plan, service = _service(tmp_path)
    try:
        plan, original = service.replace(plan.plan_id, [PlanTodoItem("", "keep")])
        with pytest.raises(ValueError, match="empty content"):
            service.replace(plan.plan_id, [PlanTodoItem("", "")])

        loaded = service.snapshot(plan.plan_id)
        assert loaded.revision == original.revision
        assert [item["content"] for item in loaded.items] == ["keep"]
    finally:
        store.close()


def test_todo_service_terminal_reconciliation_is_truthful_and_idempotent(tmp_path):  # noqa: ANN001
    store, plan, service = _service(tmp_path)
    try:
        plan, started = service.replace(plan.plan_id, [
            PlanTodoItem("", "done", status="completed"),
            PlanTodoItem("", "active", status="in_progress"),
            PlanTodoItem("", "later", status="pending"),
        ])
        plan, closed = service.finalize(plan.plan_id, success=False)
        assert [item["status"] for item in closed.items] == [
            "completed", "failed", "cancelled",
        ]
        assert closed.revision == started.revision + 1

        plan, repeated = service.finalize(plan.plan_id, success=False)
        assert repeated.revision == closed.revision
    finally:
        store.close()
