# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The pinned official DeepSeek V4.1 counter as a *certified* native-plane meter.

``DeepSeekV41TokenEstimator`` counts the rendered wire request with the official
tokenizer and the official request renderer, so for an explicitly bound official
DeepSeek endpoint the count is EXACT.  The native plane refuses any counter without a
declared ``count_mode`` and a certification receipt (``GENERIC_TOKEN_BOUND_UNCERTIFIED``);
this module declares both and binds the deployment's model limits.

The V4.1 endpoint counts prior reported output against the window (thinking tool
continuations), so the limits are certified with ``input_limit_scope=WIRE_PLUS_PRIOR`` and
``requires_prior_output_reserve=True``: the binding needs a real prior reserve reader
(``native_plane.RunPriorReserve``) and never meters without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from simple_harness.agents.arp.meter import MeterBinding, PriorBasis, model_limits
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.strict import digest
from simple_harness.providers import ProviderRequest

from .deepseek_tokens import RECIPE_COMMIT, RECIPE_VERSION, TOKENIZER_SHA256, DeepSeekV41TokenEstimator

COUNTER_ID = "deepseek-v41-official-exact"
COUNTER_REVISION = 1
CERTIFICATION_RULE = (
    "the official DeepSeek V4.1 tokenizer and the official chat request renderer count the "
    "rendered wire request; prior reported output tokens are reserved separately"
)


class CertifiedDeepSeekCounter(DeepSeekV41TokenEstimator):
    """The official counter declaring its EXACT mode for the native meter."""

    count_mode = "EXACT"

    def __init__(self, tokenizer_path: Path, *, model: str = "deepseek-flash", tool_schema_mode: str = "legacy") -> None:
        super().__init__(tokenizer_path, model=model, tool_schema_mode=tool_schema_mode)
        self.model = model

    def count_request_tokens(self, request: ProviderRequest) -> int:
        return self.estimate_input_tokens(request)


def certification_ref(counter: CertifiedDeepSeekCounter) -> Pin:
    body = {
        "rule": CERTIFICATION_RULE,
        "counter_id": COUNTER_ID,
        "counter_revision": COUNTER_REVISION,
        "tokenizer_fingerprint": counter.fingerprint,
        "recipe_version": RECIPE_VERSION,
        "recipe_commit": RECIPE_COMMIT,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "count_mode": counter.count_mode,
    }
    return Pin("receipt", f"meter-certification:{COUNTER_ID}", 0, digest(body))


def deepseek_meter_binding(
    counter: CertifiedDeepSeekCounter,
    *,
    input_limit_tokens: int,
    max_output_tokens: int,
    prior_reserve: Callable[[str], PriorBasis],
    combined_limit_tokens: int | None = None,
    capability_receipt: dict[str, Any] | None = None,
) -> MeterBinding:
    """Bind the certified counter to the deployment's model limits and prior reserve reader."""

    if not isinstance(counter, CertifiedDeepSeekCounter):
        raise TypeError("deepseek_meter_binding needs the certified DeepSeek counter")
    limits = model_limits(
        model=counter.model,
        tokenizer=counter,
        input_limit_tokens=input_limit_tokens,
        max_output_tokens=max_output_tokens,
        combined_limit_tokens=combined_limit_tokens,
        provider_id="deepseek-official",
        counter_id=COUNTER_ID,
        counter_revision=COUNTER_REVISION,
        input_limit_scope="WIRE_PLUS_PRIOR",
        requires_prior_output_reserve=True,
        capability_receipt=capability_receipt or {
            "kind": "model-capability",
            "model": counter.model,
            "input_limit_tokens": input_limit_tokens,
            "max_output_tokens": max_output_tokens,
            "combined_limit_tokens": combined_limit_tokens,
        },
    )
    return MeterBinding(tokenizer=counter, model_limits=limits, certification_ref=certification_ref(counter), prior_reserve=prior_reserve)


__all__ = ("COUNTER_ID", "CertifiedDeepSeekCounter", "certification_ref", "deepseek_meter_binding")
