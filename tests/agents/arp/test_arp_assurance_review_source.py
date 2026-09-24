# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""An Assurance review turn on the native plane can prove what the reviewer saw.

Host native run (2026-09-24, host-final-arp10): the review turn succeeded but its import
failed EXPOSURE_UNAVAILABLE — the exposure reader only knew the legacy context-selection
row, which the native plane never writes.  The native plane's own proof is the frozen
manifest (``arp_context_requests``): re-rendering it must reproduce the planned wire hash,
and only journal messages whose exact bytes are in that wire count as exposure.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.runtime.assurance_turn_sources import _exposure
from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.contracts import AgentTurnState

CONFIG = AgentConfig(name="w", instructions="你是审阅者。", model_profile_ref="p", tool_names=("echo",))


def _review(tmp_path, script, *, tamper=None):  # type: ignore[no-untyped-def]
    async def case():  # type: ignore[no-untyped-def]
        runtime = build(tmp_path, ScriptedProvider(script), tool_executor=EchoToolExecutor(), tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA})
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="review", caller=trusted_caller())
            receipt = await agent.submit("请审阅：报告写了三个要点", input_id="i1")
            result = await agent.wait_turn(receipt.turn_id, timeout=10)
            assert result.state is AgentTurnState.COMMITTED, result.error
            uow = runtime.uow
            binding = uow.read_agent_binding(agent.agent_id)
            turn = uow.read_agent_turn(receipt.turn_id)
            if tamper is not None:
                tamper(uow.database.connection)
            exposure = _exposure(uow, binding.run_id, turn, result, native_context=runtime.arp.context)
            frozen = store.read_context_by_request_key(uow.database.connection, exposure["provider_request_id"])
            return exposure, frozen

    return asyncio.run(case())


def test_single_request_review_exposes_the_user_input(tmp_path) -> None:
    exposure, _ = _review(tmp_path, ["三个要点都在，通过。"])
    texts = [m["message"].get("content") for m in exposure["messages"]]
    assert "请审阅：报告写了三个要点" in texts
    assert exposure["wire_input_hash"] and exposure["selection_id"].startswith("ctx-")
    assert exposure["provider_input_hash"]


def test_tool_turn_review_proves_the_actual_wire_including_the_tool_result(tmp_path) -> None:
    exposure, frozen = _review(tmp_path, [("echo", {}), "工具看过了，通过。"])
    kinds = [m["kind"] for m in exposure["messages"]]
    assert any("tool" in str(k).lower() for k in kinds), kinds
    assert exposure["wire_input_hash"] == frozen.planned_request_hash
    assert exposure["source_highwater"] == frozen.journal_highwater


def test_a_manifest_that_no_longer_reproduces_its_hash_is_never_accepted(tmp_path) -> None:
    def tamper(connection):  # type: ignore[no-untyped-def]
        connection.execute("DROP TRIGGER IF EXISTS arp_context_requests_immutable")
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='arp_context_requests'").fetchall():
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("UPDATE arp_context_requests SET planned_request_hash=?", ("0" * 64,))

    with pytest.raises(AssuranceError) as info:
        _review(tmp_path, [("echo", {}), "通过。"], tamper=tamper)
    assert info.value.code == "REVIEW_PROVIDER_INPUT_MISMATCH"
