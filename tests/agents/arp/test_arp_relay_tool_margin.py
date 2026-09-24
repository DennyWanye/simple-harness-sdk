# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Relay tool-preamble margin (user decision 2026-09-24).

Measured on a relay: whenever tools are present it adds a fixed ~110-token preamble the
official renderer does not have (independent of tool count and parameters).  A relay
deployment therefore counts as an upper bound: exact official count + a learned margin
(floor 160, raised on any uncovered excess, never lowered, persisted); the official endpoint
keeps the exact counter with no margin.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent_orchestrator.runtime.deepseek_meter import (
    CalibratingProvider,
    CertifiedDeepSeekCounter,
    RelayDeepSeekCounter,
    RelayToolMargin,
    certification_ref,
    deepseek_meter_binding,
)
from simple_harness.contracts import Message, MessageRole, RequestId
from simple_harness.providers import ProviderRequest, ProviderResponse, ProviderToolSpec
from simple_harness.providers.base import ProviderUsage

TOKENIZER = Path(os.environ.get("SH_TOKENIZER_PATH") or (Path.home() / "Library/Application Support/deskpet/models/deepseek-v41/tokenizer.json"))
TOOL = ProviderToolSpec("lookup", "查资料", {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]})


def _request(*, tools: bool) -> ProviderRequest:
    return ProviderRequest(RequestId("r"), (Message(MessageRole.USER, "查一下"),), tools=(TOOL,) if tools else (), max_output_tokens=16)


def test_the_margin_starts_at_the_floor_rises_on_an_uncovered_excess_and_never_falls(tmp_path) -> None:
    state = tmp_path / "relay-margin.json"
    margin = RelayToolMargin(state_path=state)
    assert margin.value == 160 and margin.charge(_request(tools=True)) == 160 and margin.charge(_request(tools=False)) == 0
    assert margin.observe(reported=500, counted=390, has_tools=True) is False  # +110 is covered
    assert margin.observe(reported=40, counted=13, has_tools=False) is True  # uncovered even without tools
    assert margin.value == 160  # 27 + 50 < 160: never lowered
    assert margin.observe(reported=700, counted=500, has_tools=True) is True  # +200 > 160
    assert margin.value == 250 and margin.alarms[-1]["excess"] == 200
    assert margin.observe(reported=0, counted=500, has_tools=True) is False  # a zero report teaches nothing
    reopened = RelayToolMargin(state_path=state)
    assert reopened.value == 250 and reopened.max_excess == 200


@pytest.fixture
def counters():  # type: ignore[no-untyped-def]
    pytest.importorskip("deepseek_recipe")
    if not TOKENIZER.is_file():
        pytest.skip("pinned DeepSeek tokenizer not installed on this machine")
    margin = RelayToolMargin()
    exact = CertifiedDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash", thinking="disabled")
    relay = RelayDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash", thinking="disabled", margin=margin)
    return exact, relay, margin


def test_the_relay_counter_is_an_upper_bound_with_a_stable_identity(counters) -> None:  # type: ignore[no-untyped-def]
    exact, relay, margin = counters
    with_tools, without = _request(tools=True), _request(tools=False)
    assert relay.count_request_tokens(with_tools) == exact.count_request_tokens(with_tools) + 160
    assert relay.count_request_tokens(without) == exact.count_request_tokens(without)
    assert relay.count_mode == "CERTIFIED_UPPER_BOUND" and exact.count_mode == "EXACT"
    before = (relay.fingerprint, certification_ref(relay))
    margin.observe(reported=exact.count_request_tokens(with_tools) + 300, counted=exact.count_request_tokens(with_tools), has_tools=True)
    assert relay.count_request_tokens(with_tools) == exact.count_request_tokens(with_tools) + 350
    assert (relay.fingerprint, certification_ref(relay)) == before  # a raised margin never breaks a frozen run
    assert relay.fingerprint != exact.fingerprint and certification_ref(relay) != certification_ref(exact)
    binding = deepseek_meter_binding(relay, input_limit_tokens=8192, max_output_tokens=1024)
    assert binding.count_mode == "CERTIFIED_UPPER_BOUND" and binding.model_limits["counter_id"] == "deepseek-v41-relay-upper-bound"


def test_the_calibrating_provider_learns_from_every_reported_prompt_count(counters) -> None:  # type: ignore[no-untyped-def]
    exact, relay, margin = counters

    class Relay:
        target = "relay-target"
        thinking = "disabled"

        def __init__(self, extra: int) -> None:
            self.extra = extra

        async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
            reported = exact.count_request_tokens(request) + (self.extra if request.tools else 0)
            return ProviderResponse(request.request_id, Message(MessageRole.ASSISTANT, "ok"), usage=ProviderUsage(reported, 1, reported + 1), model="m", finish_reason="stop")

    provider = CalibratingProvider(Relay(110), relay)
    assert provider.target == "relay-target" and provider.thinking == "disabled"
    asyncio.run(provider.invoke(_request(tools=True), cancel=None))
    assert margin.value == 160 and not margin.alarms and margin.max_excess == 110
    asyncio.run(CalibratingProvider(Relay(230), relay).invoke(_request(tools=True), cancel=None))
    assert margin.value == 280 and margin.alarms[-1]["excess"] == 230


def test_the_ordinary_path_plans_the_relay_margin_with_its_tools(counters) -> None:  # type: ignore[no-untyped-def]
    """Review 2026-09-24: the non-ARP Context port planned tools without the margin, so a
    near-full window was refused on every turn by the final re-count."""
    from types import SimpleNamespace

    from simple_harness.agents.context.port import JournalContextPort
    from simple_harness.agents.context.tokenizer import count_tools

    exact, relay, margin = counters
    port = SimpleNamespace(_tokenizer=relay, _tool_specs_for_run=lambda run_id: (TOOL,))
    assert JournalContextPort.tool_tokens(port, "r") == count_tools(relay, (TOOL,)) + margin.value  # type: ignore[arg-type]
    none = SimpleNamespace(_tokenizer=relay, _tool_specs_for_run=lambda run_id: ())
    assert JournalContextPort.tool_tokens(none, "r") == 0  # type: ignore[arg-type]
    plain = SimpleNamespace(_tokenizer=exact, _tool_specs_for_run=lambda run_id: (TOOL,))
    assert JournalContextPort.tool_tokens(plain, "r") == count_tools(exact, (TOOL,))  # type: ignore[arg-type]


def test_the_legacy_counter_keeps_the_released_identity_and_semantics() -> None:
    pytest.importorskip("deepseek_recipe")
    if not TOKENIZER.is_file():
        pytest.skip("pinned DeepSeek tokenizer not installed on this machine")
    from agent_orchestrator.runtime.deepseek_tokens import LegacyPriorOutputDeepSeekCounter

    legacy = LegacyPriorOutputDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash")
    # The fingerprint released on main before 2026-09-24 (reproduced from that source).
    assert legacy.fingerprint == "deepseek-v41:24caaa2f95c620f603f1536676a3566ca4d3aaefbf96e101667fc7bdea9a2d25"
    assert legacy.requires_prior_output_reserve is True and legacy.bound_protocol == "deepseek-v41-chat-text-plus-prior-output-v1"
    enabled = CertifiedDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash", thinking="enabled")
    assert legacy.estimate_input_tokens(_request(tools=True)) == enabled.count_request_tokens(_request(tools=True))  # endpoint-default render


def test_a_legacy_pool_on_a_relay_keeps_its_identity_and_charges_the_margin() -> None:
    pytest.importorskip("deepseek_recipe")
    if not TOKENIZER.is_file():
        pytest.skip("pinned DeepSeek tokenizer not installed on this machine")
    from agent_orchestrator.runtime.deepseek_meter import LegacyRelayDeepSeekCounter
    from agent_orchestrator.runtime.deepseek_tokens import LegacyPriorOutputDeepSeekCounter

    margin = RelayToolMargin()
    legacy = LegacyPriorOutputDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash")
    relay = LegacyRelayDeepSeekCounter(TOKENIZER, model="deepseek-v4.1-flash", margin=margin)
    assert relay.fingerprint == legacy.fingerprint and relay.requires_prior_output_reserve is True
    assert relay.estimate_input_tokens(_request(tools=True)) == legacy.estimate_input_tokens(_request(tools=True)) + 160
    assert relay.estimate_input_tokens(_request(tools=False)) == legacy.estimate_input_tokens(_request(tools=False))
    assert isinstance(CalibratingProvider(object(), relay), CalibratingProvider)
