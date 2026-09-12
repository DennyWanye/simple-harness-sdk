"""A real lost lease must not become a permanent cancellation of its AgentTurn.

Oracle: expire the real SQLite lease before the first physical Provider call,
let the production heartbeat observe renewal failure and cancel its driver,
then recover the SAME runtime/Agent/input. The original turn must finish with
exactly one handoff. A durable CANCEL_REQUESTED at that same boundary instead
stays cancelled, with no handoff. No edits to kernel cancel tokens or fake PASS.
"""

import asyncio

import pytest
from provider_fixture import MODEL, ScriptedProvider

from simple_harness import RunId
from simple_harness.agents import AgentConfig, AgentTurnState, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_same_runtime_lease_loss_recovers_original_turn_without_duplicate_handoff(
    tmp_path, monkeypatch, caplog, cancel_requested,
):
    async def exercise():
        clock = {"now": 10.0}
        provider = ScriptedProvider(["original answer"])
        ports = AgentRuntimePorts(
            provider=provider, authorization=AllowAllAuthorization(), model=MODEL,
            database_path=str(tmp_path / "execution.db"), owner_id="same-owner",
            lease_ttl_seconds=.3, clock=lambda: clock["now"],
        )
        async with build_agent_runtime(ports) as runtime:
            context = runtime.kernel._ports.context
            original_prepare = context.prepare
            preparing, driver_cancelled, renewal_lost = (
                asyncio.Event(), asyncio.Event(), asyncio.Event(),
            )
            first = True
            lost = {}

            async def pause_before_provider(run_id):
                nonlocal first
                if first:
                    first = False
                    preparing.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        driver_cancelled.set()
                        raise
                await original_prepare(run_id)

            uow_type = type(runtime.uow)
            original_renew = uow_type.renew_runtime_lease

            def observe_real_renewal(uow, lease, **kwargs):
                try:
                    return original_renew(uow, lease, **kwargs)
                except UnitOfWorkConflict:
                    # Observe the actual UoW refusal; do not manufacture it or
                    # directly cancel a kernel/driver task from this fixture.
                    if uow is runtime.uow:
                        lost.update(epoch=lease.epoch, task=asyncio.current_task())
                        renewal_lost.set()
                    raise

            monkeypatch.setattr(context, "prepare", pause_before_provider)
            monkeypatch.setattr(uow_type, "renew_runtime_lease", observe_real_renewal)
            agent = await runtime.create(
                AgentConfig(name="worker", instructions="Answer the question.",
                            model_profile_ref="default"), creation_key="original-agent",
            )
            receipt = await agent.submit("original input", input_id="original-input")
            await asyncio.wait_for(preparing.wait(), 3)
            original_turn = runtime.uow.read_agent_turn(receipt.turn_id)
            assert original_turn.phase == "running"
            assert provider.calls == 0
            assert runtime.uow.list_provider_invocations(RunId(agent.run_id)) == ()
            # Advance only the clock supplied through AgentRuntimePorts, beyond
            # the original SQLite lease. The ordinary heartbeat must reject it.
            clock["now"] += 2.0
            await asyncio.wait_for(renewal_lost.wait(), 3)
            await asyncio.wait_for(driver_cancelled.wait(), 3)
            await asyncio.wait_for(asyncio.shield(lost["task"]), 3)
            assert runtime.uow.read_run(agent.run_id).state is RunState.RUNNING
            assert runtime.uow.read_agent_turn_result(receipt.turn_id) is None
            assert provider.calls == 0

            if cancel_requested:
                # Real cancellation CAS at the crash boundary, before a recovery
                # driver can terminalize it. This is not an in-memory token edit.
                run = runtime.uow.read_run(agent.run_id)
                runtime.uow.request_run_cancel(
                    run_id=run.run_id, expected_version=run.version,
                    event_id="user-cancel-original", now=clock["now"],
                )
                assert runtime.uow.read_run(agent.run_id).state is RunState.CANCEL_REQUESTED

            await asyncio.wait_for(runtime.recover_pending_turns(), 3)
            if cancel_requested:
                assert runtime.uow.read_run(agent.run_id).state is RunState.CANCELLED
                assert provider.calls == 0
                assert runtime.uow.list_provider_invocations(RunId(agent.run_id)) == ()
            else:
                # Agent.wait_turn's timeout uses the intentionally frozen ports
                # clock. The runner bound must instead use the real event loop.
                result = await asyncio.wait_for(
                    agent.wait_turn(receipt.turn_id, timeout=2), timeout=3,
                )
                assert result.state is AgentTurnState.COMMITTED
                assert result.public_output.content == "original answer"
                assert result.turn_id == receipt.turn_id
                current = runtime.uow.read_agent_turn(receipt.turn_id)
                assert current.input_id == original_turn.input_id
                assert current.input_hash == original_turn.input_hash
                assert current.lease_epoch > lost["epoch"]
                invocation, = runtime.uow.list_provider_invocations(RunId(agent.run_id))
                assert invocation.handoff_attempt == 1 and str(invocation.state) == "succeeded"
                assert provider.calls == 1
            assert len(runtime.uow.list_agent_turns(agent.agent_id)) == 1
            calls = provider.calls
            await asyncio.wait_for(runtime.recover_pending_turns(), 3)
            assert provider.calls == calls
            assert not any(record.message == "sdk_run_driver_failed" for record in caplog.records)

    async def bounded_exercise():
        # Include runtime startup and async context-manager teardown in the
        # wall-clock bound; a red oracle must not wait on a virtual deadline.
        await asyncio.wait_for(exercise(), timeout=15)

    asyncio.run(bounded_exercise())
