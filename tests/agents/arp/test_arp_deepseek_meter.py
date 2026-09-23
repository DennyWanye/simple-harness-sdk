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
    prior_calls: list[str] = []

    def prior(run_id: str) -> PriorBasis:
        prior_calls.append(run_id)
        return PriorBasis(7, Pin("receipt", "prior-output:r1", 0, "a" * 64))

    binding = deepseek_meter_binding(counter, input_limit_tokens=262_144, max_output_tokens=32_768, prior_reserve=prior)
    assert isinstance(binding, MeterBinding)
    assert binding.count_mode == "EXACT" and binding.input_scope == "WIRE_PLUS_PRIOR"
    assert binding.model_limits["requires_prior_output_reserve"] is True
    assert binding.model_limits["counter_id"] == COUNTER_ID
    assert binding.certification_ref == certification_ref(counter) and binding.certification_ref.kind == "receipt"
    request = ProviderRequest(
        request_id=RequestId("req-1"),
        messages=[Message(role=MessageRole.SYSTEM, content="你是助手。"), Message(role=MessageRole.USER, content="蓝鲸有多长？")],
        tools=[], max_output_tokens=64,
    )
    adapter = NativeMeterAdapter(binding)
    wire = adapter.count_wire(request)
    assert wire == counter.estimate_input_tokens(request) > 0
    measurement = adapter.measure(request, run_id="r1", max_input_budget=100_000, requested_output_tokens=64)
    assert prior_calls == ["r1"]
    assert measurement.receipt["wire_input_tokens"] == wire and measurement.receipt["prior_output_reserve_tokens"] == 7
    assert measurement.receipt["count_mode"] == "EXACT" and measurement.receipt["input_limit_scope"] == "WIRE_PLUS_PRIOR"
    # Without a prior reserve reader the binding is honest: it cannot meter this deployment.
    without = MeterBinding(tokenizer=counter, model_limits=binding.model_limits, certification_ref=binding.certification_ref)
    with pytest.raises(ArpError) as missing:
        NativeMeterAdapter(without).measure(request, run_id="r1", max_input_budget=100_000, requested_output_tokens=64)
    assert missing.value.code == "PRIOR_RESERVE_UNAVAILABLE"
    assert NO_PRIOR.tokens == 0


def test_the_binding_refuses_a_counter_that_is_not_the_certified_one() -> None:
    class Impostor:
        fingerprint = "x"
        count_mode = "EXACT"
        model = "deepseek-flash"

    with pytest.raises(TypeError):
        deepseek_meter_binding(Impostor(), input_limit_tokens=10, max_output_tokens=1, prior_reserve=lambda run_id: NO_PRIOR)  # type: ignore[arg-type]
