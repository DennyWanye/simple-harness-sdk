# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Sanitized evidence and auditable main-model analysis contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import FrozenJsonValue, JsonValue, canonical_json, thaw_json

from .disclosure_protocol import (
    COGNITIVE_MEMORY_SCHEMA_VERSION,
    HUMAN_MEMORY_SCHEMA_VERSION,
    DisclosureContext,
    _bounded_text,
    _canonical_hash,
    _digest,
    _domain_hash,
    _exact_keys,
    _freeze_object,
    _identifier,
    _non_negative_number,
    _object,
    _objects,
    _optional_identifier,
    _positive_int,
    _schema_version,
    _strings,
    _thaw_object,
)
from .information_classification_protocol import InformationAttribute, PrivacyClass


class EvidenceSourceKind(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    PROVIDER_RECORD = "provider_record"
    RUNTIME_EVENT = "runtime_event"
    TYPED_OBSERVATION = "typed_observation"


class RemovedSpanType(StrEnum):
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"
    COOKIE = "cookie"
    AUTHORIZATION_HEADER = "authorization_header"
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    HIDDEN_REASONING = "hidden_reasoning"
    OTHER_CREDENTIAL = "other_credential"


class EvidenceReasonCode(StrEnum):
    SANITIZED_AND_ACCEPTED = "evidence_sanitized_and_accepted"
    SOURCE_HASH_MISMATCH = "evidence_source_hash_mismatch"
    SANITIZED_HASH_MISMATCH = "evidence_sanitized_hash_mismatch"
    FILTER_POLICY_UNSUPPORTED = "evidence_filter_policy_unsupported"
    CREDENTIAL_CANARY_REJECTED = "evidence_credential_canary_rejected"
    HIDDEN_REASONING_REJECTED = "evidence_hidden_reasoning_rejected"
    ORDERED_EVIDENCE_MISMATCH = "analysis_ordered_evidence_mismatch"
    VALIDATOR_ACCEPTED = "analysis_validator_accepted"
    VALIDATOR_REJECTED = "analysis_validator_rejected"


class EvidenceActorRole(StrEnum):
    """Actor that produced the sanitized source item.

    This is provenance, not an authorization decision.  In particular an
    ``ASSISTANT`` span remains model output even when it is quoted exactly.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    RUNTIME = "runtime"
    EXTERNAL = "external"


class EvidenceProvenance(StrEnum):
    AUTHENTICATED_USER = "authenticated_user"
    MODEL_OUTPUT = "model_output"
    TRUSTED_TOOL = "trusted_tool"
    HOST_RUNTIME = "host_runtime"
    EXTERNAL_SOURCE = "external_source"


class EvidenceSupportKind(StrEnum):
    EXPLICIT_USER_ASSERTION = "explicit_user_assertion"
    EXPLICIT_USER_CORRECTION = "explicit_user_correction"
    TYPED_OBSERVATION = "typed_observation"
    RUNTIME_EVENT = "runtime_event"
    MODEL_INFERENCE = "model_inference"
    CONTEXT_ONLY = "context_only"


EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1 = "sanitized-string-identity-utf8/v1"
EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION = 3
CONVERSATION_EVIDENCE_SCHEMA_VERSION = 3


def _normalize_evidence_text(value: str, version: str) -> str:
    if version != EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1:
        raise ValueError("unsupported evidence normalization_version")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ProposedTypedObservationRef:
    """Model-proposed reference to a Host-registered observation receipt.

    This DTO never proves acceptance.  Memory must resolve the referenced
    receipt through :class:`EvidenceAuthorityVerifierPort`; a model-created
    object with matching-looking strings has no authority.
    """

    schema_id: str
    schema_version: int
    registered_schema_hash: str
    observation_receipt_id: str
    observation_receipt_hash: str
    authority_issuer_id: str
    json_pointer: str
    value_hash: str
    observation_ref_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.schema_id, "schema_id", max_length=512)
        _positive_int(self.schema_version, "schema_version")
        _digest(self.registered_schema_hash, "registered_schema_hash")
        _identifier(self.observation_receipt_id, "observation_receipt_id")
        _digest(self.observation_receipt_hash, "observation_receipt_hash")
        _identifier(self.authority_issuer_id, "authority_issuer_id", max_length=1024)
        pointer = _bounded_text(self.json_pointer, "json_pointer", max_bytes=2048)
        if not pointer.startswith("/"):
            raise ValueError("json_pointer must be an RFC 6901 absolute pointer")
        _digest(self.value_hash, "value_hash")
        object.__setattr__(
            self,
            "observation_ref_hash",
            _domain_hash("simple-harness/proposed-typed-observation-ref/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "registered_schema_hash": self.registered_schema_hash,
            "observation_receipt_id": self.observation_receipt_id,
            "observation_receipt_hash": self.observation_receipt_hash,
            "authority_issuer_id": self.authority_issuer_id,
            "json_pointer": self.json_pointer,
            "value_hash": self.value_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProposedTypedObservationRef:
        _exact_keys(
            value,
            {
                "schema_id",
                "schema_version",
                "registered_schema_hash",
                "observation_receipt_id",
                "observation_receipt_hash",
                "authority_issuer_id",
                "json_pointer",
                "value_hash",
            },
            "ProposedTypedObservationRef",
        )
        return cls(
            schema_id=_identifier(value["schema_id"], "schema_id", max_length=512),
            schema_version=_positive_int(value["schema_version"], "schema_version"),
            registered_schema_hash=_digest(
                value["registered_schema_hash"], "registered_schema_hash"
            ),
            observation_receipt_id=_identifier(
                value["observation_receipt_id"], "observation_receipt_id"
            ),
            observation_receipt_hash=_digest(
                value["observation_receipt_hash"], "observation_receipt_hash"
            ),
            authority_issuer_id=_identifier(
                value["authority_issuer_id"], "authority_issuer_id", max_length=1024
            ),
            json_pointer=_bounded_text(value["json_pointer"], "json_pointer", max_bytes=2048),
            value_hash=_digest(value["value_hash"], "value_hash"),
        )


@dataclass(frozen=True, slots=True)
class EvidenceSpanRef:
    """Model-proposed exact span binding into admitted sanitized evidence.

    The wire shape deliberately does not repeat the source text.  A trusted
    verifier resolves the envelope and receipt, follows ``item_json_pointer``,
    then checks the UTF-8 byte boundaries and quote.  Multiple spans can cite
    the same evidence item without weakening evidence ownership.
    """

    span_id: str
    evidence_id: str
    envelope_hash: str
    sanitized_hash: str
    admission_receipt_id: str
    admission_receipt_hash: str
    source_kind: EvidenceSourceKind
    item_ordinal: int
    item_id: str
    item_json_pointer: str
    start_byte: int
    end_byte: int
    exact_quote: str
    quote_hash: str
    source_hash: str
    normalization_version: str
    actor_role: EvidenceActorRole
    provenance: EvidenceProvenance
    support_kind: EvidenceSupportKind
    typed_observation: ProposedTypedObservationRef | None
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    span_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.span_id, "span_id")
        _identifier(self.evidence_id, "evidence_id")
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported EvidenceSpanRef schema_version")
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.sanitized_hash, "sanitized_hash"),
            (self.admission_receipt_hash, "admission_receipt_hash"),
            (self.quote_hash, "quote_hash"),
            (self.source_hash, "source_hash"),
        ):
            _digest(value, name)
        _identifier(self.item_id, "item_id", max_length=1024)
        _identifier(self.admission_receipt_id, "admission_receipt_id")
        object.__setattr__(self, "source_kind", EvidenceSourceKind(self.source_kind))
        _positive_int(self.item_ordinal, "item_ordinal")
        pointer = _bounded_text(self.item_json_pointer, "item_json_pointer", max_bytes=2048)
        if not pointer.startswith("/"):
            raise ValueError("item_json_pointer must be an RFC 6901 absolute pointer")
        quote = _bounded_text(self.exact_quote, "exact_quote", max_bytes=16_384)
        if _canonical_text_digest(quote) != self.quote_hash:
            raise ValueError("quote_hash does not bind exact_quote")
        start = _non_negative_int(self.start_byte, "start_byte")
        end = _non_negative_int(self.end_byte, "end_byte")
        if end <= start:
            raise ValueError("end_byte must be greater than start_byte")
        _normalize_evidence_text("", self.normalization_version)
        object.__setattr__(self, "actor_role", EvidenceActorRole(self.actor_role))
        object.__setattr__(self, "provenance", EvidenceProvenance(self.provenance))
        object.__setattr__(self, "support_kind", EvidenceSupportKind(self.support_kind))
        if self.support_kind is EvidenceSupportKind.TYPED_OBSERVATION:
            if not isinstance(self.typed_observation, ProposedTypedObservationRef):
                raise ValueError(
                    "typed_observation support requires a proposed typed_observation ref"
                )
        elif self.typed_observation is not None:
            raise ValueError("typed_observation ref is only valid for typed_observation support")
        object.__setattr__(
            self,
            "span_hash",
            _domain_hash("simple-harness/evidence-span-ref/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "span_id": self.span_id,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "sanitized_hash": self.sanitized_hash,
            "admission_receipt_id": self.admission_receipt_id,
            "admission_receipt_hash": self.admission_receipt_hash,
            "source_kind": self.source_kind.value,
            "item_ordinal": self.item_ordinal,
            "item_id": self.item_id,
            "item_json_pointer": self.item_json_pointer,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "exact_quote": self.exact_quote,
            "quote_hash": self.quote_hash,
            "source_hash": self.source_hash,
            "normalization_version": self.normalization_version,
            "actor_role": self.actor_role.value,
            "provenance": self.provenance.value,
            "support_kind": self.support_kind.value,
            "typed_observation": (
                None if self.typed_observation is None else self.typed_observation.to_json()
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> EvidenceSpanRef:
        _exact_keys(
            value,
            {
                "schema_version",
                "span_id",
                "evidence_id",
                "envelope_hash",
                "sanitized_hash",
                "admission_receipt_id",
                "admission_receipt_hash",
                "source_kind",
                "item_ordinal",
                "item_id",
                "item_json_pointer",
                "start_byte",
                "end_byte",
                "exact_quote",
                "quote_hash",
                "source_hash",
                "normalization_version",
                "actor_role",
                "provenance",
                "support_kind",
                "typed_observation",
            },
            "EvidenceSpanRef",
        )
        observation_value = value["typed_observation"]
        if observation_value is not None:
            observation = ProposedTypedObservationRef.from_json(
                _object(observation_value, "typed_observation")
            )
        else:
            observation = None
        return cls(
            span_id=_identifier(value["span_id"], "span_id"),
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            envelope_hash=_digest(value["envelope_hash"], "envelope_hash"),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            admission_receipt_id=_identifier(value["admission_receipt_id"], "admission_receipt_id"),
            admission_receipt_hash=_digest(
                value["admission_receipt_hash"], "admission_receipt_hash"
            ),
            source_kind=EvidenceSourceKind(value["source_kind"]),  # type: ignore[arg-type]
            item_ordinal=_positive_int(value["item_ordinal"], "item_ordinal"),
            item_id=_identifier(value["item_id"], "item_id", max_length=1024),
            item_json_pointer=_bounded_text(
                value["item_json_pointer"], "item_json_pointer", max_bytes=2048
            ),
            start_byte=_non_negative_int(value["start_byte"], "start_byte"),
            end_byte=_non_negative_int(value["end_byte"], "end_byte"),
            exact_quote=_bounded_text(value["exact_quote"], "exact_quote", max_bytes=16_384),
            quote_hash=_digest(value["quote_hash"], "quote_hash"),
            source_hash=_digest(value["source_hash"], "source_hash"),
            normalization_version=_identifier(
                value["normalization_version"], "normalization_version"
            ),
            actor_role=EvidenceActorRole(value["actor_role"]),  # type: ignore[arg-type]
            provenance=EvidenceProvenance(value["provenance"]),  # type: ignore[arg-type]
            support_kind=EvidenceSupportKind(value["support_kind"]),  # type: ignore[arg-type]
            typed_observation=observation,
            schema_version=_cognitive_schema_version(value["schema_version"], "EvidenceSpanRef"),
        )


def _canonical_text_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cognitive_schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != COGNITIVE_MEMORY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _conversation_evidence_schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != CONVERSATION_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _rfc6901_absolute_pointer(value: object, name: str) -> str:
    pointer = _bounded_text(value, name, max_bytes=2048)
    if not pointer.startswith("/"):
        raise ValueError(f"{name} must be an RFC 6901 absolute pointer")
    index = 0
    while index < len(pointer):
        if pointer[index] == "~":
            if index + 1 >= len(pointer) or pointer[index + 1] not in {"0", "1"}:
                raise ValueError(f"{name} contains an invalid RFC 6901 escape")
            index += 2
            continue
        index += 1
    return pointer


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Ordered evidence reference.

    ``content_hash`` is the hash of the referenced durable evidence object in
    the enclosing protocol context.  For admitted sanitized evidence and all
    cognitive mutation plans this is specifically ``envelope_hash``; other
    protocols must document their referenced object's canonical hash.
    """

    evidence_id: str
    content_hash: str
    ordinal: int

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence_id")
        _digest(self.content_hash, "content_hash")
        _positive_int(self.ordinal, "ordinal")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> EvidenceRef:
        _exact_keys(value, {"evidence_id", "content_hash", "ordinal"}, "EvidenceRef")
        return cls(
            _identifier(value["evidence_id"], "evidence_id"),
            _digest(value["content_hash"], "content_hash"),
            _positive_int(value["ordinal"], "ordinal"),
        )


def _evidence_refs(value: object, name: str = "evidence_refs") -> tuple[EvidenceRef, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, EvidenceRef) for item in value
    ):
        raise TypeError(f"{name} must contain EvidenceRef values")
    refs = tuple(value)
    if len({ref.evidence_id for ref in refs}) != len(refs):
        raise ValueError(f"{name} evidence_id values must be unique")
    if refs and [ref.ordinal for ref in refs] != list(range(1, len(refs) + 1)):
        raise ValueError(f"{name} ordinals must be contiguous and start at 1")
    return refs


def _refs_from_json(value: object, name: str = "evidence_refs") -> tuple[EvidenceRef, ...]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise TypeError(f"{name} must be an array of objects")
    return _evidence_refs(tuple(EvidenceRef.from_json(item) for item in value), name)


@dataclass(frozen=True, slots=True)
class RemovedSpanSummary:
    span_type: RemovedSpanType
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "span_type", RemovedSpanType(self.span_type))
        _positive_int(self.count, "count")

    def to_json(self) -> dict[str, JsonValue]:
        return {"span_type": self.span_type.value, "count": self.count}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RemovedSpanSummary:
        _exact_keys(value, {"span_type", "count"}, "RemovedSpanSummary")
        return cls(RemovedSpanType(value["span_type"]), _positive_int(value["count"], "count"))  # type: ignore[arg-type]


class ConversationEvidenceRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class ConversationToolCausalLink:
    tool_call_id: str
    tool_name: str
    parent_item_ordinal: int
    terminal_receipt_id: str
    terminal_receipt_hash: str

    def __post_init__(self) -> None:
        _identifier(self.tool_call_id, "tool_call_id")
        _identifier(self.tool_name, "tool_name")
        _positive_int(self.parent_item_ordinal, "parent_item_ordinal")
        _identifier(self.terminal_receipt_id, "terminal_receipt_id")
        _digest(self.terminal_receipt_hash, "terminal_receipt_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "parent_item_ordinal": self.parent_item_ordinal,
            "terminal_receipt_id": self.terminal_receipt_id,
            "terminal_receipt_hash": self.terminal_receipt_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ConversationToolCausalLink:
        _exact_keys(
            value,
            {
                "tool_call_id",
                "tool_name",
                "parent_item_ordinal",
                "terminal_receipt_id",
                "terminal_receipt_hash",
            },
            "ConversationToolCausalLink",
        )
        return cls(
            _identifier(value["tool_call_id"], "tool_call_id"),
            _identifier(value["tool_name"], "tool_name"),
            _positive_int(value["parent_item_ordinal"], "parent_item_ordinal"),
            _identifier(value["terminal_receipt_id"], "terminal_receipt_id"),
            _digest(value["terminal_receipt_hash"], "terminal_receipt_hash"),
        )


@dataclass(frozen=True, slots=True)
class ConversationEvidenceMetadata:
    """Registered causal metadata for Short-Horizon eligibility.

    The model never creates this object.  The Host registers complete causal
    groups and issues the companion receipt; missing metadata leaves raw
    evidence durable but makes it ineligible for the Short-Horizon index.
    """

    metadata_id: str
    authority_issuer_id: str
    evidence_id: str
    envelope_hash: str
    admission_receipt_id: str
    admission_receipt_hash: str
    run_id: str
    subject: str
    source_hash: str
    sanitized_hash: str
    conversation_id: str
    primary_conversation_id: str
    causal_group_id: str
    causal_group_sequence: int
    item_ordinal: int
    group_item_count: int
    ordered_group_manifest_hash: str
    role: ConversationEvidenceRole
    occurred_at: float
    task_scope_id: str | None
    tool_causal_link: ConversationToolCausalLink | None
    entities: tuple[str, ...]
    public_text_json_pointer: str | None = None
    public_text_hash: str | None = None
    public_text_normalization_version: str | None = None
    evidence_item_authority_id: str | None = None
    evidence_item_authority_hash: str | None = None
    effective_privacy_class: PrivacyClass | None = None
    information_attributes: tuple[InformationAttribute, ...] | None = None
    classification_authority_ref: str | None = None
    schema_version: int = CONVERSATION_EVIDENCE_SCHEMA_VERSION
    metadata_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CONVERSATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported ConversationEvidenceMetadata schema_version")
        for value, name in (
            (self.metadata_id, "metadata_id"),
            (self.authority_issuer_id, "authority_issuer_id"),
            (self.evidence_id, "evidence_id"),
            (self.admission_receipt_id, "admission_receipt_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.conversation_id, "conversation_id"),
            (self.primary_conversation_id, "primary_conversation_id"),
            (self.causal_group_id, "causal_group_id"),
        ):
            _identifier(value, name)
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.admission_receipt_hash, "admission_receipt_hash"),
            (self.source_hash, "source_hash"),
            (self.sanitized_hash, "sanitized_hash"),
        ):
            _digest(value, name)
        for numeric_value, name in (
            (self.causal_group_sequence, "causal_group_sequence"),
            (self.item_ordinal, "item_ordinal"),
            (self.group_item_count, "group_item_count"),
        ):
            _positive_int(numeric_value, name)
        if self.item_ordinal > self.group_item_count:
            raise ValueError("item_ordinal exceeds group_item_count")
        _digest(self.ordered_group_manifest_hash, "ordered_group_manifest_hash")
        object.__setattr__(self, "role", ConversationEvidenceRole(self.role))
        occurred_at = _non_negative_number(self.occurred_at, "occurred_at")
        object.__setattr__(self, "occurred_at", occurred_at)
        _optional_identifier(self.task_scope_id, "task_scope_id")
        if self.role is ConversationEvidenceRole.TOOL:
            if not isinstance(self.tool_causal_link, ConversationToolCausalLink):
                raise ValueError("tool role requires tool_causal_link")
            if self.tool_causal_link.parent_item_ordinal >= self.item_ordinal:
                raise ValueError(
                    "tool causal parent must be within the group and earlier than the tool item"
                )
        elif self.tool_causal_link is not None:
            raise ValueError("tool_causal_link is only valid for tool role")
        entities = tuple(_bounded_text(item, "entity", max_bytes=1024) for item in self.entities)
        if len(entities) > 128 or len(set(entities)) != len(entities):
            raise ValueError("entities must be unique and bounded")
        object.__setattr__(self, "entities", entities)
        recall_values = (
            self.public_text_json_pointer,
            self.public_text_hash,
            self.public_text_normalization_version,
            self.evidence_item_authority_id,
            self.evidence_item_authority_hash,
            self.effective_privacy_class,
            self.information_attributes,
            self.classification_authority_ref,
        )
        present = tuple(value is not None for value in recall_values)
        if any(present) and not all(present):
            raise ValueError("authorized public_text fields must be all present or all absent")
        if all(present):
            pointer = _rfc6901_absolute_pointer(
                cast(str, self.public_text_json_pointer),
                "public_text_json_pointer",
            )
            _digest(self.public_text_hash, "public_text_hash")
            _normalize_evidence_text(
                "", cast(str, self.public_text_normalization_version)
            )
            _identifier(self.evidence_item_authority_id, "evidence_item_authority_id")
            _digest(self.evidence_item_authority_hash, "evidence_item_authority_hash")
            privacy = PrivacyClass(cast(str, self.effective_privacy_class))
            raw_attributes = self.information_attributes
            if not isinstance(raw_attributes, (tuple, list)):
                raise TypeError("information_attributes must be an array")
            try:
                attributes = tuple(InformationAttribute(item) for item in raw_attributes)
            except (TypeError, ValueError) as exc:
                raise ValueError("information_attributes contains an unknown value") from exc
            if len(attributes) > 32 or len(set(attributes)) != len(attributes):
                raise ValueError("information_attributes must be unique and bounded")
            attributes = tuple(sorted(attributes, key=lambda item: item.value))
            _identifier(
                self.classification_authority_ref,
                "classification_authority_ref",
                max_length=1024,
            )
            object.__setattr__(self, "public_text_json_pointer", pointer)
            object.__setattr__(self, "effective_privacy_class", privacy)
            object.__setattr__(self, "information_attributes", attributes)
        object.__setattr__(
            self,
            "metadata_hash",
            _domain_hash("simple-harness/conversation-evidence-metadata/v3", self.to_json()),
        )

    @property
    def belongs_to_primary_conversation(self) -> bool:
        return self.conversation_id == self.primary_conversation_id

    @property
    def has_authorized_public_text(self) -> bool:
        return self.public_text_json_pointer is not None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "metadata_id": self.metadata_id,
            "authority_issuer_id": self.authority_issuer_id,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "admission_receipt_id": self.admission_receipt_id,
            "admission_receipt_hash": self.admission_receipt_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "source_hash": self.source_hash,
            "sanitized_hash": self.sanitized_hash,
            "conversation_id": self.conversation_id,
            "primary_conversation_id": self.primary_conversation_id,
            "causal_group_id": self.causal_group_id,
            "causal_group_sequence": self.causal_group_sequence,
            "item_ordinal": self.item_ordinal,
            "group_item_count": self.group_item_count,
            "ordered_group_manifest_hash": self.ordered_group_manifest_hash,
            "role": self.role.value,
            "occurred_at": self.occurred_at,
            "task_scope_id": self.task_scope_id,
            "tool_causal_link": (
                None if self.tool_causal_link is None else self.tool_causal_link.to_json()
            ),
            "entities": list(self.entities),
            "public_text_json_pointer": self.public_text_json_pointer,
            "public_text_hash": self.public_text_hash,
            "public_text_normalization_version": self.public_text_normalization_version,
            "evidence_item_authority_id": self.evidence_item_authority_id,
            "evidence_item_authority_hash": self.evidence_item_authority_hash,
            "effective_privacy_class": (
                None
                if self.effective_privacy_class is None
                else self.effective_privacy_class.value
            ),
            "information_attributes": (
                None
                if self.information_attributes is None
                else [item.value for item in self.information_attributes]
            ),
            "classification_authority_ref": self.classification_authority_ref,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ConversationEvidenceMetadata:
        _exact_keys(
            value,
            {
                "schema_version",
                "metadata_id",
                "authority_issuer_id",
                "evidence_id",
                "envelope_hash",
                "admission_receipt_id",
                "admission_receipt_hash",
                "run_id",
                "subject",
                "source_hash",
                "sanitized_hash",
                "conversation_id",
                "primary_conversation_id",
                "causal_group_id",
                "causal_group_sequence",
                "item_ordinal",
                "group_item_count",
                "ordered_group_manifest_hash",
                "role",
                "occurred_at",
                "task_scope_id",
                "tool_causal_link",
                "entities",
                "public_text_json_pointer",
                "public_text_hash",
                "public_text_normalization_version",
                "evidence_item_authority_id",
                "evidence_item_authority_hash",
                "effective_privacy_class",
                "information_attributes",
                "classification_authority_ref",
            },
            "ConversationEvidenceMetadata",
        )
        occurred_at = value["occurred_at"]
        if isinstance(occurred_at, bool) or not isinstance(occurred_at, (int, float)):
            raise TypeError("occurred_at must be numeric")
        tool_link = value["tool_causal_link"]
        return cls(
            metadata_id=_identifier(value["metadata_id"], "metadata_id"),
            authority_issuer_id=_identifier(value["authority_issuer_id"], "authority_issuer_id"),
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            envelope_hash=_digest(value["envelope_hash"], "envelope_hash"),
            admission_receipt_id=_identifier(value["admission_receipt_id"], "admission_receipt_id"),
            admission_receipt_hash=_digest(
                value["admission_receipt_hash"], "admission_receipt_hash"
            ),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            conversation_id=_identifier(value["conversation_id"], "conversation_id"),
            primary_conversation_id=_identifier(
                value["primary_conversation_id"], "primary_conversation_id"
            ),
            causal_group_id=_identifier(value["causal_group_id"], "causal_group_id"),
            causal_group_sequence=_positive_int(
                value["causal_group_sequence"], "causal_group_sequence"
            ),
            item_ordinal=_positive_int(value["item_ordinal"], "item_ordinal"),
            group_item_count=_positive_int(value["group_item_count"], "group_item_count"),
            ordered_group_manifest_hash=_digest(
                value["ordered_group_manifest_hash"], "ordered_group_manifest_hash"
            ),
            role=ConversationEvidenceRole(value["role"]),  # type: ignore[arg-type]
            occurred_at=float(occurred_at),
            task_scope_id=_optional_identifier(value["task_scope_id"], "task_scope_id"),
            tool_causal_link=(
                None
                if tool_link is None
                else ConversationToolCausalLink.from_json(_object(tool_link, "tool_causal_link"))
            ),
            entities=_strings(value["entities"], "entities"),
            public_text_json_pointer=(
                None
                if value["public_text_json_pointer"] is None
                else _bounded_text(
                    value["public_text_json_pointer"],
                    "public_text_json_pointer",
                    max_bytes=2048,
                )
            ),
            public_text_hash=(
                None
                if value["public_text_hash"] is None
                else _digest(value["public_text_hash"], "public_text_hash")
            ),
            public_text_normalization_version=_optional_identifier(
                value["public_text_normalization_version"],
                "public_text_normalization_version",
            ),
            evidence_item_authority_id=_optional_identifier(
                value["evidence_item_authority_id"], "evidence_item_authority_id"
            ),
            evidence_item_authority_hash=(
                None
                if value["evidence_item_authority_hash"] is None
                else _digest(
                    value["evidence_item_authority_hash"],
                    "evidence_item_authority_hash",
                )
            ),
            effective_privacy_class=(
                None
                if value["effective_privacy_class"] is None
                else PrivacyClass(value["effective_privacy_class"])  # type: ignore[arg-type]
            ),
            information_attributes=(
                None
                if value["information_attributes"] is None
                else tuple(
                    InformationAttribute(item)
                    for item in _strings(
                        value["information_attributes"], "information_attributes"
                    )
                )
            ),
            classification_authority_ref=(
                None
                if value["classification_authority_ref"] is None
                else _identifier(
                    value["classification_authority_ref"],
                    "classification_authority_ref",
                    max_length=1024,
                )
            ),
            schema_version=_conversation_evidence_schema_version(
                value["schema_version"], "ConversationEvidenceMetadata"
            ),
        )


@dataclass(frozen=True, slots=True)
class ConversationEvidenceMetadataReceipt:
    receipt_id: str
    metadata_id: str
    authority_issuer_id: str
    evidence_id: str
    envelope_hash: str
    admission_receipt_id: str
    admission_receipt_hash: str
    run_id: str
    subject: str
    source_hash: str
    sanitized_hash: str
    metadata_hash: str
    issuer_ref: str
    accepted: bool
    schema_version: int = CONVERSATION_EVIDENCE_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CONVERSATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported ConversationEvidenceMetadataReceipt schema_version")
        _identifier(self.receipt_id, "receipt_id")
        _identifier(self.metadata_id, "metadata_id")
        _identifier(self.authority_issuer_id, "authority_issuer_id", max_length=1024)
        _identifier(self.evidence_id, "evidence_id")
        _identifier(self.admission_receipt_id, "admission_receipt_id")
        _identifier(self.run_id, "run_id")
        _identifier(self.subject, "subject")
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.admission_receipt_hash, "admission_receipt_hash"),
            (self.source_hash, "source_hash"),
            (self.sanitized_hash, "sanitized_hash"),
            (self.metadata_hash, "metadata_hash"),
        ):
            _digest(value, name)
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        if self.issuer_ref != self.authority_issuer_id:
            raise ValueError("metadata receipt issuer differs from authority_issuer_id")
        if self.accepted is not True:
            raise ValueError("conversation metadata receipt must be accepted")
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash(
                "simple-harness/conversation-evidence-metadata-receipt/v3",
                self.to_json(),
            ),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "metadata_id": self.metadata_id,
            "authority_issuer_id": self.authority_issuer_id,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "admission_receipt_id": self.admission_receipt_id,
            "admission_receipt_hash": self.admission_receipt_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "source_hash": self.source_hash,
            "sanitized_hash": self.sanitized_hash,
            "metadata_hash": self.metadata_hash,
            "issuer_ref": self.issuer_ref,
            "accepted": self.accepted,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ConversationEvidenceMetadataReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "metadata_id",
                "authority_issuer_id",
                "evidence_id",
                "envelope_hash",
                "admission_receipt_id",
                "admission_receipt_hash",
                "run_id",
                "subject",
                "source_hash",
                "sanitized_hash",
                "metadata_hash",
                "issuer_ref",
                "accepted",
            },
            "ConversationEvidenceMetadataReceipt",
        )
        accepted = value["accepted"]
        if not isinstance(accepted, bool):
            raise TypeError("accepted must be a boolean")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            metadata_id=_identifier(value["metadata_id"], "metadata_id"),
            authority_issuer_id=_identifier(
                value["authority_issuer_id"], "authority_issuer_id", max_length=1024
            ),
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            envelope_hash=_digest(value["envelope_hash"], "envelope_hash"),
            admission_receipt_id=_identifier(value["admission_receipt_id"], "admission_receipt_id"),
            admission_receipt_hash=_digest(
                value["admission_receipt_hash"], "admission_receipt_hash"
            ),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            metadata_hash=_digest(value["metadata_hash"], "metadata_hash"),
            issuer_ref=_identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            accepted=accepted,
            schema_version=_conversation_evidence_schema_version(
                value["schema_version"], "ConversationEvidenceMetadataReceipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class SanitizedEvidenceEnvelope:
    evidence_id: str
    run_id: str
    subject: str
    source_kind: EvidenceSourceKind
    source_ref: str
    source_hash: str
    sanitized_payload: Mapping[str, FrozenJsonValue]
    sanitized_hash: str
    filter_policy_version: str
    removed_spans: tuple[RemovedSpanSummary, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    envelope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported SanitizedEvidenceEnvelope schema_version")
        _identifier(self.evidence_id, "evidence_id")
        _identifier(self.run_id, "run_id")
        _identifier(self.subject, "subject")
        object.__setattr__(self, "source_kind", EvidenceSourceKind(self.source_kind))
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _digest(self.source_hash, "source_hash")
        _digest(self.sanitized_hash, "sanitized_hash")
        _identifier(self.filter_policy_version, "filter_policy_version")
        payload = _freeze_object(self.sanitized_payload, "sanitized_payload")
        if _canonical_hash(_thaw_object(payload)) != self.sanitized_hash:
            raise ValueError(EvidenceReasonCode.SANITIZED_HASH_MISMATCH.value)
        spans = tuple(self.removed_spans)
        if not all(isinstance(span, RemovedSpanSummary) for span in spans):
            raise TypeError("removed_spans must contain RemovedSpanSummary values")
        if len({span.span_type for span in spans}) != len(spans):
            raise ValueError("removed_spans span types must be unique")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        object.__setattr__(self, "sanitized_payload", payload)
        object.__setattr__(self, "removed_spans", spans)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(
            self,
            "envelope_hash",
            _domain_hash("simple-harness/sanitized-evidence-envelope/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "source_kind": self.source_kind.value,
            "source_ref": self.source_ref,
            "source_hash": self.source_hash,
            "sanitized_payload": _thaw_object(self.sanitized_payload),
            "sanitized_hash": self.sanitized_hash,
            "filter_policy_version": self.filter_policy_version,
            "removed_spans": [span.to_json() for span in self.removed_spans],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SanitizedEvidenceEnvelope:
        _exact_keys(
            value,
            {
                "schema_version",
                "evidence_id",
                "run_id",
                "subject",
                "source_kind",
                "source_ref",
                "source_hash",
                "sanitized_payload",
                "sanitized_hash",
                "filter_policy_version",
                "removed_spans",
                "disclosure_context",
                "evidence_refs",
            },
            "SanitizedEvidenceEnvelope",
        )
        return cls(
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            source_kind=EvidenceSourceKind(value["source_kind"]),  # type: ignore[arg-type]
            source_ref=_identifier(value["source_ref"], "source_ref", max_length=1024),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_payload=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["sanitized_payload"], "sanitized_payload"),
            ),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            filter_policy_version=_identifier(
                value["filter_policy_version"], "filter_policy_version"
            ),
            removed_spans=tuple(
                RemovedSpanSummary.from_json(item)
                for item in _objects(value["removed_spans"], "removed_spans")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            schema_version=_cognitive_schema_version(
                value["schema_version"], "SanitizedEvidenceEnvelope"
            ),
        )


@dataclass(frozen=True, slots=True)
class SanitizedEvidenceReceipt:
    receipt_id: str
    run_id: str
    subject: str
    evidence_id: str
    envelope_hash: str
    source_hash: str
    sanitized_hash: str
    filter_policy_version: str
    accepted: bool
    reason_codes: tuple[EvidenceReasonCode, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    admitted_at: float
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported SanitizedEvidenceReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.evidence_id, "evidence_id"),
            (self.filter_policy_version, "filter_policy_version"),
        ):
            _identifier(value, name)
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.source_hash, "source_hash"),
            (self.sanitized_hash, "sanitized_hash"),
        ):
            _digest(value, name)
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a boolean")
        reasons = tuple(EvidenceReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        if self.accepted != (EvidenceReasonCode.SANITIZED_AND_ACCEPTED in reasons):
            raise ValueError("accepted and reason_codes differ")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        admitted_at = _non_negative_number(self.admitted_at, "admitted_at")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "admitted_at", admitted_at)
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash("simple-harness/sanitized-evidence-receipt/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "source_hash": self.source_hash,
            "sanitized_hash": self.sanitized_hash,
            "filter_policy_version": self.filter_policy_version,
            "accepted": self.accepted,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "admitted_at": self.admitted_at,
        }

    def verify(self, envelope: SanitizedEvidenceEnvelope) -> None:
        if not self.accepted:
            raise ValueError("sanitized evidence receipt is not accepted")
        expected = (
            self.run_id,
            self.subject,
            self.evidence_id,
            self.envelope_hash,
            self.source_hash,
            self.sanitized_hash,
            self.filter_policy_version,
        )
        actual = (
            envelope.run_id,
            envelope.subject,
            envelope.evidence_id,
            envelope.envelope_hash,
            envelope.source_hash,
            envelope.sanitized_hash,
            envelope.filter_policy_version,
        )
        if expected != actual:
            raise ValueError("sanitized evidence receipt does not bind the envelope")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SanitizedEvidenceReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "run_id",
                "subject",
                "evidence_id",
                "envelope_hash",
                "source_hash",
                "sanitized_hash",
                "filter_policy_version",
                "accepted",
                "reason_codes",
                "disclosure_context",
                "evidence_refs",
                "admitted_at",
            },
            "SanitizedEvidenceReceipt",
        )
        accepted = value["accepted"]
        if not isinstance(accepted, bool):
            raise TypeError("accepted must be a boolean")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            envelope_hash=_digest(value["envelope_hash"], "envelope_hash"),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            filter_policy_version=_identifier(
                value["filter_policy_version"], "filter_policy_version"
            ),
            accepted=accepted,
            reason_codes=tuple(
                EvidenceReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            admitted_at=_non_negative_number(value["admitted_at"], "admitted_at"),
            schema_version=_cognitive_schema_version(
                value["schema_version"], "SanitizedEvidenceReceipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class ConversationEvidenceRegistration:
    """Post-ingestion Host registration for Short-Horizon eligibility."""

    registration_id: str
    envelope: SanitizedEvidenceEnvelope
    admission_receipt: SanitizedEvidenceReceipt
    metadata: ConversationEvidenceMetadata
    metadata_receipt: ConversationEvidenceMetadataReceipt
    recall_item_authority: EvidenceItemAuthority | None = None
    schema_version: int = CONVERSATION_EVIDENCE_SCHEMA_VERSION
    registration_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CONVERSATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported ConversationEvidenceRegistration schema_version")
        _identifier(self.registration_id, "registration_id")
        if not isinstance(self.envelope, SanitizedEvidenceEnvelope):
            raise TypeError("envelope must use SanitizedEvidenceEnvelope")
        if not isinstance(self.admission_receipt, SanitizedEvidenceReceipt):
            raise TypeError("admission_receipt must use SanitizedEvidenceReceipt")
        if not isinstance(self.metadata, ConversationEvidenceMetadata):
            raise TypeError("metadata must use ConversationEvidenceMetadata")
        if not isinstance(self.metadata_receipt, ConversationEvidenceMetadataReceipt):
            raise TypeError("metadata_receipt must use ConversationEvidenceMetadataReceipt")
        self.admission_receipt.verify(self.envelope)
        metadata = self.metadata
        metadata_receipt = self.metadata_receipt
        envelope = self.envelope
        admission = self.admission_receipt
        expected_metadata_binding = (
            metadata.evidence_id,
            metadata.envelope_hash,
            metadata.admission_receipt_id,
            metadata.admission_receipt_hash,
            metadata.run_id,
            metadata.subject,
            metadata.source_hash,
            metadata.sanitized_hash,
        )
        actual_evidence_binding = (
            envelope.evidence_id,
            envelope.envelope_hash,
            admission.receipt_id,
            admission.receipt_hash,
            envelope.run_id,
            envelope.subject,
            envelope.source_hash,
            envelope.sanitized_hash,
        )
        if expected_metadata_binding != actual_evidence_binding:
            raise ValueError("conversation metadata differs from admitted evidence")
        expected_source_kind = {
            ConversationEvidenceRole.USER: EvidenceSourceKind.USER_MESSAGE,
            ConversationEvidenceRole.ASSISTANT: EvidenceSourceKind.ASSISTANT_MESSAGE,
            ConversationEvidenceRole.TOOL: EvidenceSourceKind.TOOL_RESULT,
            ConversationEvidenceRole.RUNTIME: EvidenceSourceKind.RUNTIME_EVENT,
        }[metadata.role]
        if envelope.source_kind is not expected_source_kind:
            raise ValueError("conversation role differs from admitted evidence source_kind")
        expected_receipt_binding = (
            metadata_receipt.metadata_id,
            metadata_receipt.authority_issuer_id,
            metadata_receipt.evidence_id,
            metadata_receipt.envelope_hash,
            metadata_receipt.admission_receipt_id,
            metadata_receipt.admission_receipt_hash,
            metadata_receipt.run_id,
            metadata_receipt.subject,
            metadata_receipt.source_hash,
            metadata_receipt.sanitized_hash,
            metadata_receipt.metadata_hash,
        )
        actual_metadata_binding = (
            metadata.metadata_id,
            metadata.authority_issuer_id,
            metadata.evidence_id,
            metadata.envelope_hash,
            metadata.admission_receipt_id,
            metadata.admission_receipt_hash,
            metadata.run_id,
            metadata.subject,
            metadata.source_hash,
            metadata.sanitized_hash,
            metadata.metadata_hash,
        )
        if expected_receipt_binding != actual_metadata_binding:
            raise ValueError("conversation metadata authority receipt differs")
        if metadata.has_authorized_public_text:
            if type(self.recall_item_authority) is not EvidenceItemAuthority:
                raise ValueError(
                    "authorized public_text requires exact EvidenceItemAuthority"
                )
            expected_recall_binding = _derive_authorized_public_text_binding(
                envelope,
                admission,
                cast(EvidenceItemAuthority, self.recall_item_authority),
            )
            actual_recall_binding = (
                metadata.public_text_json_pointer,
                metadata.public_text_hash,
                metadata.public_text_normalization_version,
                metadata.evidence_item_authority_id,
                metadata.evidence_item_authority_hash,
                metadata.effective_privacy_class,
                metadata.information_attributes,
                metadata.classification_authority_ref,
            )
            if actual_recall_binding != expected_recall_binding:
                raise ValueError(
                    "conversation authorized public_text differs from item authority"
                )
        elif self.recall_item_authority is not None:
            raise ValueError(
                "recall_item_authority requires authorized public_text metadata"
            )
        object.__setattr__(
            self,
            "registration_hash",
            _domain_hash("simple-harness/conversation-evidence-registration/v3", self.to_json()),
        )

    @property
    def short_horizon_eligible(self) -> bool:
        return (
            self.metadata.belongs_to_primary_conversation
            and self.metadata.has_authorized_public_text
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "registration_id": self.registration_id,
            "evidence_id": self.envelope.evidence_id,
            "envelope_hash": self.envelope.envelope_hash,
            "admission_receipt_id": self.admission_receipt.receipt_id,
            "admission_receipt_hash": self.admission_receipt.receipt_hash,
            "metadata": self.metadata.to_json(),
            "metadata_receipt": self.metadata_receipt.to_json(),
            "recall_item_authority": (
                None
                if self.recall_item_authority is None
                else self.recall_item_authority.to_json()
            ),
        }


@dataclass(frozen=True, slots=True)
class ConversationEvidenceRegistrationRef:
    registration_id: str
    registration_hash: str
    evidence_id: str
    envelope_hash: str

    def __post_init__(self) -> None:
        _identifier(self.registration_id, "registration_id")
        _digest(self.registration_hash, "registration_hash")
        _identifier(self.evidence_id, "evidence_id")
        _digest(self.envelope_hash, "envelope_hash")


class ConversationEvidenceAuthorityVerifierPort(Protocol):
    async def resolve_conversation_registration(
        self, reference: ConversationEvidenceRegistrationRef
    ) -> ConversationEvidenceRegistration: ...


async def verify_conversation_evidence_registration(
    reference: ConversationEvidenceRegistrationRef,
    verifier: ConversationEvidenceAuthorityVerifierPort,
) -> ConversationEvidenceMetadata:
    """Resolve a durable Host registration; failure only denies derived indexing."""

    registration = await verifier.resolve_conversation_registration(reference)
    if (
        registration.registration_id,
        registration.registration_hash,
        registration.envelope.evidence_id,
        registration.envelope.envelope_hash,
    ) != (
        reference.registration_id,
        reference.registration_hash,
        reference.evidence_id,
        reference.envelope_hash,
    ):
        raise ValueError("conversation evidence registration reference differs")
    if not registration.short_horizon_eligible:
        if not registration.metadata.belongs_to_primary_conversation:
            raise ValueError("conversation evidence is not from the primary conversation")
        raise ValueError("conversation evidence has no authorized public_text")
    return registration.metadata


@dataclass(frozen=True, slots=True)
class EvidenceItemAuthority:
    """Host-issued provenance for one item inside an admitted envelope."""

    schema_version: int
    authority_id: str
    evidence_id: str
    envelope_hash: str
    sanitized_hash: str
    source_hash: str
    source_kind: EvidenceSourceKind
    item_ordinal: int
    item_id: str
    item_json_pointer: str
    normalization_version: str
    actor_role: EvidenceActorRole
    provenance: EvidenceProvenance
    required_privacy_class: PrivacyClass
    required_information_attributes: tuple[InformationAttribute, ...]
    classification_authority_ref: str
    issuer_ref: str
    authority_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported EvidenceItemAuthority schema_version")
        _identifier(self.authority_id, "authority_id")
        _identifier(self.evidence_id, "evidence_id")
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.sanitized_hash, "sanitized_hash"),
            (self.source_hash, "source_hash"),
        ):
            _digest(value, name)
        _identifier(self.item_id, "item_id", max_length=1024)
        object.__setattr__(self, "source_kind", EvidenceSourceKind(self.source_kind))
        _positive_int(self.item_ordinal, "item_ordinal")
        pointer = _bounded_text(self.item_json_pointer, "item_json_pointer", max_bytes=2048)
        if not pointer.startswith("/"):
            raise ValueError("item_json_pointer must be an RFC 6901 absolute pointer")
        _normalize_evidence_text("", self.normalization_version)
        object.__setattr__(self, "actor_role", EvidenceActorRole(self.actor_role))
        object.__setattr__(self, "provenance", EvidenceProvenance(self.provenance))
        privacy = PrivacyClass(self.required_privacy_class)
        if not isinstance(self.required_information_attributes, (tuple, list)):
            raise TypeError("required_information_attributes must be an array")
        try:
            attributes = tuple(
                InformationAttribute(item)
                for item in self.required_information_attributes
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "required_information_attributes contains an unknown value"
            ) from exc
        if len(attributes) > 32:
            raise ValueError("required_information_attributes exceeds the item limit")
        if len(set(attributes)) != len(attributes):
            raise ValueError("required_information_attributes must be unique")
        attributes = tuple(sorted(attributes, key=lambda item: item.value))
        _identifier(
            self.classification_authority_ref,
            "classification_authority_ref",
            max_length=1024,
        )
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        object.__setattr__(self, "required_privacy_class", privacy)
        object.__setattr__(self, "required_information_attributes", attributes)
        object.__setattr__(
            self,
            "authority_hash",
            _domain_hash("simple-harness/evidence-item-authority/v3", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "sanitized_hash": self.sanitized_hash,
            "source_hash": self.source_hash,
            "source_kind": self.source_kind.value,
            "item_ordinal": self.item_ordinal,
            "item_id": self.item_id,
            "item_json_pointer": self.item_json_pointer,
            "normalization_version": self.normalization_version,
            "actor_role": self.actor_role.value,
            "provenance": self.provenance.value,
            "required_privacy_class": self.required_privacy_class.value,
            "required_information_attributes": [
                item.value for item in self.required_information_attributes
            ],
            "classification_authority_ref": self.classification_authority_ref,
            "issuer_ref": self.issuer_ref,
        }


def _derive_authorized_public_text_binding(
    envelope: SanitizedEvidenceEnvelope,
    receipt: SanitizedEvidenceReceipt,
    authority: EvidenceItemAuthority,
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    PrivacyClass,
    tuple[InformationAttribute, ...],
    str,
]:
    """Derive the only recallable text binding from trusted admitted evidence."""

    receipt.verify(envelope)
    if type(authority) is not EvidenceItemAuthority:
        raise TypeError("authority must use exact EvidenceItemAuthority")
    if authority.authority_hash != _domain_hash(
        "simple-harness/evidence-item-authority/v3", authority.to_json()
    ):
        raise ValueError("EvidenceItemAuthority hash differs")
    if (
        authority.evidence_id,
        authority.envelope_hash,
        authority.sanitized_hash,
        authority.source_hash,
        authority.source_kind,
    ) != (
        envelope.evidence_id,
        envelope.envelope_hash,
        envelope.sanitized_hash,
        envelope.source_hash,
        envelope.source_kind,
    ):
        raise ValueError("EvidenceItemAuthority differs from admitted evidence")
    pointer = _rfc6901_absolute_pointer(
        authority.item_json_pointer, "item_json_pointer"
    )
    public_text = _resolve_json_pointer(
        _thaw_object(envelope.sanitized_payload), pointer
    )
    if not isinstance(public_text, str):
        raise ValueError("authorized public_text pointer must resolve to a string")
    normalized = _normalize_evidence_text(
        public_text, authority.normalization_version
    )
    return (
        pointer,
        _canonical_text_digest(normalized),
        authority.normalization_version,
        authority.authority_id,
        authority.authority_hash,
        authority.required_privacy_class,
        authority.required_information_attributes,
        authority.classification_authority_ref,
    )


@dataclass(frozen=True, slots=True)
class TypedObservationAuthorityReceipt:
    """Trusted resolver result; it is never accepted from the model payload."""

    receipt_id: str
    evidence_id: str
    envelope_hash: str
    sanitized_hash: str
    admission_receipt_id: str
    admission_receipt_hash: str
    item_ordinal: int
    item_id: str
    item_json_pointer: str
    schema_id: str
    schema_version: int
    registered_schema_hash: str
    json_pointer: str
    value_hash: str
    accepted: bool
    issuer_ref: str
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.receipt_id, "receipt_id")
        _identifier(self.evidence_id, "evidence_id")
        _digest(self.envelope_hash, "envelope_hash")
        _digest(self.sanitized_hash, "sanitized_hash")
        _identifier(self.admission_receipt_id, "admission_receipt_id")
        _digest(self.admission_receipt_hash, "admission_receipt_hash")
        _positive_int(self.item_ordinal, "item_ordinal")
        _identifier(self.item_id, "item_id", max_length=1024)
        item_pointer = _bounded_text(self.item_json_pointer, "item_json_pointer", max_bytes=2048)
        if not item_pointer.startswith("/"):
            raise ValueError("item_json_pointer must be an RFC 6901 absolute pointer")
        _identifier(self.schema_id, "schema_id", max_length=512)
        _positive_int(self.schema_version, "schema_version")
        _digest(self.registered_schema_hash, "registered_schema_hash")
        pointer = _bounded_text(self.json_pointer, "json_pointer", max_bytes=2048)
        if not pointer.startswith("/"):
            raise ValueError("json_pointer must be an RFC 6901 absolute pointer")
        _digest(self.value_hash, "value_hash")
        if self.accepted is not True:
            raise ValueError("typed observation authority receipt must be accepted")
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash(
                "simple-harness/typed-observation-authority-receipt/v2",
                self.to_json(),
            ),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "receipt_id": self.receipt_id,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "sanitized_hash": self.sanitized_hash,
            "admission_receipt_id": self.admission_receipt_id,
            "admission_receipt_hash": self.admission_receipt_hash,
            "item_ordinal": self.item_ordinal,
            "item_id": self.item_id,
            "item_json_pointer": self.item_json_pointer,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "registered_schema_hash": self.registered_schema_hash,
            "json_pointer": self.json_pointer,
            "value_hash": self.value_hash,
            "accepted": self.accepted,
            "issuer_ref": self.issuer_ref,
        }


@dataclass(frozen=True, slots=True)
class AdmittedEvidenceAuthority:
    """Trusted resolver result; never deserialized from an LLM tool call."""

    envelope: SanitizedEvidenceEnvelope
    receipt: SanitizedEvidenceReceipt
    item_authority: EvidenceItemAuthority

    def __post_init__(self) -> None:
        if type(self.envelope) is not SanitizedEvidenceEnvelope:
            raise TypeError("envelope must use SanitizedEvidenceEnvelope")
        if type(self.receipt) is not SanitizedEvidenceReceipt:
            raise TypeError("receipt must use SanitizedEvidenceReceipt")
        if type(self.item_authority) is not EvidenceItemAuthority:
            raise TypeError("item_authority must use EvidenceItemAuthority")


def authorize_conversation_public_text(
    metadata: ConversationEvidenceMetadata,
    admitted: AdmittedEvidenceAuthority,
) -> ConversationEvidenceMetadata:
    """Host-only derivation of the text and classification eligible for recall.

    Consumers never provide a pointer or classification.  The Host first
    resolves ``AdmittedEvidenceAuthority`` and this function derives every
    optional recall field as one all-or-none binding.
    """

    if type(metadata) is not ConversationEvidenceMetadata:
        raise TypeError("metadata must use exact ConversationEvidenceMetadata")
    if type(admitted) is not AdmittedEvidenceAuthority:
        raise TypeError("admitted must use exact AdmittedEvidenceAuthority")
    envelope = admitted.envelope
    if (
        metadata.evidence_id,
        metadata.envelope_hash,
        metadata.admission_receipt_id,
        metadata.admission_receipt_hash,
        metadata.run_id,
        metadata.subject,
        metadata.source_hash,
        metadata.sanitized_hash,
    ) != (
        envelope.evidence_id,
        envelope.envelope_hash,
        admitted.receipt.receipt_id,
        admitted.receipt.receipt_hash,
        envelope.run_id,
        envelope.subject,
        envelope.source_hash,
        envelope.sanitized_hash,
    ):
        raise ValueError("conversation metadata differs from admitted evidence")
    binding = _derive_authorized_public_text_binding(
        envelope, admitted.receipt, admitted.item_authority
    )
    return replace(
        metadata,
        public_text_json_pointer=binding[0],
        public_text_hash=binding[1],
        public_text_normalization_version=binding[2],
        evidence_item_authority_id=binding[3],
        evidence_item_authority_hash=binding[4],
        effective_privacy_class=binding[5],
        information_attributes=binding[6],
        classification_authority_ref=binding[7],
    )


class EvidenceAuthorityVerifierPort(Protocol):
    """Authority seam used by Memory before accepting any proposed span."""

    async def resolve_admitted_evidence(
        self, span: EvidenceSpanRef
    ) -> AdmittedEvidenceAuthority: ...

    async def resolve_typed_observation(
        self, reference: ProposedTypedObservationRef
    ) -> TypedObservationAuthorityReceipt: ...


def _resolve_json_pointer(document: object, pointer: str) -> object:
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ValueError("evidence JSON pointer does not exist")
            current = current[token]
        elif isinstance(current, (tuple, list)):
            if not token.isdigit():
                raise ValueError("evidence JSON pointer array index is invalid")
            index = int(token)
            if index >= len(current):
                raise ValueError("evidence JSON pointer array index is out of range")
            current = current[index]
        else:
            raise ValueError("evidence JSON pointer traverses a scalar")
    return current


def _verify_evidence_span_authority(
    span: EvidenceSpanRef,
    envelope: SanitizedEvidenceEnvelope,
    receipt: SanitizedEvidenceReceipt,
    authority: EvidenceItemAuthority,
    *,
    typed_observation_receipt: TypedObservationAuthorityReceipt | None = None,
) -> None:
    """Fail closed unless a proposal is proven against trusted admitted data."""

    if authority.schema_version != EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION:
        raise ValueError("unsupported EvidenceItemAuthority schema_version")
    expected_authority_hash = _domain_hash(
        "simple-harness/evidence-item-authority/v3", authority.to_json()
    )
    if authority.authority_hash != expected_authority_hash:
        raise ValueError("EvidenceItemAuthority hash differs")
    receipt.verify(envelope)
    if (
        span.admission_receipt_id != receipt.receipt_id
        or span.admission_receipt_hash != receipt.receipt_hash
    ):
        raise ValueError("evidence span differs from admission receipt")
    if (
        span.evidence_id,
        span.envelope_hash,
        span.sanitized_hash,
        span.source_hash,
        span.source_kind,
        span.item_ordinal,
        span.item_id,
        span.item_json_pointer,
        span.normalization_version,
        span.actor_role,
        span.provenance,
    ) != (
        authority.evidence_id,
        authority.envelope_hash,
        authority.sanitized_hash,
        authority.source_hash,
        authority.source_kind,
        authority.item_ordinal,
        authority.item_id,
        authority.item_json_pointer,
        authority.normalization_version,
        authority.actor_role,
        authority.provenance,
    ):
        raise ValueError("evidence span differs from Host item authority")
    if (
        envelope.evidence_id != span.evidence_id
        or envelope.envelope_hash != span.envelope_hash
        or envelope.sanitized_hash != span.sanitized_hash
        or envelope.source_hash != span.source_hash
        or envelope.source_kind is not span.source_kind
    ):
        raise ValueError("evidence span differs from admitted envelope")
    projected = _resolve_json_pointer(
        _thaw_object(envelope.sanitized_payload), span.item_json_pointer
    )
    if not isinstance(projected, str):
        raise ValueError("evidence span pointer must resolve to canonical text")
    normalized = _normalize_evidence_text(projected, span.normalization_version)
    encoded = normalized.encode("utf-8")
    if span.end_byte > len(encoded):
        raise ValueError("evidence span exceeds canonical text UTF-8 bytes")
    try:
        selected = encoded[span.start_byte : span.end_byte].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("evidence span splits a UTF-8 codepoint") from exc
    if selected != span.exact_quote:
        raise ValueError("evidence span does not select exact_quote")

    expected_provenance = {
        EvidenceSupportKind.EXPLICIT_USER_ASSERTION: (
            EvidenceActorRole.USER,
            EvidenceProvenance.AUTHENTICATED_USER,
        ),
        EvidenceSupportKind.EXPLICIT_USER_CORRECTION: (
            EvidenceActorRole.USER,
            EvidenceProvenance.AUTHENTICATED_USER,
        ),
        EvidenceSupportKind.RUNTIME_EVENT: (
            EvidenceActorRole.RUNTIME,
            EvidenceProvenance.HOST_RUNTIME,
        ),
        EvidenceSupportKind.MODEL_INFERENCE: (
            EvidenceActorRole.ASSISTANT,
            EvidenceProvenance.MODEL_OUTPUT,
        ),
    }
    if span.support_kind is EvidenceSupportKind.TYPED_OBSERVATION and (
        span.actor_role,
        span.provenance,
    ) not in {
        (EvidenceActorRole.TOOL, EvidenceProvenance.TRUSTED_TOOL),
        (EvidenceActorRole.EXTERNAL, EvidenceProvenance.EXTERNAL_SOURCE),
    }:
        raise ValueError("typed observation conflicts with trusted provenance")
    required = expected_provenance.get(span.support_kind)
    if required is not None and (span.actor_role, span.provenance) != required:
        raise ValueError("evidence support kind conflicts with trusted provenance")

    proposed_observation = span.typed_observation
    if proposed_observation is None:
        if typed_observation_receipt is not None:
            raise ValueError("unexpected typed observation authority receipt")
        return
    if typed_observation_receipt is None:
        raise ValueError("typed observation authority receipt is required")
    if (
        typed_observation_receipt.evidence_id,
        typed_observation_receipt.envelope_hash,
        typed_observation_receipt.sanitized_hash,
        typed_observation_receipt.admission_receipt_id,
        typed_observation_receipt.admission_receipt_hash,
        typed_observation_receipt.item_ordinal,
        typed_observation_receipt.item_id,
        typed_observation_receipt.item_json_pointer,
    ) != (
        span.evidence_id,
        span.envelope_hash,
        span.sanitized_hash,
        span.admission_receipt_id,
        span.admission_receipt_hash,
        span.item_ordinal,
        span.item_id,
        span.item_json_pointer,
    ):
        raise ValueError("typed observation authority receipt differs from admitted evidence item")
    if (
        proposed_observation.observation_receipt_id,
        proposed_observation.observation_receipt_hash,
        proposed_observation.authority_issuer_id,
        proposed_observation.schema_id,
        proposed_observation.schema_version,
        proposed_observation.registered_schema_hash,
        proposed_observation.json_pointer,
        proposed_observation.value_hash,
    ) != (
        typed_observation_receipt.receipt_id,
        typed_observation_receipt.receipt_hash,
        typed_observation_receipt.issuer_ref,
        typed_observation_receipt.schema_id,
        typed_observation_receipt.schema_version,
        typed_observation_receipt.registered_schema_hash,
        typed_observation_receipt.json_pointer,
        typed_observation_receipt.value_hash,
    ):
        raise ValueError("typed observation proposal differs from authority receipt")
    observed_value = _resolve_json_pointer(
        _thaw_object(envelope.sanitized_payload), proposed_observation.json_pointer
    )
    observed_json = thaw_json(cast(FrozenJsonValue, observed_value))
    import hashlib

    observed_hash = hashlib.sha256(canonical_json(observed_json).encode("utf-8")).hexdigest()
    if observed_hash != proposed_observation.value_hash:
        raise ValueError("typed observation value_hash differs from admitted evidence")


async def verify_evidence_span(
    span: EvidenceSpanRef,
    verifier: EvidenceAuthorityVerifierPort,
) -> EvidenceItemAuthority:
    """Production-safe span verification through the injected authority port.

    Callers cannot supply an ``EvidenceItemAuthority`` directly.  The trusted
    Host/Memory resolver must return it, and typed observations require a
    second accepted authority receipt resolved by the same port.
    """

    admitted = await verifier.resolve_admitted_evidence(span)
    if type(admitted) is not AdmittedEvidenceAuthority:
        raise TypeError(
            "authority resolver must return exact AdmittedEvidenceAuthority"
        )
    typed_receipt = None
    if span.typed_observation is not None:
        typed_receipt = await verifier.resolve_typed_observation(span.typed_observation)
    _verify_evidence_span_authority(
        span,
        admitted.envelope,
        admitted.receipt,
        admitted.item_authority,
        typed_observation_receipt=typed_receipt,
    )
    return admitted.item_authority


class ExecutionEvidenceKind(StrEnum):
    PROVIDER_INVOCATION = "provider_invocation"
    TOOL_INVOCATION = "tool_invocation"
    CONTEXT_SNAPSHOT = "context_snapshot"
    ROUTE_DECISION = "route_decision"
    RUN_TERMINAL = "run_terminal"


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    event_id: str
    run_id: str
    subject: str
    kind: ExecutionEvidenceKind
    public_payload: Mapping[str, FrozenJsonValue]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    occurred_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported ExecutionEvidence schema_version")
        for value, name in (
            (self.event_id, "event_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        object.__setattr__(self, "kind", ExecutionEvidenceKind(self.kind))
        payload = _freeze_object(self.public_payload, "public_payload")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        occurred_at = _non_negative_number(self.occurred_at, "occurred_at")
        object.__setattr__(self, "public_payload", payload)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "evidence_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "kind": self.kind.value,
            "public_payload": _thaw_object(self.public_payload),
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ExecutionEvidence:
        _exact_keys(
            value,
            {
                "schema_version",
                "event_id",
                "run_id",
                "subject",
                "kind",
                "public_payload",
                "disclosure_context",
                "evidence_refs",
                "idempotency_key",
                "occurred_at",
            },
            "ExecutionEvidence",
        )
        return cls(
            event_id=_identifier(value["event_id"], "event_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            kind=ExecutionEvidenceKind(value["kind"]),  # type: ignore[arg-type]
            public_payload=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["public_payload"], "public_payload"),
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            occurred_at=_non_negative_number(value["occurred_at"], "occurred_at"),
            schema_version=_schema_version(value["schema_version"], "ExecutionEvidence"),
        )


@dataclass(frozen=True, slots=True)
class AnalysisBudget:
    max_input_tokens: int
    max_output_tokens: int
    deadline_ms: int
    max_cost_microunits: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "deadline_ms"):
            _positive_int(getattr(self, name), name)
        if (
            isinstance(self.max_cost_microunits, bool)
            or not isinstance(self.max_cost_microunits, int)
            or self.max_cost_microunits < 0
        ):
            raise ValueError("max_cost_microunits must be a non-negative integer")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_ms": self.deadline_ms,
            "max_cost_microunits": self.max_cost_microunits,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> AnalysisBudget:
        _exact_keys(
            value,
            {"max_input_tokens", "max_output_tokens", "deadline_ms", "max_cost_microunits"},
            "AnalysisBudget",
        )
        cost = value["max_cost_microunits"]
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError("max_cost_microunits must be a non-negative integer")
        return cls(
            _positive_int(value["max_input_tokens"], "max_input_tokens"),
            _positive_int(value["max_output_tokens"], "max_output_tokens"),
            _positive_int(value["deadline_ms"], "deadline_ms"),
            cost,
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisRequest:
    job_id: str
    run_id: str
    subject: str
    ordered_evidence_refs: tuple[EvidenceRef, ...]
    prompt_version: str
    result_schema_version: str
    policy_version: str
    provider_id: str
    model_id: str
    model_config_hash: str
    attempt: int
    budget: AnalysisBudget
    disclosure_context: DisclosureContext
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisRequest schema_version")
        for value, name in (
            (self.job_id, "job_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.prompt_version, "prompt_version"),
            (self.result_schema_version, "result_schema_version"),
            (self.policy_version, "policy_version"),
            (self.provider_id, "provider_id"),
            (self.model_id, "model_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _digest(self.model_config_hash, "model_config_hash")
        _positive_int(self.attempt, "attempt")
        refs = _evidence_refs(self.ordered_evidence_refs, "ordered_evidence_refs")
        if not refs:
            raise ValueError("ordered_evidence_refs must be non-empty")
        if not isinstance(self.budget, AnalysisBudget):
            raise TypeError("budget must use AnalysisBudget")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        object.__setattr__(self, "ordered_evidence_refs", refs)
        object.__setattr__(self, "request_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "ordered_evidence_refs": [ref.to_json() for ref in self.ordered_evidence_refs],
            "prompt_version": self.prompt_version,
            "result_schema_version": self.result_schema_version,
            "policy_version": self.policy_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_config_hash": self.model_config_hash,
            "attempt": self.attempt,
            "budget": self.budget.to_json(),
            "disclosure_context": self.disclosure_context.to_json(),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisRequest:
        _exact_keys(
            value,
            {
                "schema_version",
                "job_id",
                "run_id",
                "subject",
                "ordered_evidence_refs",
                "prompt_version",
                "result_schema_version",
                "policy_version",
                "provider_id",
                "model_id",
                "model_config_hash",
                "attempt",
                "budget",
                "disclosure_context",
                "idempotency_key",
            },
            "MemoryAnalysisRequest",
        )
        return cls(
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            ordered_evidence_refs=_refs_from_json(
                value["ordered_evidence_refs"], "ordered_evidence_refs"
            ),
            prompt_version=_identifier(value["prompt_version"], "prompt_version"),
            result_schema_version=_identifier(
                value["result_schema_version"], "result_schema_version"
            ),
            policy_version=_identifier(value["policy_version"], "policy_version"),
            provider_id=_identifier(value["provider_id"], "provider_id"),
            model_id=_identifier(value["model_id"], "model_id"),
            model_config_hash=_digest(value["model_config_hash"], "model_config_hash"),
            attempt=_positive_int(value["attempt"], "attempt"),
            budget=AnalysisBudget.from_json(_object(value["budget"], "budget")),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisRequest"),
        )


class AnalysisValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class MemoryAnalysisResult:
    job_id: str
    run_id: str
    request_hash: str
    provider_response_id: str | None
    structured_result: Mapping[str, FrozenJsonValue]
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    latency_ms: int
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    result_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisResult schema_version")
        _identifier(self.job_id, "job_id")
        _identifier(self.run_id, "run_id")
        _digest(self.request_hash, "request_hash")
        _optional_identifier(self.provider_response_id, "provider_response_id")
        result = _freeze_object(self.structured_result, "structured_result")
        for name in ("input_tokens", "output_tokens", "cost_microunits", "latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "structured_result", result)
        object.__setattr__(self, "result_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "request_hash": self.request_hash,
            "provider_response_id": self.provider_response_id,
            "structured_result": _thaw_object(self.structured_result),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_microunits": self.cost_microunits,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisResult:
        _exact_keys(
            value,
            {
                "schema_version",
                "job_id",
                "run_id",
                "request_hash",
                "provider_response_id",
                "structured_result",
                "input_tokens",
                "output_tokens",
                "cost_microunits",
                "latency_ms",
            },
            "MemoryAnalysisResult",
        )
        counts: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens", "cost_microunits", "latency_ms"):
            count = value[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            counts[name] = count
        return cls(
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            provider_response_id=_optional_identifier(
                value["provider_response_id"], "provider_response_id"
            ),
            structured_result=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["structured_result"], "structured_result"),
            ),
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            cost_microunits=counts["cost_microunits"],
            latency_ms=counts["latency_ms"],
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisResult"),
        )


def _analysis_domain_hash(domain: str, payload: dict[str, JsonValue]) -> str:
    return _canonical_hash({"protocol": domain, "payload": payload})


@dataclass(frozen=True, slots=True)
class MemoryAnalysisDeliveryReceipt:
    """Host record proving durable delivery of one exact provider result.

    This receipt is separate from :class:`MemoryAnalysisReceipt`, which records
    Memory-side validation/application. Its public hashes are audit material,
    not authority; consumers must call ``MemoryAnalysisDeliveryAuthorityPort``.
    """

    receipt_id: str
    issuer_id: str
    run_id: str
    job_id: str
    request_hash: str
    result_hash: str
    attempt: int
    provider_response_id: str | None
    provider_response_hash: str
    issued_at: float
    host_receipt_id: str
    host_receipt_hash: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisDeliveryReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.issuer_id, "issuer_id"),
            (self.run_id, "run_id"),
            (self.job_id, "job_id"),
            (self.host_receipt_id, "host_receipt_id"),
        ):
            _identifier(value, name)
        _optional_identifier(self.provider_response_id, "provider_response_id")
        for value, name in (
            (self.request_hash, "request_hash"),
            (self.result_hash, "result_hash"),
            (self.provider_response_hash, "provider_response_hash"),
            (self.host_receipt_hash, "host_receipt_hash"),
        ):
            _digest(value, name)
        _positive_int(self.attempt, "attempt")
        issued_at = _non_negative_number(self.issued_at, "issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(
            self,
            "receipt_hash",
            _analysis_domain_hash("memory-analysis/delivery-receipt/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "issuer_id": self.issuer_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "request_hash": self.request_hash,
            "result_hash": self.result_hash,
            "attempt": self.attempt,
            "provider_response_id": self.provider_response_id,
            "provider_response_hash": self.provider_response_hash,
            "issued_at": self.issued_at,
            "host_receipt_id": self.host_receipt_id,
            "host_receipt_hash": self.host_receipt_hash,
        }

    def _verify_result_link(self, result: MemoryAnalysisResult) -> None:
        if not isinstance(result, MemoryAnalysisResult):
            raise TypeError("result must use MemoryAnalysisResult")
        exact = (
            (self.job_id, result.job_id),
            (self.run_id, result.run_id),
            (self.request_hash, result.request_hash),
            (self.result_hash, result.result_hash),
            (self.provider_response_id, result.provider_response_id),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Memory analysis delivery differs from result")

    def verify_result(self, request: MemoryAnalysisRequest, result: MemoryAnalysisResult) -> None:
        if not isinstance(request, MemoryAnalysisRequest):
            raise TypeError("request must use MemoryAnalysisRequest")
        self._verify_result_link(result)
        exact = (
            (self.job_id, request.job_id),
            (self.run_id, request.run_id),
            (self.request_hash, request.request_hash),
            (self.attempt, request.attempt),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Memory analysis delivery differs from request or result")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisDeliveryReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "issuer_id",
                "run_id",
                "job_id",
                "request_hash",
                "result_hash",
                "attempt",
                "provider_response_id",
                "provider_response_hash",
                "issued_at",
                "host_receipt_id",
                "host_receipt_hash",
            },
            "MemoryAnalysisDeliveryReceipt",
        )
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            issuer_id=_identifier(value["issuer_id"], "issuer_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            job_id=_identifier(value["job_id"], "job_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            result_hash=_digest(value["result_hash"], "result_hash"),
            attempt=_positive_int(value["attempt"], "attempt"),
            provider_response_id=_optional_identifier(
                value["provider_response_id"], "provider_response_id"
            ),
            provider_response_hash=_digest(
                value["provider_response_hash"], "provider_response_hash"
            ),
            issued_at=_non_negative_number(value["issued_at"], "issued_at"),
            host_receipt_id=_identifier(value["host_receipt_id"], "host_receipt_id"),
            host_receipt_hash=_digest(value["host_receipt_hash"], "host_receipt_hash"),
            schema_version=_schema_version(
                value["schema_version"], "MemoryAnalysisDeliveryReceipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisResultEnvelope:
    result: MemoryAnalysisResult
    delivery_receipt: MemoryAnalysisDeliveryReceipt
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    envelope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisResultEnvelope schema_version")
        if not isinstance(self.result, MemoryAnalysisResult):
            raise TypeError("result must use MemoryAnalysisResult")
        if not isinstance(self.delivery_receipt, MemoryAnalysisDeliveryReceipt):
            raise TypeError("delivery_receipt must use MemoryAnalysisDeliveryReceipt")
        self.delivery_receipt._verify_result_link(self.result)
        object.__setattr__(
            self,
            "envelope_hash",
            _analysis_domain_hash("memory-analysis/result-envelope/v1", self.to_json()),
        )

    def verify_request(self, request: MemoryAnalysisRequest) -> None:
        self.delivery_receipt.verify_result(request, self.result)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "result": self.result.to_json(),
            "delivery_receipt": self.delivery_receipt.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisResultEnvelope:
        _exact_keys(
            value,
            {"schema_version", "result", "delivery_receipt"},
            "MemoryAnalysisResultEnvelope",
        )
        return cls(
            result=MemoryAnalysisResult.from_json(_object(value["result"], "result")),
            delivery_receipt=MemoryAnalysisDeliveryReceipt.from_json(
                _object(value["delivery_receipt"], "delivery_receipt")
            ),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisResultEnvelope"),
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisReceipt:
    receipt_id: str
    job_id: str
    run_id: str
    request_hash: str
    result_hash: str
    validator_version: str
    validation_status: AnalysisValidationStatus
    reason_codes: tuple[EvidenceReasonCode, ...]
    committed_revision: int | None
    committed_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.job_id, "job_id"),
            (self.run_id, "run_id"),
            (self.validator_version, "validator_version"),
        ):
            _identifier(value, name)
        _digest(self.request_hash, "request_hash")
        _digest(self.result_hash, "result_hash")
        object.__setattr__(
            self, "validation_status", AnalysisValidationStatus(self.validation_status)
        )
        reasons = tuple(EvidenceReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        if self.validation_status is AnalysisValidationStatus.ACCEPTED:
            if EvidenceReasonCode.VALIDATOR_ACCEPTED not in reasons:
                raise ValueError("accepted analysis requires analysis_validator_accepted")
            if self.committed_revision is None:
                raise ValueError("accepted analysis requires committed_revision")
        elif self.committed_revision is not None:
            raise ValueError("non-accepted analysis cannot commit a revision")
        if self.committed_revision is not None:
            _positive_int(self.committed_revision, "committed_revision")
        committed_at = _non_negative_number(self.committed_at, "committed_at")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "committed_at", committed_at)
        object.__setattr__(self, "receipt_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "request_hash": self.request_hash,
            "result_hash": self.result_hash,
            "validator_version": self.validator_version,
            "validation_status": self.validation_status.value,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "committed_revision": self.committed_revision,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "job_id",
                "run_id",
                "request_hash",
                "result_hash",
                "validator_version",
                "validation_status",
                "reason_codes",
                "committed_revision",
                "committed_at",
            },
            "MemoryAnalysisReceipt",
        )
        revision = value["committed_revision"]
        if revision is not None:
            revision = _positive_int(revision, "committed_revision")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            result_hash=_digest(value["result_hash"], "result_hash"),
            validator_version=_identifier(value["validator_version"], "validator_version"),
            validation_status=AnalysisValidationStatus(value["validation_status"]),  # type: ignore[arg-type]
            reason_codes=tuple(
                EvidenceReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            committed_revision=revision,
            committed_at=_non_negative_number(value["committed_at"], "committed_at"),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisReceipt"),
        )


class MemoryAnalysisExecutorPort(Protocol):
    async def analyze_memory(
        self, request: MemoryAnalysisRequest
    ) -> MemoryAnalysisResultEnvelope: ...


class MemoryAnalysisDeliveryAuthorityPort(Protocol):
    """Verifies an exact delivery against Host durable state or authenticated proof."""

    async def verify_analysis_delivery(
        self, request: MemoryAnalysisRequest, envelope: MemoryAnalysisResultEnvelope
    ) -> None: ...


__all__ = (
    "CONVERSATION_EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION",
    "EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1",
    "AdmittedEvidenceAuthority",
    "AnalysisBudget",
    "AnalysisValidationStatus",
    "ConversationEvidenceAuthorityVerifierPort",
    "ConversationEvidenceMetadata",
    "ConversationEvidenceMetadataReceipt",
    "ConversationEvidenceRegistration",
    "ConversationEvidenceRegistrationRef",
    "ConversationEvidenceRole",
    "ConversationToolCausalLink",
    "EvidenceActorRole",
    "EvidenceAuthorityVerifierPort",
    "EvidenceItemAuthority",
    "EvidenceProvenance",
    "EvidenceReasonCode",
    "EvidenceRef",
    "EvidenceSourceKind",
    "EvidenceSpanRef",
    "EvidenceSupportKind",
    "ExecutionEvidence",
    "ExecutionEvidenceKind",
    "MemoryAnalysisDeliveryAuthorityPort",
    "MemoryAnalysisDeliveryReceipt",
    "MemoryAnalysisExecutorPort",
    "MemoryAnalysisReceipt",
    "MemoryAnalysisRequest",
    "MemoryAnalysisResult",
    "MemoryAnalysisResultEnvelope",
    "RemovedSpanSummary",
    "RemovedSpanType",
    "ProposedTypedObservationRef",
    "SanitizedEvidenceEnvelope",
    "SanitizedEvidenceReceipt",
    "TypedObservationAuthorityReceipt",
    "authorize_conversation_public_text",
    "verify_conversation_evidence_registration",
    "verify_evidence_span",
)
