# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""A creation key is unique per owner, not across the whole library (RP-E2 leftover).

Creation identity is (owner_scope, creation_key): the intent table and the Agent id
already say so.  The session row used to key on the bare creation key, so a second
owner that happened to pick the same key died in C3 with SESSION_IDENTITY_MISMATCH.
"""

from __future__ import annotations

import asyncio

from arp_fixture import build, trusted_caller

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


def test_two_owners_may_use_the_same_creation_key(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path)
        async with runtime:
            creation = runtime.arp.creation
            first = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller("c-a"), owner_scope="owner-a")
            second = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller("c-b"), owner_scope="owner-b")
            assert first.binding.agent_id != second.binding.agent_id
            assert first.binding.owner_scope == "owner-a" and second.binding.owner_scope == "owner-b"
            # Each owner's replay still returns its own Agent.
            again = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller("c-a"), owner_scope="owner-a")
            assert again.binding.agent_id == first.binding.agent_id
            rows = runtime.uow.database.connection.execute(
                "SELECT COUNT(*) FROM arp_agent_sessions WHERE state!='PURGED'"
            ).fetchone()[0]
            assert rows == 2

    asyncio.run(case())


def test_legacy_rows_keyed_on_the_bare_key_still_replay(tmp_path, monkeypatch) -> None:
    """Libraries written before the fix hold the bare key; replaying them is not a mismatch."""

    from simple_harness.agents.arp import creation as creation_module

    async def case() -> None:
        runtime = build(tmp_path)
        async with runtime:
            creation = runtime.arp.creation
            with monkeypatch.context() as legacy:
                legacy.setattr(creation_module, "session_creation_key", lambda _owner, key: key)
                first = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller(), owner_scope="owner-a")
            connection = runtime.uow.database.connection
            assert store.read_live_session(connection, first.binding.agent_id).creation_key == "k1"
            again = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller(), owner_scope="owner-a")
            assert again.binding.agent_id == first.binding.agent_id
            # A different owner is not blocked by the legacy bare-key row.
            other = await creation.create(CONFIG, creation_key="k1", caller=trusted_caller("c-b"), owner_scope="owner-b")
            assert other.binding.agent_id != first.binding.agent_id

    asyncio.run(case())
