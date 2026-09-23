"""O10: a cancelled task's active foreign attempt remains a commit convergence fact."""

from __future__ import annotations

import pytest
from test_h1h_commit_guard import _plan_revision_count, _setup
from test_plan_commits import ROOT_TASK, TOOLS, _world

from agent_orchestrator.contracts import Attempt, Budget, Task
from agent_orchestrator.contracts.state_machines import AttemptStatus, TaskStatus
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.runtime.planning_operations import StoreOperationReader, read_running_work


def _cancelled_task_with_active_foreign_attempt(world, *, expires_at: float) -> str:
    """Persist the O10 state using only real Task/Attempt Store writers.

    This is deliberately not an operation-reader double: the Task has already been
    cancelled (as a retired/replaced leaf can be), while the independently durable
    worker Attempt is still RUNNING under a different executor's lease.
    """

    task = Task(
        id=ROOT_TASK,
        mission_id=world.mission.id,
        parent_task_ids=(),
        dependency_ids=(),
        goal="retired task still has a provider call in flight",
        rationale="O10 Store fixture",
        success_criteria=("prove convergence",),
        verification_policy=("format_check",),
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=1_000, max_attempts=2),
        priority=1.0,
        status=TaskStatus.CANCELLED,
        version=1,
    )
    world.store.insert_task(task, ordinal=1)
    attempt_id = "o10-retired-task:attempt-1"
    world.store.insert_attempt(
        Attempt(
            id=attempt_id,
            task_id=task.id,
            mission_id=world.mission.id,
            role="worker",
            model="fixture",
            prompt_version="worker-hierarchical-v2",
            context_version="ctx",
            budget_reserved=Budget(max_tokens=500),
            lease_owner="foreign-executor",
            lease_expires_at=expires_at,
            status=AttemptStatus.RUNNING,
            retry_of=None,
            created_at=float(world.store.now),
            version=1,
            ordinal=1,
            creation_key="o10-retired-task:attempt-1",
            input_id="o10-input",
        )
    )
    return attempt_id


@pytest.mark.parametrize("expired", (False, True), ids=("live-foreign-lease", "expired-lease"))
def test_o10_cancelled_task_active_attempt_is_not_filtered_and_blocks_real_commit(
    tmp_path, expired: bool
) -> None:
    world = _world(tmp_path, key=f"o10-retired-running-{expired}")
    expires_at = float(world.store.now) + (-1.0 if expired else 86_400.0)
    attempt_id = _cancelled_task_with_active_foreign_attempt(world, expires_at=expires_at)

    # This is the production Store reader used by preview and the pre-commit guard.
    # Expiry never converts a call into a negative proof, so both leases remain in
    # the immutable RuntimeWorkSnapshot.
    runtime = read_running_work(
        world.mission.id,
        ("retiring-method-instance",),
        reader=StoreOperationReader(world.store),
    )
    assert runtime.retiring_instance_ids == ("retiring-method-instance",)
    assert runtime.requires_convergence is True
    assert runtime.live_attempt_refs == (
        {
            "attempt_id": attempt_id,
            "task_id": ROOT_TASK,
            "status": str(AttemptStatus.RUNNING),
            "lease_owner": "foreign-executor",
            "lease_expires_at": expires_at,
            "version": 1,
            "json": world.store.get_attempt(attempt_id).to_json(),
        },
    )

    # Build the official planning admission after the Store state exists. Its
    # commit-time producer reread must retain the same evidence and reject for
    # convergence, without releasing a reservation, completing the call, or taking
    # the other executor's lease.
    _api, _grant, admission = _setup(world)
    before_changes = world.store.connection.total_changes
    with pytest.raises(PlanCommitRejected, match="RUNNING_WORK_UNRESOLVED"):
        world.service.commit_planning_revision(world.command, world.principal, admission=admission)

    assert _plan_revision_count(world) == 0
    assert world.store.connection.total_changes == before_changes
    task = world.store.get_task(ROOT_TASK)
    attempt = world.store.get_attempt(attempt_id)
    assert task is not None and task.status is TaskStatus.CANCELLED
    assert attempt is not None and attempt.status is AttemptStatus.RUNNING
    assert attempt.lease_owner == "foreign-executor"
    assert attempt.lease_expires_at == expires_at
