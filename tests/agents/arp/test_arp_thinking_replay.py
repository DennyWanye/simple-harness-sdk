# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Thinking mode end to end (user decision 2026-09-24: support both modes).

Enabled (REASONING_REPLAY): each turn's private reasoning is kept in the durable response's
private continuation, never in the Journal, and is replayed on *every* earlier assistant
message of later requests (DeepSeek rejects a tool loop with any reasoning missing); the
replayed reasoning is counted.  Disabled: nothing is kept, nothing is replayed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from arp_fixture import ExactWordTokenizer, MODEL, RecordingAuthorization, activation_receipt, standalone_profile, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness import Message, MessageRole
from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.meter import MeterBinding, model_limits
from simple_harness.agents.arp.pins import Pin, digest
from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root
from simple_harness.agents.arp.runtime import build_arp_runtime
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.agents.wire import restore_reasoning, restore_wire_messages
from simple_harness.contracts import CallId, RequestId, thaw_json
from simple_harness.execution.provider_invocations import provider_response_from_json, provider_response_json
from simple_harness.providers import ProviderResponse
from simple_harness.providers.base import (
    PROVIDER_REASONING_KEY,
    ProviderContinuationCapability,
    ProviderContinuationMode,
    ProviderUsage,
)

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
REPLAY = ProviderContinuationCapability(ProviderContinuationMode.REASONING_REPLAY)


def _reasoning(n: int) -> str:
    return f"私有 推理 第{n}轮 先 想 一 想 再 回答"  # nine words


class ThinkingProvider(ScriptedProvider):
    """A thinking-mode provider: returns private reasoning alongside each answer."""

    def __init__(self, script, *, replay: bool) -> None:  # type: ignore[no-untyped-def]
        super().__init__(script)
        self.replay = replay

    @property
    def continuation_capability(self) -> ProviderContinuationCapability:
        return REPLAY if self.replay else ProviderContinuationCapability()

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        response = await super().invoke(request, cancel=cancel)
        n = len(self.requests)
        return replace(response, reasoning_content=_reasoning(n), usage=ProviderUsage(10 * n, 7 * n, 17 * n))


def _build(tmp_path, provider):  # type: ignore[no-untyped-def]
    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    tokenizer = ExactWordTokenizer()
    limits = model_limits(model=MODEL, tokenizer=tokenizer, input_limit_tokens=8192, max_output_tokens=1024, provider_id="scripted-test")
    meter = MeterBinding(
        tokenizer=tokenizer, model_limits=limits,
        certification_ref=Pin("receipt", "meter-certification:test-exact-words", 0, digest({"rule": "one token per word"})),
    )
    ports = AgentRuntimePorts(
        provider=provider, authorization=RecordingAuthorization(), database_path=str(tmp_path / "runtime.db"),
        model=MODEL, owner_id="arp-test-owner", tokenizer=tokenizer, default_max_output_tokens=256, max_output_tokens_ceiling=1024,
    )
    arp = ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt(), meter=meter)
    return build_arp_runtime(ports, arp)


def _run(tmp_path, *, replay: bool):  # type: ignore[no-untyped-def]
    provider = ThinkingProvider(["one", "two", "three"], replay=replay)
    runtime = _build(tmp_path, provider)
    facts: dict = {}

    async def scenario():  # type: ignore[no-untyped-def]
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="a0", caller=trusted_caller())
            for key, text in (("t1", "hello"), ("t2", "again"), ("t3", "third")):
                result = await agent.wait_turn((await agent.submit(text, input_id=key)).turn_id, timeout=10)
                assert result.state is AgentTurnState.COMMITTED, result.error
            conn = runtime.uow.database.connection
            facts["journal"] = " ".join(str(row[0]) for row in conn.execute("SELECT message_json FROM base_agent_session_journal_v1"))
            facts["responses"] = [json.loads(row[0]) for row in conn.execute("SELECT response_json FROM provider_invocations ORDER BY claimed_at")]
            key = conn.execute("SELECT original_request_key FROM arp_context_requests ORDER BY created_at_ms DESC LIMIT 1").fetchone()[0]
            facts["wire_tokens"] = int(store.read_context_by_request_key(conn, str(key)).manifest["token_receipt"]["wire_input_tokens"]) if "token_receipt" in store.read_context_by_request_key(conn, str(key)).manifest else None
            facts["manifest"] = dict(store.read_context_by_request_key(conn, str(key)).manifest)

    asyncio.run(scenario())
    return provider, facts


def _wire_tokens(manifest) -> int:  # type: ignore[no-untyped-def]
    found = []

    def walk(node):  # type: ignore[no-untyped-def]
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "wire_input_tokens" and type(v) is int:
                    found.append(v)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(json.loads(json.dumps(manifest, default=lambda o: dict(o) if hasattr(o, "items") else list(o))))
    assert found
    return found[0]


def test_enabled_keeps_reasoning_private_and_replays_it_on_every_earlier_assistant_turn(tmp_path) -> None:
    provider, facts = _run(tmp_path / "on", replay=True)
    third = provider.requests[2]
    assistants = [m for m in third.messages if m.role is MessageRole.ASSISTANT]
    assert [m.content for m in assistants] == ["one", "two"]
    assert [m.metadata.get(PROVIDER_REASONING_KEY) for m in assistants] == [_reasoning(1), _reasoning(2)]
    # Durable: the private continuation (schema 2) holds it; the public message never does.
    assert [r["continuation"]["schema_version"] for r in facts["responses"]] == [2, 2, 2]
    assert [r["continuation"]["reasoning_content"] for r in facts["responses"]] == [_reasoning(1), _reasoning(2), _reasoning(3)]
    assert all(r["message"]["metadata"] == {} for r in facts["responses"])
    assert "私有" not in facts["journal"]  # never Context, so never recalled or cited

    _, off = _run(tmp_path / "off", replay=False)
    # The replayed reasoning is metered: nine words for each of the two earlier turns.
    assert _wire_tokens(facts["manifest"]) - _wire_tokens(off["manifest"]) == 2 * 9


def test_disabled_keeps_and_replays_nothing(tmp_path) -> None:
    provider, facts = _run(tmp_path, replay=False)
    third = provider.requests[2]
    assert all(PROVIDER_REASONING_KEY not in m.metadata for m in third.messages)
    assert [r["continuation"]["schema_version"] for r in facts["responses"]] == [1, 1, 1]
    assert all("reasoning_content" not in r["continuation"] for r in facts["responses"])


def test_every_assistant_message_gets_a_reasoning_even_when_none_is_on_record() -> None:
    stamped = Message(MessageRole.ASSISTANT, "", metadata={"provider_turn_ordinal": 2})
    unknown = Message(MessageRole.ASSISTANT, "old", metadata={"provider_turn_ordinal": 9})
    unstamped = Message(MessageRole.ASSISTANT, "legacy")
    user = Message(MessageRole.USER, "hi")
    restored = restore_reasoning((user, stamped, unknown, unstamped), {2: "我要查一下"})
    assert PROVIDER_REASONING_KEY not in restored[0].metadata
    assert [m.metadata[PROVIDER_REASONING_KEY] for m in restored[1:]] == ["我要查一下", "", ""]


def test_replayed_reasoning_survives_the_tool_call_restore() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE execution_effects (run_id TEXT, raw_call_id TEXT, turn_ordinal INTEGER, call_ordinal INTEGER, tool_name TEXT, arguments_json TEXT)")
    conn.execute("CREATE TABLE provider_invocations (invocation_id TEXT, run_id TEXT, request_id TEXT, state TEXT, response_json TEXT, settled_at REAL)")
    conn.execute("INSERT INTO execution_effects VALUES ('r', 'c1', 1, 1, 'lookup', '{\"q\": \"x\"}')")
    conn.execute("INSERT INTO provider_invocations VALUES ('i1', 'r', 'r:provider-turn:1', 'succeeded', ?, 1.0)",
                 (json.dumps({"continuation": {"schema_version": 2, "reasoning_content": "先查资料"}}),))
    messages = (
        Message(MessageRole.USER, "查一下"),
        Message(MessageRole.ASSISTANT, "", metadata={"provider_turn_ordinal": 1}),
        Message(MessageRole.TOOL, "结果", call_id=CallId("c1"), name="lookup"),
    )
    restored, fallbacks = restore_wire_messages(messages, conn, "r", replay_reasoning=True)
    assistant = restored[1]
    assert fallbacks == 0
    assert assistant.metadata[PROVIDER_REASONING_KEY] == "先查资料"
    assert thaw_json(assistant.metadata["provider_tool_calls"]) == [{"id": "c1", "name": "lookup", "arguments": {"q": "x"}}]
    plain, _ = restore_wire_messages(messages, conn, "r", replay_reasoning=False)
    assert PROVIDER_REASONING_KEY not in plain[1].metadata


def test_durable_private_reasoning_is_mode_bound() -> None:
    response = ProviderResponse(RequestId("q"), Message(MessageRole.ASSISTANT, "ok"), model=MODEL, finish_reason="stop", reasoning_content="想一想")
    stored = provider_response_json(response, capability=REPLAY)
    assert stored["continuation"]["schema_version"] == 2 and stored["continuation"]["reasoning_content"] == "想一想"
    assert provider_response_from_json(stored, expected_capability=REPLAY).reasoning_content == "想一想"
    plain = provider_response_json(response)
    assert plain["continuation"]["schema_version"] == 1 and "reasoning_content" not in plain["continuation"]
    assert provider_response_from_json(plain).reasoning_content is None
    forged = json.loads(json.dumps(plain))
    forged["continuation"].update(schema_version=2, reasoning_content="偷偷塞进去")
    with pytest.raises(ValueError):
        provider_response_from_json(forged)
