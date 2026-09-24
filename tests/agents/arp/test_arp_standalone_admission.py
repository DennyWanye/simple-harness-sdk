# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E4: a standalone ARP runtime (no orchestrator admission) freezes the context
request *before* the physical handoff, so a prior-reserve reader never sees the
request's own invocation as an unresolved prior call."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from arp_fixture import ExactWordTokenizer, MODEL, RecordingAuthorization, activation_receipt, standalone_profile, trusted_caller
from provider_fixture import ScriptedProvider

from agent_orchestrator.runtime.native_plane import RunPriorReserve
from simple_harness.agents import AgentConfig
from simple_harness.agents.arp.meter import MeterBinding, model_limits
from simple_harness.agents.arp.pins import Pin, digest
from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root
from simple_harness.agents.arp.runtime import build_arp_runtime
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness import Message, MessageRole
from simple_harness.agents.arp import store
from simple_harness.providers import ProviderResponse
from simple_harness.providers.base import ProviderUsage

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


class UsageProvider(ScriptedProvider):
    """Scripted answers that report real usage, like a gateway does."""

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        response = await super().invoke(request, cancel=cancel)
        n = len(self.requests)
        return replace(response, usage=ProviderUsage(input_tokens=10 * n, output_tokens=7 * n, total_tokens=17 * n))


def _build(tmp_path, provider=None):  # type: ignore[no-untyped-def]
    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    tokenizer = ExactWordTokenizer()
    reader = RunPriorReserve()
    limits = model_limits(
        model=MODEL, tokenizer=tokenizer, input_limit_tokens=8192, max_output_tokens=1024, provider_id="scripted-test",
        input_limit_scope="WIRE_PLUS_PRIOR", requires_prior_output_reserve=True,
    )
    meter = MeterBinding(
        tokenizer=tokenizer, model_limits=limits, prior_reserve=reader,
        certification_ref=Pin("receipt", "meter-certification:test-exact-words", 0, digest({"rule": "one token per word"})),
    )
    ports = AgentRuntimePorts(
        provider=provider or UsageProvider(["one", "two"]), authorization=RecordingAuthorization(), database_path=str(tmp_path / "runtime.db"),
        model=MODEL, owner_id="arp-test-owner", tokenizer=tokenizer, default_max_output_tokens=256, max_output_tokens_ceiling=1024,
    )
    arp = ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt(), meter=meter)
    runtime = build_arp_runtime(ports, arp)
    reader.bind(runtime)
    return runtime, reader


def test_standalone_runtime_freezes_context_before_handoff_and_sums_prior_output(tmp_path) -> None:
    runtime, reader = _build(tmp_path)

    async def scenario():  # type: ignore[no-untyped-def]
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="a0", caller=trusted_caller())
            first = await agent.wait_turn((await agent.submit("hello", input_id="t1")).turn_id, timeout=10)
            assert first.state is AgentTurnState.COMMITTED, first.error
            second = await agent.wait_turn((await agent.submit("again", input_id="t2")).turn_id, timeout=10)
            assert second.state is AgentTurnState.COMMITTED, second.error
            conn = runtime.uow.database.connection
            invocations = conn.execute("SELECT state, handed_off_at FROM provider_invocations ORDER BY handed_off_at").fetchall()
            requests = conn.execute("SELECT created_at_ms FROM arp_context_requests ORDER BY created_at_ms").fetchall()
            assert [row[0] for row in invocations] == ["succeeded", "succeeded"] and len(requests) == 2
            # Each context request was frozen no later than its invocation's physical handoff.
            for (created_ms,), (_, handed_off_at) in zip(requests, invocations):
                assert created_ms <= int(handed_off_at * 1000) + 1
            # The prior reserve after two calls is their recorded output, by name — the second
            # request was metered with the first call's 7 while its own call was still ``claimed``.
            basis = reader(str(agent.run_id))
            assert basis.tokens == 7 + 14 and basis.basis_ref is not None and basis.basis_ref.id == f"prior-output:{agent.run_id}"

    asyncio.run(scenario())


class EmptyThenUsage(UsageProvider):
    """The gateway's failure mode: first an empty completion with no usage, then normal replies."""

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        if not self.requests:
            self.requests.append(request)
            return ProviderResponse(request.request_id, Message(MessageRole.ASSISTANT, ""), model=MODEL, finish_reason="stop")
        return await super().invoke(request, cancel=cancel)


def test_one_empty_reply_does_not_freeze_the_agent(tmp_path) -> None:
    """RP-E4 (real model): an empty gateway reply used to make every later request of the
    Agent PRIOR_RESERVE_UNAVAILABLE.  Its own output cap is now reserved instead."""
    runtime, _reader = _build(tmp_path, EmptyThenUsage(["after"]))

    async def scenario():  # type: ignore[no-untyped-def]
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="a0", caller=trusted_caller())
            first = await agent.wait_turn((await agent.submit("hello", input_id="t1")).turn_id, timeout=10)
            assert first.state is AgentTurnState.FAILED and first.error["error_code"] == "provider_empty_response"
            second = await agent.wait_turn((await agent.submit("again", input_id="t2")).turn_id, timeout=10)
            assert second.state is AgentTurnState.COMMITTED, second.error
            conn = runtime.uow.database.connection
            key = conn.execute("SELECT original_request_key FROM arp_context_requests ORDER BY created_at_ms DESC LIMIT 1").fetchone()[0]
            manifest = store.read_context_by_request_key(conn, str(key)).manifest
            # The failed call's own output cap (the runtime default, 256) is reserved by name.
            assert manifest["prior_output_reserve_tokens"] == 256

    asyncio.run(scenario())
