# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real Commit/SDK oracles for protected tails and frozen-price admission."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from graph_helpers7 import node, spec
from test_provider_budget_guard import ActualProvider, Counter, create_bound, grants
from test_provider_budget_recovery import until

from agent_orchestrator.contracts import Budget, ids
from agent_orchestrator.governance.budgets import BudgetError, BudgetExhausted
from agent_orchestrator.governance.tail_budget import TailAllocation, TailBudgetLedger, TailReserve
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService, Reservation, task_account
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.providers import (
    ProviderReconciliationObservation,
    ProviderReconciliationState,
    ProviderUsage,
)
from simple_harness.runtime.consumer_adapter import (
    ConsumerRuntimePolicies,
    _DefaultRuntimeReconciliation,
    _DefaultToolReconciliation,
)


@asynccontextmanager
async def priced_runtime(
    tmp_path,
    *,
    money=6000,
    provider=None,
    empty_retry=False,
    price=None,
    declared_price=None,
    evidence=None,
    slots=1,
    estimate=100,
):
    store = Store.open(tmp_path / "orchestrator.db")
    commit = CommitService(
        store,
        global_budget=Budget(
            max_tokens=400_000,
            max_cost_micros=40_000,
            max_attempts=24,
        ),
    )
    mission, _ = commit.create_mission(
        spec(
            budget=Budget(
                max_tokens=200_000,
                max_cost_micros=20_000,
                max_attempts=12,
            )
        )
    )
    planning = commit.begin_planning(mission.id)
    tasks, _ = commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node(
                        "A",
                        budget={
                            "max_tokens": 20_000,
                            "max_cost_micros": money,
                            "max_attempts": 3,
                        },
                    )
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "fixture"},
    )
    task = tasks[0]
    price = price or FrozenPriceEstimator("price-original", "consumer", 1_000_000, 2_000_000)
    counter = Counter(estimate)
    guard = ProviderBudgetGuard(
        commit,
        owner="test-owner",
        estimator=counter,
        max_slots=slots,
        price_tables={"agent.general": declared_price or price},
    )
    provider = provider or ActualProvider()
    if empty_retry:
        provider.script[0] = ""
    policies = ConsumerRuntimePolicies(
        "consumer_supplied",
        False,
        "consumer_reconciles" if evidence else "fail_closed",
        estimator=price,
        tool_reconciliation=_DefaultToolReconciliation() if evidence else None,
        provider_reconciliation=evidence,
        runtime_reconciliation=_DefaultRuntimeReconciliation() if evidence else None,
    )
    ports = AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "execution.db"),
        provider_admission=guard,
        policies=policies,
        default_max_output_tokens=1000,
        max_output_tokens_ceiling=2000 if empty_retry else 1000,
        empty_response_retries=int(empty_retry),
    )
    try:
        async with build_agent_runtime(ports) as runtime:
            yield commit, mission, task, guard, provider, runtime
    finally:
        provider.allow.set()
        store.close()


def balances(commit, account):
    return {
        s.account_id: (
            s.reserved_tokens,
            s.reserved_cost_micros,
            s.attempts_created,
            s.reserved_attempts,
        )
        for s in commit.ledger._chain(account)
    }


def test_tail_blocks_worker_then_transfers_real_synthesis_without_double_reserving(tmp_path):
    async def exercise():
        async with priced_runtime(tmp_path) as (commit, mission, task, guard, provider, runtime):
            tails = TailBudgetLedger(commit.ledger)
            account = task_account(task.id)
            with commit.store.transaction():
                hold = tails.reserve_selection_tail(
                    "round-A",
                    account,
                    TailReserve(12_000, 5000, attempts=1),
                    mission_id=mission.id,
                    task_revision="contract-A",
                )
                assert hold["subject_id"] == "tail:round-A"
                assert all(v == (12_000, 5000, 0, 1) for v in balances(commit, account).values())
            worker, first_attempt, key = await create_bound(commit, task, guard, runtime, "worker")
            denied = await worker.ask("request", input_id=key, timeout=5)
            assert str(denied.state) == "failed" and provider.calls == 0
            # An ordinary Worker cannot grow into the genuinely held money.
            assert commit.ledger.reservation(first_attempt.id)["reserved_cost_micros"] == 0
            with commit.store.transaction():
                before = balances(commit, account)
                synthesis = ids.attempt_id(task.id, 2)
                allocations = [
                    TailAllocation(synthesis, account, "synthesis", 4000, 2500, counts_attempt=True)
                ]
                receipt = tails.transfer_selection_reserve(
                    "round-A",
                    synthesis,
                    allocations,
                    task_revision="contract-A",
                )
                after = balances(commit, account)
                assert all(after[k][:2] == before[k][:2] for k in before)
                assert all(after[k][2:] == (before[k][2] + 1, before[k][3] - 1) for k in before)
                assert (
                    tails.transfer_selection_reserve(
                        "round-A",
                        synthesis,
                        allocations,
                        task_revision="contract-A",
                    )
                    == receipt
                )
                assert balances(commit, account) == after
                with pytest.raises(BudgetError, match="replay"):
                    tails.transfer_selection_reserve(
                        "round-A",
                        synthesis,
                        [replace(allocations[0], tokens=4001)],
                        task_revision="contract-A",
                    )
                assert balances(commit, account) == after
            # Actual create_attempt consumes the pre-transferred exact reservation;
            # it does not create a free Attempt or add a second reservation charge.
            c, actual_attempt, c_key = await create_bound(
                commit,
                task,
                guard,
                runtime,
                "synthesis",
                role="synthesis",
                reservation=Reservation(tokens=4000, cost_micros=2500),
            )
            assert actual_attempt.id == synthesis
            assert balances(commit, account) == after
            result = await c.ask("request", input_id=c_key, timeout=5)
            assert str(result.state) == "committed" and provider.calls == 1
            facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=c.agent_id)
            with commit.store.transaction():
                commit.ledger.import_usage(subject_id=synthesis, mission_id=mission.id, facts=facts)
                settled = commit.ledger.settle(subject_id=synthesis)
                assert (settled["settled_tokens"], settled["settled_cost_micros"]) == (150, 200)
                tails.release_tail("round-A", task_revision="contract-A", reason="round-finished")
                # The failed zero-handoff Worker remains a real consumed Attempt.
                assert all(v[2:] == (2, 0) for v in balances(commit, account).values())

    asyncio.run(exercise())


def test_tail_attempt_protection_and_failed_transfer_leave_all_chains_unchanged(tmp_path):
    async def exercise():
        async with priced_runtime(tmp_path) as (commit, mission, task, _, _, _runtime):
            tails = TailBudgetLedger(commit.ledger)
            account = task_account(task.id)
            with commit.store.transaction():
                tails.reserve_selection_tail(
                    "round",
                    account,
                    TailReserve(5000, 2000, attempts=1),
                    mission_id=mission.id,
                    task_revision="revision",
                )
                for n in (1, 2):
                    commit.ledger.reserve(
                        account_id=account,
                        subject_id=f"candidate-{n}",
                        tokens=1000,
                        cost_micros=200,
                        counts_attempt=True,
                    )
                before = balances(commit, account)
                with pytest.raises(BudgetExhausted, match="attempts"):
                    commit.ledger.reserve(
                        account_id=account,
                        subject_id="third-candidate",
                        tokens=1,
                        cost_micros=1,
                        counts_attempt=True,
                    )
                with pytest.raises(BudgetExhausted, match="cost_micros"):
                    tails.transfer_selection_reserve(
                        "round",
                        "C",
                        [
                            TailAllocation(
                                "C",
                                account,
                                "synthesis",
                                2000,
                                2001,
                                counts_attempt=True,
                            )
                        ],
                        task_revision="revision",
                    )
                assert balances(commit, account) == before
                assert commit.ledger.reservation("C") is None
                tails.transfer_selection_reserve(
                    "round",
                    "C",
                    [
                        TailAllocation(
                            "C",
                            account,
                            "synthesis",
                            2000,
                            1200,
                            counts_attempt=True,
                        )
                    ],
                    task_revision="revision",
                )
                assert all(v[2:] == (3, 0) for v in balances(commit, account).values())
                with pytest.raises(BudgetExhausted, match="attempts"):
                    commit.ledger.reserve(
                        account_id=account,
                        subject_id="fourth",
                        tokens=1,
                        cost_micros=1,
                        counts_attempt=True,
                    )

    asyncio.run(exercise())


def test_priced_shared_last_money_allows_only_one_actual_concurrent_handoff(tmp_path):
    async def exercise():
        provider = ActualProvider(blocked=True)
        async with priced_runtime(tmp_path, money=3000, provider=provider, slots=2) as (
            commit,
            _,
            task,
            guard,
            _,
            runtime,
        ):
            first = await create_bound(commit, task, guard, runtime, "first")
            second = await create_bound(commit, task, guard, runtime, "second")
            running = asyncio.create_task(first[0].ask("request", input_id=first[2], timeout=5))
            await asyncio.wait_for(provider.entered.wait(), 5)
            before = balances(commit, task_account(task.id))
            captured = []
            acquire = guard.acquire

            async def capture(**kwargs):
                try:
                    return await acquire(**kwargs)
                except ProviderAdmissionDenied as exc:
                    captured.append(exc.detail)
                    raise

            guard.acquire = capture
            result = await second[0].ask("request", input_id=second[2], timeout=5)
            assert str(result.state) == "failed" and provider.calls == 1
            assert balances(commit, task_account(task.id)) == before
            assert len(grants(commit)) == 1
            assert captured[0]["reason_code"] == "budget_exhausted"
            assert captured[0]["dimension"] == "cost_micros"
            assert captured[0]["requested"] == 2100 and captured[0]["remaining"] == 900
            assert captured[0]["invocation_id"] and captured[0]["handoff_ordinal"] == 1
            provider.allow.set()
            assert str((await running).state) == "committed"

    asyncio.run(exercise())


def test_priced_real_empty_retry_settles_original_frozen_price_and_prior_outputs(tmp_path):
    async def exercise():
        async with priced_runtime(tmp_path, empty_retry=True) as (
            commit,
            mission,
            task,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "retry")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "committed" and provider.calls == 2
            rows = grants(commit)
            assert [r["state"] for r in rows] == ["SETTLED", "SETTLED"]
            assert rows[1]["prior_output_upper"] == 50
            assert {r["actual_cost_micros"] for r in rows} == {200}
            assert len({r["price_digest"] for r in rows}) == 1
            assert commit.ledger.reservation(attempt.id)["reserved_cost_micros"] == 4350
            with commit.store.transaction():
                facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
                commit.ledger.import_usage(
                    subject_id=attempt.id, mission_id=mission.id, facts=facts
                )
                settled = commit.ledger.settle(subject_id=attempt.id)
                assert (settled["settled_tokens"], settled["settled_cost_micros"]) == (300, 400)
                for account in commit.ledger._chain(task_account(task.id)):
                    assert account.settled_cost_micros == 400 and account.reserved_cost_micros == 0

    asyncio.run(exercise())


def test_priced_unknown_survives_cancel_tail_release_and_actual_sdk_reconciliation(tmp_path):
    class Lost(ActualProvider):
        actual = None

        async def invoke(self, request, *, cancel):
            self.actual = await super().invoke(request, cancel=cancel)
            raise RuntimeError("actual response receipt lost")

    provider = Lost()

    class Evidence:
        completed = False

        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.COMPLETED
                if self.completed
                else ProviderReconciliationState.STILL_UNKNOWN,
                "actual-receipt:" + invocation.invocation_id,
                provider.actual if self.completed else None,
            )

    async def exercise():
        evidence = Evidence()
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            mission,
            task,
            guard,
            _,
            runtime,
        ):
            tails = TailBudgetLedger(commit.ledger)
            with commit.store.transaction():
                tails.reserve_tail(
                    "future-critic",
                    task_account(task.id),
                    TailReserve(1000, 500),
                    mission_id=mission.id,
                    task_revision="A",
                    purpose="critic",
                )
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "lost")
            receipt = await agent.submit("request", input_id=key)
            intent = commit.store.get_intent_for_subject(attempt.id)
            commit.record_submitted(
                intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
            )
            await until(lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN")
            bridge = AgentBridge(runtime, unpriced=False)
            assert bridge.usage_facts(agent_id=agent.agent_id) == []
            assert grants(commit)[0]["cost_upper_micros"] == 2100
            commit.cancel_mission(mission.id)
            with commit.store.transaction():
                tails.release_tail("future-critic", task_revision="A", reason="mission-cancelled")
                with pytest.raises(BudgetError, match="unknown"):
                    commit.ledger.settle(subject_id=attempt.id)
            cold = ProviderBudgetGuard(
                commit,
                owner="new-owner",
                estimator=guard.estimator,
                max_slots=1,
                price_tables=guard.price_tables,
            )
            cold.recover(runtime.uow)
            assert grants(commit)[0]["state"] == "UNKNOWN"
            assert commit.ledger.reservation(attempt.id)["reserved_cost_micros"] == 2100
            evidence.completed = True
            await runtime.kernel.reconcile()
            cold.recover(runtime.uow)
            assert grants(commit)[0]["state"] == "SETTLED"
            assert grants(commit)[0]["actual_cost_micros"] == 200
            with commit.store.transaction():
                facts = bridge.usage_facts(agent_id=agent.agent_id)
                assert sum(f.cost_micros for f in facts) == 200
                assert (
                    commit.ledger.import_usage(
                        subject_id=attempt.id, mission_id=mission.id, facts=facts
                    )
                    == 1
                )
                assert (
                    commit.ledger.import_usage(
                        subject_id=attempt.id, mission_id=mission.id, facts=facts
                    )
                    == 0
                )
                assert commit.ledger.settle(subject_id=attempt.id)["settled_cost_micros"] == 200
            cold.recover(runtime.uow)
            assert provider.calls == 1 and not commit.store.list_mission_claims(mission.id)

    asyncio.run(exercise())


def test_money_only_overrun_is_committed_before_cold_recovery_raises(tmp_path):
    async def exercise():
        price = FrozenPriceEstimator("expensive-input", "consumer", 10_000_000, 1_000_000)
        async with priced_runtime(tmp_path, price=price) as (
            commit,
            mission,
            task,
            guard,
            provider,
            runtime,
        ):
            original = provider.invoke

            async def large_input(request, *, cancel):
                response = await original(request, cancel=cancel)
                return replace(
                    response,
                    usage=ProviderUsage(input_tokens=1000, output_tokens=50, total_tokens=1050),
                )

            provider.invoke = large_input
            observe = guard.observe
            guard.observe = lambda *a, **kw: None  # lose only Host's actual terminal observation
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "overrun")
            await agent.ask("request", input_id=key, timeout=5)
            guard.observe = observe
            cold = ProviderBudgetGuard(
                commit,
                owner="new-owner",
                estimator=guard.estimator,
                max_slots=1,
                price_tables=guard.price_tables,
            )
            with pytest.raises(ProviderAdmissionDenied, match="exceeded"):
                cold.recover(runtime.uow)
            row = grants(commit)[0]
            assert row["actual_tokens"] == 1050 <= row["total_upper"]
            assert row["actual_cost_micros"] == 10050 > row["cost_upper_micros"] == 2000
            assert row["state"] == "OVERRUN"
            cold.recover(runtime.uow)
            assert grants(commit)[0] == row
            with commit.store.transaction():
                facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
                commit.ledger.import_usage(
                    subject_id=attempt.id, mission_id=mission.id, facts=facts
                )
                assert commit.ledger.settle(subject_id=attempt.id)["settled_cost_micros"] == 10050
                assert commit.ledger.account(task_account(task.id)).remaining_cost_micros() == -4050

    asyncio.run(exercise())


@pytest.mark.parametrize("changed", ["snapshot", "rate"])
def test_frozen_profile_price_mismatch_is_zero_handoff(tmp_path, changed):
    async def exercise():
        actual = FrozenPriceEstimator("price-original", "consumer", 1_000_000, 2_000_000)
        different = replace(
            actual,
            **(
                {"snapshot_id": "other-price"}
                if changed == "snapshot"
                else {"input_micros_per_million_tokens": 1}
            ),
        )
        # Differently frozen admission/SDK prices never authorize a transport.
        async with priced_runtime(tmp_path, price=actual, declared_price=different) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            agent, _, key = await create_bound(commit, task, guard, runtime, "mismatch")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed" and provider.calls == 0
            assert grants(commit) == []

    asyncio.run(exercise())


def test_known_tokens_with_untrusted_price_are_not_imported_as_final_zero(tmp_path):
    async def exercise():
        async with priced_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):
            invoke = provider.invoke

            async def wrong_model(request, *, cancel):
                return replace(await invoke(request, cancel=cancel), model="unpriced-other-model")

            provider.invoke = wrong_model
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "unknown-money")
            await agent.ask("request", input_id=key, timeout=5)
            assert provider.calls == 1
            row = grants(commit)[0]
            assert row["state"] == "UNKNOWN" and row["actual_tokens"] == 150
            assert row["actual_cost_micros"] is None and row["cost_upper_micros"] == 2100
            assert AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id) == []
            with commit.store.transaction():
                with pytest.raises(BudgetError, match="unknown"):
                    commit.ledger.settle(subject_id=attempt.id)
            guard.recover(runtime.uow)
            assert grants(commit)[0]["state"] == "UNKNOWN"

    asyncio.run(exercise())
