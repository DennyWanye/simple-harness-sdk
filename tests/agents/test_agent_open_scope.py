# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T4 (AC8 / BA05): ``open`` / ``binding`` / ``create`` are owner-scoped gates."""

from __future__ import annotations

import asyncio

import pytest
from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents import AgentConfig, AgentNotFound, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization

MISSING = "agent-" + "0" * 32


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="scope-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config():
    return AgentConfig(
        name="alice-secret-name", instructions="绝密指令 42", model_profile_ref="profile-1"
    )


async def _two_owners(tmp_path, provider):
    alice = build_agent_runtime(
        _ports(tmp_path, provider, owner_id="alice-rt"), owner_scope="alice"
    )
    bob = build_agent_runtime(_ports(tmp_path, provider, owner_id="bob-rt"), owner_scope="bob")
    return alice, bob


def test_open_across_owner_scope_is_indistinguishable_from_not_found(tmp_path):
    async def case():
        alice, bob = await _two_owners(tmp_path, ScriptedProvider([]))
        async with alice, bob:
            a = await alice.create(_config(), creation_key="k1")
            with pytest.raises(AgentNotFound) as cross:
                await bob.open(a.agent_id)
            with pytest.raises(AgentNotFound) as missing:
                await bob.open(MISSING)
            assert type(cross.value) is type(missing.value)
            assert str(cross.value) == str(missing.value).replace(MISSING, a.agent_id)
            assert cross.value.code == missing.value.code == "agent_not_found"

    asyncio.run(case())


def test_cross_owner_error_leaks_no_content(tmp_path):
    async def case():
        alice, bob = await _two_owners(tmp_path, ScriptedProvider([]))
        async with alice, bob:
            a = await alice.create(_config(), creation_key="k1")
            with pytest.raises(AgentNotFound) as cross:
                await bob.open(a.agent_id)
            text = str(cross.value)
            for secret in (a.config.name, a.config.instructions, "k1", "alice"):
                assert secret not in text
            assert a.run_id not in text or a.run_id == a.agent_id

    asyncio.run(case())


def test_cross_owner_cannot_read_results(tmp_path):
    async def case():
        alice, bob = await _two_owners(tmp_path, ScriptedProvider(["答"]))
        async with alice, bob:
            a = await alice.create(_config(), creation_key="k1")
            result = await a.ask("问", input_id="i1", timeout=5)
            assert result.public_output.content == "答"
            with pytest.raises(AgentNotFound):
                await bob.open(a.agent_id)
            assert bob.binding(a.agent_id) is None
            # Same creation_key under bob's scope is bob's own, distinct Agent.
            b = await bob.create(_config(), creation_key="k1")
            assert b.agent_id != a.agent_id and b.run_id != a.run_id
            assert b.history() == ()

    asyncio.run(case())


def test_same_owner_open_still_works_after_restart(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        clock = {"now": 10.0}
        alice = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="alice-rt", clock=lambda: clock["now"]),
            owner_scope="alice",
        )
        async with alice:
            a = await alice.create(_config(), creation_key="k1")
            expected = alice.binding(a.agent_id).config_hash
        clock["now"] = 100.0
        again = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="alice-rt-2", clock=lambda: clock["now"]),
            owner_scope="alice",
        )
        async with again:
            reopened = await again.open(a.agent_id)
            assert again.binding(reopened.agent_id).config_hash == expected
            same = await again.create(_config(), creation_key="k1")
            assert same.agent_id == a.agent_id

    asyncio.run(case())


def test_create_with_foreign_agent_id_collision_raises_not_found(tmp_path):
    """A foreign binding under the same derived id must not leak via the config check."""

    async def case():
        alice, bob = await _two_owners(tmp_path, ScriptedProvider([]))
        async with alice, bob:
            a = await alice.create(_config(), creation_key="k1")
            # Forge the collision: rewrite the binding's owner while keeping the id.
            alice.uow.database.connection.execute(
                "UPDATE base_agent_bindings_v1 SET owner_scope='bob' WHERE agent_id=?",
                (a.agent_id,),
            )
            alice.uow.database.connection.commit()
            different = AgentConfig(name="other", instructions="x", model_profile_ref="p")
            with pytest.raises(AgentNotFound):
                await alice.create(different, creation_key="k1")
            with pytest.raises(AgentNotFound):
                await alice.open(a.agent_id)
            assert alice.binding(a.agent_id) is None

    asyncio.run(case())
