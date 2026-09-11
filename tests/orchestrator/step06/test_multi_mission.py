# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-01 (D6-1): two Missions in one Orchestrator both make progress, spend only
their own quota under a Global Budget (§18.2), and never see each other's material."""

from __future__ import annotations

import asyncio

import pytest
from helpers_step06 import config, events_of, only, spec

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus
from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.commit_service import GLOBAL_ACCOUNT, mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import (
    demo_dynamic_dag_provider,
    graph_proposal_step,
    recorder_scripts,
)


def test_s6_01_two_missions_progress_together_and_spend_only_their_own_quota(tmp_path):
    a_script = recorder_scripts()["A"]
    # the second Mission's Worker tries to read the first Mission's workspace (data isolation)
    peeking = [("workspace_read_file", {"path": "../../peek/analysis.md"})] + a_script
    # two Missions share one scripted provider: one Planner script and one script per
    # (key, Attempt) for each of them
    provider = demo_dynamic_dag_provider(
        tasks=only("AD"),
        planner_steps=[graph_proposal_step(only("AD"))] * 2,
        per_attempt={
            "A": [a_script, peeking],
            "D": [recorder_scripts()["D"], recorder_scripts()["D"]],
        },
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, global_budget=Budget(max_tokens=1_000_000, max_attempts=40)),
            provider,
        ) as orchestrator:
            first = await orchestrator.submit_mission(
                spec(
                    "m-1",
                    success_criteria=("file:DOCS.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=8),
                )
            )
            second = await orchestrator.submit_mission(
                spec(
                    "m-2",
                    success_criteria=("file:DOCS.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=8),
                )
            )
            with pytest.raises(BudgetError):  # §18.2: a Mission may not exceed the Global Budget
                await orchestrator.submit_mission(
                    spec(
                        "m-3",
                        success_criteria=("file:DOCS.md",),
                        budget=Budget(max_tokens=2_000_000),
                    )
                )
            await orchestrator.run()
            store = orchestrator.store
            for mission in (first, second):
                assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                    orchestrator.progress_log
                )
            # both progressed: the allocation order rotated, so Attempts of the two Missions interleave
            created = [
                e.mission_id
                for e in sorted(
                    store.list_events(first.id) + store.list_events(second.id), key=lambda e: e.seq
                )
                if e.type == "AttemptCreated"
            ]
            assert set(created) == {first.id, second.id}
            assert created != sorted(created) and created != sorted(
                created, reverse=True
            )  # not "all of one, then the other"
            # quotas: each Mission account carries only its own settlements; the Global is the sum
            with store.transaction():
                acc_1 = orchestrator.commit.ledger.account(mission_account(first.id))
                acc_2 = orchestrator.commit.ledger.account(mission_account(second.id))
                glob = orchestrator.commit.ledger.account(GLOBAL_ACCOUNT)
            assert acc_1.settled_tokens > 0 and acc_2.settled_tokens > 0
            assert acc_1.reserved_tokens == 0 and acc_2.reserved_tokens == 0
            assert glob.settled_tokens == acc_1.settled_tokens + acc_2.settled_tokens
            assert glob.attempts_created == acc_1.attempts_created + acc_2.attempts_created
            assert glob.parent_id is None and acc_1.parent_id == GLOBAL_ACCOUNT
            usage_1 = {
                u["subject_id"] for u in orchestrator.commit.ledger.costs_report(first.id)["usage"]
            }
            usage_2 = {
                u["subject_id"] for u in orchestrator.commit.ledger.costs_report(second.id)["usage"]
            }
            assert (
                usage_1 and usage_2 and not (usage_1 & usage_2)
            )  # no usage_ref lands on the other Mission
            # isolation: the peek across workspaces was refused at the gateway
            rejected = [
                c
                for c in orchestrator.assembled.gateway.calls
                if c["tool"] == "workspace_read_file" and c["outcome"].startswith("rejected")
            ]
            assert rejected and rejected[0]["arguments"]["path"].startswith("../")
            # knowledge stays per Mission: the second Mission's Attempts saw none of the first's
            for task in store.list_tasks(second.id):
                for attempt in store.list_attempts(task.id):
                    frozen = store.get_intent_for_subject(attempt.id).config.get("knowledge", [])
                    for knowledge_id in frozen:
                        record = store.get_knowledge(knowledge_id)
                        assert record is None or record.mission_id == second.id

    asyncio.run(case())


def test_s6_01_one_mission_exhausting_its_pool_does_not_touch_the_other(tmp_path):
    provider = demo_dynamic_dag_provider(
        tasks=only("A"),
        planner_steps=[graph_proposal_step(only("A"))] * 2,
        per_attempt={"A": [recorder_scripts()["A"], recorder_scripts()["A"]]},
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            starved = await orchestrator.submit_mission(
                spec(
                    "m-starved",
                    success_criteria=("file:analysis.md",),
                    budget=Budget(max_tokens=100, max_attempts=4),
                )
            )
            healthy = await orchestrator.submit_mission(
                spec("m-healthy", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(starved.id).status is MissionStatus.FAILED
            assert store.get_mission(starved.id).stop_reason == "budget_exhausted"
            assert store.get_mission(healthy.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            assert not events_of(
                store, starved.id, "AttemptCreated"
            )  # nothing was funded from elsewhere
            attempts = [a for t in store.list_tasks(healthy.id) for a in store.list_attempts(t.id)]
            assert attempts and all(a.status is AttemptStatus.COMPLETED for a in attempts)

    asyncio.run(case())
