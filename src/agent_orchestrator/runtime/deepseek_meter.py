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


RELAY_COUNTER_ID = "deepseek-v41-relay-upper-bound"
RELAY_TOOL_MARGIN_FLOOR = 160
RELAY_CERTIFICATION_RULE = (
    "the official DeepSeek V4.1 tokenizer and chat request renderer count the rendered wire "
    "request; a relay may add its own tool-calling preamble (measured 2026-09-24: a fixed "
    "~110 tokens whenever tools are present, independent of tool count and parameters), so "
    "every request with tools is charged a learned margin: at least the floor, raised to the "
    "largest observed (reported - counted) excess plus the floor's headroom, never lowered"
)


class RelayToolMargin:
    """The learned, monotone tool-preamble margin of one relay deployment.

    Rule 2026-09-24 (may overcount, never undercount): start at ``floor``; every reported
    prompt count is compared with the exact official count of the same wire request; an
    excess the current margin does not cover raises the margin (to the excess plus the
    headroom the floor keeps over the measured preamble) and records an alarm.  The value
    never decreases and survives restarts through ``state_path`` when given.  It is not part
    of any fingerprint: raising it only charges later requests more.
    """

    def __init__(self, *, floor: int = RELAY_TOOL_MARGIN_FLOOR, headroom: int = 50, state_path: Path | None = None) -> None:
        if type(floor) is not int or floor < 0 or type(headroom) is not int or headroom < 0:
            raise ValueError("relay margin floor and headroom must be nonnegative integers")
        self.floor = floor
        self.headroom = headroom
        self.state_path = None if state_path is None else Path(state_path)
        self.value = floor
        self.max_excess: int | None = None
        self.alarms: list[dict[str, int]] = []
        self.observations = 0
        if self.state_path is not None and self.state_path.is_file():
            import json

            try:
                saved = json.loads(self.state_path.read_text())
                stored = saved.get("value")
                if type(stored) is int and stored > self.value:
                    self.value = stored
                if type(saved.get("max_excess")) is int:
                    self.max_excess = saved["max_excess"]
            except (OSError, ValueError, AttributeError):
                pass  # an unreadable state never lowers the floor

    def charge(self, request: ProviderRequest) -> int:
        return self.value if request.tools else 0

    def observe(self, *, reported: int, counted: int, has_tools: bool) -> bool:
        """Record one reported prompt count against the exact count; True on an alarm."""
        if type(reported) is not int or reported <= 0 or type(counted) is not int:
            return False  # no usable report (e.g. a relay that answers 0): nothing learned
        self.observations += 1
        excess = reported - counted
        self.max_excess = excess if self.max_excess is None else max(self.max_excess, excess)
        covered = self.value if has_tools else 0
        if excess <= covered:
            return False
        self.alarms.append({"reported": reported, "counted": counted, "margin": covered, "excess": excess})
        self.value = max(self.value, excess + self.headroom)
        self._save()
        return True

    def _save(self) -> None:
        if self.state_path is None:
            return
        import json

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"value": self.value, "max_excess": self.max_excess, "floor": self.floor}))
        tmp.replace(self.state_path)


class RelayDeepSeekCounter(CertifiedDeepSeekCounter):
    """The official counter plus a relay's learned tool-preamble margin: an upper bound.

    For a relay (not the official endpoint) the exact official count is not the server's
    count, so the mode is CERTIFIED_UPPER_BOUND.  ``exact_input_tokens`` stays the official
    count (used to learn the margin); every counted request with tools adds the margin.
    """

    count_mode = "CERTIFIED_UPPER_BOUND"

    def __init__(self, tokenizer_path: Path, *, margin: RelayToolMargin, **kwargs: Any) -> None:
        if not isinstance(margin, RelayToolMargin):
            raise TypeError("RelayDeepSeekCounter needs a RelayToolMargin")
        super().__init__(tokenizer_path, **kwargs)
        self.margin = margin
        self.fingerprint = self.fingerprint + f":relay-tool-margin-floor-{margin.floor}"

    def exact_input_tokens(self, request: ProviderRequest) -> int:
        return super().estimate_input_tokens(request)

    def estimate_input_tokens(self, request: ProviderRequest) -> int:
        return self.exact_input_tokens(request) + self.margin.charge(request)


class CalibratingProvider:
    """A provider decorator that learns a relay's tool-preamble margin from every reported
    prompt count (the request it sees is the final wire request)."""

    def __init__(self, inner: Any, counter: RelayDeepSeekCounter) -> None:
        if not isinstance(counter, RelayDeepSeekCounter):
            raise TypeError("CalibratingProvider needs the relay counter")
        self._inner = inner
        self._counter = counter

    @property
    def target(self) -> Any:
        return self._inner.target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def invoke(self, request: ProviderRequest, *, cancel: Any) -> Any:
        response = await self._inner.invoke(request, cancel=cancel)
        usage = getattr(response, "usage", None)
        reported = getattr(usage, "input_tokens", None)
        if type(reported) is int and reported > 0:
            try:
                counted = self._counter.exact_input_tokens(request)
            except ValueError:
                return response  # uncountable request (e.g. media): nothing learned
            self._counter.margin.observe(reported=reported, counted=counted, has_tools=bool(request.tools))
        return response


def certification_ref(counter: CertifiedDeepSeekCounter) -> Pin:
    relay = isinstance(counter, RelayDeepSeekCounter)
    body = {
        "rule": RELAY_CERTIFICATION_RULE if relay else CERTIFICATION_RULE,
        "counter_id": RELAY_COUNTER_ID if relay else COUNTER_ID,
        "counter_revision": COUNTER_REVISION,
        "tokenizer_fingerprint": counter.fingerprint,
        "recipe_version": RECIPE_VERSION,
        "recipe_commit": RECIPE_COMMIT,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "count_mode": counter.count_mode,
        "input_limit_scope": "WIRE_ONLY",
        "context_scope_source": CONTEXT_SCOPE_SOURCE,
        **({"relay_tool_margin_floor": counter.margin.floor} if relay else {}),
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
        provider_id="deepseek-relay" if isinstance(counter, RelayDeepSeekCounter) else "deepseek-official",
        counter_id=RELAY_COUNTER_ID if isinstance(counter, RelayDeepSeekCounter) else COUNTER_ID,
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


__all__ = (
    "CONTEXT_SCOPE_SOURCE",
    "COUNTER_ID",
    "RELAY_COUNTER_ID",
    "RELAY_TOOL_MARGIN_FLOOR",
    "CalibratingProvider",
    "CertifiedDeepSeekCounter",
    "RelayDeepSeekCounter",
    "RelayToolMargin",
    "certification_ref",
    "deepseek_meter_binding",
)
