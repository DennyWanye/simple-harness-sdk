"""Typed, receipt-free intents and actual Memory grants for one Provider handoff."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from simple_harness.contracts import canonical_json
from simple_harness.runtime.recall_protocol_v4 import (
    ContextFragmentBindingV2,
    ContextFragmentV2,
    RecallContextUseAuthorizationRequestV1,
    RecallContextUseReceiptV1,
    RecallItemBindingV1,
)


def use_hash(domain, payload):
    return hashlib.sha256(
        canonical_json({"domain": domain, "payload": payload}).encode()
    ).hexdigest()


def _text(value):
    if type(value) is not str or not value.strip() or "\0" in value or len(value) > 1024:
        raise ValueError("context_use_identifier_invalid")


def _sha(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("context_use_hash_invalid")


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("context_use_ordinal_invalid")


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("context_use_time_invalid")


def _keys(value, fields):
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ValueError("context_use_fields_differ")


@dataclass(frozen=True, slots=True)
class RecallContextUseIntentV1:
    """One retained result, with exact fragments and whole-message commitments.

    The configured snapshot authority attests complete source decomposition.
    Message ordinals are one-based in the final Provider request.
    """

    fragments: tuple[ContextFragmentV2, ...]
    message_bindings: tuple[tuple[int, str], ...]
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("context_use_intent_schema_invalid")
        if type(self.fragments) is not tuple or not 1 <= len(self.fragments) <= 32:
            raise ValueError("context_use_fragments_missing_or_over_limit")
        first = None
        seen = set()
        for fragment in self.fragments:
            if type(fragment) is not ContextFragmentV2 or fragment.fragment_id in seen:
                raise ValueError("context_use_fragment_invalid")
            seen.add(fragment.fragment_id)
            binding = fragment.recall_binding
            if binding is None or binding.item_id is None or binding.item_hash is None:
                raise ValueError("context_use_item_binding_missing")
            if binding.use_receipt_id is not None or binding.use_receipt_hash is not None:
                raise ValueError("context_use_hash_cycle")
            identity = (
                fragment.subject,
                fragment.run_id,
                binding.decision_id,
                binding.decision_hash,
                binding.result_id,
                binding.result_hash,
            )
            if first is not None and first != identity:
                raise ValueError("context_use_result_identity_differs")
            first = identity
        if type(self.message_bindings) is not tuple or not 1 <= len(self.message_bindings) <= 32:
            raise ValueError("context_use_message_bindings_missing")
        ordinals = []
        for row in self.message_bindings:
            if type(row) is not tuple or len(row) != 2:
                raise ValueError("context_use_message_binding_invalid")
            _positive(row[0])
            _sha(row[1])
            ordinals.append(row[0])
        if ordinals != sorted(set(ordinals)):
            raise ValueError("context_use_message_ordinals_invalid")

    def to_json(self):
        return dict(
            schema_version=1,
            fragments=[f.to_json() for f in self.fragments],
            message_bindings=[{"ordinal": i, "message_hash": h} for i, h in self.message_bindings],
        )

    @classmethod
    def from_json(cls, value):
        _keys(value, ("schema_version", "fragments", "message_bindings"))
        for row in value["message_bindings"]:
            _keys(row, ("ordinal", "message_hash"))
        return cls(
            tuple(ContextFragmentV2.from_json(f) for f in value["fragments"]),
            tuple((r["ordinal"], r["message_hash"]) for r in value["message_bindings"]),
            value["schema_version"],
        )

    def memory_attempt_id(self, attempt):
        binding = self.fragments[0].recall_binding
        return use_hash(
            "simple-harness/provider-memory-result-attempt/v1",
            {
                "provider_attempt_id": attempt.provider_attempt_id,
                "decision_id": binding.decision_id,
                "decision_hash": binding.decision_hash,
                "result_id": binding.result_id,
                "result_hash": binding.result_hash,
            },
        )

    def request(self, attempt):
        binding = self.fragments[0].recall_binding
        fragments = tuple(
            ContextFragmentBindingV2(f.fragment_id, f.fragment_hash) for f in self.fragments
        )
        return RecallContextUseAuthorizationRequestV1(
            attempt.subject,
            attempt.run_id,
            attempt.turn_id,
            self.memory_attempt_id(attempt),
            binding.decision_id,
            binding.decision_hash,
            binding.result_id,
            binding.result_hash,
            tuple(
                RecallItemBindingV1(f.recall_binding.item_id, f.recall_binding.item_hash)
                for f in self.fragments
            ),
            fragments,
            hashlib.sha256(canonical_json([f.to_json() for f in fragments]).encode()).hexdigest(),
            attempt.requested_at,
        )


_ATTEMPT_FIELDS = (
    "schema_version",
    "authority_scope_ref",
    "subject",
    "run_id",
    "turn_id",
    "continuation_id",
    "provider_request_id",
    "provider_turn_ordinal",
    "handoff_ordinal",
    "context_snapshot_id",
    "context_snapshot_revision",
    "request_fingerprint",
    "requested_at",
    "intents",
)


@dataclass(frozen=True, slots=True)
class ProviderContextUseAttemptV1:
    authority_scope_ref: str
    subject: str
    run_id: str
    turn_id: str
    continuation_id: str | None
    provider_request_id: str
    provider_turn_ordinal: int
    handoff_ordinal: int
    context_snapshot_id: str
    context_snapshot_revision: int
    request_fingerprint: str
    requested_at: float
    intents: tuple[RecallContextUseIntentV1, ...]
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("context_use_attempt_schema_invalid")
        for name in (
            "authority_scope_ref",
            "subject",
            "run_id",
            "turn_id",
            "provider_request_id",
            "context_snapshot_id",
        ):
            _text(getattr(self, name))
        if self.continuation_id is not None:
            _text(self.continuation_id)
        for name in ("provider_turn_ordinal", "handoff_ordinal", "context_snapshot_revision"):
            _positive(getattr(self, name))
        _sha(self.request_fingerprint)
        _time(self.requested_at)
        object.__setattr__(self, "requested_at", float(self.requested_at))
        if type(self.intents) is not tuple or len(self.intents) > 32:
            raise ValueError("context_use_attestation_missing_or_over_limit")
        seen = set()
        fragments = set()
        for intent in self.intents:
            if type(intent) is not RecallContextUseIntentV1:
                raise TypeError("context_use_intent_type_invalid")
            request = intent.request(self)
            if request.result_id in seen:
                raise ValueError("context_use_duplicate_result")
            seen.add(request.result_id)
            for fragment in intent.fragments:
                if (
                    fragment.subject != self.subject
                    or fragment.run_id != self.run_id
                    or fragment.fragment_id in fragments
                ):
                    raise ValueError("context_use_fragment_identity_differs")
                fragments.add(fragment.fragment_id)
        if len(fragments) > 32:
            raise ValueError("context_use_total_items_over_limit")

    @property
    def provider_attempt_id(self):
        identity = {
            k: getattr(self, k)
            for k in (
                "authority_scope_ref",
                "subject",
                "run_id",
                "turn_id",
                "continuation_id",
                "provider_request_id",
                "handoff_ordinal",
            )
        }
        return use_hash("simple-harness/provider-memory-attempt/v1", identity)

    @property
    def intent_hash(self):
        return use_hash("simple-harness/provider-context-use-intent/v1", self.to_json())

    def to_json(self):
        return {
            k: ([i.to_json() for i in self.intents] if k == "intents" else getattr(self, k))
            for k in _ATTEMPT_FIELDS
        }

    @classmethod
    def from_json(cls, value):
        _keys(value, _ATTEMPT_FIELDS)
        return cls(
            **(
                dict(value)
                | {
                    "intents": tuple(
                        RecallContextUseIntentV1.from_json(i) for i in value["intents"]
                    )
                }
            )
        )

    def validate_provider_request(self, run_id, request):
        from .provider_invocations import provider_request_fingerprint, provider_request_json

        if (
            self.run_id != run_id.value
            or self.provider_request_id != request.request_id.value
            or self.request_fingerprint != provider_request_fingerprint(request)
        ):
            raise ValueError("context_use_provider_identity_differs")
        messages = provider_request_json(request)["messages"]
        for intent in self.intents:
            for ordinal, expected in intent.message_bindings:
                if (
                    ordinal > len(messages)
                    or hashlib.sha256(canonical_json(messages[ordinal - 1]).encode()).hexdigest()
                    != expected
                ):
                    raise ValueError("context_use_message_hash_differs")

    def next_handoff(self, now):
        return replace(self, handoff_ordinal=self.handoff_ordinal + 1, requested_at=now)


@dataclass(frozen=True, slots=True)
class ProviderContextUseGrantV1:
    attempt: ProviderContextUseAttemptV1
    receipts: tuple[RecallContextUseReceiptV1, ...]

    def __post_init__(self):
        if (
            type(self.attempt) is not ProviderContextUseAttemptV1
            or type(self.receipts) is not tuple
            or len(self.receipts) != len(self.attempt.intents)
        ):
            raise ValueError("context_use_grant_incomplete")
        seen = set()
        for intent, receipt in zip(self.attempt.intents, self.receipts, strict=True):
            if type(receipt) is not RecallContextUseReceiptV1 or receipt.receipt_id in seen:
                raise ValueError("context_use_receipt_invalid")
            seen.add(receipt.receipt_id)
            receipt.validate_request(intent.request(self.attempt))
            if not self.attempt.requested_at <= receipt.authorized_at < receipt.expires_at:
                raise ValueError("context_use_receipt_time_invalid")

    def validate_handoff(self, now):
        _time(now)
        if any(not r.authorized_at <= now < r.expires_at for r in self.receipts):
            raise ValueError("context_use_grant_expired")

    def to_json(self):
        return dict(
            schema_version=1,
            attempt=self.attempt.to_json(),
            receipts=[r.to_json() for r in self.receipts],
        )

    @property
    def grant_hash(self):
        return use_hash("simple-harness/provider-context-use-grant/v1", self.to_json())

    @classmethod
    def from_json(cls, value):
        _keys(value, ("schema_version", "attempt", "receipts"))
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("context_use_grant_schema_invalid")
        return cls(
            ProviderContextUseAttemptV1.from_json(value["attempt"]),
            tuple(RecallContextUseReceiptV1.from_json(r) for r in value["receipts"]),
        )


@dataclass(frozen=True, slots=True)
class ProviderContextUseViewV1:
    invocation_id: str
    invocation_state: str
    invocation_version: int
    handoff_attempt: int
    authority_scope_ref: str
    subject: str
    run_id: str
    turn_id: str
    continuation_id: str | None
    provider_request_id: str
    provider_turn_ordinal: int
    handoff_ordinal: int
    context_snapshot_id: str
    context_snapshot_revision: int
    request_fingerprint: str
    requested_at: float
    provider_attempt_id: str
    intent_hash: str
    grant_hash: str
    requests: tuple[RecallContextUseAuthorizationRequestV1, ...]
    receipts: tuple[RecallContextUseReceiptV1, ...]
    message_bindings: tuple[tuple[tuple[int, str], ...], ...]

    @classmethod
    def from_grant(cls, record, grant):
        """Payload-free observational projection; never a dispatch capability."""
        attempt = grant.attempt
        fields = {
            name: getattr(attempt, name)
            for name in (
                "authority_scope_ref",
                "subject",
                "run_id",
                "turn_id",
                "continuation_id",
                "provider_request_id",
                "provider_turn_ordinal",
                "handoff_ordinal",
                "context_snapshot_id",
                "context_snapshot_revision",
                "request_fingerprint",
                "requested_at",
                "provider_attempt_id",
                "intent_hash",
            )
        }
        return cls(
            record.invocation_id,
            record.state.value,
            record.version,
            record.handoff_attempt,
            **fields,
            grant_hash=grant.grant_hash,
            requests=tuple(i.request(attempt) for i in attempt.intents),
            receipts=grant.receipts,
            message_bindings=tuple(i.message_bindings for i in attempt.intents),
        )


class RecallContextUseAuthorityPort(Protocol):
    @property
    def authority_scope_ref(self) -> str: ...
    def authorize_recall_context_use(
        self, request: RecallContextUseAuthorizationRequestV1
    ) -> Awaitable[RecallContextUseReceiptV1]: ...
