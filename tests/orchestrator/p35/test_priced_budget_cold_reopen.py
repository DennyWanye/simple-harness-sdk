# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Close/reopen both real SQLite owners before late priced reconciliation.

This is a database/runtime cold reopen control, not an OS kill/process test.
The reconciliation evidence is the response actually produced before its receipt
was lost. No invocation/grant/usage/lease rows are manufactured by the fixture.
"""

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from test_provider_budget_guard import ActualProvider, Counter, create_bound, grants
from test_provider_budget_recovery import until
from test_tail_and_priced_budget import priced_runtime

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.governance.tail_budget import TailBudgetLedger, TailReserve
from agent_orchestrator.orchestrator.commit_service import CommitService, task_account
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import build_agent_runtime
from simple_harness.contracts import RunId
from simple_harness.providers import ProviderReconciliationObservation, ProviderReconciliationState


def test_priced_unknown_reopens_both_databases_new_owner_then_settles_original_charge_once(
    tmp_path,
):
    class ReceiptLost(ActualProvider):
        actual = None

        async def invoke(self, request, *, cancel):
            self.actual = await super().invoke(request, cancel=cancel)
            raise RuntimeError("response was produced; original transport receipt lost")

    provider = ReceiptLost()

    class Evidence:
        completed = False

        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.COMPLETED
                if self.completed
                else ProviderReconciliationState.STILL_UNKNOWN,
                "original-service-receipt:" + invocation.invocation_id,
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
            with commit.store.transaction():
                original_tail = TailBudgetLedger(commit.ledger).reserve_tail(
                    "future-critic",
                    task_account(task.id),
                    TailReserve(1000, 500),
                    mission_id=mission.id,
                    task_revision="original-task-contract",
                    purpose="critic",
                )
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "cold-priced")
            receipt = await agent.submit("request", input_id=key)
            intent = commit.store.get_intent_for_subject(attempt.id)
            commit.record_submitted(
                intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
            )
            await until(lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN")
            old_grant = grants(commit)[0]
            original_reservation = commit.ledger.reservation(attempt.id)
            assert original_reservation["reserved_cost_micros"] == 2100
            assert old_grant["actual_cost_micros"] is None
            assert provider.calls == 1 and provider.actual.usage.total_tokens == 150
            old_lease = runtime.uow.read_provider_runtime_lease(agent.run_id)
            assert old_lease is not None
            old_price = runtime.uow.list_provider_invocations(RunId(agent.run_id))[
                0
            ].estimator_digest
            ports = runtime.ports
            original_prices = guard.price_tables
            # Keep only identities and external service evidence across close.
            mission_id, task_id, subject_id = mission.id, task.id, attempt.id
            agent_id, turn_id = agent.agent_id, receipt.turn_id
            old_store_connection = commit.store.connection
            old_sdk_connection = runtime.uow.database.connection
        # The context has genuinely closed both connections; a new guard over
        # the old Store/runtime would not satisfy these assertions.
        for connection in (old_store_connection, old_sdk_connection):
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        reopened_store = Store.open(tmp_path / "orchestrator.db")
        recovered = CommitService(reopened_store)
        cold = ProviderBudgetGuard(
            recovered,
            owner="new-orchestrator-owner",
            estimator=Counter(100),
            max_slots=1,
            price_tables=original_prices,
        )
        try:
            assert recovered.ledger.reservation(subject_id) == original_reservation
            assert grants(recovered)[0] == old_grant
            reopened_tail = reopened_store.connection.execute(
                "SELECT * FROM budget_tail_holds WHERE hold_id='future-critic'",
            ).fetchone()
            assert dict(reopened_tail) == original_tail
            async with build_agent_runtime(
                replace(
                    ports,
                    owner_id="new-sdk-owner",
                    provider_admission=cold,
                )
            ) as runtime2:
                assert runtime2.uow.database.connection is not old_sdk_connection
                assert reopened_store.connection is not old_store_connection
                await runtime2.recover_pending_turns()
                await until(
                    lambda: (
                        (lease := runtime2.uow.read_provider_runtime_lease(agent_id)) is not None
                        and lease.owner_id == "new-sdk-owner"
                        and lease.epoch > old_lease.epoch
                    )
                )
                current_lease = runtime2.uow.read_provider_runtime_lease(agent_id)
                assert current_lease.owner_id != old_lease.owner_id
                cold.recover(runtime2.uow)
                assert grants(recovered)[0]["state"] == "UNKNOWN" and provider.calls == 1
                assert (
                    runtime2.uow.list_provider_invocations(RunId(agent_id))[0].estimator_digest
                    == old_price
                )
                recovered.cancel_mission(mission_id)
                with reopened_store.transaction():
                    TailBudgetLedger(recovered.ledger).release_tail(
                        "future-critic",
                        task_revision="original-task-contract",
                        reason="cancelled",
                    )
                    with pytest.raises(BudgetError, match="unknown"):
                        recovered.ledger.settle(subject_id=subject_id)
                assert recovered.ledger.reservation(subject_id)["reserved_cost_micros"] == 2100
                evidence.completed = True
                await runtime2.kernel.reconcile()
                cold.recover(runtime2.uow)
                record = runtime2.uow.list_provider_invocations(RunId(agent_id))[0]
                assert str(record.state) == "succeeded" and record.handoff_attempt == 1
                assert record.estimator_digest == old_price
                assert record.budget_charge.amount_micros == 200
                settled_grant = grants(recovered)[0]
                assert settled_grant["state"] == "SETTLED"
                assert settled_grant["actual_cost_micros"] == 200
                assert settled_grant["intent_id"] == intent.intent_id
                assert settled_grant["turn_id"] == turn_id
                assert settled_grant["subject_id"] == subject_id
                bridge = AgentBridge(runtime2, unpriced=False)
                facts = bridge.usage_facts(agent_id=agent_id)
                assert len(facts) == 1 and facts[0].cost_micros == 200
                with reopened_store.transaction():
                    assert (
                        recovered.ledger.import_usage(
                            subject_id=subject_id, mission_id=mission_id, facts=facts
                        )
                        == 1
                    )
                    settled = recovered.ledger.settle(subject_id=subject_id)
                    assert settled["reservation_id"] == original_reservation["reservation_id"]
                    assert (settled["settled_tokens"], settled["settled_cost_micros"]) == (150, 200)
                    assert all(
                        a.settled_cost_micros == 200 and a.reserved_cost_micros == 0
                        for a in recovered.ledger._chain(task_account(task_id))
                    )
                    assert (
                        recovered.ledger.import_usage(
                            subject_id=subject_id, mission_id=mission_id, facts=facts
                        )
                        == 0
                    )
                    assert recovered.ledger.settle(subject_id=subject_id) == settled
                await runtime2.kernel.reconcile()
                cold.recover(runtime2.uow)
                assert grants(recovered)[0] == settled_grant
                assert provider.calls == 1 and not reopened_store.list_mission_claims(mission_id)
        finally:
            reopened_store.close()

    asyncio.run(exercise())
