from __future__ import annotations

from kyagent.planner import PlanStore


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
    done = store.set_status(plan.plan_id, "complete", current_step="respond")
    assert done.status == "complete"
    store.close()

    reopened = PlanStore(db)
    loaded = reopened.get(plan.plan_id)
    assert loaded.status == "complete"
    assert loaded.steps[1].detail == "Checking tools"
    assert reopened.latest(1)[0].plan_id == plan.plan_id
    reopened.close()
