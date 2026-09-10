# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 5 (BA31–BA36, BA39): crash points, wake races, stale leases, request identity
across restarts, runtime-wide concurrency caps, history query cost, lost tool responses."""

from __future__ import annotations

import asyncio

from provider_fixture import MODEL, ScriptedProvider
from tool_fixture import ECHO_SCHEMA

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState, build_agent_runtime
from simple_harness.agents.execution import AgentExecutionDriver
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.execution.uow import ContinuationState, RunState
from simple_harness.runtime.termination import TerminationLimits

TOOL = ("echo", {})


def _ports(tmp_path, provider, *, executor=None, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="s5-owner",
        termination_limits=TerminationLimits(
            max_turns=10_000,
            max_tool_calls=20_000,
            max_wall_seconds=365.0 * 86_400.0,
            max_cost_micros=10_000_000_000,
            max_consecutive_same_tool=100,
        ),
    )
    if executor is not None:
        base.update(
            tool_executor=executor, tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA}
        )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(tools=(), **limits):
    return AgentConfig(
        name="w",
        instructions="你是助手。",
        model_profile_ref="p",
        tool_names=tools,
        limits=AgentLimits(**limits),
    )


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


def test_crash_after_final_cas_never_regenerates_the_answer(tmp_path):
    """BA31: the result is staged inside the loop's final checkpoint CAS, so dying
    between that CAS and the driver's return costs no second model call."""

    async def case():
        provider = ScriptedProvider(["只答一次"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        original = AgentExecutionDriver._run_turn
        crashed = {"n": 0}

        async def crash_after_loop(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if crashed["n"] == 0:
                crashed["n"] += 1
                raise RuntimeError("process died after the final CAS")
            return result

        AgentExecutionDriver._run_turn = crash_after_loop  # type: ignore[method-assign]
        try:
            async with runtime:
                agent = await runtime.create(_config(), creation_key="cas")
                receipt = await agent.submit("问", input_id="i1")
                for _ in range(100):
                    if crashed["n"]:
                        break
                    await asyncio.sleep(0.02)
                assert crashed["n"] == 1
                # The result was staged by the final CAS before the crash; the Run is not
                # dead, and whether the same process already finalized it or a restart
                # will, the answer is never regenerated.
                turn = runtime.uow.read_agent_turn(receipt.turn_id)
                assert turn is not None and turn.phase in ("result_pending", "committed")
                assert provider.calls == 1
                run = runtime.uow.read_run(agent.run_id)
                assert run is not None and run.state is not RunState.FAILED
                staged = _rows(
                    runtime.uow,
                    "SELECT staged_result_hash FROM base_agent_turns_v1 WHERE turn_id=?",
                    receipt.turn_id,
                )[0][0]
                assert staged is not None
        finally:
            AgentExecutionDriver._run_turn = original  # type: ignore[method-assign]
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="s5-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            await restarted.recover_pending_turns()
            reopened = await restarted.open(agent.agent_id)
            result = await reopened.wait_turn(receipt.turn_id, timeout=5)
            assert result.public_output.content == "只答一次"
            assert provider.calls == 1
            assert restarted.uow.read_continuation(receipt.turn_id).state is ContinuationState.ACKED

    asyncio.run(case())


def test_input_arriving_while_the_idle_drive_releases_is_not_lost(tmp_path):
    """BA32: an input enqueued exactly as the Run gives up authority still wakes."""

    async def case():
        provider = ScriptedProvider(["一", "二"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            kernel = runtime.kernel
            agent = await runtime.create(_config(), creation_key="race")
            original = kernel._abandon_run_authority
            fired = {"n": 0}

            async def submit_inside_release(run_id):
                if (
                    run_id == agent.run_id
                    and fired["n"] == 0
                    and agent.get_result(agent.turn_id_for("i0")) is not None
                ):
                    fired["n"] += 1
                    # The input lands while this very drive is releasing authority
                    # after the first turn (the idle-release window of BA32).
                    await agent.submit("后", input_id="i1")
                await original(run_id)

            kernel._abandon_run_authority = submit_inside_release  # type: ignore[method-assign]
            await agent.ask("先", input_id="i0", timeout=5)
            for _ in range(200):
                if fired["n"]:
                    break
                await asyncio.sleep(0.01)
            assert fired["n"] == 1
            result = await agent.wait_turn(agent.turn_id_for("i1"), timeout=5)
            assert result.public_output.content == "二"

    asyncio.run(case())


def test_stale_lease_holder_cannot_overwrite_the_new_owner(tmp_path):
    """BA33: an executor whose lease lapsed can neither hand off nor commit."""

    async def case():
        clock = {"now": 10.0}
        slow = ScriptedProvider(["迟到的答案"], blocked=True)
        first = build_agent_runtime(
            _ports(tmp_path, slow, lease_ttl_seconds=1.0, clock=lambda: clock["now"])
        )
        async with first:
            agent = await first.create(_config(), creation_key="stale")
            receipt = await agent.submit("问", input_id="i1")
            await asyncio.sleep(0.05)  # first executor is inside the blocked provider call
            # Its lease lapses (heartbeat stopped; clock jumps past the TTL).
            heartbeat = first.kernel._heartbeats.get(agent.run_id)
            if heartbeat is not None:
                heartbeat.cancel()
            clock["now"] = 50.0
            fresh = ScriptedProvider(["新主人的答案"])
            # The new owner never re-issues the lapsed executor's uncertain call on its
            # own (BA34): the Host's reconciliation confirms it never started first.
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
                async def observe(self, invocation):
                    return ProviderReconciliationObservation(
                        ProviderReconciliationState.CONFIRMED_NOT_STARTED,
                        f"host:{invocation.invocation_id}",
                    )

            policies = ConsumerRuntimePolicies(
                "unpriced_local",
                False,
                "consumer_reconciles",
                tool_reconciliation=_DefaultToolReconciliation(),
                provider_reconciliation=NotStarted(),
                runtime_reconciliation=_DefaultRuntimeReconciliation(),
            )
            second = build_agent_runtime(
                _ports(
                    tmp_path,
                    fresh,
                    owner_id="s5-owner-2",
                    lease_ttl_seconds=30.0,
                    clock=lambda: clock["now"],
                    policies=policies,
                )
            )
            async with second:
                await second.recover_pending_turns()
                reopened = await second.open(agent.agent_id)
                result = None
                for _ in range(200):  # bounded real-time poll: the clocks are fake
                    result = reopened.get_result(receipt.turn_id)
                    if result is not None:
                        break
                    await asyncio.sleep(0.02)
                assert result is not None, (
                    second.uow.read_agent_turn(receipt.turn_id).phase,
                    second.uow.read_continuation(receipt.turn_id).state,
                    second.uow.read_run(agent.run_id).state,
                    list(second.kernel._leases),
                )
                assert result.public_output.content == "新主人的答案"
                # Now the stale executor's provider call returns; its commit must lose.
                slow.allow.set()
                await asyncio.sleep(0.3)
                final = reopened.get_result(receipt.turn_id)
                assert final is not None and final.public_output.content == "新主人的答案"
                results = _rows(
                    second.uow,
                    "SELECT COUNT(*) FROM base_agent_turn_results_v1 WHERE turn_id=?",
                    receipt.turn_id,
                )[0][0]
                assert results == 1
                run = second.uow.read_run(agent.run_id)
                assert run is not None and run.state is RunState.WAITING

    asyncio.run(case())


def test_request_ids_stay_unique_across_restarts_and_turns(tmp_path):
    """BA34: provider request identities never collide after a restart."""

    async def case():
        provider = ScriptedProvider(["一", "二", "三"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agent = await runtime.create(_config(), creation_key="ids")
            await agent.ask("1", input_id="i1", timeout=5)
            await agent.ask("2", input_id="i2", timeout=5)
        clock["now"] = 100.0
        again = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="s5-owner-2", clock=lambda: clock["now"])
        )
        async with again:
            reopened = await again.open(agent.agent_id)
            await reopened.ask("3", input_id="i3", timeout=5)
            ids = [r.request_id.value for r in provider.requests]
            assert ids == [f"{agent.run_id}:provider-turn:{n}" for n in (1, 2, 3)]
            invocations = _rows(
                again.uow, "SELECT COUNT(*) FROM provider_invocations WHERE run_id=?", agent.run_id
            )[0][0]
            assert invocations == 3

    asyncio.run(case())


def test_concurrency_caps_are_enforced_fairly_across_agents(tmp_path):
    """BA35: at most N model calls and M tool calls in flight, all Agents finish."""

    async def case():
        provider = ScriptedProvider([])
        gate = asyncio.Event()
        original = provider.invoke
        peak = {"model": 0, "now": 0}

        async def slow_invoke(request, *, cancel):
            peak["now"] += 1
            peak["model"] = max(peak["model"], peak["now"])
            try:
                await gate.wait()
                provider.script.append("答")
                return await original(request, cancel=cancel)
            finally:
                peak["now"] -= 1

        provider.invoke = slow_invoke  # type: ignore[method-assign]
        async with build_agent_runtime(
            _ports(tmp_path, provider, max_concurrent_model_calls=2)
        ) as runtime:
            agents = await runtime.create_many([_config()] * 6, batch_key="fair")
            tasks = [
                asyncio.create_task(agent.ask(f"问{i}", input_id="i1", timeout=10))
                for i, agent in enumerate(agents)
            ]
            await asyncio.sleep(0.3)
            assert peak["model"] <= 2 and runtime._assembled.wire.max_in_flight <= 2
            gate.set()
            results = await asyncio.gather(*tasks)
            assert all(r.state is AgentTurnState.COMMITTED for r in results)
            assert runtime._assembled.wire.max_in_flight == 2
            assert provider.calls == 6

    asyncio.run(case())


def test_history_queries_are_bounded_tool_calls(tmp_path):
    """BA36: session_history tools count against the per-turn tool limit and leave a
    durable effect row, so their cost is visible and capped."""

    async def case():
        search = ("session_history_search", {"query": "连接池"})
        provider = ScriptedProvider(["记下", search, search, search, "不该到这"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(
                _config(tools=("session_history_search",), max_tool_calls_per_turn=2),
                creation_key="cost",
            )
            await agent.ask("连接池上限 20", input_id="i1", timeout=5)
            failed = await agent.ask("反复搜", input_id="i2", timeout=10)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "react_max_tool_calls_exceeded"
            effects = _rows(
                runtime.uow,
                "SELECT tool_name, state FROM execution_effects WHERE run_id=? ORDER BY turn_ordinal",
                agent.run_id,
            )
            assert [row[0] for row in effects] == ["session_history_search"] * 2
            assert all(row[1] == "succeeded" for row in effects)
            assert runtime._session_tools.searches == 2

    asyncio.run(case())


def test_lost_tool_response_is_never_blindly_retried(tmp_path):
    """BA39: a tool whose response is lost after the side effect stays UNKNOWN until the
    Host reconciles; re-driving never executes it twice."""

    from simple_harness.contracts import EffectId
    from simple_harness.execution.recovery import ResolutionOutcome
    from simple_harness.tools import ToolResult

    class SideEffectThenLost:
        def __init__(self):
            self.executions = 0

        async def execute(self, call, context):  # type: ignore[no-untyped-def]
            self.executions += 1
            raise asyncio.CancelledError()  # transport gone after the effect happened

    async def case():
        provider = ScriptedProvider([TOOL, "看到结果了"])
        tool = SideEffectThenLost()
        async with build_agent_runtime(_ports(tmp_path, provider, executor=tool)) as runtime:
            registry = runtime.kernel._ports.tools._registry
            real_invoke = registry.invoke

            async def invoke(call, context, **kwargs):
                if call.name == "echo":
                    tool.executions += 1
                    raise asyncio.CancelledError()
                return await real_invoke(call, context, **kwargs)

            registry.invoke = invoke  # type: ignore[method-assign]
            agent = await runtime.create(_config(tools=("echo",)), creation_key="lost")
            receipt = await agent.submit("用工具", input_id="i1")
            for _ in range(100):
                if runtime.uow.list_open_wait_blockers_for_run(agent.run_id):
                    break
                await asyncio.sleep(0.02)
            assert tool.executions == 1
            # Re-driving the Run does not re-execute the tool.
            await runtime.kernel.reconcile()
            await asyncio.sleep(0.2)
            assert tool.executions == 1
            effects = _rows(
                runtime.uow,
                "SELECT effect_id, state FROM execution_effects WHERE run_id=?",
                agent.run_id,
            )
            assert [row[1] for row in effects] == ["unknown"]
            record = runtime.uow.read_effect(EffectId(str(effects[0][0])))
            runtime.uow.record_tool_reconciliation(
                record,
                outcome=ResolutionOutcome.COMPLETED,
                result=ToolResult.succeeded(record.call_id, {"echo": "recovered"}),
                evidence_ref="host-evidence:recovered",
                now=runtime.ports.clock(),
            )
            registry.invoke = real_invoke  # type: ignore[method-assign]
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "看到结果了"
            assert tool.executions == 1

    asyncio.run(case())
