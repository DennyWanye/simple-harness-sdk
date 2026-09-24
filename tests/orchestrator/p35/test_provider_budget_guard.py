# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real SDK requests, Commit reservations and public cancel; no network/handmade PASS."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

from graph_helpers7 import graph_service, node
from provider_fixture import ScriptedProvider

from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.providers import ProviderUsage


class Counter:
    fingerprint = "fixture-text-count-v1"
    bound_protocol = "fixture-text-only-v1"
    requires_prior_output_reserve = True

    def __init__(self, tokens):
        self.tokens = tokens
        self.requests = []

    def estimate_input_tokens(self, request):
        self.requests.append(request)
        return self.tokens


class ActualProvider(ScriptedProvider):
    def __init__(self, *, blocked=False):
        super().__init__(["actual answer"] * 8, blocked=blocked)
        self.entered = asyncio.Event()

    async def invoke(self, request, *, cancel):
        self.entered.set()
        response = await super().invoke(request, cancel=cancel)
        if response.message.content == "":
            response = replace(response, finish_reason="length")
        return replace(
            response, usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        )


class ZeroUsageProvider(ActualProvider):
    """A relay that answers normally but reports usage 0/0/0 (observed 2026-09-24)."""

    async def invoke(self, request, *, cancel):
        response = await super().invoke(request, cancel=cancel)
        return replace(response, usage=ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0))


@asynccontextmanager
async def setup_runtime(
    tmp_path, *, tokens=20_000, estimate=100, slots=1, blocked=False, empty_retry=False, provider_factory=ActualProvider
):
    commit, mission, tasks = graph_service(tmp_path, nodes=[node("A", tokens=tokens)])
    counter = Counter(estimate)
    guard = ProviderBudgetGuard(commit, owner="test-owner", estimator=counter, max_slots=slots)
    provider = provider_factory(blocked=blocked)
    if empty_retry:
        provider.script[0] = ""
    ports = AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "execution.db"),
        provider_admission=guard,
        default_max_output_tokens=1_000,
        max_output_tokens_ceiling=2_000 if empty_retry else 1_000,
        empty_response_retries=1 if empty_retry else 0,
    )
    try:
        async with build_agent_runtime(ports) as runtime:
            yield commit, mission, tasks["A"], guard, provider, runtime
    finally:
        commit.store.close()


async def create_bound(commit, task, guard, runtime, key, *, role="worker", reservation=None):
    agent = await runtime.create(
        AgentConfig(name=key, instructions="Answer briefly.", model_profile_ref="agent.general"),
        creation_key=key,
    )
    attempt, intent = commit.create_attempt(
        task.id,
        role=role,
        model="agent-model",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=reservation or Reservation(tokens=4_000, cost_micros=0),
        intent_config={
            "agent_config": agent.config.to_json(),
            "message": user_message_json("request"),
            "provider_admission_fingerprint": guard.fingerprint,
        },
        input_hash="h",
        candidates_per_task=2,
    )
    key = intent.input_id
    commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
    commit.record_agent_created(
        intent.intent_id, agent_id=agent.agent_id, expected_turn_id=agent.turn_id_for(key)
    )
    return agent, attempt, key


def grants(commit):
    return [
        dict(row)
        for row in commit.store.connection.execute(
            "SELECT * FROM provider_token_grants ORDER BY created_at, invocation_id"
        )
    ]


def test_input_plus_output_refuses_before_physical_handoff(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path, tokens=9_000, estimate=8_500) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "one")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed"
            assert provider.calls == 0
            assert result.error["source_kind"] == "provider_admission"
            assert result.error["retryable"] is False
            denial = result.error["detail"]
            assert denial["reason_code"] == "budget_exhausted"
            assert denial["dimension"] == "tokens"
            assert denial["requested"] == 5500 and denial["remaining"] == 5000
            assert denial["request_tokens"] == 9500
            from agent_orchestrator.runtime.model_router import classify_turn_error

            assert classify_turn_error(result.error) == "admission_denied"
            persisted = runtime.uow.read_agent_turn_result(result.turn_id)
            assert persisted is not None
            assert persisted.result_json["error"]["detail"] == denial
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert all(record.handoff_attempt == 0 for record in records)
            assert commit.ledger.reservation(attempt.id)["reserved_tokens"] == 4_000
            assert not AgentBridge(runtime, unpriced=True).usage_facts(agent_id=agent.agent_id)
            assert not grants(commit)

    asyncio.run(exercise())


def test_shared_task_reservation_admits_only_one_concurrent_request(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path, estimate=15_000, slots=2, blocked=True) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            first = await create_bound(commit, task, guard, runtime, "one")
            second = await create_bound(commit, task, guard, runtime, "two")
            calls = [
                asyncio.create_task(agent.ask("request", input_id=key, timeout=5))
                for agent, _, key in (first, second)
            ]
            await asyncio.wait_for(provider.entered.wait(), 3)
            for _ in range(100):
                if any(call.done() for call in calls):
                    break
                await asyncio.sleep(0.001)
            assert provider.calls == 1
            assert len(grants(commit)) == 1
            assert (
                sum(
                    commit.ledger.reservation(attempt.id)["reserved_tokens"]
                    for _, attempt, _ in (first, second)
                )
                == 20_000
            )
            provider.allow.set()
            outcomes = await asyncio.gather(*calls)
            assert sorted(str(result.state) for result in outcomes) == ["committed", "failed"]
            assert grants(commit)[0]["actual_tokens"] == 150

    asyncio.run(exercise())


def test_public_cancel_while_slot_waiting_never_hands_off_waiter(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path, blocked=True) as (
            commit,
            mission,
            task,
            guard,
            provider,
            runtime,
        ):
            first = await create_bound(commit, task, guard, runtime, "one")
            second = await create_bound(commit, task, guard, runtime, "two")
            running = asyncio.create_task(first[0].ask("request", input_id=first[2], timeout=5))
            await asyncio.wait_for(provider.entered.wait(), 3)
            waiting = asyncio.create_task(second[0].ask("request", input_id=second[2], timeout=5))
            await asyncio.sleep(0.02)
            commit.cancel_mission(mission.id)
            provider.allow.set()
            await asyncio.gather(running, waiting)
            assert provider.calls == 1
            assert len(grants(commit)) == 1
            assert grants(commit)[0]["actual_tokens"] == 150
            second_records = runtime.uow.list_provider_invocations(RunId(second[0].run_id))
            assert all(record.handoff_attempt == 0 for record in second_records)

    asyncio.run(exercise())


def test_real_empty_length_usage_is_settled_and_reserved_for_next_call(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path, empty_retry=True) as (
            commit,
            _,
            task,
            guard,
            provider,
            runtime,
        ):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "empty")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "committed"
            assert provider.calls == 2
            rows = grants(commit)
            assert [row["state"] for row in rows] == ["SETTLED", "SETTLED"]
            assert [row["prior_output_upper"] for row in rows] == [0, 50]
            assert [row["output_ceiling"] for row in rows] == [1000, 2000]
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert {str(record.state) for record in records} == {"failed", "succeeded"}
            bridge = AgentBridge(runtime, unpriced=True)
            facts = bridge.usage_facts(agent_id=agent.agent_id)
            assert sum(fact.tokens for fact in facts) == 300
            with commit.store.transaction():
                commit.ledger.import_usage(
                    subject_id=attempt.id, mission_id=attempt.mission_id, facts=facts
                )
                settled = commit.ledger.settle(subject_id=attempt.id)
                assert settled["settled_tokens"] == 300
                assert not commit.ledger.has_unknown_usage(attempt.id)

    asyncio.run(exercise())


def test_zero_usage_is_held_unknown_and_never_holds_the_next_call(tmp_path):
    """Rule 2026-09-24 (may overcount, never undercount): a 0/0/0 usage report is not a zero
    charge.  Each grant stays UNKNOWN with its reservation held (late accounting may still
    resolve it), the ledger imports no fact for it, and — unlike before — the next call of the
    same run is still admitted: the earlier call's output is bounded by its own cap instead of
    holding the Agent."""

    async def exercise():
        async with setup_runtime(tmp_path, empty_retry=True, provider_factory=ZeroUsageProvider) as (
            commit, _, task, guard, provider, runtime,
        ):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "zero")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "committed"
            assert provider.calls == 2
            rows = grants(commit)
            # First call: empty reply (failed) with usage 0/0/0 -> unresolved, held UNKNOWN.
            # Second call: admitted after it (no hold); its prior-output bound is the first
            # call's own cap 1000 (the fixture counter requires a prior reserve); also 0/0/0.
            assert [row["state"] for row in rows] == ["UNKNOWN", "UNKNOWN"]
            assert [row["actual_tokens"] for row in rows] == [None, None]
            assert [row["prior_output_upper"] for row in rows] == [0, 1000]
            assert [row["total_upper"] for row in rows] == [1100, 3100]
            bridge = AgentBridge(runtime, unpriced=True)
            assert bridge.usage_facts(agent_id=agent.agent_id) == []
            # A hierarchical terminal import keeps both calls on the books as unknown —
            # never as a zero (independent review 2026-09-24, blocker 2).
            unknown = bridge.usage_facts(agent_id=agent.agent_id, include_unknown=True)
            assert len(unknown) == 2 and all(fact.unknown and fact.tokens == 0 for fact in unknown)
            with commit.store.transaction():
                assert commit.ledger.has_unknown_usage(attempt.id)
                assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"
                # Both grants terminated on the wire: marked in the shared store, so no pool
                # — this one or another sharing the store — counts them as holding a slot
                # (independent review 2026-09-24, blocker 1).
                marks = commit.store.connection.execute(
                    "SELECT invocation_id, handoff_ordinal FROM provider_grant_wire_terminal_v1 ORDER BY handoff_ordinal"
                ).fetchall()
                assert {(m[0], m[1]) for m in marks} == {(r["invocation_id"], r["handoff_ordinal"]) for r in rows} and len(marks) == 2
                assert guard.held_grant_rows() == []
                other_pool = ProviderBudgetGuard(commit, owner="other-pool", estimator=Counter(100), max_slots=1)
                assert other_pool.held_grant_rows() == []
                # A legacy (unguarded) pool sharing the store counts the same set.
                from agent_orchestrator.runtime.provider_budget_guard import held_guarded_grants
                assert held_guarded_grants(commit.store) == 0

    asyncio.run(exercise())


class BrokenWireProvider(ActualProvider):
    """The relay drops the connection mid-stream after the request was handed off
    (host-final-arp10, 2026-09-24: httpx RemoteProtocolError after 4m45s)."""

    async def invoke(self, request, *, cancel):
        self.requests.append(request)
        raise RuntimeError("peer closed connection without sending complete message body")


def test_an_unknown_outcome_frees_its_slot_but_keeps_its_charge(tmp_path):
    """Rule 2026-09-24 (may overcount, never undercount, never freeze): a call whose outcome
    is unknowable ended on the wire — it must not hold the pool's only slot forever, and its
    upper bound stays spent.  Before: the UNKNOWN grant counted as holding a slot, so with
    one slot every later call of the Mission waited (host-final-arp10, planner 3 never sent)."""

    async def exercise():
        async with setup_runtime(tmp_path, provider_factory=BrokenWireProvider) as (
            commit, _, task, guard, provider, runtime,
        ):
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "broken")
            try:
                await agent.ask("request", input_id=key, timeout=2)
            except Exception:  # noqa: BLE001 - the turn itself is not what this test pins
                pass
            assert provider.calls == 1
            rows = grants(commit)
            assert [row["state"] for row in rows] == ["UNKNOWN"]
            assert rows[0]["actual_tokens"] is None and rows[0]["total_upper"] > 0
            from agent_orchestrator.runtime.provider_budget_guard import held_guarded_grants

            with commit.store.transaction():
                assert guard.held_grant_rows() == []
                assert held_guarded_grants(commit.store) == 0
                assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"

    asyncio.run(exercise())

