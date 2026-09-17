# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3d / defect D4: a hierarchical Mission opens no legacy management round.

``_request_management`` had exactly one switch — ``dynamic_graph`` — and no branch for
the mode.  So a hierarchical Task that failed verification opened a Manager intent, the
Manager answered with a legacy ``TaskGraphChange``, and ``commit_graph_change`` refused
every one of them unconditionally:

    ManagementRequested(round 1)
      → PolicySuggestionRefused{operations:["add_task"]}
      → HierarchicalGraphChangeRefused{reason:"SEMANTICS_IS_HIERARCHICAL"}
      → ManagementDecided{decision:"rejected"}

In the Grok acceptance run that loop burned ``max_manager_rounds`` on 20 L1 episodes
(``management_exhausted``) and ``no_progress_limit`` on 3 L4 episodes
(``no_progress``) — in both cases replacing the reason the Task really failed for with
the reason a repair loop closed by construction ran out.

The door that *is* open is a ``PlanRevisionProposal``.  Until a hierarchical Manager
exists to walk through it, the honest answer is to open no round and record that.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_end_to_end import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    build_world,
    committed,
)

from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    MANAGEMENT_NOT_APPLICABLE,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402


def _insert_legacy_task(world, task_id: str) -> None:
    from agent_orchestrator.contracts import Budget, Task, TaskStatus

    world.store.insert_task(
        Task(
            id=task_id,
            mission_id=world.mission.id,
            parent_task_ids=(),
            dependency_ids=(),
            goal="a legacy task that failed verification",
            rationale="the subject of the management round",
            success_criteria=("file:a.md",),
            verification_policy=("format_check",),
            allowed_tools=world.mission.allowed_tools,
            budget=Budget(max_tokens=1_000, max_attempts=2),
            priority=1.0,
            status=TaskStatus.ACTIVE,
            version=1,
        ),
        ordinal=1 + len(world.store.list_tasks(world.mission.id)),
    )


def _drive_management(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS):
    """Ask ``_request_management`` for a real task of a real Mission, and see what it did."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = (
        committed(evidence, key=f"p23d-manage-{mode}", mode=mode, demand=True)
        if mode == HIERARCHICAL_SEMANTICS
        else build_world(evidence, key=f"p23d-manage-{mode}", mode=mode)
    )
    mission_id = world.mission.id
    if mode == HIERARCHICAL_SEMANTICS:
        task_id = next(
            item.id for item in world.store.list_tasks(mission_id) if item.kind != "compound"
        )
    else:
        # A legacy Mission has no plan to commit through this entry (§18.5 rule 1), so
        # it gets the display row a legacy Planner would have written.
        task_id = "task-legacy-1"
        _insert_legacy_task(world, task_id)
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case():
        async with Orchestrator(config, RoleScriptedProvider({"manager": []})) as loop:
            if mode == HIERARCHICAL_SEMANTICS:
                world.env.semantics = HtnStore(loop.store)
                loop.install_hierarchical(planning=world.env)
            else:
                loop.install_hierarchical(planning=None)
            mission = loop.store.get_mission(mission_id)
            task = loop.store.get_task(task_id)
            assert mission is not None and task is not None
            intent = await loop._request_management(
                mission,
                task,
                trigger="verification_failed:attempt-1",
                result_id=None,
                attempt_id=None,
            )
            manager_intents = [
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == mission_id and item.kind == "manager"
            ]
            events = [item for item in loop.store.list_events(mission_id)]
            return intent, manager_intents, events

    return asyncio.run(case())


def test_a_hierarchical_mission_opens_no_legacy_manager_intent(tmp_path) -> None:
    """The invariant that keeps the closed loop closed.

    **Mutation**: drop the mode branch from ``_request_management`` and the Manager
    intent comes back — which is the whole of defect D4.
    """

    intent, manager_intents, _ = _drive_management(tmp_path)
    assert intent is None
    assert manager_intents == []


def test_the_refusal_is_recorded_where_an_operator_reads_it(tmp_path) -> None:
    _, _, events = _drive_management(tmp_path)
    recorded = [item for item in events if item.type == MANAGEMENT_NOT_APPLICABLE]
    assert len(recorded) == 1
    payload = recorded[0].payload
    assert payload["reason"] == "SEMANTICS_IS_HIERARCHICAL"
    assert payload["redirect"] == "commit_plan_revision"
    assert payload["trigger"] == "verification_failed:attempt-1"


def test_no_management_round_is_counted_and_no_decision_is_recorded(tmp_path) -> None:
    """Nothing is spent: neither a ``max_manager_rounds`` slot nor a no-progress one.

    ``ManagementRequested`` is what ``_manager_rounds`` counts, and
    ``ManagementDecided{rejected}`` is the line the old loop wrote every round — the
    one §4.3 of the diagnosis asked to keep out of the no-progress reckoning.  Neither
    exists any more, which is the strongest form of "it does not count".
    """

    _, _, events = _drive_management(tmp_path)
    kinds = {item.type for item in events}
    assert "ManagementRequested" not in kinds
    assert "ManagementDecided" not in kinds
    assert "HierarchicalGraphChangeRefused" not in kinds


def test_the_legacy_mode_still_opens_its_management_round(tmp_path) -> None:
    """The other half: the branch is about the mode, not about switching management off."""

    intent, manager_intents, events = _drive_management(tmp_path, mode="legacy")
    assert intent is not None
    assert [item.kind for item in manager_intents] == ["manager"]
    assert MANAGEMENT_NOT_APPLICABLE not in {item.type for item in events}
    assert "ManagementRequested" in {item.type for item in events}


@pytest.mark.parametrize("mode", [HIERARCHICAL_SEMANTICS, "legacy"])
def test_the_record_is_written_once_per_trigger_not_once_per_cycle(tmp_path, mode) -> None:
    """The event id is keyed by the subject, exactly like the intent it stands in for."""

    _, _, first = _drive_management(tmp_path / mode, mode=mode)
    written = [item for item in first if item.type == MANAGEMENT_NOT_APPLICABLE]
    assert len(written) == (1 if mode == HIERARCHICAL_SEMANTICS else 0)
