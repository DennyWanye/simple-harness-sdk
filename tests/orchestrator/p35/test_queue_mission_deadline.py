# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A02: real SDK slot wait survives stall/lease windows; Mission time still runs.

The workload graph is controlled. Scheduling, SDK requests, admission, lease
renewal, Mission expiration and accounting run in the actual Orchestrator.
"""

from __future__ import annotations

import asyncio
import time

from graph_helpers7 import node, spec
from test_multi_mission_load import _until
from test_multi_profile_load import Load
from test_provider_budget_guard import Counter, grants

from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from simple_harness.contracts import RunId


def test_queued_attempt_renews_without_loss_until_original_mission_deadline(tmp_path):
    async def exercise():
        load = Load()
        profiles = {
            name: RuntimeProfile(name, load.provider(name), model)
            for name, model in (("workers", "worker-model"), ("critics", "critic-model"))
        }
        cfg = OrchestratorConfig(
            evidence_root=tmp_path,
            max_concurrency=3,
            max_concurrent_model_calls=1,
            candidates_per_task=1,
            dynamic_graph=False,
            verifier_workers=2,
            lease_seconds=1,
            sdk_lease_ttl_seconds=0.5,
            stall_seconds=0.15,
        )
        async with Orchestrator(
            cfg,
            profiles=profiles,
            routing=RoutingRules("workers", by_role={"critic": "critics"}),
            provider_token_estimator=Counter(1000),
            poll_interval=0.002,
        ) as orch:

            async def add(label, deadline=None):
                mission = await orch.submit_mission(
                    spec(
                        label,
                        goal=label,
                        success_criteria=("file:a.md",),
                        budget=Budget(
                            max_tokens=200000, max_attempts=12, max_runtime_seconds=deadline
                        ),
                    )
                )
                planning = orch.commit.begin_planning(mission.id)
                tasks, _ = orch.commit.commit_task_graph(
                    mission.id,
                    TaskGraphProposal.from_json(
                        {
                            "tasks": [
                                node(
                                    "A",
                                    tokens=80000,
                                    verification_policy=[
                                        "format_check",
                                        "rule_check",
                                        "critic_review",
                                    ],
                                )
                            ]
                        }
                    ),
                    base_version=planning.version,
                    source={"planner": "controlled queue workload"},
                )
                return mission, tasks[0]

            await add("A")
            runner = asyncio.create_task(orch.run())
            try:
                await _until(lambda: load.active["critics"] == 1, runner)
                mission, task = await add("C", 3)

                def waiting():
                    return next(
                        (
                            i
                            for i in orch.store.list_intents("SUBMITTED")
                            if i.mission_id == mission.id
                            and i.kind == "attempt"
                            and orch._provider_admission.waiting_for_slot(
                                agent_id=i.agent_id, turn_id=i.expected_turn_id
                            )
                        ),
                        None,
                    )

                await _until(waiting, runner)
                original = waiting()
                attempt = orch.store.get_attempt(original.subject_id)
                old_expiry = attempt.lease_expires_at
                observed = time.monotonic()
                while time.monotonic() - observed < 1.2:
                    await asyncio.sleep(0.02)
                    current = waiting()
                    assert current is not None and current.intent_id == original.intent_id
                    assert current.agent_id == original.agent_id
                    assert current.expected_turn_id == original.expected_turn_id
                    assert orch.store.get_mission(mission.id).status is MissionStatus.ACTIVE
                    assert len(orch.store.list_attempts(task.id)) == 1
                    assert not any(call[1] == "C" for call in load.calls)
                current_attempt = orch.store.get_attempt(attempt.id)
                assert current_attempt.lease_expires_at > old_expiry
                live = await orch.bridge_for(original).liveness(
                    agent_id=original.agent_id,
                    turn_id=original.expected_turn_id,
                )
                assert live.alive and live.blocked and live.blocker["billable"] is False
                assert not orch.bridge_for(original).usage_facts(agent_id=original.agent_id)
                await _until(
                    lambda: orch.store.get_mission(mission.id).status is MissionStatus.FAILED,
                    runner,
                )
                failed = orch.store.get_mission(mission.id)
                assert failed.stop_reason == "budget_exhausted"
                assert failed.budget.max_runtime_seconds == 3
                assert failed.final_report["detail"]["dimension"] == "runtime"
                assert failed.created_at == mission.created_at
                assert failed.final_report["detail"]["elapsed_seconds"] >= 3
                assert failed.final_report["detail"]["human_wait_seconds"] == 0
                load.gates["A"].set()
                await asyncio.wait_for(runner, 8)
                assert len(orch.store.list_attempts(task.id)) == 1
                assert not any(call[1] == "C" for call in load.calls)
                assert not (
                    {"AttemptLost", "AttemptTimedOut"}
                    & {event.type for event in orch.store.list_events(mission.id)}
                )
                records = orch.assembled.pools["workers"].runtime.uow.list_provider_invocations(
                    RunId(original.agent_id),
                )
                assert len(records) == 1 and records[0].handoff_attempt == 0
                with orch.store.transaction():
                    costs = orch.commit.ledger.costs_report(mission.id)
                account = next(a for a in costs["accounts"] if a["scope"] == "mission")
                assert account["settled_tokens"] == account["reserved_tokens"] == 0
                assert not any(
                    row["state"] in {"RESERVED", "HANDED_OFF", "UNKNOWN"}
                    for row in grants(orch.commit)
                )
            finally:
                for gate in load.gates.values():
                    gate.set()
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())
