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


@asynccontextmanager
async def setup_runtime(
    tmp_path, *, tokens=20_000, estimate=100, slots=1, blocked=False, empty_retry=False
):
    commit, mission, tasks = graph_service(tmp_path, nodes=[node("A", tokens=tokens)])
    counter = Counter(estimate)
    guard = ProviderBudgetGuard(commit, owner="test-owner", estimator=counter, max_slots=slots)
    provider = ActualProvider(blocked=blocked)
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


async def create_bound(commit, task, guard, runtime, key):
    agent = await runtime.create(
        AgentConfig(name=key, instructions="Answer briefly.", model_profile_ref="agent.general"),
        creation_key=key,
    )
    attempt, intent = commit.create_attempt(
        task.id,
        role="worker",
        model="agent-model",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
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
