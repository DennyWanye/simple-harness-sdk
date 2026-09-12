# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real Mission/graph accounting and actual priced SDK calls around system pools.

Default runtime system hooks are separately integrated by the main owner. These
controls use public Commit graph writes, never invent a system Task or a PASS.
"""

import asyncio
from dataclasses import replace

import pytest
from graph_helpers7 import node, spec
from test_provider_budget_guard import ActualProvider, create_bound, grants
from test_tail_and_priced_budget import priced_runtime

from agent_orchestrator.contracts import Budget, ids
from agent_orchestrator.governance.budgets import BudgetError, BudgetExhausted
from agent_orchestrator.governance.mission_system_tail import (
    MissionSystemTailLedger,
    SystemTailBinding,
    SystemTailRoute,
)
from agent_orchestrator.governance.tail_budget import TailReserve
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    Reservation,
    task_account,
)
from agent_orchestrator.planning.candidate_selection import selection_revision
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.assessments import mission_contract_revision
from simple_harness.execution.budget import FrozenPriceEstimator

PRICE = FrozenPriceEstimator("price-original", "consumer", 1_000_000, 2_000_000)
BINDING = SystemTailBinding(
    SystemTailRoute("agent.general", "fixture", "worker-route-v1", PRICE),
    SystemTailRoute("agent.general", "fixture", "critic-route-v1", PRICE),
    "fixture-explicit-total-token-and-micro-cap-v1",
)
ALLOWANCE = TailReserve(8000, 6000, attempts=2)


def account_values(commit):
    return [
        dict(row)
        for row in commit.store.connection.execute(
            "SELECT * FROM budget_accounts ORDER BY account_id"
        ).fetchall()
    ]


@pytest.fixture
def planned_system(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    commit = CommitService(
        store, global_budget=Budget(max_tokens=400_000, max_cost_micros=40_000, max_attempts=24)
    )
    try:
        mission, _ = commit.create_mission(
            spec(
                budget=Budget(max_tokens=200_000, max_cost_micros=20_000, max_attempts=12),
                synthesis={
                    "goal": "Synthesize actual Work results",
                    "success_criteria": ["file:summary.md"],
                    "outputs": ["summary.md"],
                    "verification_policy": ["format_check", "rule_check"],
                    "budget": {"max_tokens": 8000, "max_cost_micros": 6000, "max_attempts": 2},
                },
                conflict_reserve_tokens=8000,
            )
        )
        pools = MissionSystemTailLedger(commit.ledger)
        with store.transaction():
            revision = mission_contract_revision(mission)
            pools.reserve_pool(
                "synthesis-original",
                mission_id=mission.id,
                purpose="synthesis",
                reserve=ALLOWANCE,
                mission_revision=revision,
                route_binding=BINDING,
            )
            assert store.list_tasks(mission.id) == []
            # No placeholder Task/account, and no actual Attempt for a mere hold.
            accounts = account_values(commit)
            assert {row["scope"] for row in accounts} == {"global", "mission"}
            assert all(row["reserved_tokens"] == 8000 for row in accounts)
            assert all(row["reserved_cost_micros"] == 6000 for row in accounts)
            assert all(row["reserved_attempts"] == 2 for row in accounts)
            assert all(row["attempts_created"] == 0 for row in accounts)
        planning = commit.begin_planning(mission.id)
        with store.transaction():
            tasks, _ = commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A")]}),
                base_version=planning.version,
                source={"planner": "actual-graph-fixture"},
            )
            system = next(task for task in tasks if task.kind == "synthesis")
            work = next(task for task in tasks if task.kind == "work")
        yield commit, mission, work, system, pools
    finally:
        store.close()


def test_mission_pool_to_real_system_task_conserves_ancestors_and_original_attempt_cap(
    planned_system,
):
    commit, mission, work, system, pools = planned_system
    revision = selection_revision(commit.store, system)
    work_before = commit.ledger.account(task_account(work.id))
    before = account_values(commit)
    with commit.store.transaction():
        receipt = pools.transfer_to_task_hold(
            "synthesis-original",
            task_id=system.id,
            reserve=ALLOWANCE,
            semantic_revision=revision,
            route_binding=BINDING,
        )
        after = account_values(commit)
        for original in before:
            if original["scope"] != "task":
                current = next(row for row in after if row["account_id"] == original["account_id"])
                for field in (
                    "reserved_tokens",
                    "reserved_cost_micros",
                    "reserved_attempts",
                    "attempts_created",
                    "limits_json",
                ):
                    assert current[field] == original[field]
        assert commit.ledger.account(task_account(work.id)) == work_before
        actual = commit.ledger.account(task_account(system.id))
        assert actual.reserved_tokens == 8000 and actual.reserved_cost_micros == 6000
        assert actual.reserved_attempts == 2 and actual.attempts_created == 0
        assert actual.remaining_attempts() == 0 and actual.limits.max_attempts == 2
        assert commit.store.list_attempts(system.id) == []
        assert (
            pools.transfer_to_task_hold(
                "synthesis-original",
                task_id=system.id,
                reserve=ALLOWANCE,
                semantic_revision=revision,
                route_binding=BINDING,
            )
            == receipt
        )
        assert account_values(commit) == after  # replay writes no balance/version
        assert pools.pool("synthesis-original")["remaining_attempts"] == 0
        original = commit.ledger.reservation("mission-system:synthesis-original")
        assert original["reserved_tokens"] == original["reserved_cost_micros"] == 0
        pools.release_unused_pool(
            "synthesis-original",
            mission_revision=mission_contract_revision(mission),
            reason="all-allocated",
        )
        assert commit.ledger.account(task_account(system.id)) == actual
        assert commit.ledger.reservation("tail:" + receipt["hold_id"])["state"] == "RESERVED"


@pytest.mark.parametrize("dimension", ["tokens", "cost_micros", "attempts"])
def test_mission_pool_failures_leave_original_balances_even_when_caught_inside_transaction(
    planned_system,
    dimension,
):
    commit, _, work, system, pools = planned_system
    revision = selection_revision(commit.store, system)
    with commit.store.transaction():
        before = account_values(commit)
        args = dict(
            task_id=system.id, reserve=ALLOWANCE, semantic_revision=revision, route_binding=BINDING
        )
        with pytest.raises(BudgetError, match="same-Mission system Task"):
            pools.transfer_to_task_hold("synthesis-original", **{**args, "task_id": work.id})
        with pytest.raises(BudgetError, match="revision"):
            pools.transfer_to_task_hold(
                "synthesis-original", **{**args, "semantic_revision": "old"}
            )
        expensive = replace(
            BINDING,
            worker=replace(
                BINDING.worker,
                price=FrozenPriceEstimator("new-price", "consumer", 3_000_000, 4_000_000),
            ),
        )
        with pytest.raises(BudgetError, match="routing/price"):
            pools.transfer_to_task_hold(
                "synthesis-original", **{**args, "route_binding": expensive}
            )
        with pytest.raises(BudgetError):
            pools.transfer_to_task_hold(
                "synthesis-original",
                **{
                    **args,
                    "reserve": replace(ALLOWANCE, **{dimension: getattr(ALLOWANCE, dimension) + 1}),
                },
            )
        # Catch inside the owning transaction, then commit: not merely relying
        # on outer rollback to hide partial updates to a held original pool.
        assert account_values(commit) == before
        assert pools.pool("synthesis-original")["remaining_attempts"] == 2
        assert commit.ledger.reservation(f"tail:system:{system.id}") is None


def test_mission_pool_cannot_spend_task_cap_already_used_by_an_actual_reservation(planned_system):
    commit, mission, _, system, pools = planned_system
    with commit.store.transaction():
        # A real service reservation on the actual system Task already owns
        # part of its cap. A Mission pool cannot override that Task limitation.
        intent = commit.create_service_intent(
            kind="critic",
            subject_id=system.id + ":root-review:1",
            task_id=system.id,
            mission_id=mission.id,
            account_id=task_account(system.id),
            creation_key="root-review",
            input_id="review-input",
            input_hash="actual-input",
            config={},
            reservation=Reservation(100, 100),
        )
        before = account_values(commit)
        with pytest.raises(BudgetExhausted, match="tokens"):
            pools.transfer_to_task_hold(
                "synthesis-original",
                task_id=system.id,
                reserve=ALLOWANCE,
                semantic_revision=selection_revision(commit.store, system),
                route_binding=BINDING,
            )
        assert account_values(commit) == before
        assert commit.store.get_intent(intent.intent_id) == intent
        assert (
            commit.ledger.reservation("mission-system:synthesis-original")["reserved_tokens"]
            == 8000
        )


def test_system_actual_attempt_creation_failure_rolls_back_provisional_transfer(planned_system):
    commit, _, _, system, pools = planned_system
    revision = selection_revision(commit.store, system)
    with commit.store.transaction():
        pools.transfer_to_task_hold(
            "synthesis-original",
            task_id=system.id,
            reserve=ALLOWANCE,
            semantic_revision=revision,
            route_binding=BINDING,
        )
    before = account_values(commit)
    actual_id = ids.attempt_id(system.id, 1)
    with pytest.raises(CommitRejected, match="BLOCKED"):
        with commit.store.transaction():
            with pytest.raises(BudgetError, match="routing/price"):
                pools.consume_task_hold(
                    task_id=system.id,
                    attempt_id=actual_id,
                    subject_id=actual_id,
                    reservation=Reservation(4000, 3000),
                    semantic_revision=revision,
                    route_binding=replace(
                        BINDING,
                        worker=replace(
                            BINDING.worker, profile_fingerprint="changed-runtime-profile"
                        ),
                    ),
                )
            assert account_values(commit) == before
            pools.consume_task_hold(
                task_id=system.id,
                attempt_id=actual_id,
                subject_id=actual_id,
                reservation=Reservation(4000, 3000),
                semantic_revision=revision,
                route_binding=BINDING,
            )
            provisional = commit.ledger.account(task_account(system.id))
            assert provisional.attempts_created == provisional.reserved_attempts == 1
            assert provisional.remaining_attempts() == 0
            # Real Commit still enforces dependencies. No fixture marks Work
            # accepted to force a synthesis run; the entire allocation rolls back.
            commit.create_attempt(
                system.id,
                role="synthesis",
                model="fixture",
                prompt_version="synthesis-v1",
                context_version="ctx",
                reservation=Reservation(4000, 3000),
                intent_config={},
                input_hash="actual-system-input",
            )
    assert account_values(commit) == before
    assert commit.ledger.reservation(actual_id) is None
    assert commit.store.get_attempt(actual_id) is None


def test_unused_mission_conflict_pool_releases_without_erasing_actual_sdk_unknown_charge(tmp_path):
    async def exercise():
        provider = ActualProvider(blocked=True)
        async with priced_runtime(tmp_path, provider=provider) as (
            commit,
            mission,
            work,
            guard,
            _,
            runtime,
        ):
            pools = MissionSystemTailLedger(commit.ledger)
            revision = mission_contract_revision(mission)
            with commit.store.transaction():
                pools.reserve_pool(
                    "future-conflict",
                    mission_id=mission.id,
                    purpose="conflict",
                    reserve=ALLOWANCE,
                    mission_revision=revision,
                    route_binding=BINDING,
                )
            work_before = commit.ledger.account(task_account(work.id))
            assert work_before.reserved_tokens == work_before.reserved_attempts == 0
            agent, attempt, key = await create_bound(
                commit,
                work,
                guard,
                runtime,
                "actual-with-future-conflict",
                reservation=Reservation(4000, 0),
            )
            receipt = await agent.submit("request", input_id=key)
            intent = commit.store.get_intent_for_subject(attempt.id)
            commit.record_submitted(
                intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
            )
            await asyncio.wait_for(provider.entered.wait(), 5)
            commit.cancel_mission(mission.id)
            guard.recover(runtime.uow)
            assert grants(commit)[0]["state"] == "UNKNOWN"
            bridge = AgentBridge(runtime, unpriced=False)
            assert bridge.usage_facts(agent_id=agent.agent_id) == []
            with commit.store.transaction():
                released = pools.pool("future-conflict")
                assert released["state"] == "RELEASED"
                assert released["release_reason"] == "mission_terminal"
                with pytest.raises(BudgetError, match="replay changed reason"):
                    pools.release_unused_pool(
                        "future-conflict", mission_revision=revision, reason="cancelled"
                    )
                pools.release_unused_pool(
                    "future-conflict", mission_revision=revision, reason="mission_terminal"
                )
                with pytest.raises(BudgetError, match="unknown"):
                    commit.ledger.settle(subject_id=attempt.id)
            chain = commit.ledger._chain(task_account(work.id))
            assert all(account.reserved_cost_micros == 2100 for account in chain)
            assert all(
                account.attempts_created == 1 and account.reserved_attempts == 0
                for account in chain
            )
            provider.allow.set()
            await agent.wait_turn(receipt.turn_id, timeout=5)
            guard.recover(runtime.uow)
            facts = bridge.usage_facts(agent_id=agent.agent_id)
            assert len(facts) == 1 and facts[0].cost_micros == 200
            assert commit.import_usage(attempt.id, mission.id, facts) == 1
            commit.settle_subject(attempt.id, mission.id, task_id=work.id)
            assert commit.import_usage(attempt.id, mission.id, facts) == 0
            assert grants(commit)[0]["state"] == "SETTLED"
            assert provider.calls == 1 and not commit.store.list_mission_claims(mission.id)
            for account in commit.ledger._chain(task_account(work.id)):
                assert account.reserved_tokens == account.reserved_cost_micros == 0
                assert account.settled_tokens == 150 and account.settled_cost_micros == 200
                assert account.attempts_created == 1 and account.reserved_attempts == 0
            assert commit.store.get_attempt(ids.attempt_id(work.id, 2)) is None

    asyncio.run(exercise())
