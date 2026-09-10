# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T2 (AC2 / BA08): one Agent, many inputs — strict order and a bounded queue."""

from __future__ import annotations

import asyncio

import pytest
from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness.agents import (
    AgentConfig,
    AgentLimits,
    AgentPendingInputsExhausted,
    AgentTurnNotFound,
    AgentTurnState,
    build_agent_runtime,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime.context import SqliteContextPort


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="queue-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(**limits):
    return AgentConfig(
        name="worker",
        instructions="你是助手。",
        model_profile_ref="profile-1",
        limits=AgentLimits(**limits),
    )


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


class _Spy:
    """Records non-overlapping intervals of a sync or async callable."""

    def __init__(self) -> None:
        self.active = 0
        self.overlaps = 0
        self.calls: list[tuple] = []

    def enter(self, *key):
        self.active += 1
        if self.active > 1:
            self.overlaps += 1
        self.calls.append(key)

    def leave(self):
        self.active -= 1


def test_two_concurrent_inputs_are_processed_in_order(tmp_path):
    async def case():
        provider = ScriptedProvider(["第一答", "第二答"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="o-1")
            r1, r2 = await asyncio.gather(
                agent.submit("问一", input_id="i1"), agent.submit("问二", input_id="i2")
            )
            assert (r1.seq, r2.seq) == (1, 2)
            out1 = await agent.wait_turn(r1.turn_id, timeout=5)
            out2 = await agent.wait_turn(r2.turn_id, timeout=5)
            assert out1.public_output.content == "第一答"
            assert out2.public_output.content == "第二答"
            assert provider.calls == 2
            second = message_texts(provider.requests[1])
            assert "问一" in second and "第一答" in second and "问二" in second
            first = message_texts(provider.requests[0])
            assert "问二" not in first
            rows = _rows(
                runtime.uow,
                "SELECT c.continuation_id, c.fifo_seq, c.state, t.seq FROM continuations c "
                "JOIN base_agent_turns_v1 t ON t.turn_id = c.continuation_id "
                "WHERE c.run_id=? ORDER BY c.fifo_seq",
                agent.run_id,
            )
            assert [row[2] for row in rows] == ["acked", "acked"]
            assert [row[3] for row in rows] == [1, 2]
            assert [row[1] for row in rows] == sorted(row[1] for row in rows)

    asyncio.run(case())


def test_context_is_never_written_concurrently(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二", "答三"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            context = runtime.kernel._ports.context
            spy = _Spy()
            original = context.append
            revisions: list[int] = []
            append_ids: list[str] = []
            conflicts = 0

            def spying_append(run_id, lease, expected_revision, append_id, entries):
                nonlocal conflicts
                spy.enter(run_id, append_id)
                try:
                    revisions.append(expected_revision)
                    append_ids.append(append_id)
                    return original(run_id, lease, expected_revision, append_id, entries)
                except UnitOfWorkConflict:
                    conflicts += 1
                    raise
                finally:
                    spy.leave()

            context.append = spying_append  # type: ignore[method-assign]
            agent = await runtime.create(_config(), creation_key="c-1")
            receipts = await asyncio.gather(
                *(agent.submit(f"问{n}", input_id=f"i{n}") for n in (1, 2, 3))
            )
            for receipt in receipts:
                await agent.wait_turn(receipt.turn_id, timeout=5)
            run_revisions = list(revisions)
            assert spy.overlaps == 0
            assert conflicts == 0
            assert len(append_ids) == len(set(append_ids))
            assert run_revisions == sorted(run_revisions)
            assert len(set(run_revisions)) == len(run_revisions)
            snapshot = SqliteContextPort(runtime.uow.database).load(RunId(agent.run_id))
            assert snapshot.revision > run_revisions[-1]

    asyncio.run(case())


def test_only_one_drive_task_per_run(tmp_path):
    async def case():
        provider = ScriptedProvider(["a", "b", "c", "d"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            kernel = runtime.kernel
            spy = _Spy()
            original = kernel._drive

            async def spying_drive(run_id: str) -> None:
                spy.enter(run_id)
                try:
                    await original(run_id)
                finally:
                    spy.leave()

            kernel._drive = spying_drive  # type: ignore[method-assign]
            agent = await runtime.create(_config(), creation_key="d-1")
            receipts = await asyncio.gather(
                *(agent.submit(f"问{n}", input_id=f"i{n}") for n in (1, 2, 3, 4))
            )
            for receipt in receipts:
                await agent.wait_turn(receipt.turn_id, timeout=5)
            assert spy.overlaps == 0
            assert provider.calls == 4
            assert [r.seq for r in receipts] == [1, 2, 3, 4]

    asyncio.run(case())


def test_pending_inputs_quota_rejects_the_third_input(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二", "答三"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(max_pending_inputs=2), creation_key="q-1")
            r1 = await agent.submit("问一", input_id="i1")
            r2 = await agent.submit("问二", input_id="i2")
            with pytest.raises(AgentPendingInputsExhausted) as info:
                await agent.submit("问三", input_id="i3")
            assert info.value.code == "agent_pending_inputs_exhausted"
            turns = _rows(
                runtime.uow,
                "SELECT turn_id FROM base_agent_turns_v1 WHERE agent_id=?",
                agent.agent_id,
            )
            conts = _rows(
                runtime.uow,
                "SELECT continuation_id FROM continuations WHERE run_id=?",
                agent.run_id,
            )
            assert len(turns) == 2 and len(conts) == 2
            with pytest.raises(AgentTurnNotFound):
                agent.turn_state(agent.turn_id_for("i3"))
            provider.allow.set()
            assert (await agent.wait_turn(r1.turn_id, timeout=5)).public_output.content == "答一"
            assert (await agent.wait_turn(r2.turn_id, timeout=5)).public_output.content == "答二"
            r3 = await agent.submit("问三", input_id="i3")
            assert r3.seq == 3
            assert (await agent.wait_turn(r3.turn_id, timeout=5)).public_output.content == "答三"

    asyncio.run(case())


def test_quota_does_not_block_idempotent_replay(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(max_pending_inputs=2), creation_key="r-1")
            await agent.submit("问一", input_id="i1")
            r2 = await agent.submit("问二", input_id="i2")
            replay = await agent.submit("问二", input_id="i2")
            assert replay == r2
            assert agent.turn_state(r2.turn_id) in {AgentTurnState.QUEUED, AgentTurnState.RUNNING}
            assert len(_rows(runtime.uow, "SELECT 1 FROM base_agent_turns_v1")) == 2
            provider.allow.set()
            await agent.wait_turn(r2.turn_id, timeout=5)

    asyncio.run(case())


def test_idle_agent_holds_no_lease_fence_or_heartbeat(tmp_path):
    """Challenge finding create-many-activates-and-leases-every-idle-agent."""

    async def case():
        provider = ScriptedProvider(["答一", "答二"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            kernel = runtime.kernel
            agents = await runtime.create_many([_config()] * 5, batch_key="idle")
            for _ in range(50):
                if not kernel._live.active_run_ids():
                    break
                await asyncio.sleep(0.01)
            assert kernel._live.active_run_ids() == ()
            assert not (set(kernel._leases) & {a.run_id for a in agents})
            assert not (set(kernel._heartbeats) & {a.run_id for a in agents})
            marks = ",".join("?" * len(agents))
            live = _rows(
                runtime.uow,
                f"SELECT COUNT(*) FROM workflow_leases WHERE expires_at > ? AND run_id IN ({marks})",
                runtime.ports.clock(),
                *[a.run_id for a in agents],
            )[0][0]
            assert live == 0
            # Waking an idle Agent still works, twice in a row.
            first = await agents[0].ask("问一", input_id="i1", timeout=5)
            assert first.public_output.content == "答一"
            for _ in range(50):
                if agents[0].run_id not in kernel._leases:
                    break
                await asyncio.sleep(0.01)
            assert agents[0].run_id not in kernel._leases
            second = await agents[0].ask("问二", input_id="i2", timeout=5)
            assert second.public_output.content == "答二"

    asyncio.run(case())


def test_inputs_are_never_lost_under_a_burst(tmp_path):
    async def case():
        provider = ScriptedProvider([f"答{n}" for n in range(30)])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agents = await runtime.create_many([_config()] * 3, batch_key="burst")
            receipts = []
            for n in range(10):
                for agent in agents:
                    receipts.append((agent, await agent.submit(f"问{n}", input_id=f"i{n}")))
                    await asyncio.sleep(0.001 * (n % 3))
            for agent, receipt in receipts:
                await agent.wait_turn(receipt.turn_id, timeout=10)
            assert provider.calls == 30

    asyncio.run(case())
