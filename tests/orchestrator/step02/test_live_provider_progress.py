"""A live multi-call Worker must not time out from a terminal-only turn bound."""

from __future__ import annotations

import asyncio

from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step, role_of
from test_single_task_closure import GOOD, PROPOSAL, config, spec, worker_script

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator


class PacedWorker(RoleScriptedProvider):
    async def invoke(self, request, *, cancel):
        if role_of(request) == "worker":
            await asyncio.sleep(0.25)
        return await super().invoke(request, cancel=cancel)


def test_live_worker_progress_prevents_false_stall_without_new_attempt(tmp_path):
    async def case():
        worker = worker_script(GOOD)
        provider = PacedWorker({
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker,
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        })
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.12, stall_seconds=0.65),
            provider, poll_interval=0.02,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("live-provider-progress"))
            await asyncio.wait_for(orchestrator.run(), 10)
            task, = orchestrator.store.list_tasks(mission.id)
            attempts = orchestrator.store.list_attempts(task.id)
            assert orchestrator.store.count_events(mission.id, "AttemptTimedOut") == 0
            assert len(attempts) == 1
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            assert provider.by_role["worker"] == len(worker)
            assert attempts[0].progress_marker is not None

    asyncio.run(case())
