# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Actual successful SDK calls, immutable late accounting and two SQLite owners."""

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_provider_budget_guard import ActualProvider, Counter, grants
from test_succeeded_missing_usage_boundary import (
    OriginalUsageEvidence,
    UsageOmitted,
    successful_without_usage,
)
from test_tail_and_priced_budget import priced_runtime

from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import build_agent_runtime
from simple_harness.execution.provider_accounting import ACCOUNTING_KIND
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.providers import (
    ProviderAccountingIdentity,
    ProviderAccountingObservation,
    ProviderAccountingState,
    ProviderUsage,
)


class AccountingEvidence(OriginalUsageEvidence):
    available = False

    def __init__(self, provider):
        super().__init__(provider)
        self.accounting_observed = []

    async def observe_accounting(self, original):
        self.accounting_observed.append(original.invocation_id)
        assert original.request_id == self.provider.actual.request_id
        return ProviderAccountingObservation(
            ProviderAccountingState.KNOWN
            if self.available
            else ProviderAccountingState.STILL_UNKNOWN,
            ProviderAccountingIdentity.from_record(original),
            "original-accounting:" + original.invocation_id,
            self.provider.actual.usage if self.available else None,
        )


def receipt_count(runtime):
    return runtime.uow.database.connection.execute(
        "SELECT COUNT(*) FROM run_events WHERE kind=?", (ACCOUNTING_KIND,)
    ).fetchone()[0]


def import_original_once(commit, runtime, subject_id, mission_id, agent_id):
    facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent_id)
    assert len(facts) == 1 and facts[0].tokens == 150 and facts[0].cost_micros == 200
    assert commit.import_usage(subject_id, mission_id, facts) == 1
    with commit.store.transaction():
        settled = commit.ledger.settle(subject_id=subject_id)
    assert settled["settled_tokens"] == 150 and settled["settled_cost_micros"] == 200
    assert commit.import_usage(subject_id, mission_id, facts) == 0
    with commit.store.transaction():
        assert commit.ledger.settle(subject_id=subject_id) == settled
    for account in commit.ledger._chain(settled["account_id"]):
        assert account.reserved_tokens == account.reserved_cost_micros == 0
        assert account.settled_tokens == 150 and account.settled_cost_micros == 200


def test_actual_late_accounting_preserves_response_and_prices_original_call_once(tmp_path):
    async def exercise():
        provider = UsageOmitted()
        evidence = AccountingEvidence(provider)
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
            original_reservation = commit.ledger.reservation(attempt.id)["reservation_id"]
            await runtime.kernel.reconcile()
            assert receipt_count(runtime) == 0 and grants(commit)[0]["state"] == "UNKNOWN"
            evidence.available = True
            await runtime.kernel.reconcile()
            assert receipt_count(runtime) == 1 and evidence.observed == []
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            effective = runtime.uow.read_effective_provider_invocation(original.invocation_id)
            assert effective.state == original.state and effective.version == original.version
            assert effective.response_json == original.response_json
            assert effective.request_fingerprint == original.request_fingerprint
            assert effective.estimator_digest == original.estimator_digest
            assert effective.budget_charge.amount_micros == 200
            assert runtime.uow.read_provider_budget(original.run_id).committed_micros == 200
            assert grants(commit)[0]["state"] == "SETTLED"
            assert grants(commit)[0]["actual_cost_micros"] == 200
            held = commit.ledger.reservation(attempt.id)
            assert held["reservation_id"] == original_reservation and held["state"] == "RESERVED"
            observed = list(evidence.accounting_observed)
            await runtime.kernel.reconcile()
            assert evidence.accounting_observed == observed and receipt_count(runtime) == 1
            import_original_once(commit, runtime, attempt.id, mission.id, agent.agent_id)
            assert provider.calls == 1 and not commit.store.list_mission_claims(mission.id)

    asyncio.run(exercise())


def test_accounting_receipt_before_host_import_survives_two_database_close_reopen_cancelled(
    tmp_path,
):
    async def exercise():
        provider = UsageOmitted()
        evidence = AccountingEvidence(provider)
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
            commit.cancel_mission(mission.id)
            evidence.available = True
            observation = await evidence.observe_accounting(original)
            # Fault boundary: SDK receipt committed; no guard/Host usage import
            # yet. The actual successful result/state are never rewritten.
            payload = runtime.uow.record_provider_accounting(
                original,
                observation=observation,
                now=commit.store.now,
            )
            assert grants(commit)[0]["state"] == "UNKNOWN"
            assert commit.ledger.usage_for(attempt.id)[0] == 0
            original_hold = commit.ledger.reservation(attempt.id)
            ports, prices = runtime.ports, guard.price_tables
            subject_id, mission_id, agent_id = attempt.id, mission.id, agent.agent_id
            connections = (commit.store.connection, runtime.uow.database.connection)
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        store = Store.open(tmp_path / "orchestrator.db")
        commit2 = CommitService(store)
        guard2 = ProviderBudgetGuard(
            commit2,
            owner="new-accounting-owner",
            estimator=Counter(100),
            max_slots=1,
            price_tables=prices,
        )
        try:
            async with build_agent_runtime(
                replace(ports, provider_admission=guard2, owner_id="new-sdk-owner")
            ) as runtime2:
                assert commit2.ledger.reservation(subject_id) == original_hold
                assert (
                    runtime2.uow.read_provider_accounting_receipt(original.invocation_id) == payload
                )
                await runtime2.recover_pending_turns()
                await runtime2.kernel.reconcile()
                guard2.recover(runtime2.uow)
                assert runtime2.uow.read_provider_invocation(original.invocation_id) == original
                assert grants(commit2)[0]["state"] == "SETTLED"
                assert grants(commit2)[0]["actual_cost_micros"] == 200
                assert receipt_count(runtime2) == 1
                import_original_once(commit2, runtime2, subject_id, mission_id, agent_id)
                assert provider.calls == 1 and not commit2.store.list_mission_claims(mission_id)
                assert str(commit2.store.get_mission(mission_id).status) == "CANCELLED"
        finally:
            store.close()

    asyncio.run(exercise())


def test_two_sqlite_writers_create_one_immutable_receipt_and_reject_collisions(tmp_path):
    async def exercise():
        provider = UsageOmitted()
        evidence = AccountingEvidence(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            _,
            task,
            guard,
            _,
            runtime,
        ):
            _, _, original = await successful_without_usage(commit, task, guard, provider, runtime)
            evidence.available = True
            observation = await evidence.observe_accounting(original)
            barrier = threading.Barrier(2)

            def write():
                database = Database.open(runtime.ports.database_path)
                try:
                    uow = SqliteExecutionUnitOfWork(database)
                    barrier.wait(timeout=5)
                    return uow.record_provider_accounting(
                        original,
                        observation=observation,
                        now=original.settled_at,
                    )
                finally:
                    database.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(write) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            assert results[0] == results[1] and receipt_count(runtime) == 1
            before_events = runtime.uow.database.connection.execute(
                "SELECT COUNT(*) FROM run_events"
            ).fetchone()[0]
            for changed in (
                replace(observation, evidence_ref="conflicting-evidence"),
                replace(observation, usage=ProviderUsage(100, 75, 175)),
            ):
                with pytest.raises(UnitOfWorkConflict, match="immutable receipt conflict"):
                    runtime.uow.record_provider_accounting(
                        original,
                        observation=changed,
                        now=commit.store.now,
                    )
            assert (
                runtime.uow.database.connection.execute(
                    "SELECT COUNT(*) FROM run_events"
                ).fetchone()[0]
                == before_events
            )
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert provider.calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "field",
    [
        "request_fingerprint",
        "target_digest",
        "estimator_digest",
        "handoff_attempt",
        "run_id",
        "response_digest",
    ],
)
def test_accounting_rejects_foreign_original_authority_before_any_write(tmp_path, field):
    async def exercise():
        provider = UsageOmitted()
        evidence = AccountingEvidence(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            _,
            task,
            guard,
            _,
            runtime,
        ):
            _, _, original = await successful_without_usage(commit, task, guard, provider, runtime)
            evidence.available = True
            observation = await evidence.observe_accounting(original)
            observation = replace(
                observation,
                identity=replace(
                    observation.identity,
                    **{field: 2 if field == "handoff_attempt" else "0" * 64},
                ),
            )
            with pytest.raises(UnitOfWorkConflict, match="original identity"):
                runtime.uow.record_provider_accounting(
                    original,
                    observation=observation,
                    now=commit.store.now,
                )
            assert receipt_count(runtime) == 0 and grants(commit)[0]["state"] == "UNKNOWN"
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert provider.calls == 1

    asyncio.run(exercise())


def test_late_actual_overrun_is_committed_before_guard_refusal(tmp_path):
    class LateOverrun(UsageOmitted):
        async def invoke(self, request, *, cancel):
            response = await ActualProvider.invoke(self, request, cancel=cancel)
            self.actual = replace(response, usage=ProviderUsage(100, 1500, 1600))
            return replace(self.actual, usage=None)

    async def exercise():
        provider = LateOverrun()
        evidence = AccountingEvidence(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            mission,
            task,
            guard,
            _,
            runtime,
        ):
            agent, attempt, original = await successful_without_usage(
                commit, task, guard, provider, runtime, actual_tokens=1600
            )
            # Authoritative late billing includes the actual larger output;
            # the transport did not supply any usage in its successful response.
            late_usage = provider.actual.usage
            observation = ProviderAccountingObservation(
                ProviderAccountingState.KNOWN,
                ProviderAccountingIdentity.from_record(original),
                "original-overrun-invoice",
                late_usage,
            )
            payload = runtime.uow.record_provider_accounting(
                original,
                observation=observation,
                now=commit.store.now,
            )
            assert payload["budget"]["amount_micros"] == 3100
            with pytest.raises(ProviderAdmissionDenied, match="exceeded"):
                guard.recover(runtime.uow)
            assert grants(commit)[0]["state"] == "OVERRUN"
            assert grants(commit)[0]["actual_tokens"] == 1600
            assert grants(commit)[0]["actual_cost_micros"] == 3100
            assert receipt_count(runtime) == 1
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
            assert len(facts) == 1 and facts[0].cost_micros == 3100
            commit.import_usage(attempt.id, mission.id, facts)
            with commit.store.transaction():
                settled = commit.ledger.settle(subject_id=attempt.id)
            assert settled["settled_tokens"] == 1600 and settled["settled_cost_micros"] == 3100
            assert provider.calls == 1

    asyncio.run(exercise())
