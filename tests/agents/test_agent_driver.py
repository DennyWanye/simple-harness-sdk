# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T6: AgentExecutionDriver on the assembled runtime (no user Memory)."""

from __future__ import annotations

import asyncio

from kernel_fixture import create_agent, submit
from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness.agents.execution import build_agent_execution_driver
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import assemble_runtime
from simple_harness.contracts import RunId
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.uow import ContinuationState, RunState
from simple_harness.runtime.drivers.react import build_react_driver
from simple_harness.runtime.termination import TerminationLimits


def _ports(tmp_path, provider, **overrides):
    return AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "agents.db"),
        model=MODEL,
        owner_id="agent-runtime-1",
        **overrides,
    )


async def _settle(runtime, run_id: str) -> None:
    await asyncio.sleep(0.05)
    await runtime.wait_idle(RunId(run_id))


def test_create_without_input_returns_waiting_without_provider(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-idle")
            run = uow.read_run("agent-idle")
            assert run is not None and run.state is RunState.WAITING
            assert provider.calls == 0
            assert uow.list_agent_turns("agent-idle") == ()
            assert (
                uow.database.connection.execute(
                    "SELECT COUNT(*) FROM base_agent_turn_results_v1"
                ).fetchone()[0]
                == 0
            )

    asyncio.run(case())


def test_unexpected_continuation_kind_keeps_run_waiting_and_acks(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-odd")
            uow.enqueue_continuation(
                continuation_id="odd-1",
                run_id="agent-odd",
                payload={"kind": "child_terminal", "child_run_id": "x"},
                now=3.0,
            )
            asyncio.create_task(runtime._wake_continuation("agent-odd"))
            await _settle(runtime, "agent-odd")
            run = uow.read_run("agent-odd")
            assert run is not None and run.state is RunState.WAITING
            continuation = uow.read_continuation("odd-1")
            assert continuation is not None and continuation.state is ContinuationState.ACKED
            assert provider.calls == 0
            assert uow.list_agent_turns("agent-odd") == ()
            assert (
                uow.database.connection.execute(
                    "SELECT COUNT(*) FROM child_terminal_receipts"
                ).fetchone()[0]
                == 0
            )
            kinds = [
                str(r[0])
                for r in uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id='agent-odd'"
                )
            ]
            assert "run.failed" not in kinds and "run.completed" not in kinds

    asyncio.run(case())


def test_two_turns_same_agent_reuse_one_execution_identity(tmp_path):
    async def case():
        provider = ScriptedProvider(["第一轮回答", "第二轮回答"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-two")
            first = await submit(runtime, uow, agent_id="agent-two", input_id="i1", text="问题一")
            await _settle(runtime, "agent-two")
            second = await submit(runtime, uow, agent_id="agent-two", input_id="i2", text="问题二")
            await _settle(runtime, "agent-two")
            results = [uow.read_agent_turn_result(t.turn_id) for t in (first, second)]
            assert all(r is not None for r in results)
            assert [t.seq for t in uow.list_agent_turns("agent-two")] == [1, 2]
            assert provider.calls == 2
            second_texts = message_texts(provider.requests[1])
            assert "第一轮回答" in second_texts and "问题二" in second_texts
            kinds = [
                str(r[0])
                for r in uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id='agent-two'"
                )
            ]
            assert not ({"run.completed", "run.failed", "run.cancelled"} & set(kinds))
            run = uow.read_run("agent-two")
            assert run is not None and run.state is RunState.WAITING
            from simple_harness.agents.contracts import AgentTurnResult

            decoded = AgentTurnResult.from_json(results[1].result_json)
            assert decoded.public_output is not None
            assert decoded.public_output.content == "第二轮回答"
            assert decoded.state.value == "committed" and decoded.delegation_count == 0

    asyncio.run(case())


def test_budget_exceeded_is_a_failed_turn_not_a_dead_agent(tmp_path):
    async def case():
        provider = ScriptedProvider(["只够一轮"])
        limits = TerminationLimits(max_turns=1, max_tool_calls=1)
        assembled = assemble_runtime(_ports(tmp_path, provider, termination_limits=limits))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-budget")
            first = await submit(runtime, uow, agent_id="agent-budget", input_id="i1", text="一")
            await _settle(runtime, "agent-budget")
            assert uow.read_agent_turn_result(first.turn_id) is not None
            second = await submit(runtime, uow, agent_id="agent-budget", input_id="i2", text="二")
            await _settle(runtime, "agent-budget")
            failed = uow.read_agent_turn_result(second.turn_id)
            assert failed is not None
            assert dict(failed.result_json)["state"] == "failed"
            turn = uow.read_agent_turn(second.turn_id)
            assert turn is not None and turn.phase == "failed"
            run = uow.read_run("agent-budget")
            assert run is not None and run.state is RunState.WAITING
            assert provider.calls == 1

    asyncio.run(case())


def test_agent_policy_fingerprint_differs_from_react():
    limits = TerminationLimits()
    budget = BudgetPolicy()
    estimator = FrozenPriceEstimator("v", "p", 0, 0)
    agent = build_agent_execution_driver(limits=limits, budget_policy=budget, estimator=estimator)
    react = build_react_driver(limits=limits, budget_policy=budget, estimator=estimator)
    assert agent.policy_fingerprint != react.policy_fingerprint
    assert agent.provider_budget_fingerprint == react.provider_budget_fingerprint


def test_provider_rejection_is_a_failed_turn_and_next_turn_recovers(tmp_path):
    from simple_harness.providers.errors import ProviderRequestRejectedError

    async def case():
        provider = ScriptedProvider(["第二轮正常"])
        original = provider.invoke
        state = {"rejected": False}

        async def reject_once(request, *, cancel):
            if not state["rejected"]:
                state["rejected"] = True
                raise ProviderRequestRejectedError()
            return await original(request, cancel=cancel)

        provider.invoke = reject_once  # type: ignore[method-assign]
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-reject")
            first = await submit(runtime, uow, agent_id="agent-reject", input_id="i1", text="一")
            await _settle(runtime, "agent-reject")
            failed = uow.read_agent_turn_result(first.turn_id)
            assert failed is not None and dict(failed.result_json)["state"] == "failed"
            assert uow.read_run("agent-reject").state is RunState.WAITING
            second = await submit(runtime, uow, agent_id="agent-reject", input_id="i2", text="二")
            await _settle(runtime, "agent-reject")
            ok = uow.read_agent_turn_result(second.turn_id)
            assert ok is not None and dict(ok.result_json)["state"] == "committed"
            assert uow.read_run("agent-reject").state is RunState.WAITING

    asyncio.run(case())


def test_unknown_tool_name_is_a_visible_rejection_not_a_dead_agent(tmp_path):
    async def case():
        provider = ScriptedProvider([("no_such_tool", {}), "改用自己回答"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-halluc")
            turn = await submit(runtime, uow, agent_id="agent-halluc", input_id="i1", text="一")
            await _settle(runtime, "agent-halluc")
            result = uow.read_agent_turn_result(turn.turn_id)
            assert result is not None and dict(result.result_json)["state"] == "committed"
            assert "tool_not_exposed" in "\n".join(message_texts(provider.requests[1]))
            assert uow.read_run("agent-halluc").state is RunState.WAITING

    asyncio.run(case())


def test_unknown_provider_outcome_suspends_the_turn_and_resumes_after_reconcile(tmp_path):
    """Review F2: UNKNOWN outbound work inside a BaseAgent turn must not deadlock."""
    from simple_harness.providers.errors import ProviderTransportError
    from simple_harness.providers.reconciliation import (
        ProviderReconciliationObservation,
        ProviderReconciliationState,
    )
    from simple_harness.runtime.consumer_adapter import (
        ConsumerRuntimePolicies,
        _DefaultRuntimeReconciliation,
        _DefaultToolReconciliation,
    )

    class NotStarted:
        def __init__(self):
            self.observed = 0

        async def observe(self, invocation):
            self.observed += 1
            return ProviderReconciliationObservation(
                ProviderReconciliationState.CONFIRMED_NOT_STARTED,
                f"test-evidence:{invocation.invocation_id}",
            )

    async def case():
        provider = ScriptedProvider(["恢复后的回答"])
        original = provider.invoke
        state = {"failed": False}

        async def transport_failure_once(request, *, cancel):
            if not state["failed"]:
                state["failed"] = True
                raise ProviderTransportError()
            return await original(request, cancel=cancel)

        provider.invoke = transport_failure_once  # type: ignore[method-assign]
        reconciliation = NotStarted()
        policies = ConsumerRuntimePolicies(
            "unpriced_local",
            False,
            "consumer_reconciles",
            tool_reconciliation=_DefaultToolReconciliation(),
            provider_reconciliation=reconciliation,
            runtime_reconciliation=_DefaultRuntimeReconciliation(),
        )
        assembled = assemble_runtime(_ports(tmp_path, provider, policies=policies))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-unknown")
            turn = await submit(runtime, uow, agent_id="agent-unknown", input_id="i1", text="问")
            await _settle(runtime, "agent-unknown")
            run = uow.read_run("agent-unknown")
            assert run is not None and run.state is RunState.WAITING
            assert uow.read_agent_turn(turn.turn_id).phase in {"queued", "running"}
            continuation = uow.read_continuation(turn.turn_id)
            assert continuation is not None and continuation.state is ContinuationState.CLAIMED
            blockers = uow.database.connection.execute(
                "SELECT COUNT(*) FROM run_wait_blockers WHERE run_id='agent-unknown'"
            ).fetchone()[0]
            assert blockers == 1
            # The Host-side reconciliation confirms the request never started; the kernel
            # resumes the same turn under a fresh lease and the input is not re-queued.
            await runtime.reconcile()
            for _ in range(50):
                if uow.read_agent_turn_result(turn.turn_id) is not None:
                    break
                await asyncio.sleep(0.05)
            result = uow.read_agent_turn_result(turn.turn_id)
            assert result is not None and dict(result.result_json)["state"] == "committed"
            assert uow.read_continuation(turn.turn_id).state is ContinuationState.ACKED
            # The failed transport attempt never reached the scripted provider's ledger.
            assert state["failed"] and provider.calls == 1 and reconciliation.observed >= 1
            assert uow.list_agent_turns("agent-unknown")[0].seq == 1
            assert len(uow.list_agent_turns("agent-unknown")) == 1

    asyncio.run(case())


def test_finalize_exception_keeps_agent_alive_and_recovers(tmp_path):
    """Review F3: a non-conflict failure after stage must not terminalize the Run."""

    async def case():
        provider = ScriptedProvider(["一次回答"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        original = runtime._finalize_agent_turn
        blows = {"count": 0}

        async def blow_up_once(*args, **kwargs):
            if blows["count"] == 0:
                blows["count"] += 1
                raise RuntimeError("injected finalize failure")
            return await original(*args, **kwargs)

        runtime._finalize_agent_turn = blow_up_once  # type: ignore[method-assign]
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-blow")
            turn = await submit(runtime, uow, agent_id="agent-blow", input_id="i1", text="问")
            await _settle(runtime, "agent-blow")
            assert blows["count"] == 1
            run = uow.read_run("agent-blow")
            assert run is not None and run.state is RunState.WAITING  # never FAILED
            assert uow.read_agent_turn(turn.turn_id).phase == "result_pending"
            # Waking the Run finalizes first (no second model call) and acks the input.
            await runtime._wake_continuation("agent-blow")
            await _settle(runtime, "agent-blow")
            result = uow.read_agent_turn_result(turn.turn_id)
            assert result is not None and provider.calls == 1
            assert uow.read_continuation(turn.turn_id).state is ContinuationState.ACKED
            assert uow.read_run("agent-blow").state is RunState.WAITING

    asyncio.run(case())


def test_first_turn_rerun_does_not_duplicate_the_user_message(tmp_path):
    """Review F4: re-driving the first turn appends the user message exactly once."""
    from simple_harness.contracts import RunId as _RunId

    async def case():
        provider = ScriptedProvider(["回答一", "回答二"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        original = runtime._finalize_agent_turn
        blows = {"count": 0}

        async def blow_up_once(*args, **kwargs):
            if blows["count"] == 0:
                blows["count"] += 1
                raise RuntimeError("crash before finalize")
            return await original(*args, **kwargs)

        runtime._finalize_agent_turn = blow_up_once  # type: ignore[method-assign]
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-dup")
            turn = await submit(runtime, uow, agent_id="agent-dup", input_id="i1", text="你好")
            await _settle(runtime, "agent-dup")
            await runtime._wake_continuation("agent-dup")
            await _settle(runtime, "agent-dup")
            assert uow.read_agent_turn_result(turn.turn_id) is not None
            second = await submit(runtime, uow, agent_id="agent-dup", input_id="i2", text="再问")
            await _settle(runtime, "agent-dup")
            assert uow.read_agent_turn_result(second.turn_id) is not None
            stored = runtime._ports.context.load(_RunId("agent-dup")).messages
            user_texts = [m.content for m in stored if m.role.value == "user"]
            assert user_texts == ["你好", "再问"]
            # kernel_fixture agents carry no instructions: the first entry is the user turn.
            assert [m.role.value for m in stored][:2] == ["user", "assistant"]

    asyncio.run(case())
