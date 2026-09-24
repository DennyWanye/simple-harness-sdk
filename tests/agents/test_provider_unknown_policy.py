# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""An unknowable provider outcome ends the turn at once under ``provider_unknown="fail_turn"``.

host-final-arp10 (2026-09-24): the relay dropped a stream after hand-off; with nothing able
to reconcile a chat completion the Agent waited ~17 minutes for the wall clock.  The
orchestrator re-dispatches failed turns, so its pools fail the turn now; the call itself
stays UNKNOWN (never released, never re-sent).  The default stays "wait".
"""

from __future__ import annotations

import asyncio
import time

import pytest
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId

CONFIG = AgentConfig(name="w", instructions="Answer briefly.", model_profile_ref="p")


class BrokenWire(ScriptedProvider):
    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        raise RuntimeError("peer closed connection without sending complete message body")


def _ask(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    async def case():  # type: ignore[no-untyped-def]
        provider = BrokenWire([])
        ports = AgentRuntimePorts(provider=provider, authorization=AllowAllAuthorization(), database_path=str(tmp_path / "x.db"), **overrides)
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(CONFIG, creation_key="k")
            receipt = await agent.submit("hello", input_id="i1")
            started = time.monotonic()
            try:
                result = await agent.wait_turn(receipt.turn_id, timeout=3)
            except Exception as error:  # noqa: BLE001 - "wait" never ends the turn
                result = error
            states = [str(r.state) for r in runtime.uow.list_provider_invocations(RunId(agent.run_id))]
            return result, states, time.monotonic() - started, provider.calls

    return asyncio.run(case())


def test_fail_turn_ends_the_turn_now_and_keeps_the_call_unknown(tmp_path) -> None:
    result, states, took, calls = _ask(tmp_path, provider_unknown="fail_turn")
    assert result.state is AgentTurnState.FAILED
    assert result.error["error_code"] == "provider_outcome_unknown" and result.error["retryable"] is True
    assert states == ["unknown"] and calls == 1 and took < 3


def test_default_wait_keeps_the_historical_behaviour(tmp_path) -> None:
    result, states, _took, calls = _ask(tmp_path)
    assert not hasattr(result, "state") or result.state is not AgentTurnState.FAILED
    assert states == ["unknown"] and calls == 1


def test_only_known_policies_are_accepted(tmp_path) -> None:
    with pytest.raises(ValueError):
        AgentRuntimePorts(provider=BrokenWire([]), authorization=AllowAllAuthorization(), database_path=str(tmp_path / "y.db"), provider_unknown="retry")
