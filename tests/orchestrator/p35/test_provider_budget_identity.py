# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Independent review controls for missing usage and released-grant identity."""

import asyncio
from dataclasses import replace

import pytest
from test_provider_budget_guard import create_bound, grants, setup_runtime

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from simple_harness.contracts import Message, MessageRole, RunId
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.providers import ProviderRequestRejectedError, ProviderUsage


def test_real_terminal_failure_without_usage_keeps_allowance_and_slot_unknown(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):

            async def rejected(request, *, cancel):
                provider.requests.append(request)
                raise ProviderRequestRejectedError()

            provider.invoke = rejected
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "missing-usage")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed" and provider.calls == 1
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and str(records[0].state) == "failed"
            assert grants(commit)[0]["state"] == "UNKNOWN"
            bridge = AgentBridge(runtime, unpriced=True)
            assert bridge.usage_facts(agent_id=agent.agent_id) == []
            with commit.store.transaction():
                with pytest.raises(BudgetError, match="unknown"):
                    commit.ledger.settle(subject_id=attempt.id)
                assert commit.ledger.usage_for(attempt.id)[0] == 0
            held = grants(commit)
            guard.recover(runtime.uow)
            assert grants(commit) == held

    asyncio.run(exercise())


def test_priced_ports_require_explicit_admission_capability(tmp_path):
    from test_provider_budget_guard import ActualProvider

    from simple_harness.agents import AgentRuntimePorts
    from simple_harness.agents.ports import AllowAllAuthorization
    from simple_harness.execution.budget import FrozenPriceEstimator
    from simple_harness.runtime.consumer_adapter import ConsumerRuntimePolicies

    class OldTokenOnlyGuard:
        fingerprint = "legacy-token-only"
        acquire = handoff = observe = recover = lambda *a, **kw: None

        def waiting_for_slot(self, **kwargs):
            return False

    with pytest.raises(ValueError, match="priced"):
        AgentRuntimePorts(
            provider=ActualProvider(),
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "never-opened.db"),
            policies=ConsumerRuntimePolicies(
                "consumer_supplied",
                False,
                "fail_closed",
                estimator=FrozenPriceEstimator("fixture-price", "consumer", 1000, 2000),
            ),
            provider_admission=OldTokenOnlyGuard(),
        )
    assert not (tmp_path / "never-opened.db").exists()


def test_cold_overrun_commits_actual_usage_before_refusing_and_blocks_next_call(tmp_path):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):
            actual_invoke = provider.invoke

            async def overrun_response(request, *, cancel):
                response = await actual_invoke(request, cancel=cancel)
                return replace(
                    response,
                    usage=ProviderUsage(
                        input_tokens=1200,
                        output_tokens=50,
                        total_tokens=1250,
                    ),
                )

            provider.invoke = overrun_response
            # Lose only the Host observation after real SDK settlement. The SDK
            # response/usage and handoff are produced normally, not fabricated.
            original_observe = guard.observe
            guard.observe = lambda ticket, *, record: None
            agent, attempt, key = await create_bound(commit, task, guard, runtime, "overrun")
            await agent.ask("request", input_id=key, timeout=5)
            guard.observe = original_observe
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and str(records[0].state) == "succeeded"
            assert records[0].usage_json["usage"]["total_tokens"] == 1250
            assert grants(commit)[0]["state"] == "HANDED_OFF"

            cold = ProviderBudgetGuard(
                commit, owner="test-owner", estimator=guard.estimator, max_slots=1
            )
            with pytest.raises(ProviderAdmissionDenied, match="exceeded"):
                cold.recover(runtime.uow)
            observed = grants(commit)
            assert observed[0]["state"] == "OVERRUN"
            assert observed[0]["actual_tokens"] == 1250
            assert observed[0]["actual_output_tokens"] == 50
            assert observed[0]["total_upper"] == 1100
            cold.recover(runtime.uow)
            assert grants(commit) == observed

            next_agent, _, next_key = await create_bound(
                commit, task, guard, runtime, "after-overrun"
            )
            denied = await next_agent.ask("request", input_id=next_key, timeout=5)
            assert str(denied.state) == "failed" and provider.calls == 1
            next_records = runtime.uow.list_provider_invocations(RunId(next_agent.run_id))
            assert all(record.handoff_attempt == 0 for record in next_records)
            facts = AgentBridge(runtime, unpriced=True).usage_facts(agent_id=agent.agent_id)
            with commit.store.transaction():
                commit.ledger.import_usage(
                    subject_id=attempt.id, mission_id=attempt.mission_id, facts=facts
                )
                assert commit.ledger.settle(subject_id=attempt.id)["settled_tokens"] == 1250

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["none", "wire", "bound"])
def test_released_never_handed_off_grant_requires_identical_request_and_allowance(tmp_path, change):
    async def exercise():
        async with setup_runtime(tmp_path) as (commit, _, task, guard, provider, runtime):
            original = guard.acquire

            async def release_and_reacquire(**kwargs):
                ticket = await original(**kwargs)
                record = runtime.uow.read_provider_invocation(ticket.invocation_id)
                assert str(record.state) == "claimed" and record.handoff_attempt == 0
                guard.observe(ticket, record=record)
                assert grants(commit)[0]["state"] == "RELEASED"
                if change == "wire":
                    kwargs["request"] = replace(
                        kwargs["request"], messages=(Message(MessageRole.USER, "different wire"),)
                    )
                if change == "bound":
                    guard.estimator.tokens += 1
                return await original(**kwargs)

            guard.acquire = release_and_reacquire
            agent, _, key = await create_bound(commit, task, guard, runtime, "re-admit")
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == ("committed" if change == "none" else "failed")
            assert provider.calls == int(change == "none")
            assert len(grants(commit)) == 1
            assert grants(commit)[0]["state"] == ("SETTLED" if change == "none" else "RELEASED")

    asyncio.run(exercise())
