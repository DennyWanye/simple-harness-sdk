# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T5 (AC5 / AC6 / AC7): idempotent, all-or-nothing, model-call-free batches."""

from __future__ import annotations

import asyncio

import pytest
from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents import (
    AgentBatchIdentityConflict,
    AgentBatchRejected,
    AgentConfig,
    AgentInstanceCapExceeded,
    build_agent_runtime,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import AgentRuntime, agent_id_for
from simple_harness.contracts import RunId
from simple_harness.execution.dispatch import ProviderInvocationCoordinator

TABLES = (
    "base_agent_bindings_v1",
    "runs",
    "conversation_run_modes",
    "base_agent_creation_batches_v1",
    "continuations",
    "base_agent_turns_v1",
)


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="batch-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(instructions="你是助手。", tool_names=()):
    return AgentConfig(
        name="worker",
        instructions=instructions,
        model_profile_ref="profile-1",
        tool_names=tuple(tool_names),
    )


def _counts(uow):
    connection = uow.database.connection
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES
    }


def test_hundred_idle_agents_make_no_provider_request(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        instances = 0
        original_init = ProviderInvocationCoordinator.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal instances
            instances += 1
            original_init(self, *args, **kwargs)

        ProviderInvocationCoordinator.__init__ = counting_init  # type: ignore[method-assign]
        try:
            async with runtime:
                agents = await runtime.create_many([_config()] * 100, batch_key="b100")
                assert provider.calls == 0
                counts = _counts(runtime.uow)
                assert counts["base_agent_turns_v1"] == 0
                assert counts["base_agent_bindings_v1"] == 100
                assert len({a.agent_id for a in agents}) == 100
                assert len({a.run_id for a in agents}) == 100
                context = runtime.kernel._ports.context
                assert context.load(RunId(agents[0].run_id)).revision == 0
                assert context.load(RunId(agents[99].run_id)).revision == 0
                assert runtime.kernel._ports.provider is runtime.kernel._ports.provider
                assert instances <= 1
            clock["now"] = 100.0
            restarted = build_agent_runtime(
                _ports(tmp_path, provider, owner_id="batch-owner-2", clock=lambda: clock["now"])
            )
            async with restarted:
                await restarted.recover_pending_turns()
                assert restarted.kernel._live.active_run_ids() == ()
                assert provider.calls == 0
        finally:
            ProviderInvocationCoordinator.__init__ = original_init  # type: ignore[method-assign]

    asyncio.run(case())


def test_same_batch_key_same_content_returns_the_same_ids(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        configs = [_config("A"), _config("B"), _config("C")]
        async with runtime:
            first = await runtime.create_many(configs, batch_key="same")
            second = await runtime.create_many(configs, batch_key="same")
            assert [a.agent_id for a in first] == [a.agent_id for a in second]
            counts = _counts(runtime.uow)
            assert counts["base_agent_creation_batches_v1"] == 1
            assert counts["base_agent_bindings_v1"] == 3
            batch = runtime.uow.read_agent_batch("default", "same")
            assert batch is not None and batch.state == "committed"
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="batch-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            third = await restarted.create_many(configs, batch_key="same")
            assert [a.agent_id for a in third] == [a.agent_id for a in first]
            assert _counts(restarted.uow)["base_agent_bindings_v1"] == 3

    asyncio.run(case())


def test_same_batch_key_different_content_conflicts(tmp_path):
    async def case():
        async with build_agent_runtime(_ports(tmp_path, ScriptedProvider([]))) as runtime:
            configs = [_config("A"), _config("B")]
            await runtime.create_many(configs, batch_key="k")
            before = _counts(runtime.uow)
            with pytest.raises(AgentBatchIdentityConflict) as info:
                await runtime.create_many([_config("A"), _config("B!")], batch_key="k")
            assert info.value.code == "batch_identity_conflict"
            with pytest.raises(AgentBatchIdentityConflict):
                await runtime.create_many([_config("B"), _config("A")], batch_key="k")
            with pytest.raises(AgentBatchIdentityConflict):
                await runtime.create_many(configs + [_config("C")], batch_key="k")
            assert _counts(runtime.uow) == before

    asyncio.run(case())


def test_existing_binding_conflict_is_caught_before_any_write(tmp_path):
    """Challenge finding batch-preflight-misses-existing-binding-conflicts."""

    async def case():
        async with build_agent_runtime(_ports(tmp_path, ScriptedProvider([]))) as runtime:
            # A single create under the key the batch will derive for index 1.
            await runtime.create(_config("solo"), creation_key="pre:1")
            before = _counts(runtime.uow)
            with pytest.raises(AgentBatchIdentityConflict):
                await runtime.create_many(
                    [_config("A"), _config("B"), _config("C")], batch_key="pre"
                )
            assert _counts(runtime.uow) == before
            # Same content as the pre-existing instance is fine and adopts it.
            agents = await runtime.create_many([_config("A"), _config("solo")], batch_key="pre")
            assert len(agents) == 2

    asyncio.run(case())


def test_invalid_config_leaves_no_partial_batch(tmp_path):
    async def case():
        async with build_agent_runtime(_ports(tmp_path, ScriptedProvider([]))) as runtime:
            before = _counts(runtime.uow)
            configs = [
                _config("0"),
                _config("1"),
                _config("2", tool_names=("no_such_tool",)),
                _config("3"),
                _config("4"),
            ]
            with pytest.raises(AgentBatchRejected) as info:
                await runtime.create_many(configs, batch_key="bad")
            assert info.value.index == 2
            assert info.value.error_code == "agent_batch_unknown_tool"
            assert info.value.code == "agent_batch_rejected"
            assert _counts(runtime.uow) == before
            with pytest.raises(AgentBatchRejected) as typed:
                await runtime.create_many([_config(), "not-a-config"], batch_key="bad2")  # type: ignore[list-item]
            assert typed.value.index == 1
            assert _counts(runtime.uow) == before
            # The delegate tool is a known name.
            ok = await runtime.create_many(
                [_config(tool_names=("agent_delegate",))], batch_key="ok"
            )
            assert len(ok) == 1

    asyncio.run(case())


def test_batch_size_and_instance_caps_are_enforced_before_any_write(tmp_path):
    async def case():
        ports = _ports(tmp_path, ScriptedProvider([]), max_batch_size=3, max_agents=5)
        async with build_agent_runtime(ports) as runtime:
            before = _counts(runtime.uow)
            with pytest.raises(AgentBatchRejected) as info:
                await runtime.create_many([_config()] * 4, batch_key="big")
            assert info.value.error_code == "agent_batch_too_large"
            assert _counts(runtime.uow) == before
            await runtime.create_many([_config()] * 3, batch_key="first")
            after_first = _counts(runtime.uow)
            with pytest.raises(AgentBatchRejected) as cap:
                await runtime.create_many([_config()] * 3, batch_key="second")
            assert cap.value.error_code == "agent_batch_instance_cap"
            assert _counts(runtime.uow) == after_first
            # Replaying the committed batch never counts against the cap.
            again = await runtime.create_many([_config()] * 3, batch_key="first")
            assert len(again) == 3
            with pytest.raises(AgentBatchRejected):
                await runtime.create_many([], batch_key="empty")

    asyncio.run(case())


def test_reserved_batch_is_resumed_not_duplicated(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        configs = [_config("A"), _config("B"), _config("C")]
        original = AgentRuntime.create
        created = 0

        async def crashing_create(self, config, *, creation_key):
            nonlocal created
            created += 1
            if created == 2:
                raise RuntimeError("process died mid-batch")
            return await original(self, config, creation_key=creation_key)

        AgentRuntime.create = crashing_create  # type: ignore[method-assign]
        try:
            async with runtime:
                with pytest.raises(RuntimeError):
                    await runtime.create_many(configs, batch_key="crash")
                reserved = runtime.uow.read_agent_batch("default", "crash")
                assert reserved is not None and reserved.state == "reserved"
                assert _counts(runtime.uow)["base_agent_bindings_v1"] == 1
        finally:
            AgentRuntime.create = original  # type: ignore[method-assign]
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="batch-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            agents = await restarted.create_many(configs, batch_key="crash")
            assert [a.agent_id for a in agents] == list(reserved.agent_ids)
            counts = _counts(restarted.uow)
            assert counts["base_agent_bindings_v1"] == 3
            assert counts["base_agent_creation_batches_v1"] == 1
            committed = restarted.uow.read_agent_batch("default", "crash")
            assert committed is not None and committed.state == "committed"
            assert provider.calls == 0

    asyncio.run(case())


def test_instance_cap_is_enforced_at_insert_for_single_create(tmp_path):
    """Review F4: the cap is decided where the binding is written, not only in batches."""

    async def case():
        ports = _ports(tmp_path, ScriptedProvider([]), max_agents=2)
        async with build_agent_runtime(ports) as runtime:
            await runtime.create(_config("a"), creation_key="a")
            await runtime.create(_config("b"), creation_key="b")
            before = _counts(runtime.uow)
            with pytest.raises(AgentInstanceCapExceeded) as info:
                await runtime.create(_config("c"), creation_key="c")
            assert info.value.code == "agent_instance_cap_exceeded"
            assert (
                _counts(runtime.uow)["base_agent_bindings_v1"] == before["base_agent_bindings_v1"]
            )
            # Replaying an existing key is never blocked by the cap.
            same = await runtime.create(_config("a"), creation_key="a")
            assert same.agent_id == agent_id_for("default", "a")

    asyncio.run(case())
