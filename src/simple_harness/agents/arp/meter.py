# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``NativeMeterAdapter``: real token metering of the final wire request (§4, INTERFACES §5).

The adapter never guesses.  A ``MeterBinding`` names the deployment's counter
(``TokenizerPort``), its certified count mode, the renderer / serializer it was
certified against and the model limits.  ``UpperBoundTokenizer`` (bytes / 2) has no
certification and is refused with ``GENERIC_TOKEN_BOUND_UNCERTIFIED``; a counter
that declares neither ``count_mode`` nor a certification receipt is refused the
same way.  Only text + tool JSON schemas are metered (``coverage=TEXT_TOOLS_JSON``);
any non-text content block is ``MEDIA_UNSUPPORTED``.

``P`` (prior output the server still counts) comes from the binding's
``prior_reserve`` reader, decomposed once; the adapter never derives it from an
aggregate estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from simple_harness.contracts.messages import Message
from simple_harness.execution.provider_invocations import provider_request_fingerprint
from simple_harness.providers import ProviderRequest

from ..context.tokenizer import (
    MESSAGE_OVERHEAD_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    TOOL_OVERHEAD_TOKENS,
    TokenizerPort,
    count_message,
    count_tools,
)
from .codec import check
from .errors import ArpError
from .pins import Pin
from .strict import digest

COUNT_MODES = ("EXACT", "CERTIFIED_UPPER_BOUND")
INPUT_SCOPES = ("WIRE_ONLY", "WIRE_PLUS_PRIOR")

#: The renderer this SDK's ``count_message`` / ``count_tools`` implement: role
#: framing plus per-message / per-tool / per-request overheads.
RENDERER_ID = {
    "renderer": "simple_harness.agents.context.tokenizer:v1",
    "message_overhead": MESSAGE_OVERHEAD_TOKENS,
    "tool_overhead": TOOL_OVERHEAD_TOKENS,
    "request_overhead": REQUEST_OVERHEAD_TOKENS,
}
SERIALIZER_ID = {"serializer": "simple_harness.execution.provider_invocations.provider_request_json:v1"}
RENDERER_HASH = digest(RENDERER_ID)
SERIALIZER_HASH = digest(SERIALIZER_ID)


def _non_text_blocks(message: Message) -> bool:
    if isinstance(message.content, str):
        return False
    for block in message.content:
        kind = getattr(block, "type", None) or getattr(block, "kind", None)
        if kind not in (None, "text"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class PriorBasis:
    """``P`` and the receipt it was read from (never inferred from an aggregate)."""

    tokens: int
    basis_ref: Pin | None

    def __post_init__(self) -> None:
        if type(self.tokens) is not int or self.tokens < 0:
            raise ArpError("PRIOR_RESERVE_UNAVAILABLE", "prior reserve must be a nonnegative integer")
        if self.tokens > 0 and self.basis_ref is None:
            raise ArpError("PRIOR_RESERVE_UNAVAILABLE", "positive prior reserve needs its basis receipt")


NO_PRIOR = PriorBasis(0, None)


@dataclass(frozen=True, slots=True)
class MeterBinding:
    """The deployment's real counter bound to its certification and model limits."""

    tokenizer: TokenizerPort
    model_limits: Mapping[str, Any]
    certification_ref: Pin
    prior_reserve: Callable[[str], PriorBasis] | None = None

    def __post_init__(self) -> None:
        name = type(self.tokenizer).__name__
        mode = getattr(self.tokenizer, "count_mode", None)
        if name == "UpperBoundTokenizer" or mode not in COUNT_MODES:
            raise ArpError(
                "GENERIC_TOKEN_BOUND_UNCERTIFIED",
                f"{name} declares no certified count mode",
                detail={"tokenizer": getattr(self.tokenizer, "fingerprint", name)},
            )
        self.certification_ref.require_kind("receipt")
        limits = check("ModelLimits", dict(self.model_limits))
        object.__setattr__(self, "model_limits", limits)
        if limits["count_mode"] != mode:
            raise ArpError("BAD_TOKEN_RECEIPT", "counter mode differs from the model limits")
        if limits["renderer_hash"] != RENDERER_HASH or limits["serializer_hash"] != SERIALIZER_HASH:
            raise ArpError("TOKEN_SERIALIZER_CHANGED", "model limits certified another renderer/serializer")
        if limits["tokenizer_ref"]["content_hash"] != digest(self.tokenizer.fingerprint):
            raise ArpError("TOKEN_SERIALIZER_CHANGED", "model limits certified another tokenizer")
        if not limits["supports_tools"]:
            raise ArpError("FORMAT_UNSUPPORTED", "deployment does not support tool schemas")

    @property
    def count_mode(self) -> str:
        return str(self.model_limits["count_mode"])

    @property
    def input_scope(self) -> str:
        return str(self.model_limits["input_limit_scope"])

    @property
    def tokenizer_hash(self) -> str:
        return digest(self.tokenizer.fingerprint)

    @property
    def meter_ref(self) -> Pin:
        return Pin(
            "policy",
            f"meter:{self.model_limits['counter_id']}",
            int(self.model_limits["counter_revision"]),
            digest(
                {
                    "tokenizer": self.tokenizer.fingerprint,
                    "renderer": RENDERER_HASH,
                    "serializer": SERIALIZER_HASH,
                    "mode": self.count_mode,
                }
            ),
        )

    def prior_for(self, run_id: str) -> PriorBasis:
        if self.prior_reserve is None:
            if self.model_limits["requires_prior_output_reserve"]:
                raise ArpError("PRIOR_RESERVE_UNAVAILABLE", "deployment requires a prior reserve reader")
            return NO_PRIOR
        basis = self.prior_reserve(run_id)
        if not isinstance(basis, PriorBasis):
            raise ArpError("PRIOR_RESERVE_UNAVAILABLE", "prior reserve reader returned no basis")
        return basis


def model_limits(
    *,
    model: str,
    tokenizer: TokenizerPort,
    input_limit_tokens: int,
    max_output_tokens: int,
    combined_limit_tokens: int | None = None,
    provider_id: str = "consumer",
    counter_id: str | None = None,
    counter_revision: int = 1,
    input_limit_scope: str = "WIRE_ONLY",
    requires_prior_output_reserve: bool = False,
    capability_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``ModelLimits`` body for a counter certified against this SDK's renderer."""

    mode = getattr(tokenizer, "count_mode", None)
    if mode not in COUNT_MODES:
        raise ArpError("GENERIC_TOKEN_BOUND_UNCERTIFIED", "tokenizer declares no certified count mode")
    receipt = dict(capability_receipt or {"kind": "model-capability", "model": model})
    body = {
        "schema_version": 1,
        "provider_ref": Pin("provider", provider_id, 1, digest({"provider": provider_id})).to_json(),
        "model_ref": model,
        "input_limit_tokens": input_limit_tokens,
        "combined_limit_tokens": combined_limit_tokens,
        "max_output_tokens": max_output_tokens,
        "supports_tools": True,
        "supports_images": False,
        "counter_id": counter_id or tokenizer.fingerprint,
        "counter_revision": counter_revision,
        "count_mode": mode,
        "renderer_hash": RENDERER_HASH,
        "capability_receipt_ref": Pin("receipt", f"capability:{model}", 0, digest(receipt)).to_json(),
        "input_limit_scope": input_limit_scope,
        "requires_prior_output_reserve": requires_prior_output_reserve,
        "tokenizer_ref": Pin("artifact", f"tokenizer:{tokenizer.fingerprint}", 1, digest(tokenizer.fingerprint)).to_json(),
        "serializer_hash": SERIALIZER_HASH,
        "endpoint_protocol_ref": Pin("policy", f"protocol:{provider_id}", 1, digest({"protocol": provider_id})).to_json(),
        "supported_response_formats": ["text"],
        "supported_tool_schema_modes": ["json_schema"],
    }
    return check("ModelLimits", body)


@dataclass(frozen=True, slots=True)
class Measurement:
    receipt: Mapping[str, Any]
    wire_tokens: int
    request_hash: str


class NativeMeterAdapter:
    """Meter the final rendered wire request once, after tool-call restoration."""

    def __init__(self, binding: MeterBinding) -> None:
        self.binding = binding

    def count_wire(self, request: ProviderRequest) -> int:
        for message in request.messages:
            if _non_text_blocks(message):
                raise ArpError("MEDIA_UNSUPPORTED", "only text content is metered in this version")
        exact = getattr(self.binding.tokenizer, "count_request_tokens", None)
        if callable(exact):
            tokens = exact(request)
            if type(tokens) is not int or tokens < 0:
                raise ArpError("BAD_TOKEN_RECEIPT", "rendered request counter returned a non-count")
            return tokens
        tokenizer = self.binding.tokenizer
        return sum(count_message(tokenizer, m) for m in request.messages) + count_tools(
            tokenizer, tuple(request.tools)
        )

    def measure(
        self,
        request: ProviderRequest,
        *,
        run_id: str,
        max_input_budget: int,
        requested_output_tokens: int,
    ) -> Measurement:
        limits = self.binding.model_limits
        if type(requested_output_tokens) is not int or requested_output_tokens < 1:
            raise ArpError("INVALID_OUTPUT_RESERVE")
        if requested_output_tokens > int(limits["max_output_tokens"]):
            raise ArpError("INVALID_OUTPUT_RESERVE", "requested output exceeds the deployment limit")
        prior = self.binding.prior_for(run_id)
        wire = self.count_wire(request)
        request_hash = provider_request_fingerprint(request)
        receipt = {
            "schema_version": 1,
            "meter_ref": self.binding.meter_ref.to_json(),
            "request_hash": request_hash,
            "wire_input_tokens": wire,
            "prior_output_reserve_tokens": prior.tokens,
            "requested_output_tokens": requested_output_tokens,
            "billed_input_allowance_tokens": None,
            "count_mode": self.binding.count_mode,
            "input_limit_scope": self.binding.input_scope,
            "tokenizer_hash": self.binding.tokenizer_hash,
            "renderer_hash": RENDERER_HASH,
            "serializer_hash": SERIALIZER_HASH,
            "prior_basis_ref": None if prior.basis_ref is None else prior.basis_ref.to_json(),
            "proof_ref": Pin(
                "receipt",
                f"meter:{request.request_id.value}",
                0,
                digest({"request_hash": request_hash, "wire": wire, "prior": prior.tokens}),
            ).to_json(),
            "coverage": "TEXT_TOOLS_JSON",
            "max_input_budget": max_input_budget,
        }
        if wire > max_input_budget:
            raise ArpError(
                "FINAL_CONTEXT_OVERFLOW",
                "final wire count exceeds the input budget",
                detail={"wire_input_tokens": wire, "max_input_budget": max_input_budget},
            )
        return Measurement(check("TokenReceipt", receipt), wire, request_hash)


__all__ = (
    "COUNT_MODES",
    "INPUT_SCOPES",
    "Measurement",
    "MeterBinding",
    "NO_PRIOR",
    "NativeMeterAdapter",
    "PriorBasis",
    "RENDERER_HASH",
    "SERIALIZER_HASH",
    "model_limits",
)
