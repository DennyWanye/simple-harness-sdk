# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P2 accounting interface controls over real SDK Provider invocations.

These tests cover authority, FAILED late usage, and receipt/witness transactions.
They intentionally do not import/settle Orchestrator usage by hand, and do not
claim coverage of the separate automatic terminal-Mission/service recovery scan.
Only the Provider and its external accounting observation are controlled.
"""

import asyncio
from dataclasses import replace

import pytest
from test_provider_budget_guard import ActualProvider, create_bound, grants
from test_succeeded_missing_usage_boundary import UsageOmitted, successful_without_usage
from test_tail_and_priced_budget import priced_runtime

from agent_orchestrator.runtime.agent_worker import AgentBridge
from simple_harness.contracts import RunId
from simple_harness.execution.provider_accounting import ACCOUNTING_KIND, accounting_event_id
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.providers import (
    ProviderAccountingIdentity,
    ProviderAccountingObservation,
    ProviderAccountingState,
    ProviderUsage,
)
from simple_harness.providers.errors import ProviderProtocolError


async def completed_call(commit, task, guard, runtime, key):
    agent, attempt, input_id = await create_bound(commit, task, guard, runtime, key)
    receipt = await agent.submit("request", input_id=input_id)
    intent = commit.store.get_intent_for_subject(attempt.id)
    commit.record_submitted(
        intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
    )
    result = await agent.wait_turn(receipt.turn_id, timeout=5)
    [record] = runtime.uow.list_provider_invocations(RunId(agent.run_id))
    return agent, attempt, result, record


def accounting_rows(runtime, record):
    """Read only this receipt and its same-transaction witness, not all runtime logs."""
    identity = accounting_event_id(record)
    return [
        tuple(row)
        for row in runtime.uow.database.connection.execute(
            "SELECT event_id,run_id,durable_seq,kind,payload_json,created_at FROM run_events"
            " WHERE event_id=? OR (kind='audit.event.v2'"
            " AND json_extract(payload_json,'$.source_id')=?) ORDER BY durable_seq",
            (identity, identity),
        )
    ]


@pytest.mark.parametrize("replace_counts", [False, True], ids=["same-known", "changed-known"])
def test_known_usage_refuses_accounting_receipt_even_with_exact_original_identity(
    tmp_path, replace_counts
):
    async def exercise():
        async with priced_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):
            agent, attempt, result, original = await completed_call(
                commit, task, guard, runtime, "known-usage"
            )
            assert str(result.state) == "committed"
            assert str(original.state) == "succeeded" and original.handoff_attempt == 1
            assert original.usage_json["usage"]["total_tokens"] == 150
            assert original not in runtime.uow.list_pending_provider_accounting()
            before_grants = grants(commit)
            before_budget = runtime.uow.read_provider_budget(original.run_id)
            before_facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
            assert len(before_facts) == 1 and before_facts[0].tokens == 150
            assert before_facts[0].cost_micros == 200
            observation = ProviderAccountingObservation(
                ProviderAccountingState.KNOWN,
                ProviderAccountingIdentity.from_record(original),
                "known-original-invoice",
                ProviderUsage(100, 75, 175) if replace_counts else ProviderUsage(100, 50, 150),
            )
            with pytest.raises(UnitOfWorkConflict, match="terminal missing usage"):
                runtime.uow.record_provider_accounting(
                    original, observation=observation, now=commit.store.now
                )
            assert accounting_rows(runtime, original) == []
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert (
                runtime.uow.read_effective_provider_invocation(original.invocation_id) == original
            )
            assert runtime.uow.read_provider_budget(original.run_id) == before_budget
            assert grants(commit) == before_grants
            assert (
                AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
                == before_facts
            )
            assert commit.ledger.usage_for(attempt.id)[0] == 0  # no hand-written import
            assert provider.calls == 1

    asyncio.run(exercise())


class FailedWithoutUsage(ActualProvider):
    """A real handed-off call fails at the Provider protocol boundary; invoice is later."""

    actual = None

    async def invoke(self, request, *, cancel):
        self.actual = await super().invoke(request, cancel=cancel)
        # No usage in the exception: execution knows failure, not actual billing.
        raise ProviderProtocolError()


class FailedCallAccounting:
    def __init__(self, provider):
        self.provider = provider
        self.available = False
        self.observed = []

    async def observe(self, original):
        raise AssertionError("terminal FAILED must not enter execution reconciliation")

    async def observe_accounting(self, original):
        assert original.request_id == self.provider.actual.request_id
        assert str(original.state) == "failed"
        self.observed.append(original.invocation_id)
        return ProviderAccountingObservation(
            ProviderAccountingState.KNOWN
            if self.available
            else ProviderAccountingState.STILL_UNKNOWN,
            ProviderAccountingIdentity.from_record(original),
            "failed-original-invoice:" + original.invocation_id,
            self.provider.actual.usage if self.available else None,
        )


def test_real_failed_missing_usage_accepts_late_accounting_without_rewriting_failed_turn(tmp_path):
    async def exercise():
        provider = FailedWithoutUsage()
        evidence = FailedCallAccounting(provider)
        async with priced_runtime(tmp_path, provider=provider, evidence=evidence) as (
            commit,
            mission,
            task,
            guard,
            _,
            runtime,
        ):
            agent, attempt, result, original = await completed_call(
                commit, task, guard, runtime, "failed-missing-usage"
            )
            assert str(result.state) == "failed"
            assert str(original.state) == "failed" and original.handoff_attempt == 1
            assert original.error_code == "provider_protocol_error"
            assert original.response_json is None and original.usage_json.get("usage") is None
            before_turn = runtime.uow.read_agent_turn_result(result.turn_id)
            before_hold = commit.ledger.reservation(attempt.id)
            before_grant = grants(commit)
            assert before_grant[0]["state"] == "UNKNOWN"
            assert AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id) == []
            await runtime.kernel.reconcile()
            assert accounting_rows(runtime, original) == []
            assert grants(commit) == before_grant
            evidence.available = True
            await runtime.kernel.reconcile()
            effective = runtime.uow.read_effective_provider_invocation(original.invocation_id)
            assert str(effective.state) == "failed" and effective.version == original.version
            assert effective.response_json is None and effective.error_code == original.error_code
            assert effective.budget_charge.amount_micros == 200
            assert runtime.uow.read_provider_budget(original.run_id).committed_micros == 200
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert runtime.uow.read_agent_turn_result(result.turn_id) == before_turn
            rows = accounting_rows(runtime, original)
            assert [row[3] for row in rows] == [ACCOUNTING_KIND, "audit.event.v2"]
            assert grants(commit)[0]["state"] == "SETTLED"
            facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
            assert len(facts) == 1 and facts[0].tokens == 150 and facts[0].cost_micros == 200
            # SDK-only boundary: no fabricated Orchestrator collector invocation.
            assert commit.ledger.usage_for(attempt.id)[0] == 0
            assert commit.ledger.reservation(attempt.id) == before_hold
            assert not commit.store.list_mission_claims(mission.id)
            observed = list(evidence.observed)
            await runtime.kernel.reconcile()
            assert evidence.observed == observed
            assert accounting_rows(runtime, original) == rows
            assert provider.calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["before_write", "after_write"])
def test_accounting_fault_rolls_back_receipt_and_witness_then_retry_is_once(tmp_path, stage):
    class InjectedAccountingFault(RuntimeError):
        pass

    async def exercise():
        provider = UsageOmitted()
        async with priced_runtime(tmp_path, provider=provider) as (
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
            observation = ProviderAccountingObservation(
                ProviderAccountingState.KNOWN,
                ProviderAccountingIdentity.from_record(original),
                "fault-boundary-original-invoice",
                provider.actual.usage,
            )
            before_budget = runtime.uow.read_provider_budget(original.run_id)
            before_grants = grants(commit)
            before_hold = commit.ledger.reservation(attempt.id)
            seen = []

            def fault(point):
                seen.append(point)
                if point == "provider_accounting." + stage:
                    # after_write means both receipt and actual audit witness have
                    # been inserted, so the rollback assertion is meaningful.
                    assert len(accounting_rows(runtime, original)) == (
                        2 if stage == "after_write" else 0
                    )
                    raise InjectedAccountingFault(point)

            with pytest.raises(InjectedAccountingFault, match=stage):
                runtime.uow.record_provider_accounting(
                    original, observation=observation, now=commit.store.now, fault=fault
                )
            assert seen[-1] == "provider_accounting." + stage
            assert not runtime.uow.database.connection.in_transaction
            assert accounting_rows(runtime, original) == []
            assert runtime.uow.read_provider_accounting_receipt(original.invocation_id) is None
            assert runtime.uow.read_provider_invocation(original.invocation_id) == original
            assert (
                runtime.uow.read_effective_provider_invocation(original.invocation_id) == original
            )
            assert runtime.uow.read_provider_budget(original.run_id) == before_budget
            assert grants(commit) == before_grants
            assert commit.ledger.reservation(attempt.id) == before_hold
            payload = runtime.uow.record_provider_accounting(
                original, observation=observation, now=commit.store.now
            )
            rows = accounting_rows(runtime, original)
            assert [row[3] for row in rows] == [ACCOUNTING_KIND, "audit.event.v2"]
            assert (
                runtime.uow.record_provider_accounting(
                    original, observation=observation, now=commit.store.now
                )
                == payload
            )
            assert accounting_rows(runtime, original) == rows
            # An alternative invoice after retry still cannot replace the receipt.
            with pytest.raises(UnitOfWorkConflict, match="immutable receipt conflict"):
                runtime.uow.record_provider_accounting(
                    original,
                    observation=replace(observation, evidence_ref="different-invoice"),
                    now=commit.store.now,
                )
            assert accounting_rows(runtime, original) == rows
            assert runtime.uow.read_provider_budget(original.run_id).committed_micros == 200
            facts = AgentBridge(runtime, unpriced=False).usage_facts(agent_id=agent.agent_id)
            assert len(facts) == 1 and facts[0].tokens == 150 and facts[0].cost_micros == 200
            assert commit.ledger.usage_for(attempt.id)[0] == 0
            assert commit.ledger.reservation(attempt.id) == before_hold
            assert provider.calls == 1

    asyncio.run(exercise())
