"""Authoritative todo state for one agent plan.

The service is deliberately UI-agnostic. It owns validation-friendly writes,
stable identities (delegated to PlanStore), revisions, and status transitions.
Consumers receive immutable full snapshots and never infer state locally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from kyagent.planner import PlanSnapshot, PlanStore, PlanTodoItem


_STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
_PRIORITIES = {"high", "medium", "low"}


@dataclass(frozen=True)
class TodoSnapshot:
    plan_id: str
    revision: int
    items: tuple[dict[str, Any], ...]

    @classmethod
    def from_plan(cls, plan: PlanSnapshot) -> "TodoSnapshot":
        return cls(
            plan_id=plan.plan_id,
            revision=plan.todo_revision,
            items=tuple(item.to_dict() for item in plan.todos),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "revision": self.revision,
            "items": list(self.items),
        }


class TodoService:
    """Single write boundary for durable todo state."""

    def __init__(self, store: PlanStore):
        self.store = store

    def snapshot(self, plan_id: str) -> TodoSnapshot:
        return TodoSnapshot.from_plan(self.store.get(plan_id))

    def replace(
        self, plan_id: str, items: Iterable[PlanTodoItem]
    ) -> tuple[PlanSnapshot, TodoSnapshot]:
        raw_items = list(items)
        if len(raw_items) > 20:
            raise ValueError("a todo snapshot may contain at most 20 items")
        normalized: list[PlanTodoItem] = []
        for index, item in enumerate(raw_items):
            content = item.content.strip()
            if not content:
                raise ValueError(f"todo item {index} has empty content")
            if len(content) > 500:
                raise ValueError(f"todo item {index} content exceeds 500 characters")
            if item.status not in _STATUSES:
                raise ValueError(f"todo item {index} has invalid status")
            if item.priority not in _PRIORITIES:
                raise ValueError(f"todo item {index} has invalid priority")
            normalized.append(PlanTodoItem(
                todo_id=item.todo_id.strip(),
                content=content,
                status=item.status,
                priority=item.priority,
            ))
        plan = self.store.replace_todos(plan_id, normalized)
        return plan, TodoSnapshot.from_plan(plan)

    def set_statuses(
        self, plan_id: str, statuses: dict[str, str]
    ) -> tuple[PlanSnapshot, TodoSnapshot]:
        current = self.store.get(plan_id)
        invalid = set(statuses.values()) - _STATUSES
        if invalid:
            raise ValueError(f"invalid todo statuses: {sorted(invalid)}")
        known_ids = {item.todo_id for item in current.todos}
        unknown_ids = set(statuses) - known_ids
        if unknown_ids:
            raise ValueError(f"unknown todo ids: {sorted(unknown_ids)}")
        updated = [
            PlanTodoItem(
                todo_id=item.todo_id,
                content=item.content,
                status=statuses.get(item.todo_id, item.status),  # type: ignore[arg-type]
                priority=item.priority,
            )
            for item in current.todos
        ]
        if all(before.status == after.status for before, after in zip(current.todos, updated)):
            return current, TodoSnapshot.from_plan(current)
        return self.replace(plan_id, updated)

    def finalize(
        self, plan_id: str, *, success: bool
    ) -> tuple[PlanSnapshot, TodoSnapshot]:
        """Close unresolved model-authored todos without claiming they completed.

        A final answer is not evidence that every listed task succeeded. Pending
        work is therefore cancelled; an active item becomes failed only when the
        enclosing run itself failed. Completed/failed items remain unchanged.
        """
        current = self.store.get(plan_id)
        statuses: dict[str, str] = {}
        for item in current.todos:
            if item.status == "pending":
                statuses[item.todo_id] = "cancelled"
            elif item.status == "in_progress":
                statuses[item.todo_id] = "cancelled" if success else "failed"
        return self.set_statuses(plan_id, statuses)
