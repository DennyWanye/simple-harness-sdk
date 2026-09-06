# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Identity-bound, payload-free terminal rejection and bounded control feedback."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from simple_harness.contracts import canonical_json
from simple_harness.contracts.messages import Message, MessageRole

MAX_MANDATORY_CONTEXT_REPAIRS = 2
_REASON = "pending_prospective_occurrence"


def _identity(value: object) -> None:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError("mandatory_context_identity_invalid")


def _digest(value: object) -> None:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("mandatory_context_hash_invalid")


@dataclass(frozen=True, slots=True)
class MandatoryContextRejectionV1:
    run_id: str
    provider_turn_ordinal: int
    request_fingerprint: str
    reason: str = _REASON

    def __post_init__(self) -> None:
        _identity(self.run_id)
        _digest(self.request_fingerprint)
        if type(self.provider_turn_ordinal) is not int or self.provider_turn_ordinal < 1:
            raise ValueError("mandatory_context_turn_invalid")
        if self.reason != _REASON:
            raise ValueError("mandatory_context_reason_unsupported")

    def to_json(self) -> dict:
        return dict(schema_version=1, run_id=self.run_id,
                    provider_turn_ordinal=self.provider_turn_ordinal,
                    request_fingerprint=self.request_fingerprint, reason=self.reason)

    @classmethod
    def from_json(cls, value: object) -> MandatoryContextRejectionV1:
        if (not isinstance(value, Mapping) or set(value) != {
                "schema_version", "run_id", "provider_turn_ordinal", "request_fingerprint", "reason"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError("mandatory_context_rejection_wire_invalid")
        return cls(**{k: v for k, v in value.items() if k != "schema_version"})


class MandatoryContextActionRequired(RuntimeError):
    """Only this exact public exception is a recoverable terminal decision."""
    code = "mandatory_context_action_required"
    def __init__(self, rejection: MandatoryContextRejectionV1) -> None:
        if type(rejection) is not MandatoryContextRejectionV1:
            raise TypeError("mandatory_context_rejection_required")
        self.rejection = rejection
        super().__init__("mandatory_context_action_required")


class MandatoryContextActionExhausted(RuntimeError):
    code = "mandatory_context_action_repair_exhausted"
    def __init__(self) -> None:
        super().__init__("mandatory_context_action_repair_exhausted")


@dataclass(frozen=True, slots=True)
class MandatoryContextFeedbackV1:
    rejection: MandatoryContextRejectionV1
    provider_request_id: str
    response_digest: str
    repair_ordinal: int

    def __post_init__(self) -> None:
        if type(self.rejection) is not MandatoryContextRejectionV1:
            raise TypeError("mandatory_context_rejection_required")
        _identity(self.provider_request_id)
        _digest(self.response_digest)
        if type(self.repair_ordinal) is not int or not 1 <= self.repair_ordinal <= MAX_MANDATORY_CONTEXT_REPAIRS:
            raise ValueError("mandatory_context_repair_ordinal_invalid")

    def to_json(self) -> dict:
        return dict(schema_version=1, rejection=self.rejection.to_json(),
                    provider_request_id=self.provider_request_id,
                    response_digest=self.response_digest, repair_ordinal=self.repair_ordinal)

    @classmethod
    def from_json(cls, value: object) -> MandatoryContextFeedbackV1:
        if (not isinstance(value, Mapping) or set(value) != {
                "schema_version", "rejection", "provider_request_id", "response_digest", "repair_ordinal"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError("mandatory_context_feedback_wire_invalid")
        return cls(MandatoryContextRejectionV1.from_json(value["rejection"]),
                   value["provider_request_id"], value["response_digest"], value["repair_ordinal"])

    def message(self) -> Message:
        return Message(MessageRole.SYSTEM, canonical_json({
            "kind": "mandatory_context_action_required", "feedback": self.to_json(),
            "instruction": "The prior direct answer cannot finish this Run: mandatory context actions remain pending. "
            "Use the currently authorized context_route or prospective_ack tools as appropriate to handle the "
            "current pending context before finishing. Presentation is not acknowledgement. Do not claim that "
            "an action ran or was acknowledged without its real tool result. Obey current permissions; if handling "
            "is unavailable this Run will fail after bounded attempts.",
        }), metadata={"source": "sdk_mandatory_context_control", "schema_version": 1})
