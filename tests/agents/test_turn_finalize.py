# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T5: AgentTurn result carrier, stage/finalize in the kernel, recovery."""

from __future__ import annotations

import asyncio

import pytest
from kernel_fixture import EchoAgentDriver, build, create_agent, submit

from simple_harness import Message, MessageRole
from simple_harness.agents.contracts import AgentTurnResult, AgentTurnState
from simple_harness.contracts import RunId
from simple_harness.execution.uow import ContinuationState, RunState, UnitOfWorkConflict
from simple_harness.runtime import ConversationTurnOutput, DriverResult
from simple_harness.runtime.agent_turn import AgentTurnOutcome

TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


def _outcome(turn_id="a:input:i1", agent_id="a"):
    result = AgentTurnResult(
        turn_id=turn_id, agent_id=agent_id, seq=1, state=AgentTurnState.COMMITTED,
        public_output=Message(MessageRole.ASSISTANT, "ok"),
    )
    return result.to_outcome(input_id="i1", input_hash="0" * 64)


def test_driver_result_rejects_both_carriers():
    outcome = _outcome()
    output = ConversationTurnOutput(Message(MessageRole.ASSISTANT, "x"), "x")
    with pytest.raises(ValueError):
        DriverResult(RunState.COMPLETED, agent_turn_outcome=outcome)
    with pytest.raises(ValueError):
        DriverResult(RunState.WAITING, agent_turn_outcome=outcome, conversation_output=output)
    with pytest.raises(ValueError):
        DriverResult(RunState.COMPLETED, agent_turn_outcome=outcome, conversation_output=output)
    with pytest.raises(TypeError):
        DriverResult(RunState.WAITING, agent_turn_outcome="nope")  # type: ignore[arg-type]
    assert DriverResult(RunState.WAITING, agent_turn_outcome=outcome).agent_turn_outcome is outcome
    assert isinstance(outcome, AgentTurnOutcome)


def test_committed_turn_leaves_run_non_terminal(tmp_path):
    async def case():
        driver = EchoAgentDriver()
        runtime, uow, database = build(tmp_path, driver=driver)
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-a")
            assert driver.calls == 1  # creation drives once, no input -> idle
            record = await submit(runtime, uow, agent_id="agent-a", input_id="i1", text="hi")
            await asyncio.sleep(0.05)
            await runtime.wait_idle(RunId("agent-a"))
            run = uow.read_run("agent-a")
            assert run is not None and run.state is RunState.WAITING
            result = uow.read_agent_turn_result(record.turn_id)
            assert result is not None
            turn = uow.read_agent_turn(record.turn_id)
            assert turn is not None and turn.phase == "committed" and turn.seq == 1
            rows = uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turn_results_v1"
            ).fetchone()[0]
            assert rows == 1
            kinds = [
                str(r[0]) for r in uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id='agent-a'"
                )
            ]
            assert not any(k in {"run.completed", "run.failed", "run.cancelled"} for k in kinds)

    asyncio.run(case())


def test_finalize_acks_the_input_continuation_in_the_same_transaction(tmp_path):
    async def case():
        driver = EchoAgentDriver()
        runtime, uow, database = build(tmp_path, driver=driver)
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-b")
            record = await submit(runtime, uow, agent_id="agent-b", input_id="i1", text="hi")
            await asyncio.sleep(0.05)
            await runtime.wait_idle(RunId("agent-b"))
            continuation = uow.read_continuation(record.turn_id)
            assert continuation is not None and continuation.state is ContinuationState.ACKED
            assert continuation.ack_receipt_id == (
                f"agent-b:progress:{record.turn_id}:{continuation.claim_epoch}"
            )
            # Atomicity: a second turn whose finalize transaction faults after the result
            # row is written must leave neither the result row nor an acked continuation.
            second = await submit(runtime, uow, agent_id="agent-b", input_id="i2", text="again")
            await asyncio.sleep(0.05)
            await runtime.wait_idle(RunId("agent-b"))
            assert uow.read_agent_turn_result(second.turn_id) is not None

        # Direct transaction-level probe of the all-or-nothing property.
        driver2 = EchoAgentDriver()
        runtime2, uow2, _ = build(tmp_path, driver=driver2, name="atomic.db")
        async with runtime2:
            await create_agent(runtime2, uow2, agent_id="agent-c")
            record = await submit(runtime2, uow2, agent_id="agent-c", input_id="i1", text="hi")
            await asyncio.sleep(0.05)
            await runtime2.wait_idle(RunId("agent-c"))
            # Stage a fresh turn by hand, then fault the finalize after the result write.
            third = uow2.submit_agent_input(
                agent_id="agent-c", run_id="agent-c", turn_id="agent-c:input:i9", input_id="i9",
                input_hash="1" * 64, input_json={"message": {}},
                continuation_payload={"kind": "base_agent_input", "turn_id": "agent-c:input:i9"},
                now=5.0,
            )
            lease = runtime2._leases["agent-c"]
            claim = uow2.claim_continuation(run_id="agent-c", execution_lease=lease, now=6.0)
            assert claim is not None and claim.continuation_id == third.turn_id
            outcome = AgentTurnResult(
                turn_id=third.turn_id, agent_id="agent-c", seq=third.seq,
                state=AgentTurnState.COMMITTED,
                public_output=Message(MessageRole.ASSISTANT, "staged"),
            ).to_outcome(input_id="i9", input_hash="1" * 64)
            uow2.stage_agent_turn_result(
                turn_id=third.turn_id, result_hash=outcome.result_hash,
                result_json=outcome.result_object(), provider_turn_ordinal_from=None,
                provider_turn_ordinal_to=None, execution_lease=lease, now=6.0,
            )
            run = uow2.read_run("agent-c")

            def fault(point):
                if point == "agent_turn_finalize.run.before_write":
                    raise RuntimeError("injected crash after result + ack writes")

            with pytest.raises(RuntimeError):
                uow2.commit_agent_turn_result_and_idle(
                    run_id="agent-c", expected_version=run.version, turn_id=third.turn_id,
                    event_id="agent-c:agent_turn:i9:committed", payload={},
                    continuation_claim=claim, execution_lease=lease,
                    receipt_id=f"agent-c:progress:{claim.continuation_id}:{claim.claim_epoch}",
                    now=7.0, fault=fault,
                )
            assert uow2.read_agent_turn_result(third.turn_id) is None
            after = uow2.read_continuation(third.turn_id)
            assert after is not None and after.state is ContinuationState.CLAIMED
            turn = uow2.read_agent_turn(third.turn_id)
            assert turn is not None and turn.phase == "result_pending"
            # Without the fault the same call commits everything at once.
            committed = uow2.commit_agent_turn_result_and_idle(
                run_id="agent-c", expected_version=run.version, turn_id=third.turn_id,
                event_id="agent-c:agent_turn:i9:committed", payload={},
                continuation_claim=claim, execution_lease=lease,
                receipt_id=f"agent-c:progress:{claim.continuation_id}:{claim.claim_epoch}",
                now=8.0,
            )
            assert committed.result_hash == outcome.result_hash
            assert uow2.read_continuation(third.turn_id).state is ContinuationState.ACKED
            assert uow2.read_run("agent-c").state is RunState.WAITING

    asyncio.run(case())


class _CrashCut(BaseException):
    pass


def test_result_pending_then_kill_then_recover_commits_once(tmp_path):
    async def case():
        driver = EchoAgentDriver()
        runtime, uow, database = build(tmp_path, driver=driver)
        original = runtime._finalize_agent_turn
        crashes = {"count": 0}

        async def crash_after_stage(*args, **kwargs):
            crashes["count"] += 1
            raise _CrashCut("process died between stage and finalize")

        runtime._finalize_agent_turn = crash_after_stage  # type: ignore[method-assign]
        await runtime.start()
        await create_agent(runtime, uow, agent_id="agent-d")
        record = await submit(runtime, uow, agent_id="agent-d", input_id="i1", text="hi")
        await asyncio.sleep(0.05)
        await runtime.wait_idle(RunId("agent-d"))  # the crashed drive task is shielded
        assert crashes["count"] == 1
        staged = uow.read_agent_turn(record.turn_id)
        assert staged is not None and staged.phase == "result_pending"
        assert uow.read_agent_turn_result(record.turn_id) is None
        calls_before = driver.calls
        version_before = uow.read_run("agent-d").version
        runtime._finalize_agent_turn = original  # type: ignore[method-assign]
        try:
            await runtime.close()
        except BaseException:
            pass
        database.close()

        # "Restart": a new Runtime over the same file recovers and finalizes without the driver.
        # The old owner's lease is still on disk; a real restart happens after it expired.
        runtime2, uow2, database2 = build(
            tmp_path, driver=driver, owner="owner-2", clock=lambda: 100.0
        )
        async with runtime2:
            await asyncio.sleep(0.05)
            await runtime2.wait_idle(RunId("agent-d"))
            result = uow2.read_agent_turn_result(record.turn_id)
            assert result is not None and result.result_hash == staged.staged_result_hash
            rows = uow2.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turn_results_v1"
            ).fetchone()[0]
            assert rows == 1
            assert driver.calls == calls_before  # the driver was never re-entered
            assert len(driver.inputs) == 1  # the same input was never handed to the driver again
            run = uow2.read_run("agent-d")
            assert run.state is RunState.WAITING and run.version > version_before
            continuation = uow2.read_continuation(record.turn_id)
            assert continuation is not None and continuation.state is ContinuationState.ACKED

    asyncio.run(case())


def test_finalize_is_idempotent_by_receipt_id(tmp_path):
    async def case():
        driver = EchoAgentDriver()
        runtime, uow, database = build(tmp_path, driver=driver)
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-e")
            record = await submit(runtime, uow, agent_id="agent-e", input_id="i1", text="hi")
            await asyncio.sleep(0.05)
            await runtime.wait_idle(RunId("agent-e"))
            first = uow.read_agent_turn_result(record.turn_id)
            assert first is not None
            lease = runtime._leases["agent-e"]
            run = uow.read_run("agent-e")
            replay = uow.commit_agent_turn_result_and_idle(
                run_id="agent-e", expected_version=run.version, turn_id=record.turn_id,
                event_id=f"agent-e:agent_turn:{record.turn_id}:committed", payload={},
                continuation_claim=None, execution_lease=lease, receipt_id=None, now=9.0,
            )
            assert replay == first
            assert uow.read_run("agent-e").version == run.version
            rows = uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turn_results_v1"
            ).fetchone()[0]
            assert rows == 1
            with pytest.raises(UnitOfWorkConflict):
                uow.stage_agent_turn_result(
                    turn_id=record.turn_id, result_hash="f" * 64, result_json={"state": "x"},
                    provider_turn_ordinal_from=None, provider_turn_ordinal_to=None,
                    execution_lease=lease, now=9.0,
                )

    asyncio.run(case())
