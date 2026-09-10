# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Kernel-side carrier for one committed BaseAgent turn result.

Layering rule: ``simple_harness.runtime`` owns this carrier and only moves JSON.
``simple_harness.agents`` depends on ``runtime`` (never the reverse) and decodes
``result_json`` into its rich ``AgentTurnResult``.  This module must not import
``simple_harness.agents`` or SQLite.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)

_HEX = frozenset("0123456789abcdef")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-blank string")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in _HEX for ch in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def result_json_hash(value: Mapping[str, object]) -> str:
    """Canonical SHA-256 of a JSON object result body."""

    thawed = thaw_json(value)  # type: ignore[arg-type]
    if not isinstance(thawed, dict):
        raise TypeError("result body must be a JSON object")
    return hashlib.sha256(canonical_json(thawed).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentTurnOutcome:
    """Result of one AgentTurn handed from a driver to the kernel for stage/finalize.

    The Run stays non-terminal; ``result_json`` is the frozen public result body
    whose ``result_hash`` must match its canonical encoding.
    """

    agent_id: str
    turn_id: str
    input_id: str
    input_hash: str
    result_hash: str
    result_json: FrozenJsonValue
    usage_refs: tuple[str, ...] = field(default=())
    provider_turn_ordinal_from: int | None = None
    provider_turn_ordinal_to: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.agent_id, "agent_id")
        _identifier(self.turn_id, "turn_id")
        _identifier(self.input_id, "input_id")
        _digest(self.input_hash, "input_hash")
        _digest(self.result_hash, "result_hash")
        if not isinstance(self.result_json, Mapping):
            raise TypeError("result_json must be a JSON object")
        frozen = freeze_json(thaw_json(self.result_json))
        object.__setattr__(self, "result_json", frozen)
        if result_json_hash(frozen) != self.result_hash:  # type: ignore[arg-type]
            raise ValueError("result_hash does not match result_json")
        refs = tuple(self.usage_refs)
        if any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError("usage_refs must be non-empty strings")
        object.__setattr__(self, "usage_refs", refs)
        for name in ("provider_turn_ordinal_from", "provider_turn_ordinal_to"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (
            self.provider_turn_ordinal_from is not None
            and self.provider_turn_ordinal_to is not None
            and self.provider_turn_ordinal_to < self.provider_turn_ordinal_from
        ):
            raise ValueError("provider turn ordinal range is inverted")

    def result_object(self) -> dict[str, JsonValue]:
        thawed = thaw_json(self.result_json)
        assert isinstance(thawed, dict)
        return thawed


__all__ = ("AgentTurnOutcome", "result_json_hash")
