# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Exact SDK uncertain handoffs, cancellation fences and owner loss."""

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from graph_helpers7 import graph_service, node
from test_provider_budget_guard import ActualProvider, Counter, create_bound, grants, setup_runtime

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.providers import (
    CancelToken,
    ProviderReconciliationObservation,
    ProviderReconciliationState,
)
from simple_harness.runtime.consumer_adapter import (
    ConsumerRuntimePolicies,
    _DefaultRuntimeReconciliation,
    _DefaultToolReconciliation,
)


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 5)


def test_cancel_between_admission_and_sdk_cas_is_zero_handoff(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, mission, task, guard, provider, runtime):
            original = guard.handoff

            @contextmanager
            def cancel_first(ticket, *, request, cancel):
                commit.cancel_mission(mission.id)
                with original(ticket, request=request, cancel=cancel):
                    yield

            guard.handoff = cancel_first
            agent, _, key = await create_bound(commit, task, guard, runtime, "cancel-cas")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed"
            assert provider.calls == 0
            assert [row["state"] for row in grants(commit)] == ["RELEASED"]
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0

    asyncio.run(exercise())


def test_slot_queue_has_explicit_nonbillable_liveness_and_original_deadline(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path, blocked=True) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            first = await create_bound(commit, task, guard, runtime, "active")
            second = await create_bound(commit, task, guard, runtime, "queued")
            running = asyncio.create_task(first[0].ask("request", input_id=first[2], timeout=5))
            await asyncio.wait_for(provider.entered.wait(), 3)
            waiting = asyncio.create_task(second[0].ask("request", input_id=second[2], timeout=5))
            agent_id, turn_id = second[0].agent_id, second[0].turn_id_for(second[2])
            await until(lambda: guard.waiting_for_slot(agent_id=agent_id, turn_id=turn_id))
            bridge = AgentBridge(runtime, unpriced=True)
            live = await bridge.liveness(agent_id=agent_id, turn_id=turn_id)
            assert live.alive and live.blocked and live.blocker["kind"] == "provider_slot_wait"
            assert live.blocker["billable"] is False
            version = commit.store.get_attempt(second[1].id).version
            records = runtime.uow.list_provider_invocations(RunId(second[0].run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0
            assert not bridge.usage_facts(agent_id=agent_id)
            real_clock = guard._clock
            guard._clock = lambda: real_clock() + 901
            result = await waiting
            assert str(result.state) == "failed"
            assert not guard.waiting_for_slot(agent_id=agent_id, turn_id=turn_id)
            assert commit.store.get_attempt(second[1].id).version == version
            assert provider.calls == 1
            provider.allow.set()
            await running

    asyncio.run(exercise())


def test_real_orchestrator_planner_worker_critic_and_whole_account_chain(tmp_path):
    from fixtures_provider import (
        RoleScriptedProvider,
        envelope_step,
        graph_proposal_step,
        package_of,
    )
    from graph_helpers7 import spec

    from agent_orchestrator.contracts import MissionStatus
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    report = "# Finding\nThe material is complete for this fixture.\n"
    reviewed = []

    def assess(request):
        actual_reads = [
            json.loads(message.content)
            for message in request.messages
            if str(message.role) == "tool"
        ]
        actual = actual_reads[-1]["value"]["content"]
        reviewed.append(actual)
        met = actual == report
        return (
            "<critic_verdict>"
            + json.dumps(
                {
                    "verdict": "PASS" if met else "FAIL",
                    "findings": []
                    if met
                    else [{"severity": "blocker", "detail": "wrong actual file"}],
                    "mission_criteria": [
                        {
                            "criterion": criterion,
                            "met": met,
                            "reason": "Compared actual workspace read to expected fixture content",
                        }
                        for criterion in package_of(request)["mission_success_criteria"]
                    ],
                }
            )
            + "</critic_verdict>"
        )

    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        node(
                            "A",
                            tokens=50_000,
                            verification_policy=["format_check", "rule_check", "critic_review"],
                        )
                    ]
                )
            ],
            "worker": [
                ("workspace_write_file", {"path": "a.md", "content": report}),
                envelope_step(
                    summary="wrote a.md",
                    artifacts=["a.md"],
                    claims=["a.md contains the fixture finding"],
                ),
            ],
            "critic": [("workspace_read_file", {"path": "a.md"}), assess] * 2,
        }
    )

    async def exercise():
        capacity_config = OrchestratorConfig(
            evidence_root=tmp_path / "capacity",
            max_concurrency=3,
            candidates_per_task=2,
            max_concurrent_model_calls=2,
        )
        assert capacity_config.max_concurrent_model_calls == 2
        # The role script describes one Worker. Capacity independence is asserted
        # above; simultaneous candidates have their own shared-reservation oracle.
        config = OrchestratorConfig(
            evidence_root=tmp_path / "whole",
            max_concurrency=1,
            candidates_per_task=1,
            max_concurrent_model_calls=2,
        )
        async with Orchestrator(config, provider, provider_token_estimator=Counter(1000)) as orch:
            mission = await orch.submit_mission(spec(success_criteria=("file:a.md",)))
            await asyncio.wait_for(orch.run(), 15)
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orch.progress_log
            )
            assert reviewed and all(value == report for value in reviewed)
            rows = grants(orch.commit)
            assert len(rows) == sum(provider.by_role.values())
            assert all(
                row["state"] == "SETTLED" and row["actual_tokens"] <= row["total_upper"]
                for row in rows
            )
            assert set(provider.by_role) == {"planner", "worker", "critic"}
            for row in rows:
                intent = orch.store.get_intent(row["intent_id"])
                assert intent.config["provider_admission_fingerprint"] == row["fingerprint"]
            with orch.store.transaction():
                costs = orch.commit.ledger.costs_report(mission.id)
            accounts = [a for a in costs["accounts"] if a["scope"] == "mission"]
            assert accounts[0]["reserved_tokens"] == 0
            assert accounts[0]["settled_tokens"] == sum(row["actual_tokens"] for row in rows)
            assert all(row["state"] == "SETTLED" for row in costs["reservations"])

    asyncio.run(exercise())


def test_live_new_owner_refuses_without_cancelling_its_lease(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "owner")
            # An actual Commit transition acquires the other owner's lease after
            # expiration; the old executor must not cancel or take it back.
            clock = commit.store._clock
            commit.store._clock = lambda: clock() + 120
            commit.renew_lease(
                attempt.id, owner="new-owner", lease_seconds=600, liveness={"progress": 0}
            )
            before = commit.store.get_attempt(attempt.id)
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed"
            assert provider.calls == 0
            after = commit.store.get_attempt(attempt.id)
            assert after == before
            assert after.lease_owner == "new-owner"

    asyncio.run(exercise())


def test_actual_unknown_stays_held_then_sdk_reconciliation_settles_once(tmp_path):
    class ResponseLost(ActualProvider):
        actual = None

        async def invoke(self, request, *, cancel):
            self.actual = await super().invoke(request, cancel=cancel)
            raise RuntimeError("transport lost the actual response receipt")

    class Evidence:
        completed = False

        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.COMPLETED
                if self.completed
                else ProviderReconciliationState.STILL_UNKNOWN,
                "fixture-transport:" + invocation.invocation_id,
                provider.actual if self.completed else None,
            )

    async def exercise():
        nonlocal provider
        commit, mission, tasks = graph_service(tmp_path, nodes=[node("A")])
        provider = ResponseLost()
        evidence = Evidence()
        guard = ProviderBudgetGuard(commit, owner="test-owner", estimator=Counter(100), max_slots=1)
        policies = ConsumerRuntimePolicies(
            "unpriced_local",
            False,
            "consumer_reconciles",
            tool_reconciliation=_DefaultToolReconciliation(),
            provider_reconciliation=evidence,
            runtime_reconciliation=_DefaultRuntimeReconciliation(),
        )
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "execution.db"),
            provider_admission=guard,
            policies=policies,
            default_max_output_tokens=1000,
        )
        try:
            async with build_agent_runtime(ports) as runtime:
                agent, attempt, key = await create_bound(
                    commit, tasks["A"], guard, runtime, "uncertain"
                )
                receipt = await agent.submit("request", input_id=key)
                intent = commit.store.get_intent_for_subject(attempt.id)
                commit.record_submitted(
                    intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
                )
                await until(
                    lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN"
                )
                bridge = AgentBridge(runtime, unpriced=True)
                assert bridge.has_unknown_charge(agent_id=agent.agent_id)
                assert bridge.usage_facts(agent_id=agent.agent_id) == []
                with commit.store.transaction():
                    with pytest.raises(BudgetError, match="unknown"):
                        commit.ledger.settle(subject_id=attempt.id)
                held = grants(commit)
                # A separately constructed guard represents cold Host state. SDK
                # evidence, not in-memory tickets, owns the recovery decision.
                cold = ProviderBudgetGuard(
                    commit, owner="test-owner", estimator=Counter(100), max_slots=1
                )
                cold.recover(runtime.uow)
                assert grants(commit) == held
                commit.cancel_mission(mission.id)
                evidence.completed = True
                await runtime.kernel.reconcile()
                cold.recover(runtime.uow)
                known = grants(commit)
                assert known[0]["state"] == "SETTLED"
                assert known[0]["actual_tokens"] == 150
                assert provider.calls == 1
                facts = bridge.usage_facts(agent_id=agent.agent_id)
                assert sum(fact.tokens for fact in facts) == 150
                with commit.store.transaction():
                    commit.ledger.import_usage(
                        subject_id=attempt.id, mission_id=mission.id, facts=facts
                    )
                    settled = commit.ledger.settle(subject_id=attempt.id)
                assert settled["settled_tokens"] == 150
                cold.recover(runtime.uow)
                assert grants(commit) == known
                assert not commit.store.list_mission_claims(mission.id)
        finally:
            commit.store.close()

    provider = None
    asyncio.run(exercise())


async def create_service(commit, mission, guard, runtime):
    from agent_orchestrator.runtime.agent_worker import user_message_json

    agent = await runtime.create(
        AgentConfig(name="service", instructions="Answer", model_profile_ref="agent.general"),
        creation_key="service",
    )
    account = commit.store.connection.execute(
        "SELECT account_id FROM budget_accounts WHERE mission_id=? AND scope='mission'",
        (mission.id,),
    ).fetchone()[0]
    intent = commit.create_service_intent(
        kind="critic",
        subject_id=mission.id + ":critic:1",
        mission_id=mission.id,
        account_id=account,
        creation_key="service",
        input_id="service-input",
        input_hash="h",
        config={
            "agent_config": agent.config.to_json(),
            "message": user_message_json("request"),
            "provider_admission_fingerprint": guard.fingerprint,
        },
        reservation=Reservation(tokens=4000, cost_micros=0),
    )
    commit.claim_intent(intent.intent_id, owner=guard.adapter.owner, lease_seconds=60)
    commit.record_agent_created(
        intent.intent_id,
        agent_id=agent.agent_id,
        expected_turn_id=agent.turn_id_for(intent.input_id),
    )
    receipt = await agent.submit("request", input_id=intent.input_id)
    commit.record_submitted(
        intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
    )
    return agent, intent, receipt


def test_expired_service_claim_does_not_release_current_sdk_reserved_grant(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, mission, _, guard, provider, runtime):
            original = guard.handoff
            observer = ProviderBudgetGuard(
                commit, owner="other-observer", estimator=Counter(100), max_slots=1
            )
            observed = []

            @contextmanager
            def inspect(ticket, *, request, cancel):
                clock = commit.store._clock
                commit.store._clock = lambda: clock() + 120
                observer.recover(runtime.uow)
                observed.append(grants(commit)[0]["state"])
                with original(ticket, request=request, cancel=cancel):
                    yield

            guard.handoff = inspect
            agent, _, receipt = await create_service(commit, mission, guard, runtime)
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert str(result.state) == "committed"
            assert observed == ["RESERVED"] and provider.calls == 1
            assert grants(commit)[0]["state"] == "SETTLED"

    asyncio.run(exercise())


def test_new_lease_does_not_keep_old_handed_off_invocation_alive(tmp_path):
    async def exercise():
        from simple_harness.execution.dispatch import ProviderInvocationCoordinator
        from simple_harness.execution.sqlite import Database
        from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
        from simple_harness.runtime.consumer_adapter import _ConsumerProviderAdapter

        async with setup_runtime(tmp_path, blocked=True) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            agent, _, key = await create_bound(commit, task, guard, runtime, "old-handoff")
            await agent.submit("request", input_id=key)
            await asyncio.wait_for(provider.entered.wait(), 5)
            before = runtime.uow.list_provider_invocations(RunId(agent.run_id))[0]
            assert str(before.state) == "handed_off" and before.handoff_attempt == 1
            old_lease = runtime.uow.read_provider_runtime_lease(agent.run_id)
            assert old_lease is not None
            observer_db = Database.open(runtime.uow.database.path)
            try:
                observer_uow = SqliteExecutionUnitOfWork(observer_db)
                takeover_at = old_lease.expires_at + 1
                _, new_lease = observer_uow.claim_runtime_activation(
                    run_id=agent.run_id,
                    owner_id="new-sdk-owner",
                    namespace=old_lease.namespace,
                    now=takeover_at,
                    lease_ttl_seconds=60,
                )
                assert new_lease.epoch > old_lease.epoch
                # The invocation is still the actual old physical handoff;
                # acquiring the new lease did not fabricate UNKNOWN or a reply.
                assert observer_uow.read_provider_invocation(before.invocation_id) == before
                observer = ProviderInvocationCoordinator(
                    uow=observer_uow,
                    provider=_ConsumerProviderAdapter(provider, runtime.ports.model),
                    budget_policy=runtime.ports.policies.budget_policy,
                    clock=lambda: takeover_at,
                )
                assert await observer.reconcile_incomplete() == 1
                after = observer_uow.read_provider_invocation(before.invocation_id)
                assert str(after.state) == "unknown"
                assert after.error_code == "recovered_after_handoff"
                assert after.handoff_attempt == 1 and provider.calls == 1
                assert observer_uow.read_provider_runtime_lease(agent.run_id) == new_lease
                guard.recover(observer_uow)
                assert grants(commit)[0]["state"] == "UNKNOWN"
            finally:
                provider.allow.set()
                observer_db.close()

    asyncio.run(exercise())


def test_new_runtime_owner_recovers_submitted_service_and_old_owner_cannot_handoff(tmp_path):
    class NotStartedYet:
        confirmed = False

        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.CONFIRMED_NOT_STARTED
                if self.confirmed and invocation.handoff_attempt == 1
                else ProviderReconciliationState.STILL_UNKNOWN,
                "fixture-not-started:" + invocation.invocation_id,
            )

    async def exercise():
        from simple_harness.execution.dispatch import ProviderInvocationCoordinator
        from simple_harness.execution.sqlite import Database
        from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
        from simple_harness.providers.errors import ProviderTransportError
        from simple_harness.runtime.consumer_adapter import _ConsumerProviderAdapter

        commit, mission, _ = graph_service(tmp_path, nodes=[node("A")])
        provider = ActualProvider(blocked=True)
        counter = Counter(100)
        evidence = NotStartedYet()
        old_guard = ProviderBudgetGuard(commit, owner="old-orch", estimator=counter, max_slots=1)
        policies = ConsumerRuntimePolicies(
            "unpriced_local",
            False,
            "consumer_reconciles",
            tool_reconciliation=_DefaultToolReconciliation(),
            provider_reconciliation=evidence,
            runtime_reconciliation=_DefaultRuntimeReconciliation(),
        )
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "cold-sdk.db"),
            owner_id="old-sdk",
            provider_admission=old_guard,
            policies=policies,
            default_max_output_tokens=1000,
        )
        actual_invoke = provider.invoke
        captured = []

        async def not_sent(request, *, cancel):
            captured.append(
                runtime.uow.read_provider_runtime_lease(
                    request.request_id.value.split(":provider-turn:")[0]
                )
            )
            raise ProviderTransportError()

        provider.invoke = not_sent
        try:
            async with build_agent_runtime(ports) as runtime:
                agent, intent, receipt = await create_service(commit, mission, old_guard, runtime)
                agent_id = agent.agent_id
                await until(
                    lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN"
                )
            provider.invoke = actual_invoke
            evidence.confirmed = True
            new_guard = ProviderBudgetGuard(
                commit, owner="new-orch", estimator=counter, max_slots=1
            )
            async with build_agent_runtime(
                replace(ports, owner_id="new-sdk", provider_admission=new_guard)
            ) as resumed:
                await resumed.recover_pending_turns()
                await asyncio.wait_for(provider.entered.wait(), 5)
                # A routine reconciliation during this runtime's actual second
                # call must not turn its live handoff into a recovered UNKNOWN.
                before = resumed.uow.list_provider_invocations(RunId(agent_id))[0]
                assert str(before.state) == "handed_off" and before.handoff_attempt == 2
                await resumed.kernel.reconcile()
                after = resumed.uow.list_provider_invocations(RunId(agent_id))[0]
                assert after == before
                assert (
                    resumed.uow.read_reconciliation_resolution(
                        kind="provider",
                        ledger_identity=before.invocation_id,
                        handoff_attempt=2,
                    )
                    is None
                )
                # A distinct coordinator/SQLite connection has no local active
                # set. Its recovery must still respect the live canonical lease.
                observer_db = Database.open(ports.database_path)
                try:
                    observer = ProviderInvocationCoordinator(
                        uow=SqliteExecutionUnitOfWork(observer_db),
                        provider=_ConsumerProviderAdapter(provider, ports.model),
                        budget_policy=ports.policies.budget_policy,
                    )
                    assert (
                        await observer.reconcile_incomplete(provider_reconciliation=evidence) == 0
                    )
                    assert resumed.uow.list_provider_invocations(RunId(agent_id))[0] == before
                finally:
                    observer_db.close()
                rows = grants(commit)
                assert rows[0]["state"] == "RELEASED" and rows[1]["sdk_owner"] == "new-sdk"
                assert commit.store.get_intent(intent.intent_id).lease_owner == "old-orch"
                record = resumed.uow.list_provider_invocations(RunId(agent_id))[0]
                with pytest.raises(ProviderAdmissionDenied, match="runtime lease"):
                    await old_guard.acquire(
                        request=provider.requests[0],
                        record=record,
                        cancel=CancelToken(),
                        uow=resumed.uow,
                        execution_lease=captured[0],
                    )
                provider.allow.set()
                reopened = await resumed.open(agent_id)
                result = await reopened.wait_turn(receipt.turn_id, timeout=5)
                assert str(result.state) == "committed"
                assert provider.calls == 1
                assert grants(commit)[1]["state"] == "SETTLED"
        finally:
            provider.allow.set()
            commit.store.close()

    asyncio.run(exercise())
