from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_h1i_production_entry import _config, _seed_new_protocol

from agent_orchestrator.contracts import Budget, Task, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def test_planner_task_state_projection_holds_one_write_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competing process cannot split outcome and Task-state collection.

    The competing UPDATE is attempted after ``occurrence_outcomes`` has read the
    authoritative execution outcome but before the package collector reads the Task
    row again.  The Planner-intent wrapper must already hold BEGIN IMMEDIATE, so the
    second connection is refused and the frozen package carries one coherent state.
    """

    async def case() -> None:
        async with Orchestrator(
            _config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as loop:
            mission, _env, binding, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-wait-snapshot"
            )
            task = Task(
                id=str(binding.task_id),
                mission_id=mission.id,
                parent_task_ids=(),
                dependency_ids=(),
                goal="WAIT snapshot target",
                rationale="exercise the authoritative v6 task-state projection",
                success_criteria=("state:completed",),
                verification_policy=("rule_check",),
                allowed_tools=(),
                budget=Budget(max_tokens=1_000, max_attempts=1),
                priority=1.0,
                status=TaskStatus.ACTIVE,
                version=1,
            )
            loop.store.insert_task(task, ordinal=999)
            failed = replace(task, status=TaskStatus.FAILED, version=task.version + 1)
            original = dispatch.occurrence_outcomes
            lock_refused = False

            def outcomes_with_competing_writer(mission_id: str, network: object):
                nonlocal lock_refused
                outcomes = original(mission_id, network)
                contender = sqlite3.connect(
                    loop.store.path, timeout=0, isolation_level=None
                )
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        contender.execute(
                            "UPDATE tasks SET status = ?, version = ?, json = ?, "
                            "updated_at = updated_at + 1 WHERE task_id = ?",
                            (
                                str(failed.status),
                                failed.version,
                                json.dumps(
                                    failed.to_json(),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                                failed.id,
                            ),
                        )
                    lock_refused = True
                finally:
                    contender.close()
                return outcomes

            monkeypatch.setattr(dispatch, "occurrence_outcomes", outcomes_with_competing_writer)
            intent = await loop._create_planner_intent(mission.id, ordinal=1)

            assert lock_refused
            stored = loop.store.get_task(task.id)
            assert stored is not None
            assert stored.status is TaskStatus.ACTIVE
            assert stored.version == task.version
            rows = [
                row
                for section in ("open_compound_goals", "committed_primitives")
                for row in intent.config["planning_package"]["plan"][section]
                if row.get("goal_id", row.get("task_id")) == task.id
            ]
            assert rows
            assert {
                (row["task_status"], row["task_version"], row["occurrence_outcome"])
                for row in rows
            } == {(str(TaskStatus.ACTIVE), task.version, "RUNNING")}

    asyncio.run(case())
