# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Actual SDK admission against a public-Commit system hold; no network.

The shared graph helper accepts the upstream fixture through CommitService.
The system Task, initial transfers, growth, usage and settlement are not mocked.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from graph_helpers7 import complete, node, spec
from test_provider_budget_guard import ActualProvider, Counter, grants
from test_provider_budget_recovery import until

from agent_orchestrator.contracts import Budget, TaskStatus
from agent_orchestrator.governance.budgets import BudgetError, BudgetExhausted
from agent_orchestrator.governance.mission_system_tail import SystemTailBinding, SystemTailRoute
from agent_orchestrator.governance.tail_budget import TailReserve
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService, Reservation, task_account
from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.providers import ProviderReconciliationObservation, ProviderReconciliationState
from simple_harness.runtime.consumer_adapter import (
    ConsumerRuntimePolicies,
    _DefaultRuntimeReconciliation,
    _DefaultToolReconciliation,
)


def _rows(commit, *tables):
    return {
        table: [
            tuple(row)
            for row in commit.store.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        ]
        for table in tables
    }


@asynccontextmanager
async def _runtime(tmp_path, *, priced=False, counter=None, provider=None, evidence=None):
    price = FrozenPriceEstimator("system-growth-v1", "consumer", 1_000_000, 1_000_000)
    route = SystemTailRoute(
        "agent.general", "agent-model", "system-growth-route", price if priced else None
    )
    binding = SystemTailBinding(route, route, "fixture-explicit-system-allowance-v1")
    allowance = TailReserve(10_000, 10_000 if priced else 0, attempts=3)

    def factory(mission, purpose, **kwargs):
        return (allowance, binding) if purpose == "synthesis" else None

    store = Store.open(tmp_path / "orchestration.db")
    commit = CommitService(
        store,
        system_tail_factory=factory,
        global_budget=Budget(max_tokens=200_000, max_cost_micros=200_000, max_attempts=20),
    )
    try:
        mission, _ = commit.create_mission(
            spec(
                "system-growth",
                budget=Budget(max_tokens=100_000, max_cost_micros=100_000, max_attempts=10),
                synthesis={
                    "goal": "combine A",
                    "success_criteria": ["file:summary.md"],
                    "outputs": ["summary.md"],
                    "verification_policy": ["format_check", "rule_check", "critic_review"],
                    "budget": {"max_tokens": 10_000, "max_cost_micros": 10_000, "max_attempts": 3},
                },
            )
        )
        planning = commit.begin_planning(mission.id)
        tasks, _ = commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": [node("A")]}),
            base_version=planning.version,
            source={"planner": "system-growth-fixture"},
        )
        upstream = next(t for t in tasks if t.kind == "work")
        complete(commit, upstream)
        task = next(t for t in store.list_tasks(mission.id) if t.kind == "synthesis")
        assert task.status is TaskStatus.READY
        hold = commit.protected_tail_hold(commit.system_task_hold(task.id)["hold_id"])
        assert commit.ledger.reservation(hold["subject_id"])["reserved_tokens"] == 10_000
        guard = ProviderBudgetGuard(
            commit,
            owner="test-owner",
            estimator=counter or Counter(4500),
            max_slots=2,
            profile_slots={"agent.general": 2},
            price_tables={"agent.general": price if priced else None},
        )
        provider = provider or ActualProvider()
        policies = ConsumerRuntimePolicies(
            "consumer_supplied" if priced else "unpriced_local",
            False,
            "consumer_reconciles" if evidence else "fail_closed",
            estimator=price if priced else None,
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
            max_output_tokens_ceiling=2000,
            empty_response_retries=1,
        )
        async with build_agent_runtime(ports) as runtime:
            yield commit, mission, task, hold, guard, provider, runtime
    finally:
        store.close()


async def _worker(commit, task, guard, runtime, *, key="one", priced=False, floor_cost=None):
    agent = await runtime.create(
        AgentConfig(name=key, instructions="Answer briefly.", model_profile_ref="agent.general"),
        creation_key=key,
    )
    attempt, intent = commit.create_attempt(
        task.id,
        role="synthesizer",
        model="agent-model",
        prompt_version="synthesis-v1",
        context_version="fixture",
        runtime_profile_id="agent.general",
        reservation=Reservation(2000, 2000 if priced else 0),
        intent_config={
            "agent_config": agent.config.to_json(),
            "message": user_message_json("request"),
            "provider_admission_fingerprint": guard.fingerprint,
            "runtime_profile_id": "agent.general",
            "model": "agent-model",
            "first_critic_budget": {
                "minimum_tokens": 3000,
                "cost_micros": (3000 if priced else 0) if floor_cost is None else floor_cost,
            },
        },
        input_hash=key,
        candidates_per_task=2,
    )
    commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
    commit.record_agent_created(
        intent.intent_id,
        agent_id=agent.agent_id,
        expected_turn_id=agent.turn_id_for(intent.input_id),
    )
    return agent, attempt, intent


@pytest.mark.parametrize("priced", [False, True])
@pytest.mark.parametrize("later_request", [False, True])
def test_system_worker_grows_only_request_deficit_and_returns_unused_once(
    tmp_path, priced, later_request
):
    class RequestCounter(Counter):
        def estimate_input_tokens(self, request):
            self.tokens = 500 if not self.requests else 4000
            return super().estimate_input_tokens(request)

    async def exercise():
        counter = RequestCounter(500) if later_request else Counter(4500)
        provider = ActualProvider()
        if later_request:
            provider.script[0] = ""  # SDK output-cap retry creates a second actual request.
        async with _runtime(tmp_path, priced=priced, counter=counter, provider=provider) as (
            commit,
            mission,
            task,
            hold,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, intent = await _worker(commit, task, guard, runtime, priced=priced)
            before = _rows(commit, "budget_accounts")
            assert commit.ledger.account(task_account(task.id)).remaining_tokens() == 0
            result = await agent.ask("request", input_id=intent.input_id, timeout=5)
            assert str(result.state) == "committed"
            assert provider.calls == (2 if later_request else 1)
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == provider.calls and all(r.handoff_attempt == 1 for r in records)
            rows = grants(commit)
            assert len(rows) == provider.calls and all(r["state"] == "SETTLED" for r in rows)
            required = 6200 if later_request else 5500  # prior usage/output + next wire bound
            assert commit.ledger.reservation(attempt.id)["reserved_tokens"] == required
            source = commit.ledger.reservation(hold["subject_id"])
            assert source["reserved_tokens"] == 10_000 - required >= 3000
            if priced:
                assert source["reserved_cost_micros"] >= 3000
            assert _rows(commit, "budget_accounts") == before  # no new ancestor reservation
            assert (
                commit.store.connection.execute(
                    "SELECT COUNT(*) FROM commit_receipts WHERE kind='system_worker_growth' "
                    "AND subject_id=?",
                    (attempt.id,),
                ).fetchone()[0]
                == 1
            )
            snapshots = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                assert commit.grow_system_worker_allowance(
                    attempt.id,
                    tokens=required,
                    cost_micros=required if priced else 0,
                )
            assert _rows(commit, *snapshots) == snapshots
            facts = AgentBridge(runtime, unpriced=not priced).usage_facts(agent_id=agent.agent_id)
            with commit.store.transaction():
                commit.ledger.import_usage(
                    subject_id=attempt.id, mission_id=mission.id, facts=facts
                )
            commit.settle_subject(attempt.id, mission.id, task_id=task.id)
            source = commit.ledger.reservation(hold["subject_id"])
            assert source["reserved_tokens"] == 10_000 - 150 * provider.calls
            if priced:
                assert source["reserved_cost_micros"] == 10_000 - 150 * provider.calls
            after = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                assert (
                    commit.ledger.import_usage(
                        subject_id=attempt.id,
                        mission_id=mission.id,
                        facts=facts,
                    )
                    == 0
                )
            commit.settle_subject(attempt.id, mission.id, task_id=task.id)
            assert _rows(commit, *after) == after
            # A known failed Attempt can retry using only the returned remainder.
            commit.mark_attempt_lost(attempt.id, reason="controlled retry after known usage")
            retry, second, second_intent = await _worker(
                commit,
                task,
                guard,
                runtime,
                key="retry",
                priced=priced,
            )
            retried = await retry.ask("request", input_id=second_intent.input_id, timeout=5)
            assert str(retried.state) == "committed"
            retry_facts = AgentBridge(runtime, unpriced=not priced).usage_facts(
                agent_id=retry.agent_id
            )
            with commit.store.transaction():
                commit.ledger.import_usage(
                    subject_id=second.id, mission_id=mission.id, facts=retry_facts
                )
            commit.settle_subject(second.id, mission.id, task_id=task.id)
            assert commit.ledger.reservation(hold["subject_id"])["reserved_tokens"] == (
                10_000 - 150 * provider.calls
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["tokens", "cost", "sibling"])
def test_system_growth_refusal_keeps_critic_and_other_worker_reservations(tmp_path, boundary):
    async def exercise():
        priced = boundary == "cost"
        counter = Counter(6500 if boundary == "tokens" else 4500)
        async with _runtime(tmp_path, priced=priced, counter=counter) as (
            commit,
            _,
            task,
            hold,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, intent = await _worker(
                commit,
                task,
                guard,
                runtime,
                priced=priced,
                floor_cost=5000 if priced else None,
            )
            if boundary == "sibling":
                await _worker(commit, task, guard, runtime, key="sibling")
            before = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            # Catch inside the transaction too: refusal must not rely on an
            # outer rollback to hide a partially debited token/cost hold.
            with commit.store.transaction():
                with pytest.raises(BudgetExhausted):
                    guard.adapter.grow(
                        commit.ledger.reservation(attempt.id),
                        required=7500 if boundary == "tokens" else 5500,
                        required_cost_micros=5500 if priced else 0,
                    )
                assert _rows(commit, *before) == before
            result = await agent.ask("request", input_id=intent.input_id, timeout=5)
            assert str(result.state) == "failed" and provider.calls == 0
            assert result.error["source_kind"] == "provider_admission"
            assert result.error["detail"]["reason_code"] == "budget_exhausted"
            assert result.error["detail"]["dimension"] == ("cost_micros" if priced else "tokens")
            assert _rows(commit, *before) == before and grants(commit) == []
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0
            assert commit.ledger.reservation(attempt.id)["reserved_tokens"] == 2000
            assert commit.ledger.reservation(hold["subject_id"])["reserved_tokens"] >= 3000

    asyncio.run(exercise())


def test_grown_unknown_keeps_allowance_across_recovery_and_refuses_return(tmp_path):
    class ResponseLost(ActualProvider):
        async def invoke(self, request, *, cancel):
            await super().invoke(request, cancel=cancel)
            raise RuntimeError("actual response lost after handoff")

    class Evidence:
        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.STILL_UNKNOWN,
                "growth:" + invocation.invocation_id,
            )

    async def exercise():
        async with _runtime(tmp_path, provider=ResponseLost(), evidence=Evidence()) as (
            commit,
            mission,
            task,
            hold,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, intent = await _worker(commit, task, guard, runtime)
            receipt = await agent.submit("request", input_id=intent.input_id)
            commit.record_submitted(
                intent.intent_id,
                receipt={"turn_id": receipt.turn_id, "seq": receipt.seq},
            )
            await until(lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN")
            assert provider.calls == 1
            assert commit.ledger.reservation(attempt.id)["reserved_tokens"] == 5500
            assert commit.ledger.reservation(hold["subject_id"])["reserved_tokens"] == 4500
            before = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                with pytest.raises(ProviderAdmissionDenied):
                    guard.adapter.grow(commit.ledger.reservation(attempt.id), required=6000)
                with pytest.raises(BudgetError, match="unknown"):
                    commit.settle_subject(attempt.id, mission.id, task_id=task.id)
            guard.recover(runtime.uow)
            assert _rows(commit, *before) == before
            assert grants(commit)[0]["state"] == "UNKNOWN" and provider.calls == 1
            commit.cancel_mission(mission.id)
            assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"
            assert commit.ledger.reservation(attempt.id)["reserved_tokens"] == 5500

    asyncio.run(exercise())
