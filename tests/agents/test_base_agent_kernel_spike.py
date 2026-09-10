# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T6.5 价值验证里程碑（内核级 spike）：不死的执行身份。

Real ``AgentExecutionDriver`` on the assembled kernel, no ``BaseAgent``/``AgentRuntime``
handles, no ``agent.delegate``, no API fence.  Three claims:

1. one Run delivers two results and never becomes terminal;
2. each input continuation is acked and the Run is rescheduled by the kernel;
3. a crash after stage and before finalize is recovered by committing once,
   without a second Provider call.
"""

from __future__ import annotations

import asyncio

from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import assemble_runtime
from simple_harness.contracts import RunId
from simple_harness.execution.uow import ContinuationState, RunState

from kernel_fixture import create_agent, submit
from provider_fixture import MODEL, ScriptedProvider, message_texts

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "spike.db"),
        model=MODEL,
        owner_id="spike-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


async def _settle(runtime, run_id: str) -> None:
    await asyncio.sleep(0.05)
    await runtime.wait_idle(RunId(run_id))


def _event_kinds(uow, run_id: str) -> list[str]:
    return [
        str(row[0])
        for row in uow.database.connection.execute(
            "SELECT kind FROM run_events WHERE run_id=? ORDER BY durable_seq", (run_id,)
        )
    ]


def test_two_results_on_one_run_never_terminal(tmp_path):
    async def case():
        provider = ScriptedProvider(["结论 A", "结论 B"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="spike-a")
            first = await submit(runtime, uow, agent_id="spike-a", input_id="i1", text="第一问")
            await _settle(runtime, "spike-a")
            second = await submit(runtime, uow, agent_id="spike-a", input_id="i2", text="第二问")
            await _settle(runtime, "spike-a")
            turns = uow.list_agent_turns("spike-a")
            assert [t.seq for t in turns] == [1, 2]
            assert [t.phase for t in turns] == ["committed", "committed"]
            assert uow.read_agent_turn_result(first.turn_id) is not None
            assert uow.read_agent_turn_result(second.turn_id) is not None
            # Full event replay, not just the final state.
            kinds = _event_kinds(uow, "spike-a")
            assert not (TERMINAL_EVENTS & set(kinds)), kinds
            assert uow.read_run("spike-a").state is RunState.WAITING
            second_request = message_texts(provider.requests[1])
            assert "结论 A" in second_request and "第二问" in second_request
            assert provider.calls == 2

    asyncio.run(case())


def test_input_continuation_is_acked_and_rescheduled(tmp_path):
    async def case():
        provider = ScriptedProvider(["回答一", "回答二"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        reschedules: list[str] = []
        original = runtime._reschedule

        async def spy(run_id: str) -> None:
            reschedules.append(run_id)
            await original(run_id)

        runtime._reschedule = spy  # type: ignore[method-assign]
        async with runtime:
            await create_agent(runtime, uow, agent_id="spike-b")
            first = await submit(runtime, uow, agent_id="spike-b", input_id="i1", text="一")
            # Queue the second input before the first finishes: no external wake afterwards.
            second = await submit(runtime, uow, agent_id="spike-b", input_id="i2", text="二")
            await _settle(runtime, "spike-b")
            for _ in range(20):
                if uow.read_agent_turn_result(second.turn_id) is not None:
                    break
                await _settle(runtime, "spike-b")
            for turn in (first, second):
                continuation = uow.read_continuation(turn.turn_id)
                assert continuation is not None
                assert continuation.state is ContinuationState.ACKED, turn.turn_id
                assert continuation.ack_receipt_id == (
                    f"spike-b:progress:{turn.turn_id}:{continuation.claim_epoch}"
                )
            assert uow.read_agent_turn_result(second.turn_id) is not None
            assert reschedules.count("spike-b") >= 1
            assert provider.calls == 2

    asyncio.run(case())


class _CrashCut(BaseException):
    pass


def test_stage_then_kill_then_recover_commits_once(tmp_path):
    async def case():
        provider = ScriptedProvider(["唯一一次回答"])  # a second call would exhaust the script
        assembled = assemble_runtime(_ports(tmp_path, provider, clock=lambda: 10.0))
        runtime, uow = assembled.runtime, assembled.uow
        crashes = {"count": 0}

        async def crash(*args, **kwargs):
            crashes["count"] += 1
            raise _CrashCut("died between stage and finalize")

        runtime._finalize_agent_turn = crash  # type: ignore[method-assign]
        await runtime.start()
        await create_agent(runtime, uow, agent_id="spike-c")
        record = await submit(runtime, uow, agent_id="spike-c", input_id="i1", text="问")
        await _settle(runtime, "spike-c")
        assert crashes["count"] == 1
        staged = uow.read_agent_turn(record.turn_id)
        assert staged is not None and staged.phase == "result_pending"
        assert uow.read_agent_turn_result(record.turn_id) is None
        assert provider.calls == 1
        try:
            await runtime.close()
        except BaseException:
            pass
        assembled.database.close()

        # Restart after the old lease expired: recovery commits the staged result once.
        restarted = assemble_runtime(
            _ports(tmp_path, provider, owner_id="spike-owner-2", clock=lambda: 100.0)
        )
        async with restarted.runtime:
            await _settle(restarted.runtime, "spike-c")
            result = restarted.uow.read_agent_turn_result(record.turn_id)
            assert result is not None and result.result_hash == staged.staged_result_hash
            rows = restarted.uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turn_results_v1"
            ).fetchone()[0]
            assert rows == 1
            assert provider.calls == 1  # never re-generated
            continuation = restarted.uow.read_continuation(record.turn_id)
            assert continuation is not None and continuation.state is ContinuationState.ACKED
            assert restarted.uow.read_run("spike-c").state is RunState.WAITING
            assert not (TERMINAL_EVENTS & set(_event_kinds(restarted.uow, "spike-c")))

    asyncio.run(case())
