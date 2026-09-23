# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-B: protocol groups, real metering and the frozen ContextManifest (cases C01–C06 narrow forms).

Every request the scripted provider receives was composed by the native composer at
``prepare_request``: its final wire count comes from the deployment's certified
counter, the manifest binds the exact request hash, and closure / N come from the
Journal + effects ledger, never from model text.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import ExactWordTokenizer, build, meter_binding, trusted_caller
from provider_fixture import MODEL, ScriptedProvider
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.context import groups as protocol_groups
from simple_harness.agents.arp.context.composer import ArpContextRejected
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.meter import MeterBinding, model_limits
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.execution.provider_invocations import provider_request_fingerprint

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
TOOL_CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p", tool_names=("echo",))


def _tool_ports():
    return dict(tool_executor=EchoToolExecutor(), tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA})


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def _manifests(runtime):  # type: ignore[no-untyped-def]
    connection = runtime.uow.database.connection
    rows = connection.execute(
        "SELECT original_request_key FROM arp_context_requests ORDER BY provider_request_ordinal"
    ).fetchall()
    return [store.read_context_by_request_key(connection, str(r[0])) for r in rows]


def test_uncertified_counter_is_refused_before_assembly() -> None:
    tokenizer = UpperBoundTokenizer()
    with pytest.raises(ArpError) as info:
        model_limits(model=MODEL, tokenizer=tokenizer, input_limit_tokens=8192, max_output_tokens=1024)
    assert info.value.code == "GENERIC_TOKEN_BOUND_UNCERTIFIED"
    limits = model_limits(model=MODEL, tokenizer=ExactWordTokenizer(), input_limit_tokens=8192, max_output_tokens=1024)
    with pytest.raises(ArpError) as info:
        MeterBinding(tokenizer=tokenizer, model_limits=limits, certification_ref=Pin("receipt", "x", 0, "a" * 64))
    assert info.value.code == "GENERIC_TOKEN_BOUND_UNCERTIFIED"


def test_every_provider_request_has_a_frozen_manifest_with_the_real_wire_count(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider([("echo", {}), "工具已经回声，完成。"])
        runtime = build(tmp_path, provider, **_tool_ports())
        async with runtime:
            agent = await runtime.create(TOOL_CONFIG, creation_key="k1", caller=trusted_caller())
            result = await _turn(agent, "请调用 echo 工具", "i1")
            assert result.state is AgentTurnState.COMMITTED
            assert provider.calls == 2
            manifests = _manifests(runtime)
            assert len(manifests) == 2
            tokenizer = ExactWordTokenizer()
            for manifest, sent in zip(manifests, provider.requests):
                body = manifest.manifest
                # The bound hash is the hash of the request the provider actually received.
                assert body["planned_request_hash"] == provider_request_fingerprint(sent)
                assert body["token_receipt"]["request_hash"] == body["planned_request_hash"]
                assert body["token_receipt"]["count_mode"] == "EXACT"
                expected = runtime.arp.meter.count_wire(sent)
                assert body["input_token_charge"] == expected == body["token_receipt"]["wire_input_tokens"]
                assert body["input_token_charge"] <= body["effective_input_budget"]
                assert body["reserved_output_tokens"] == sent.max_output_tokens
                assert body["session_generation"] == 1 and body["adoption_revision"] == 1
                sections = {s["section"] for s in body["sections"]}
                assert {"A", "E", "G"} <= sections
                assert all(s["trust"] == "CONTROL" for s in body["sections"] if s["section"] in ("A", "E"))
                # Retrieval always has an aggregate result, never a page in flight.
                recall = store.read_context_recall(runtime.uow.database.connection, body["retrieval_receipt_ref"]["id"])
                assert recall is not None and recall.phase in ("READY", "SKIPPED")
                assert recall.result is not None and digest(recall.result) == body["retrieval_receipt_ref"]["content_hash"]
            del tokenizer
            first, second = manifests
            assert first.provider_request_ordinal == 1 and second.provider_request_ordinal == 2
            assert first.turn_id == second.turn_id
            # The second request carries the closed tool group as a required (open tail
            # became closed after the result) or recent group, never a stray message.
            assert any(s["section"] == "G" and s["block_id"].startswith("group:") for s in second.manifest["sections"])
            events = [e.event_type for e in store.list_event_bindings(runtime.uow.database.connection)]
            assert events.count("RuntimeContextPrepared") == 2
            # Replaying the frozen request reproduces the identical wire (same hash).
            wire = runtime._assembled.wire
            again = wire.prepare_request(provider.requests[1])
            assert provider_request_fingerprint(again) == second.planned_request_hash

    asyncio.run(case())


def test_closure_and_turn_counting_come_from_the_ledger(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider([("echo", {}), "第一轮完成。", "第二轮完成。"])
        runtime = build(tmp_path, provider, **_tool_ports())
        async with runtime:
            agent = await runtime.create(TOOL_CONFIG, creation_key="k1", caller=trusted_caller())
            first = await _turn(agent, "第一轮，请调用 echo", "i1")
            assert first.state is AgentTurnState.COMMITTED
            connection = runtime.uow.database.connection
            session = store.read_live_session(connection, agent.agent_id)
            snapshot = protocol_groups.capture(
                connection,
                session_id=session.session_id,
                agent_id=agent.agent_id,
                highwater=runtime.uow.agent_journal_highwater(agent.agent_id),
                charge=lambda r: 1,
                session_ref=session.pin,
            )
            kinds = [g.kind for g in snapshot.groups]
            assert kinds == ["USER_ANCHOR", "CLOSED_TOOL", "TERMINAL_ANSWER"]
            closed = snapshot.groups[1]
            assert closed.call_ids and set(closed.call_ids) == set(closed.result_call_ids)
            assert closed.closed and closed.indexable
            assert snapshot.body["turns"][0]["completed"] is True
            assert set(snapshot.body["turns"][0]["required_group_ids"]) == {g.group_id for g in snapshot.groups}
            second = await _turn(agent, "第二轮", "i2")
            assert second.state is AgentTurnState.COMMITTED
            manifest = _manifests(runtime)[-1].manifest
            # N counts the completed historic Turn whose groups are all selected; the
            # current Turn never counts.
            assert manifest["recent_complete_turn_count"] == 1
            assert manifest["selected_turn_ids"] == [first.turn_id]
            assert manifest["N_count_complete"] is True
            assert manifest["turn_id"] == second.turn_id

    asyncio.run(case())


def test_required_content_over_the_final_budget_is_a_definite_refusal(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["不应被调用"])
        # Input limit 200 words; the instructions alone are 300 words.
        runtime = build(tmp_path, provider, input_limit=400, max_output=128)
        async with runtime:
            huge = " ".join(f"规则{i}" for i in range(300))
            agent = await runtime.create(
                AgentConfig(name="w", instructions=huge, model_profile_ref="p"), creation_key="k1", caller=trusted_caller()
            )
            result = await _turn(agent, "你好", "i1")
            assert result.state is AgentTurnState.FAILED
            assert provider.calls == 0
            assert "REQUIRED_CONTEXT_TOO_LARGE" in str(result.error) or "NO_INPUT_BUDGET" in str(result.error)
            assert _manifests(runtime) == []

    asyncio.run(case())


def test_meter_refuses_output_above_the_deployment_limit(tmp_path) -> None:
    runtime = build(tmp_path, max_output=512)
    from simple_harness.contracts import Message, MessageRole, RequestId
    from simple_harness.providers import ProviderRequest

    request = ProviderRequest(RequestId("a:provider-turn:1"), (Message(MessageRole.USER, "hi"),), max_output_tokens=4096)
    with pytest.raises(ArpError) as info:
        runtime.arp.meter.measure(request, run_id="a", max_input_budget=100, requested_output_tokens=4096)
    assert info.value.code == "INVALID_OUTPUT_RESERVE"
    ok = runtime.arp.meter.measure(request, run_id="a", max_input_budget=100, requested_output_tokens=256)
    assert ok.wire_tokens == runtime.arp.meter.count_wire(request) and ok.receipt["coverage"] == "TEXT_TOOLS_JSON"
    runtime.uow.database.close()


def test_composer_errors_surface_as_definite_provider_rejections() -> None:
    error = ArpContextRejected(ArpError("REQUEST_SOURCE_STALE", "journal moved"))
    assert error.arp_code == "REQUEST_SOURCE_STALE" and error.retryable is False
    assert "REQUEST_SOURCE_STALE" in str(error)


def test_meter_binding_requires_the_runtime_tokenizer(tmp_path) -> None:
    from arp_fixture import RecordingAuthorization, activation_receipt, standalone_profile

    from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root
    from simple_harness.agents.arp.runtime import build_arp_runtime
    from simple_harness.agents.ports import AgentRuntimePorts

    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    ports = AgentRuntimePorts(
        provider=ScriptedProvider([]), authorization=RecordingAuthorization(), database_path=str(tmp_path / "runtime.db"),
        model=MODEL, tokenizer=ExactWordTokenizer(), default_max_output_tokens=128, max_output_tokens_ceiling=256,
    )
    other = meter_binding(ExactWordTokenizer(), max_output=256)
    with pytest.raises(ArpError) as info:
        build_arp_runtime(ports, ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt(), meter=other))
    assert info.value.code == "TOKEN_SERIALIZER_CHANGED"
    with pytest.raises(ArpError) as info:
        build_arp_runtime(ports, ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt()))
    assert info.value.code == "GENERIC_TOKEN_BOUND_UNCERTIFIED"


def test_shrink_drops_the_turn_of_a_dropped_group_from_the_complete_count() -> None:
    from simple_harness.agents.arp.context.composer import _shrink
    from simple_harness.agents.arp.rules import Group, Selection

    g1 = Group("g1", "t1", 1, 10)
    g2 = Group("g2", "t2", 2, 10)
    selection = Selection(recent=(g1, g2), recalled=(), used=20, input_budget=100, complete_turn_ids=("t1", "t2"), count_complete=True)
    shrunk = _shrink(selection)
    assert shrunk is not None
    assert shrunk.recent == (g2,) and shrunk.used == 10
    assert shrunk.complete_turn_ids == ("t2",)
