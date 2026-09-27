# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""One Mission's refused collection never stops another Mission (NEXT-TG-1.0 §5.1).

The refusal branch of the SUBMITTED collection loop read ``intent.id`` — the field
is ``intent_id`` — so the first BudgetError / CommitRejected meant to be isolated
raised AttributeError out of ``run()`` and stopped every Mission.  Two real
Missions on the real Store and loop: A's collection is refused three times (two
kinds), B carries on and completes; A is then collected once, with its one Worker
call and its usage kept, and the refusal is noted once per distinct reason.
"""

from __future__ import annotations

import asyncio

from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step
from step02.test_single_task_closure import GOOD, PROPOSAL, config, spec, worker_script

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator


def test_refused_collection_is_isolated_and_noted_once(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL), proposal_step(PROPOSAL)],
            "worker": worker_script(GOOD) + worker_script(GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission_a = await orchestrator.submit_mission(spec("mission-a"))
            mission_b = await orchestrator.submit_mission(spec("mission-b"))
            original = orchestrator._collect_attempt
            refusals = [
                CommitRejected("proposal rejected for this round"),
                CommitRejected("proposal rejected for this round"),
                BudgetError("reservation refused for this round"),
            ]
            refused: list[str] = []

            async def collect_attempt(intent, result):  # type: ignore[no-untyped-def]
                if intent.mission_id == mission_a.id and refusals:
                    refused.append(intent.intent_id)
                    raise refusals.pop(0)
                return await original(intent, result)

            orchestrator._collect_attempt = collect_attempt  # type: ignore[method-assign]
            await orchestrator.run()
            store = orchestrator.store
            final_a, final_b = store.get_mission(mission_a.id), store.get_mission(mission_b.id)
            assert final_b.status is MissionStatus.COMPLETED, orchestrator.progress_log
            assert final_a.status is MissionStatus.COMPLETED, orchestrator.progress_log
            assert len(refused) == 3 and len(set(refused)) == 1  # the same intent, retried
            # nothing was redone: one Worker turn per Mission, no extra model call
            assert provider.by_role["worker"] == 2 * len(worker_script(GOOD))
            for mission in (final_a, final_b):
                task = store.list_tasks(mission.id)[0]
                assert task.attempt_count == 1
            notes = [line for line in orchestrator.progress_log if "collection refused" in line]
            assert len(notes) == 2, notes  # once per distinct reason, not once per round
            assert all(refused[0] in line for line in notes)

    asyncio.run(case())
