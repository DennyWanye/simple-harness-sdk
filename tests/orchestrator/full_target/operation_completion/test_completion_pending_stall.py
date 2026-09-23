"""OCC-12: a prepared MIXED scope is external pending work, not idle failure."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_scoped_content_commit import _mixed_world  # noqa: E402

from agent_orchestrator.contracts.state_machines import MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.completion_status import read_occurrence_completion
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _pending_stall_loop(world, tmp_path) -> Orchestrator:
    """Use the real loop code over the exact Store that committed the preparation.

    ``Orchestrator.__aenter__`` owns a new ``evidence_root/orchestrator.db``.  This
    fixture already owns the only Store with the approved Spec, plan, accepted
    result and scoped contribution, so opening a second database would test an
    empty world.  The constructed production Orchestrator is deliberately pointed
    at that live Store and its matching CommitService before installing the real
    hierarchical assembly.
    """

    loop = Orchestrator(
        OrchestratorConfig(evidence_root=tmp_path / "pending-stall-evidence"),
        RoleScriptedProvider({"worker": [], "planner": []}),
    )
    loop._store = world.store  # noqa: SLF001 - real fixture Store, not a replacement
    loop._commit = world.service  # noqa: SLF001 - matching production writer
    loop.install_hierarchical()
    return loop


def _counts(world) -> tuple[int, int, int]:
    connection = world.store.connection
    return tuple(
        int(connection.execute(query, (world.mission.id,)).fetchone()[0])
        for query in (
            "SELECT count(*) FROM attempts WHERE mission_id=?",
            "SELECT count(*) FROM dispatch_intents WHERE mission_id=?",
            "SELECT count(*) FROM events WHERE mission_id=?",
        )
    )


def test_prepared_mixed_effect_pending_skips_idle_stall_and_stop(tmp_path) -> None:
    """OCC-12: no worker queue is not a reason to fail a pending real effect."""

    async def case() -> None:
        world, _, _, task, stored, _ = _mixed_world(tmp_path, with_output=True)
        mission = world.store.get_mission(world.mission.id)
        assert mission is not None and mission.status is MissionStatus.ACTIVE
        assert task.status is TaskStatus.VERIFYING
        assert task.accepted_result_id == stored.envelope.id

        occurrence = world.store.connection.execute(
            "SELECT occurrence_id FROM plan_memberships WHERE mission_id=? AND task_id=?",
            (mission.id, task.id),
        ).fetchone()
        assert occurrence is not None
        completion = read_occurrence_completion(
            world.store, mission.id, occurrence["occurrence_id"]
        )
        assert completion.preparation_ready
        assert not completion.effects_ready and not completion.complete
        assert completion.scope.required_effect_keys

        loop = _pending_stall_loop(world, tmp_path)
        before = _counts(world)
        await loop._record_hierarchical_stall()  # noqa: SLF001 - target idle gate
        assert mission.id not in loop._stalled_at  # noqa: SLF001 - no false idle fingerprint
        assert _counts(world) == before

        # Mimic an old in-memory idle fingerprint without faking any completion
        # fact: confirmation must independently re-read the durable preparation /
        # effect state and still refuse to stop the Mission.
        dispatch = loop.hierarchical
        assert dispatch is not None
        admissions = dispatch.admissions(mission.id)
        loop._stalled_at[mission.id] = loop._stall_fingerprint(  # noqa: SLF001
            mission, admissions, world.store.list_tasks(mission.id)
        )
        assert await loop._confirm_and_stop_stalled() is False  # noqa: SLF001
        after = world.store.get_mission(mission.id)
        current_task = world.store.get_task(task.id)
        assert after is not None and after.status is MissionStatus.ACTIVE
        assert current_task is not None and current_task.status is TaskStatus.VERIFYING
        assert current_task.accepted_result_id == stored.envelope.id
        assert _counts(world) == before
        assert not any(
            event.type in {"HierarchicalMissionStalled", "MissionFailed"}
            for event in world.store.list_events(mission.id)
        )

    asyncio.run(case())
