# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""NEXT-TG-1.0 §10: ARP MISSION owner mode creates Agents only from exact Mission sources.

The source reader here is a recording double of ``MissionSourcePort`` (the real one,
over the orchestrator's records, is exercised in
``tests/orchestrator/full_target/taskgraph_exec/test_mission_sources.py``).  What is
proven at this layer: creation without a reader / with foreign or unknown sources is
refused by name and writes nothing; the bound set is recorded once and pinned into the
creation command, so the same key cannot replay into other sources; every new request
re-checks before it is frozen and carries the pin; a request already frozen replays as
frozen; a crash inside creation resumes with the same sources; a delegated child
inherits its parent's sources; and moving an existing library from a STANDALONE_CHAT
profile revision to a MISSION revision keeps it starting and its old Sessions running.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import HASH, build, fixture_refs, standalone_profile, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import mission_sources, store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import TrustedCaller
from simple_harness.agents.arp.profile import RuntimeProfile
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.contracts import AgentTurnState

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


def mission_profile(revision: int = 1) -> RuntimeProfile:
    base = standalone_profile()
    return RuntimeProfile(profile_id=base.profile_id, profile_revision=revision, owner_mode="MISSION",
                          allow_lexical_degradation=True, context_policy=base.context_policy, refs=fixture_refs())


def mission_caller(intent_id: str) -> TrustedCaller:
    return TrustedCaller(
        principal_ref=Pin("principal", "host:user", 0, HASH),
        owner_contract_ref=Pin("policy", "owner-mission", 1, HASH),
        command_receipt_ref=Pin("receipt", f"dispatch-intent:{intent_id}", 0, digest({"intent": intent_id})),
    )


def sources(intent_id: str, *, kind: str = "worker", occurrence: str = "occ-1") -> dict:
    manifest = ({"manifest_hash": "a" * 64, "binding": {"occurrence_id": occurrence, "dispatch_generation": 1}}
                if kind == "worker" else {"not_applicable": "planner_has_no_worker_attempt"})
    return {"schema": mission_sources.SCHEMA, "source_kind": kind, "intent_id": intent_id, "mission_id": "mission-1",
            "occurrence": {"occurrence_id": occurrence, "plan_revision": 1}, "input_manifest": manifest}


class RecordingSources:
    def __init__(self) -> None:
        self.by_intent: dict[str, dict] = {}
        self.stale: str | None = None
        self.checks: list[str] = []

    def bind(self, *, caller, role):  # type: ignore[no-untyped-def]
        intent_id = caller.command_receipt_ref.id.split(":", 1)[1]
        if intent_id not in self.by_intent:
            raise ArpError("SOURCE_UNAVAILABLE", "no such intent")
        return self.by_intent[intent_id]

    def require_current(self, value) -> None:  # type: ignore[no-untyped-def]
        self.checks.append(value["intent_id"])
        if self.stale is not None:
            raise ArpError(self.stale, "moved")


def _count(runtime, sql: str) -> int:  # type: ignore[no-untyped-def]
    return int(runtime.uow.database.connection.execute(sql).fetchone()[0])


def _code(error: pytest.ExceptionInfo) -> str:
    return error.value.code


def test_creation_without_exact_sources_is_refused_by_name_and_writes_nothing(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path / "a", profile=mission_profile())
        async with runtime:
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "SOURCE_UNAVAILABLE"  # MISSION mode without the reader
        reader = RecordingSources()
        runtime = build(tmp_path / "b", profile=mission_profile(), mission_sources=reader)
        async with runtime:
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "SOURCE_UNAVAILABLE"  # the authority knows no such intent
            reader.by_intent["intent-1"] = sources("intent-2")  # a set naming another intent
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "REF_IDENTITY_MISMATCH"
            reader.by_intent["intent-1"] = {**sources("intent-1"), "source_kind": "anything"}
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "UNION_MISMATCH"
            reader.by_intent["intent-1"] = {**sources("intent-1"), "agent_config_hash": "0" * 64}
            with pytest.raises(ArpError) as refused:  # another configuration than the intent froze
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "REF_IDENTITY_MISMATCH"
            reader.by_intent["intent-1"] = sources("intent-1")
            reader.stale = "REQUEST_SOURCE_STALE"  # the authority withdrew it since binding
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "REQUEST_SOURCE_STALE"
            assert _count(runtime, "SELECT COUNT(*) FROM arp_creation_intents") == 0
            assert _count(runtime, "SELECT COUNT(*) FROM arp_agent_sessions") == 0

    asyncio.run(case())


def test_sources_are_recorded_once_and_the_key_never_replays_into_other_sources(tmp_path) -> None:
    async def case() -> None:
        reader = RecordingSources()
        reader.by_intent["intent-1"] = {**sources("intent-1"), "agent_config_hash": digest(CONFIG.to_json())}
        runtime = build(tmp_path, profile=mission_profile(), mission_sources=reader)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            record = mission_sources.read_record(connection, session.session_id)
            reader.by_intent["intent-1"] = sources("intent-1")
            frozen = {**sources("intent-1"), "agent_config_hash": digest(CONFIG.to_json())}
            assert record["sources"] == frozen and record["sources_hash"] == digest(frozen)
            assert record["role"] == "root" and record["delegated_from"] is None
            reader.by_intent["intent-1"] = frozen
            again = await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert again.agent_id == agent.agent_id  # the same intent replays the same Agent
            reader.by_intent["intent-1"] = sources("intent-1", occurrence="occ-2")  # moved occurrence
            with pytest.raises(ArpError) as refused:
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _code(refused) == "CREATION_IDENTITY_CONFLICT"
            # a planner has no Worker Attempt: its own role-typed set is a legal creation
            reader.by_intent["intent-p"] = sources("intent-p", kind="planner")
            planner = await runtime.create(CONFIG, creation_key="kp", caller=mission_caller("intent-p"))
            planned = store.read_live_session(connection, planner.agent_id)
            assert mission_sources.read_record(connection, planned.session_id)["sources"]["input_manifest"] == {
                "not_applicable": "planner_has_no_worker_attempt"}

    asyncio.run(case())


def test_every_new_request_rechecks_and_pins_the_sources_a_frozen_one_replays(tmp_path) -> None:
    async def case() -> None:
        reader = RecordingSources()
        reader.by_intent["intent-1"] = sources("intent-1")
        provider = ScriptedProvider(["好的。", "第二次。"])
        runtime = build(tmp_path, provider, profile=mission_profile(), mission_sources=reader)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            checks_at_creation = len(reader.checks)
            receipt = await agent.submit("第一件事", input_id="i1")
            assert (await agent.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
            assert len(reader.checks) == checks_at_creation + 1  # re-checked before freezing
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            [row] = connection.execute("SELECT original_request_key FROM arp_context_requests").fetchall()
            manifest = store.read_context_by_request_key(connection, str(row[0])).manifest
            pin = mission_sources.record_pin(session.session_id, mission_sources.read_record(connection, session.session_id))
            assert pin.to_json() in manifest["authority_refs"]
            assert manifest["owner_contract_ref"]["id"].endswith("owner-mode:MISSION")
            # a frozen request replays as frozen even after its sources moved (crash after freeze)
            reader.stale = "REQUEST_SOURCE_STALE"
            replayed = runtime._assembled.wire.prepare_request(provider.requests[0])
            from simple_harness.execution.provider_invocations import provider_request_fingerprint

            assert provider_request_fingerprint(replayed) == manifest["planned_request_hash"]
            # a new request is refused before any model call
            receipt = await agent.submit("第二件事", input_id="i2")
            result = await agent.wait_turn(receipt.turn_id, timeout=10)
            assert result.state is not AgentTurnState.COMMITTED and provider.calls == 1

    asyncio.run(case())


def test_a_crash_inside_creation_resumes_with_the_same_sources(tmp_path) -> None:
    async def case() -> None:
        reader = RecordingSources()
        reader.by_intent["intent-1"] = sources("intent-1")
        armed = {"point": "create.after_marker"}

        def fault(point: str) -> None:
            if armed.get("point") == point:
                armed.clear()
                raise RuntimeError("crash")

        runtime = build(tmp_path, profile=mission_profile(), mission_sources=reader, fault=fault)
        async with runtime:
            with pytest.raises(RuntimeError):
                await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            assert _count(runtime, "SELECT COUNT(*) FROM arp_agent_sessions") == 0
            agent = await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            assert mission_sources.read_record(connection, session.session_id)["sources"] == sources("intent-1")

    asyncio.run(case())


def test_a_delegated_child_inherits_the_parent_sources(tmp_path) -> None:
    async def case() -> None:
        reader = RecordingSources()
        reader.by_intent["intent-1"] = sources("intent-1")
        runtime = build(tmp_path, profile=mission_profile(), mission_sources=reader)
        async with runtime:
            parent = await runtime.create(CONFIG, creation_key="k1", caller=mission_caller("intent-1"))
            connection = runtime.uow.database.connection
            child_caller = TrustedCaller(
                principal_ref=Pin("principal", f"agent:{parent.agent_id}", 0, HASH),
                owner_contract_ref=Pin("policy", "owner-mission", 1, HASH),
                command_receipt_ref=Pin("receipt", "delegation:d1", 0, HASH),
            )
            record = mission_sources.record_for_creation(reader, connection, caller=child_caller, role="child")
            assert record["sources"] == sources("intent-1")
            assert record["delegated_from"]["parent_agent_id"] == parent.agent_id
            orphan = TrustedCaller(Pin("principal", "agent:nobody", 0, HASH), child_caller.owner_contract_ref,
                                   child_caller.command_receipt_ref)
            with pytest.raises(ArpError) as refused:
                mission_sources.record_for_creation(reader, connection, caller=orphan, role="child")
            assert _code(refused) == "SOURCE_UNAVAILABLE"

    asyncio.run(case())


def test_moving_a_library_from_standalone_to_mission_keeps_old_sessions_running(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["旧的。", "旧的还在。", "新的。"])
        runtime = build(tmp_path, provider)
        async with runtime:
            old = await runtime.create(CONFIG, creation_key="old", caller=trusted_caller())
            receipt = await old.submit("旧会话", input_id="i1")
            assert (await old.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
        runtime.uow.database.close()

        reader = RecordingSources()
        reader.by_intent["intent-1"] = sources("intent-1")
        runtime = build(tmp_path, provider, profile=mission_profile(revision=2), mission_sources=reader)
        async with runtime:
            connection = runtime.uow.database.connection
            assert _count(runtime, "SELECT COUNT(*) FROM arp_profiles") == 2  # both revisions kept
            reopened = await runtime.open(old.agent_id)
            receipt = await reopened.submit("旧会话继续", input_id="i2")
            assert (await reopened.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
            assert reader.checks == []  # a STANDALONE_CHAT Session is never checked as a Mission one
            rows = connection.execute("SELECT original_request_key FROM arp_context_requests ORDER BY rowid").fetchall()
            latest = store.read_context_by_request_key(connection, str(rows[-1][0])).manifest
            assert latest["owner_contract_ref"]["id"].endswith("owner-mode:STANDALONE_CHAT")
            assert latest["owner_contract_ref"]["revision"] == 1
            fresh = await runtime.create(CONFIG, creation_key="new", caller=mission_caller("intent-1"))
            session = store.read_live_session(connection, fresh.agent_id)
            assert session.profile_revision == 2
            assert mission_sources.read_record(connection, session.session_id)["sources"] == sources("intent-1")

    asyncio.run(case())
