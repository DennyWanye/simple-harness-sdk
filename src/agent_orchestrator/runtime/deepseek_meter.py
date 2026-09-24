# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The pinned official DeepSeek V4.1 counter as a *certified* native-plane meter.

``DeepSeekV41TokenEstimator`` counts the rendered wire request with the official
tokenizer and the official request renderer, so for an explicitly bound official
DeepSeek endpoint the count is EXACT.  The native plane refuses any counter without a
declared ``count_mode`` and a certification receipt (``GENERIC_TOKEN_BOUND_UNCERTIFIED``);
this module declares both and binds the deployment's model limits.

The endpoint is stateless: its context is exactly the request on the wire.  The official
thinking-mode guide (``CONTEXT_SCOPE_SOURCE``) has the *client* pass prior
``reasoning_content`` back inside the next request and retains nothing server-side, so the
limits are certified ``input_limit_scope=WIRE_ONLY`` with no prior-output reserve.  Revision 1
certified WIRE_PLUS_PRIOR on an unverified assumption; that reserve double-charged every
earlier output of the run and shrank the window monotonically (RP-E4 real-model finding).
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
COUNTER_REVISION = 2
CONTEXT_SCOPE_SOURCE = "https://api-docs.deepseek.com/guides/thinking_mode"
CERTIFICATION_RULE = (
    "the official DeepSeek V4.1 tokenizer and the official chat request renderer count the "
    "rendered wire request, which is the stateless endpoint's whole context (prior reasoning "
    "is passed back by the client); no prior-output reserve"
)


class CertifiedDeepSeekCounter(DeepSeekV41TokenEstimator):
    """The official counter declaring its EXACT mode for the native meter."""

    count_mode = "EXACT"

    def __init__(
        self, tokenizer_path: Path, *, model: str = "deepseek-flash", tool_schema_mode: str = "legacy",
        thinking: str | None = None, reasoning_effort: str | None = None,
    ) -> None:
        # DeepSeek thinks by default; an unstated mode would be rendered as thinking while
        # nothing replays the reasoning.  The certified counter is bound to an explicit mode,
        # the same one the adapter sends (review 2026-09-24).
        if thinking not in ("enabled", "disabled"):
            raise ValueError("the certified DeepSeek counter needs an explicit thinking mode: 'enabled' or 'disabled'")
        super().__init__(tokenizer_path, model=model, tool_schema_mode=tool_schema_mode, thinking=thinking, reasoning_effort=reasoning_effort)
        self.model = model
        self.thinking = thinking

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
        "input_limit_scope": "WIRE_ONLY",
        "context_scope_source": CONTEXT_SCOPE_SOURCE,
    }
    return Pin("receipt", f"meter-certification:{COUNTER_ID}", 0, digest(body))


def deepseek_meter_binding(
    counter: CertifiedDeepSeekCounter,
    *,
    input_limit_tokens: int,
    max_output_tokens: int,
    prior_reserve: Callable[[str], PriorBasis] | None = None,
    combined_limit_tokens: int | None = None,
    capability_receipt: dict[str, Any] | None = None,
) -> MeterBinding:
    """Bind the certified counter to the deployment's model limits (wire-only scope).

    A prior reserve reader is refused: the endpoint keeps no output beyond the wire, so a
    reserve would charge output that the exact wire count already covers.
    """

    if not isinstance(counter, CertifiedDeepSeekCounter):
        raise TypeError("deepseek_meter_binding needs the certified DeepSeek counter")
    if prior_reserve is not None:
        raise TypeError("the DeepSeek endpoint keeps no output beyond the wire; a prior reserve reader would double-charge it")
    limits = model_limits(
        model=counter.model,
        tokenizer=counter,
        input_limit_tokens=input_limit_tokens,
        max_output_tokens=max_output_tokens,
        combined_limit_tokens=combined_limit_tokens,
        provider_id="deepseek-official",
        counter_id=COUNTER_ID,
        counter_revision=COUNTER_REVISION,
        input_limit_scope="WIRE_ONLY",
        requires_prior_output_reserve=False,
        capability_receipt=capability_receipt or {
            "kind": "model-capability",
            "model": counter.model,
            "input_limit_tokens": input_limit_tokens,
            "max_output_tokens": max_output_tokens,
            "combined_limit_tokens": combined_limit_tokens,
        },
    )
    return MeterBinding(tokenizer=counter, model_limits=limits, certification_ref=certification_ref(counter))


__all__ = ("CONTEXT_SCOPE_SOURCE", "COUNTER_ID", "CertifiedDeepSeekCounter", "certification_ref", "deepseek_meter_binding")
