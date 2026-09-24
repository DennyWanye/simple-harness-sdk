# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E3b: the official DeepSeek V4.1 counter as a certified EXACT native meter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_orchestrator.runtime.deepseek_meter import COUNTER_ID, CertifiedDeepSeekCounter, certification_ref, deepseek_meter_binding
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.meter import NO_PRIOR, MeterBinding, NativeMeterAdapter, PriorBasis, model_limits
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.rules import Budget
from simple_harness.contracts import Message, MessageRole, RequestId
from simple_harness.providers import ProviderRequest

DEFAULT_TOKENIZER = Path.home() / "Library/Application Support/deskpet/models/deepseek-v41/tokenizer.json"


@pytest.fixture
def counter() -> CertifiedDeepSeekCounter:
    pytest.importorskip("deepseek_recipe")
    path = Path(os.environ.get("SH_TOKENIZER_PATH") or DEFAULT_TOKENIZER)
    if not path.is_file():
        pytest.skip("pinned DeepSeek tokenizer not installed on this machine")
    return CertifiedDeepSeekCounter(path)


def test_the_uncertified_official_counter_is_refused_and_the_certified_one_is_exact(counter: CertifiedDeepSeekCounter) -> None:
    from agent_orchestrator.runtime.deepseek_tokens import DeepSeekV41TokenEstimator

    plain = DeepSeekV41TokenEstimator(Path(os.environ.get("SH_TOKENIZER_PATH") or DEFAULT_TOKENIZER))
    with pytest.raises(ArpError) as refused:
        model_limits(model="deepseek-flash", tokenizer=plain, input_limit_tokens=1000, max_output_tokens=100)
    assert refused.value.code == "GENERIC_TOKEN_BOUND_UNCERTIFIED"
    assert counter.count_mode == "EXACT" and counter.fingerprint == plain.fingerprint
    assert counter.requires_prior_output_reserve is False and counter.bound_protocol == "deepseek-v41-chat-text-wire-only-v2"
    binding = deepseek_meter_binding(counter, input_limit_tokens=262_144, max_output_tokens=32_768)
    assert isinstance(binding, MeterBinding)
    # Wire-only (2026-09-24): the stateless endpoint's context is the request on the wire;
    # prior reasoning is passed back by the client (official thinking-mode guide), never kept.
    assert binding.count_mode == "EXACT" and binding.input_scope == "WIRE_ONLY"
    assert binding.model_limits["requires_prior_output_reserve"] is False and binding.prior_reserve is None
    assert binding.model_limits["counter_id"] == COUNTER_ID and binding.model_limits["counter_revision"] == 2
    assert binding.certification_ref == certification_ref(counter) and binding.certification_ref.kind == "receipt"
    # A prior reserve reader is refused: it would charge output the wire count already covers.
    with pytest.raises(TypeError):
        deepseek_meter_binding(counter, input_limit_tokens=262_144, max_output_tokens=32_768, prior_reserve=lambda run_id: PriorBasis(7, Pin("receipt", "prior-output:r1", 0, "a" * 64)))
    request = ProviderRequest(
        request_id=RequestId("req-1"),
        messages=[Message(role=MessageRole.SYSTEM, content="你是助手。"), Message(role=MessageRole.USER, content="蓝鲸有多长？")],
        tools=[], max_output_tokens=64,
    )
    adapter = NativeMeterAdapter(binding)
    wire = adapter.count_wire(request)
    assert wire == counter.estimate_input_tokens(request) > 0
    measurement = adapter.measure(request, run_id="r1", max_input_budget=100_000, requested_output_tokens=64)
    assert measurement.receipt["wire_input_tokens"] == wire and measurement.receipt["prior_output_reserve_tokens"] == 0
    assert measurement.receipt["count_mode"] == "EXACT" and measurement.receipt["input_limit_scope"] == "WIRE_ONLY"
    assert binding.prior_for("r1") is NO_PRIOR and NO_PRIOR.tokens == 0


def test_the_window_never_shrinks_with_the_runs_earlier_calls(counter: CertifiedDeepSeekCounter) -> None:
    """RP-E4 real-model finding: under the old WIRE_PLUS_PRIOR certification every earlier
    output (and every failed call's 1024 cap) was subtracted from the whole window, which
    fell from ~4800 to ~1700 in one game.  The wire-only binding meters every request of a
    run with the same capacity, whatever the run's history."""

    binding = deepseek_meter_binding(counter, input_limit_tokens=6144, max_output_tokens=1024)

    def capacity(run_id: str) -> int:
        prior = binding.prior_for(run_id)
        return Budget(configured_total=6144, model_total=binding.model_limits["combined_limit_tokens"], model_input=6144, model_output=1024,
                      output=1024, safety=64, headroom=256, recent_floor=768, recall_ceiling=1536, prior=prior.tokens, input_scope=binding.input_scope).capacity()

    assert capacity("fresh-run") == capacity("run-after-three-failed-calls") == 6144 - 1024 - 64 - 256


def test_the_binding_refuses_a_counter_that_is_not_the_certified_one() -> None:
    class Impostor:
        fingerprint = "x"
        count_mode = "EXACT"
        model = "deepseek-flash"

    with pytest.raises(TypeError):
        deepseek_meter_binding(Impostor(), input_limit_tokens=10, max_output_tokens=1)  # type: ignore[arg-type]
