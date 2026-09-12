# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Characterize an OPEN accounting gate; passing does not mean late usage works.

The real SDK receives a successful response whose transport omits usage. An
external fixture retains the matching original response with actual usage, but
the current public recovery path cannot attach it to a SUCCEEDED invocation.
No ledger state is manufactured, no UNKNOWN rewrite, no provider replay.
"""

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from test_provider_budget_guard import ActualProvider, Counter, create_bound, grants
from test_tail_and_priced_budget import priced_runtime

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import build_agent_runtime
from simple_harness.contracts import RunId
from simple_harness.execution.provider_invocations import provider_response_json
from simple_harness.execution.recovery import ResolutionOutcome
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.providers import ProviderReconciliationObservation, ProviderReconciliationState


class UsageOmitted(ActualProvider):
    actual = None

    async def invoke(self, request, *, cancel):
        self.actual = await super().invoke(request, cancel=cancel)
        return replace(self.actual, usage=None)


class OriginalUsageEvidence:
    def __init__(self, provider):
        self.provider = provider
        self.observed = []

    async def observe(self, invocation):
        self.observed.append(invocation.invocation_id)
        assert self.provider.actual.request_id == invocation.request_id
        return ProviderReconciliationObservation(
            ProviderReconciliationState.COMPLETED,
            "original-usage-receipt:" + invocation.invocation_id,
            self.provider.actual,
        )


async def successful_without_usage(commit, task, guard, provider, runtime):
    agent, attempt, key = await create_bound(commit, task, guard, runtime, "usage-omitted")
    receipt = await agent.submit("request", input_id=key)
    intent = commit.store.get_intent_for_subject(attempt.id)
    commit.record_submitted(
        intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
    )
    result = await agent.wait_turn(receipt.turn_id, timeout=5)
    assert str(result.state) == "committed" and provider.calls == 1
    records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
    assert len(records) == 1
    record = records[0]
    assert str(record.state) == "succeeded" and record.handoff_attempt == 1
    assert record.usage_json["usage"] is None and record.response_json["usage"] is None
    assert record.request_id == provider.actual.request_id
    assert provider.actual.usage.total_tokens == 150
    assert record.budget_charge.kind.value != "trusted_usage"
    assert grants(commit)[0]["state"] == "UNKNOWN"
    assert grants(commit)[0]["actual_tokens"] is None
    assert grants(commit)[0]["actual_cost_micros"] is None
    assert commit.ledger.reservation(attempt.id)["reserved_cost_micros"] == 2100
    return agent, attempt, record


def assert_accounting_held(commit, runtime, subject_id, agent_id):
    assert AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent_id) == []
    with commit.store.transaction():
        assert commit.ledger.has_unknown_usage(subject_id)
        with pytest.raises(BudgetError, match="unknown"):
            commit.ledger.settle(subject_id=subject_id)
    reservation = commit.ledger.reservation(subject_id)
    assert reservation["state"] == "RESERVED" and reservation["reserved_cost_micros"] == 2100
    assert commit.ledger.usage_for(subject_id)[0] == 0
    # Zero imported facts is absence of knowledge, not a zero-cost settlement.
    assert reservation["settled_tokens"] is None


def test_succeeded_missing_usage_has_no_current_public_late_accounting_path(tmp_path):
    async def exercise():
        provider = UsageOmitted()
        evidence = OriginalUsageEvidence(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            _,
            task,
            guard,
            _,
            runtime,
        ):
            agent, attempt, original = await successful_without_usage(
                commit, task, guard, provider, runtime
            )
            before_grants = grants(commit)
            assert original not in runtime.uow.list_incomplete_provider_invocations()
            for _ in range(2):
                await runtime.kernel.reconcile()
                guard.recover(runtime.uow)
            # The configured real reconciliation port is never asked, even
            # though it holds matching original-request evidence with usage.
            assert evidence.observed == []
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert grants(commit) == before_grants

            price = guard.price_tables["agent.general"]
            charge = price.charge_usage(provider.actual.usage)
            assert charge.amount_micros == 200
            usage = dict(
                usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                budget=charge.to_json(),
            )
            # Actual public UOW refuses the real SUCCEEDED record; no fake
            # UNKNOWN conversion is used to bypass the original state gate.
            with pytest.raises(UnitOfWorkConflict, match="requires unknown ledger"):
                runtime.uow.record_provider_reconciliation(
                    original,
                    outcome=ResolutionOutcome.COMPLETED,
                    response_json=provider_response_json(provider.actual),
                    usage_json=usage,
                    budget_charge=charge,
                    evidence_ref="original-usage-receipt:" + original.invocation_id,
                    now=commit.store.now,
                )
            # Ordinary settlement is also not a late-accounting update API.
            with pytest.raises(UnitOfWorkConflict, match="settlement CAS failed"):
                runtime.uow.settle_provider_invocation(
                    replace(original, usage_json=usage),
                    expected_version=original.version,
                )
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert (
                runtime.uow.read_reconciliation_resolution(
                    kind="provider",
                    ledger_identity=original.invocation_id,
                    handoff_attempt=1,
                )
                is None
            )
            assert grants(commit) == before_grants and provider.calls == 1
            assert_accounting_held(commit, runtime, attempt.id, agent.agent_id)
            assert not commit.store.list_mission_claims(attempt.mission_id)

    asyncio.run(exercise())


def test_succeeded_missing_usage_close_reopen_and_public_cancel_do_not_guess_or_replay(tmp_path):
    async def exercise():
        provider = UsageOmitted()
        evidence = OriginalUsageEvidence(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            mission,
            task,
            guard,
            _,
            runtime,
        ):
            agent, attempt, original = await successful_without_usage(
                commit, task, guard, provider, runtime
            )
            ports, prices = runtime.ports, guard.price_tables
            subject_id, agent_id, mission_id = attempt.id, agent.agent_id, mission.id
            original_hold = commit.ledger.reservation(subject_id)
            original_grants = grants(commit)
            old_connections = (commit.store.connection, runtime.uow.database.connection)
        for connection in old_connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        store = Store.open(tmp_path / "orchestrator.db")
        recovered = CommitService(store)
        guard2 = ProviderBudgetGuard(
            recovered,
            owner="new-observer",
            estimator=Counter(100),
            max_slots=1,
            price_tables=prices,
        )
        try:
            async with build_agent_runtime(
                replace(ports, owner_id="new-sdk-owner", provider_admission=guard2)
            ) as runtime2:
                await runtime2.recover_pending_turns()
                await runtime2.kernel.reconcile()
                guard2.recover(runtime2.uow)
                assert provider.calls == 1 and evidence.observed == []
                assert runtime2.uow.read_provider_invocation(original.invocation_id) == original
                assert recovered.ledger.reservation(subject_id) == original_hold
                assert grants(recovered) == original_grants
                recovered.cancel_mission(mission_id)
                await runtime2.kernel.reconcile()
                guard2.recover(runtime2.uow)
                assert_accounting_held(recovered, runtime2, subject_id, agent_id)
                assert runtime2.uow.read_provider_invocation(original.invocation_id) == original
                assert grants(recovered) == original_grants
                assert provider.calls == 1 and evidence.observed == []
                assert not recovered.store.list_mission_claims(mission_id)
        finally:
            store.close()

    asyncio.run(exercise())
