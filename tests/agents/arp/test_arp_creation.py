# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-A creation chain (BW01, case R01/R02/N07/N08 narrow forms).

Durable intent → kernel → one activation transaction; replays are identity-stable;
crash windows resume with the same ids; nothing calls a model at creation.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from arp_fixture import build, standalone_profile, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import PROTOCOL, store
from simple_harness.agents.arp.creation import MARKER_FILE, read_marker
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root
from simple_harness.agents.arp.runtime import build_arp_runtime
from simple_harness.execution.uow import RunState

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


def _count(runtime, sql: str, *args) -> int:
    return int(runtime.uow.database.connection.execute(sql, args).fetchone()[0])


def test_factory_refuses_allow_all_authorization(tmp_path) -> None:
    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    ports = AgentRuntimePorts(
        provider=ScriptedProvider([]),
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
    )
    with pytest.raises(ArpError) as info:
        build_arp_runtime(
            ports,
            ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt={"k": 1}),
        )
    assert info.value.code == "AUTHORITY_SOURCE_MISSING"


def test_factory_freezes_profile_and_policy_in_v11_library(tmp_path) -> None:
    runtime = build(tmp_path)
    connection = runtime.uow.database.connection
    assert runtime.uow.database.schema_version == 11
    assert runtime.arp.protocol == PROTOCOL
    assert _count(runtime, "SELECT COUNT(*) FROM arp_profiles") == 1
    assert _count(runtime, "SELECT COUNT(*) FROM arp_policy_objects") == 1
    assert store.read_profile(connection, "standalone-test", 1).body_hash == runtime.arp.profile.body_hash
    # Rebuilding over the same library replays the identical frozen rows.
    runtime.uow.database.close()
    again = build(tmp_path)
    assert _count(again, "SELECT COUNT(*) FROM arp_profiles") == 1
    again.uow.database.close()


def test_create_requires_trusted_caller_and_embedding_truth_table(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, profile=standalone_profile(embedding_required=True))
        async with runtime:
            with pytest.raises(ArpError) as info:
                await runtime.create(CONFIG, creation_key="k1")
            assert info.value.code == "AUTHORITY_SOURCE_MISSING"
            with pytest.raises(ArpError) as info:
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert info.value.code == "EMBEDDING_RESOURCE_MISSING"
            assert _count(runtime, "SELECT COUNT(*) FROM arp_creation_intents") == 0
            assert _count(runtime, "SELECT COUNT(*) FROM base_agent_bindings_v1") == 0

    asyncio.run(case())


def test_create_binds_protocol_session_adoption_and_events_in_one_transaction(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["回答"])
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            connection = runtime.uow.database.connection
            assert provider.calls == 0  # creation never calls a model
            protocol = store.read_protocol(connection, agent.agent_id)
            assert protocol is not None and protocol.protocol == PROTOCOL
            session = store.read_live_session(connection, agent.agent_id)
            assert session is not None
            assert (session.state, session.generation, session.row_version) == ("ACTIVE", 1, 2)
            assert session.profile_ref == runtime.arp.profile.pin
            adoption = store.latest_adoption(connection, session.session_id)
            assert adoption is not None and adoption.adoption_revision == 1
            assert adoption.policy_ref == runtime.arp.policy.pin
            intent = store.read_creation_intent(connection, "default", "k1")
            assert intent is not None and intent.state == "BOUND" and intent.proposed_agent_id == agent.agent_id
            events = store.list_event_bindings(connection)
            assert [e.event_type for e in events] == [
                "AgentContextPolicyAdopted",
                "RuntimeSessionStateChanged",
            ]
            assert events[1].body["old_state"] == "CREATING" and events[1].body["new_state"] == "ACTIVE"
            # The typed body binds an original run event with a real durable sequence.
            original = connection.execute(
                "SELECT durable_seq, kind FROM run_events WHERE event_id=?", (events[1].original_event_id,)
            ).fetchone()
            assert original is not None and int(original[0]) == events[1].original_eventseq
            # Marker on disk under the managed root, hash bound in the protocol row.
            directory = runtime.arp.root.resolve_relative(session.relative_directory)
            marker = read_marker(directory)
            assert marker["session_id"] == session.session_id and digest(marker) == protocol.marker_hash
            assert (directory / MARKER_FILE).is_file()
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state in (RunState.RUNNING, RunState.WAITING)
            # Replay: same key + same config → same Agent, no new rows.
            again = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert again.agent_id == agent.agent_id
            assert _count(runtime, "SELECT COUNT(*) FROM arp_agent_sessions") == 1
            assert _count(runtime, "SELECT COUNT(*) FROM arp_context_policy_adoptions") == 1
            assert _count(runtime, "SELECT COUNT(*) FROM arp_event_bindings") == 2
            with pytest.raises(ValueError):
                await runtime.create(
                    AgentConfig(name="w2", instructions="别的。", model_profile_ref="p"),
                    creation_key="k1",
                    caller=trusted_caller(),
                )
            # Same key, same config, but a different authenticated command → conflict.
            with pytest.raises(ArpError) as info:
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller("create-other"))
            assert info.value.code == "CREATION_IDENTITY_CONFLICT"
            # The Agent still runs through the unchanged kernel path.
            receipt = await agent.submit("你好", input_id="i1")
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.public_output.content == "回答"
            assert provider.calls == 1

    asyncio.run(case())


@pytest.mark.parametrize("point", ["create.after_intent", "create.after_marker", "create.after_kernel"])
def test_crash_windows_resume_with_the_same_identity(tmp_path, point: str) -> None:
    async def case() -> None:
        armed = {"on": True}

        def fault(name: str) -> None:
            if armed["on"] and name == point:
                raise RuntimeError(f"crash at {name}")

        runtime = build(tmp_path, fault=fault)
        async with runtime:
            with pytest.raises(RuntimeError):
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            connection = runtime.uow.database.connection
            intent = store.read_creation_intent(connection, "default", "k1")
            assert intent is not None and intent.state == "PREPARED"
            # Nothing is activated half way: no binding, no session, no protocol.
            assert _count(runtime, "SELECT COUNT(*) FROM base_agent_bindings_v1") == 0
            assert _count(runtime, "SELECT COUNT(*) FROM arp_agent_sessions") == 0
            armed["on"] = False
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert agent.agent_id == intent.proposed_agent_id
            session = store.read_live_session(connection, agent.agent_id)
            assert session is not None and session.state == "ACTIVE"
            assert store.read_creation_intent(connection, "default", "k1").state == "BOUND"
            assert _count(runtime, "SELECT COUNT(*) FROM runs") == 1
            assert _count(runtime, "SELECT COUNT(*) FROM arp_agent_sessions") == 1

    asyncio.run(case())


def test_create_many_goes_through_the_native_chain(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path)
        async with runtime:
            configs = [
                AgentConfig(name=f"a{i}", instructions="x", model_profile_ref="p") for i in range(3)
            ]
            agents = await runtime.create_many(configs, batch_key="b1", caller=trusted_caller("batch"))
            connection = runtime.uow.database.connection
            for agent in agents:
                assert store.read_protocol(connection, agent.agent_id).protocol == PROTOCOL
                assert store.read_live_session(connection, agent.agent_id).state == "ACTIVE"
            assert _count(runtime, "SELECT COUNT(*) FROM arp_creation_intents WHERE state='BOUND'") == 3
            replay = await runtime.create_many(configs, batch_key="b1", caller=trusted_caller("batch"))
            assert [a.agent_id for a in replay] == [a.agent_id for a in agents]

    asyncio.run(case())


def test_session_marker_tamper_is_refused(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            path = runtime.arp.root.resolve_relative(session.relative_directory) / MARKER_FILE
            body = json.loads(path.read_text())
            body["root_incarnation"] = "other"
            path.write_text(json.dumps(body))
            with pytest.raises(ArpError) as info:
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert info.value.code == "SESSION_IDENTITY_MISMATCH"

    asyncio.run(case())
