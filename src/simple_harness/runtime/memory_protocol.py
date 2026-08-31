# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Human Memory routing and mutation proposal contracts.

The model proposes these values; deterministic Host/Memory code validates and
commits them.  Working Memory is deliberately a Context role, not a stored
``LongTermMemoryType`` member.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    fingerprint_json,
    freeze_json,
    thaw_json,
)

from .disclosure_protocol import (
    COGNITIVE_MEMORY_SCHEMA_VERSION,
    HUMAN_MEMORY_SCHEMA_VERSION,
    DeliveryRecipient,
    DisclosureContext,
    DisclosureGeneration,
    DisclosurePurpose,
    DisclosureSource,
    DisclosureTrust,
    IntendedAudience,
    _bounded_text,
    _canonical_hash,
    _digest,
    _domain_hash,
    _exact_keys,
    _identifier,
    _object,
    _objects,
    _optional_identifier,
    _positive_int,
    _schema_version,
    _strings,
)
from .evidence_protocol import (
    EvidenceRef,
    EvidenceSpanRef,
    _evidence_refs,
    _refs_from_json,
)

RECALL_DECISION_SCHEMA_VERSION = 3


class LongTermMemoryType(StrEnum):
    EPISODE = "episode"
    SEMANTIC = "semantic"
    PROCEDURE = "procedure"
    PROSPECTIVE = "prospective"


class WorkingMemoryRole(StrEnum):
    CURRENT_QUERY = "current_query"
    RECENT_CAUSAL_WINDOW = "recent_causal_window"
    TASK_SCOPE_PROJECTION = "task_scope_projection"
    RECALLED_MEMORY = "recalled_memory"
    TOOL_STATE = "tool_state"


class ContextFragmentType(StrEnum):
    CURRENT_QUERY = "current_query"
    RECENT_CAUSAL_WINDOW = "recent_causal_window"
    SHORT_HORIZON = "short_horizon"
    TASK_SCOPE_PROJECTION = "task_scope_projection"
    RECALLED_MEMORY = "recalled_memory"
    TOOL_CATALOG = "tool_catalog"
    SKILL_INSTRUCTIONS = "skill_instructions"


@dataclass(frozen=True, slots=True)
class ContextFragment:
    fragment_id: str
    run_id: str
    subject: str
    fragment_type: ContextFragmentType
    source_ref: str
    source_revision: int
    content_hash: str
    token_estimate: int
    byte_estimate: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    fragment_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported ContextFragment schema_version")
        for value, name in (
            (self.fragment_id, "fragment_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
        ):
            _identifier(value, name)
        object.__setattr__(self, "fragment_type", ContextFragmentType(self.fragment_type))
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _positive_int(self.source_revision, "source_revision")
        _digest(self.content_hash, "content_hash")
        for estimate_value, estimate_name in (
            (self.token_estimate, "token_estimate"),
            (self.byte_estimate, "byte_estimate"),
        ):
            if (
                isinstance(estimate_value, bool)
                or not isinstance(estimate_value, int)
                or estimate_value < 0
            ):
                raise ValueError(f"{estimate_name} must be a non-negative integer")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "fragment_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "fragment_id": self.fragment_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "fragment_type": self.fragment_type.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "content_hash": self.content_hash,
            "token_estimate": self.token_estimate,
            "byte_estimate": self.byte_estimate,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextFragment:
        _exact_keys(
            value,
            {
                "schema_version",
                "fragment_id",
                "run_id",
                "subject",
                "fragment_type",
                "source_ref",
                "source_revision",
                "content_hash",
                "token_estimate",
                "byte_estimate",
                "disclosure_context",
                "evidence_refs",
            },
            "ContextFragment",
        )
        estimates: dict[str, int] = {}
        for name in ("token_estimate", "byte_estimate"):
            estimate = value[name]
            if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            estimates[name] = estimate
        return cls(
            fragment_id=_identifier(value["fragment_id"], "fragment_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            fragment_type=ContextFragmentType(value["fragment_type"]),  # type: ignore[arg-type]
            source_ref=_identifier(value["source_ref"], "source_ref", max_length=1024),
            source_revision=_positive_int(value["source_revision"], "source_revision"),
            content_hash=_digest(value["content_hash"], "content_hash"),
            token_estimate=estimates["token_estimate"],
            byte_estimate=estimates["byte_estimate"],
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            schema_version=_schema_version(value["schema_version"], "ContextFragment"),
        )


@dataclass(frozen=True, slots=True)
class ContextAssemblyBudget:
    max_total_tokens: int
    max_total_bytes: int
    generation_reserve_tokens: int
    safety_reserve_tokens: int

    def __post_init__(self) -> None:
        for name in (
            "max_total_tokens",
            "max_total_bytes",
            "generation_reserve_tokens",
            "safety_reserve_tokens",
        ):
            _positive_int(getattr(self, name), name)
        if self.generation_reserve_tokens + self.safety_reserve_tokens >= self.max_total_tokens:
            raise ValueError("generation and safety reserves must leave an input token budget")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "max_total_tokens": self.max_total_tokens,
            "max_total_bytes": self.max_total_bytes,
            "generation_reserve_tokens": self.generation_reserve_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextAssemblyBudget:
        _exact_keys(
            value,
            {
                "max_total_tokens",
                "max_total_bytes",
                "generation_reserve_tokens",
                "safety_reserve_tokens",
            },
            "ContextAssemblyBudget",
        )
        return cls(
            _positive_int(value["max_total_tokens"], "max_total_tokens"),
            _positive_int(value["max_total_bytes"], "max_total_bytes"),
            _positive_int(value["generation_reserve_tokens"], "generation_reserve_tokens"),
            _positive_int(value["safety_reserve_tokens"], "safety_reserve_tokens"),
        )


class ContextAssemblyReasonCode(StrEnum):
    INCLUDED = "context_fragment_included"
    DUPLICATE_OMITTED = "context_fragment_duplicate_omitted"
    TOKEN_BUDGET_OMITTED = "context_fragment_token_budget_omitted"
    BYTE_BUDGET_OMITTED = "context_fragment_byte_budget_omitted"
    DISCLOSURE_DENIED = "context_fragment_disclosure_denied"
    STALE_SOURCE_OMITTED = "context_fragment_stale_source_omitted"
    SUPPRESSED = "context_fragment_suppressed"


@dataclass(frozen=True, slots=True)
class ContextAssemblyDecision:
    decision_id: str
    run_id: str
    subject: str
    selected_fragment_refs: tuple[str, ...]
    omitted_fragment_refs: tuple[str, ...]
    snapshot_refs: tuple[str, ...]
    budget: ContextAssemblyBudget
    selected_token_estimate: int
    selected_byte_estimate: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    reason_codes: tuple[ContextAssemblyReasonCode, ...]
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported ContextAssemblyDecision schema_version")
        for value, name in (
            (self.decision_id, "decision_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        selected = tuple(
            _identifier(item, "selected_fragment_ref")
            for item in self.selected_fragment_refs
        )
        omitted = tuple(
            _identifier(item, "omitted_fragment_ref")
            for item in self.omitted_fragment_refs
        )
        snapshots = tuple(
            _identifier(item, "snapshot_ref", max_length=1024) for item in self.snapshot_refs
        )
        if len(set(selected)) != len(selected) or len(set(omitted)) != len(omitted):
            raise ValueError("fragment refs must be unique within each outcome")
        if set(selected) & set(omitted):
            raise ValueError("a fragment cannot be both selected and omitted")
        if not snapshots or len(set(snapshots)) != len(snapshots):
            raise ValueError("snapshot_refs must be non-empty and unique")
        if not isinstance(self.budget, ContextAssemblyBudget):
            raise TypeError("budget must use ContextAssemblyBudget")
        for estimate_value, estimate_name in (
            (self.selected_token_estimate, "selected_token_estimate"),
            (self.selected_byte_estimate, "selected_byte_estimate"),
        ):
            if (
                isinstance(estimate_value, bool)
                or not isinstance(estimate_value, int)
                or estimate_value < 0
            ):
                raise ValueError(f"{estimate_name} must be a non-negative integer")
        available_tokens = (
            self.budget.max_total_tokens
            - self.budget.generation_reserve_tokens
            - self.budget.safety_reserve_tokens
        )
        if self.selected_token_estimate > available_tokens:
            raise ValueError("selected_token_estimate exceeds the input token budget")
        if self.selected_byte_estimate > self.budget.max_total_bytes:
            raise ValueError("selected_byte_estimate exceeds max_total_bytes")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence_refs = _evidence_refs(self.evidence_refs)
        reasons = tuple(ContextAssemblyReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        object.__setattr__(self, "selected_fragment_refs", selected)
        object.__setattr__(self, "omitted_fragment_refs", omitted)
        object.__setattr__(self, "snapshot_refs", snapshots)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decision_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "selected_fragment_refs": list(self.selected_fragment_refs),
            "omitted_fragment_refs": list(self.omitted_fragment_refs),
            "snapshot_refs": list(self.snapshot_refs),
            "budget": self.budget.to_json(),
            "selected_token_estimate": self.selected_token_estimate,
            "selected_byte_estimate": self.selected_byte_estimate,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "reason_codes": [reason.value for reason in self.reason_codes],
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextAssemblyDecision:
        _exact_keys(
            value,
            {
                "schema_version",
                "decision_id",
                "run_id",
                "subject",
                "selected_fragment_refs",
                "omitted_fragment_refs",
                "snapshot_refs",
                "budget",
                "selected_token_estimate",
                "selected_byte_estimate",
                "disclosure_context",
                "evidence_refs",
                "reason_codes",
                "idempotency_key",
            },
            "ContextAssemblyDecision",
        )
        estimates: dict[str, int] = {}
        for name in ("selected_token_estimate", "selected_byte_estimate"):
            estimate = value[name]
            if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            estimates[name] = estimate
        return cls(
            decision_id=_identifier(value["decision_id"], "decision_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            selected_fragment_refs=_strings(
                value["selected_fragment_refs"], "selected_fragment_refs"
            ),
            omitted_fragment_refs=_strings(
                value["omitted_fragment_refs"], "omitted_fragment_refs"
            ),
            snapshot_refs=_strings(value["snapshot_refs"], "snapshot_refs"),
            budget=ContextAssemblyBudget.from_json(_object(value["budget"], "budget")),
            selected_token_estimate=estimates["selected_token_estimate"],
            selected_byte_estimate=estimates["selected_byte_estimate"],
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            reason_codes=tuple(
                ContextAssemblyReasonCode(item)
                for item in _strings(value["reason_codes"], "reason_codes")
            ),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            schema_version=_schema_version(
                value["schema_version"], "ContextAssemblyDecision"
            ),
        )


class RecallReasonCode(StrEnum):
    NO_RECALL_CONTEXT_SUFFICIENT = "recall_context_sufficient"
    USER_PREFERENCE_DEPENDENCY = "recall_user_preference_dependency"
    PAST_EVENT_DEPENDENCY = "recall_past_event_dependency"
    USER_FACT_DEPENDENCY = "recall_user_fact_dependency"
    PROCEDURE_DEPENDENCY = "recall_procedure_dependency"
    FUTURE_INTENTION_DEPENDENCY = "recall_future_intention_dependency"
    SHORT_HORIZON_DEPENDENCY = "recall_short_horizon_dependency"
    TASK_RESUME_DEPENDENCY = "recall_task_resume_dependency"
    DISCLOSURE_DENIED = "recall_disclosure_denied"
    INVALID_PLAN = "recall_invalid_plan"
    BUDGET_EXHAUSTED = "recall_budget_exhausted"
    NO_ELIGIBLE_MEMORY = "recall_no_eligible_memory"
    NEEDS_USER_CONFIRMATION = "recall_needs_user_confirmation"


_RECALL_DEPENDENCY_REASONS = frozenset(
    {
        RecallReasonCode.USER_PREFERENCE_DEPENDENCY,
        RecallReasonCode.PAST_EVENT_DEPENDENCY,
        RecallReasonCode.USER_FACT_DEPENDENCY,
        RecallReasonCode.PROCEDURE_DEPENDENCY,
        RecallReasonCode.FUTURE_INTENTION_DEPENDENCY,
        RecallReasonCode.SHORT_HORIZON_DEPENDENCY,
        RecallReasonCode.TASK_RESUME_DEPENDENCY,
    }
)
_NO_RECALL_REASONS = frozenset(
    {
        RecallReasonCode.NO_RECALL_CONTEXT_SUFFICIENT,
        RecallReasonCode.BUDGET_EXHAUSTED,
        RecallReasonCode.NO_ELIGIBLE_MEMORY,
    }
)
_REJECTED_REASONS = frozenset(
    {
        RecallReasonCode.DISCLOSURE_DENIED,
        RecallReasonCode.INVALID_PLAN,
    }
)


class RecallDecisionOutcome(StrEnum):
    NO_RECALL = "no_recall"
    RECALL = "recall"
    NEEDS_USER_CONFIRMATION = "needs_user_confirmation"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RecallConfirmationItem:
    """A post-gate conflicting candidate that requires user confirmation."""

    conflict_group_ref: str
    memory_type: LongTermMemoryType
    memory_ref: str

    def __post_init__(self) -> None:
        _identifier(self.conflict_group_ref, "conflict_group_ref")
        object.__setattr__(self, "memory_type", LongTermMemoryType(self.memory_type))
        _identifier(self.memory_ref, "memory_ref")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "conflict_group_ref": self.conflict_group_ref,
            "memory_type": self.memory_type.value,
            "memory_ref": self.memory_ref,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallConfirmationItem:
        _exact_keys(
            value,
            {"conflict_group_ref", "memory_type", "memory_ref"},
            "RecallConfirmationItem",
        )
        return cls(
            conflict_group_ref=_identifier(
                value["conflict_group_ref"], "conflict_group_ref"
            ),
            memory_type=LongTermMemoryType(value["memory_type"]),  # type: ignore[arg-type]
            memory_ref=_identifier(value["memory_ref"], "memory_ref"),
        )


@dataclass(frozen=True, slots=True)
class RecallBudget:
    max_items: int
    max_bytes: int
    max_tokens: int
    deadline_ms: int

    def __post_init__(self) -> None:
        for name in ("max_items", "max_bytes", "max_tokens", "deadline_ms"):
            _positive_int(getattr(self, name), name)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "max_tokens": self.max_tokens,
            "deadline_ms": self.deadline_ms,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallBudget:
        expected = {"max_items", "max_bytes", "max_tokens", "deadline_ms"}
        _exact_keys(value, expected, "RecallBudget")
        return cls(
            _positive_int(value["max_items"], "max_items"),
            _positive_int(value["max_bytes"], "max_bytes"),
            _positive_int(value["max_tokens"], "max_tokens"),
            _positive_int(value["deadline_ms"], "deadline_ms"),
        )


@dataclass(frozen=True, slots=True)
class LegacyRecallContextV1:
    run_id: str
    subject: str
    turn_id: str
    query: str
    active_task_scope_id: str | None
    available_memory_types: tuple[LongTermMemoryType, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    budget: RecallBudget
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    context_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported RecallContext schema_version")
        for value, name in (
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.turn_id, "turn_id"),
        ):
            _identifier(value, name)
        _bounded_text(self.query, "query", max_bytes=16_384)
        _optional_identifier(self.active_task_scope_id, "active_task_scope_id")
        types = tuple(LongTermMemoryType(item) for item in self.available_memory_types)
        if not types or len(set(types)) != len(types):
            raise ValueError("available_memory_types must be non-empty and unique")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not isinstance(self.budget, RecallBudget):
            raise TypeError("budget must use RecallBudget")
        object.__setattr__(self, "available_memory_types", types)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "context_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "turn_id": self.turn_id,
            "query": self.query,
            "active_task_scope_id": self.active_task_scope_id,
            "available_memory_types": [item.value for item in self.available_memory_types],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "budget": self.budget.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> LegacyRecallContextV1:
        _exact_keys(
            value,
            {
                "schema_version",
                "run_id",
                "subject",
                "turn_id",
                "query",
                "active_task_scope_id",
                "available_memory_types",
                "disclosure_context",
                "evidence_refs",
                "budget",
            },
            "RecallContext",
        )
        return cls(
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            turn_id=_identifier(value["turn_id"], "turn_id"),
            query=_bounded_text(value["query"], "query", max_bytes=16_384),
            active_task_scope_id=_optional_identifier(
                value["active_task_scope_id"], "active_task_scope_id"
            ),
            available_memory_types=tuple(
                LongTermMemoryType(item)
                for item in _strings(value["available_memory_types"], "available_memory_types")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            budget=RecallBudget.from_json(_object(value["budget"], "budget")),
            schema_version=_schema_version(value["schema_version"], "RecallContext"),
        )


@dataclass(frozen=True, slots=True)
class LegacyRecallPlanV1:
    plan_id: str
    run_id: str
    subject: str
    query: str
    requested_memory_types: tuple[LongTermMemoryType, ...]
    include_short_horizon: bool
    task_scope_ids: tuple[str, ...]
    entity_constraints: tuple[str, ...]
    earliest_occurred_at: float | None
    latest_occurred_at: float | None
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    budget: RecallBudget
    idempotency_key: str
    reason_codes: tuple[RecallReasonCode, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported RecallPlan schema_version")
        for value, name in (
            (self.plan_id, "plan_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _bounded_text(self.query, "query", max_bytes=16_384)
        memory_types = tuple(LongTermMemoryType(item) for item in self.requested_memory_types)
        if len(set(memory_types)) != len(memory_types):
            raise ValueError("requested_memory_types must be unique")
        if not isinstance(self.include_short_horizon, bool):
            raise TypeError("include_short_horizon must be a boolean")
        if not memory_types and not self.include_short_horizon:
            raise ValueError("RecallPlan must request a long-term type or short-horizon recall")
        task_scope_ids = tuple(_identifier(item, "task_scope_id") for item in self.task_scope_ids)
        entities = tuple(
            _bounded_text(item, "entity_constraint", max_bytes=512)
            for item in self.entity_constraints
        )
        if len(set(task_scope_ids)) != len(task_scope_ids):
            raise ValueError("task_scope_ids must be unique")
        if len(set(entities)) != len(entities):
            raise ValueError("entity_constraints must be unique")
        for time_value, time_name in (
            (self.earliest_occurred_at, "earliest_occurred_at"),
            (self.latest_occurred_at, "latest_occurred_at"),
        ):
            if time_value is not None and (
                isinstance(time_value, bool)
                or not isinstance(time_value, (int, float))
                or float(time_value) < 0
            ):
                raise ValueError(f"{time_name} must be a non-negative timestamp or null")
        if (
            self.earliest_occurred_at is not None
            and self.latest_occurred_at is not None
            and self.earliest_occurred_at > self.latest_occurred_at
        ):
            raise ValueError("recall time range is inverted")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("RecallPlan requires evidence_refs")
        if not isinstance(self.budget, RecallBudget):
            raise TypeError("budget must use RecallBudget")
        reasons = tuple(RecallReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        object.__setattr__(self, "requested_memory_types", memory_types)
        object.__setattr__(self, "task_scope_ids", task_scope_ids)
        object.__setattr__(self, "entity_constraints", entities)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "plan_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "query": self.query,
            "requested_memory_types": [item.value for item in self.requested_memory_types],
            "include_short_horizon": self.include_short_horizon,
            "task_scope_ids": list(self.task_scope_ids),
            "entity_constraints": list(self.entity_constraints),
            "earliest_occurred_at": self.earliest_occurred_at,
            "latest_occurred_at": self.latest_occurred_at,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "budget": self.budget.to_json(),
            "idempotency_key": self.idempotency_key,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> LegacyRecallPlanV1:
        _exact_keys(
            value,
            {
                "schema_version",
                "plan_id",
                "run_id",
                "subject",
                "query",
                "requested_memory_types",
                "include_short_horizon",
                "task_scope_ids",
                "entity_constraints",
                "earliest_occurred_at",
                "latest_occurred_at",
                "disclosure_context",
                "evidence_refs",
                "budget",
                "idempotency_key",
                "reason_codes",
            },
            "RecallPlan",
        )
        include_short_horizon = value["include_short_horizon"]
        if not isinstance(include_short_horizon, bool):
            raise TypeError("include_short_horizon must be a boolean")
        timestamps: dict[str, float | None] = {}
        for name in ("earliest_occurred_at", "latest_occurred_at"):
            timestamp = value[name]
            if timestamp is None:
                timestamps[name] = None
            elif isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise TypeError(f"{name} must be numeric or null")
            else:
                timestamps[name] = float(timestamp)
        return cls(
            plan_id=_identifier(value["plan_id"], "plan_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            query=_bounded_text(value["query"], "query", max_bytes=16_384),
            requested_memory_types=tuple(
                LongTermMemoryType(item)
                for item in _strings(value["requested_memory_types"], "requested_memory_types")
            ),
            include_short_horizon=include_short_horizon,
            task_scope_ids=_strings(value["task_scope_ids"], "task_scope_ids"),
            entity_constraints=_strings(value["entity_constraints"], "entity_constraints"),
            earliest_occurred_at=timestamps["earliest_occurred_at"],
            latest_occurred_at=timestamps["latest_occurred_at"],
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            budget=RecallBudget.from_json(_object(value["budget"], "budget")),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            reason_codes=tuple(
                RecallReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            schema_version=_schema_version(value["schema_version"], "RecallPlan"),
        )


class RecallSelectorDomain(StrEnum):
    MEMORY_TYPE = "memory_type"
    TASK_SCOPE = "task_scope"
    ENTITY = "entity"
    TIME = "time"
    EVENT = "event"
    ENVIRONMENT = "environment"
    TASK_PHASE = "task_phase"
    SHORT_HORIZON = "short_horizon"


class RecallRetrievalMode(StrEnum):
    EXACT = "exact"
    VECTOR = "vector"
    FULL_TEXT = "full_text"
    GRAPH = "graph"
    TEMPORAL = "temporal"


class RecallCandidateCountStage(StrEnum):
    AFTER_ALL_ELIGIBILITY_GATES = "after_all_eligibility_gates"


def _validate_recall_selector_consistency(
    domains: tuple[RecallSelectorDomain, ...],
    *,
    memory_types: tuple[LongTermMemoryType, ...],
    short_horizon: bool,
    task_scope_ids: tuple[str, ...],
    entity_constraints: tuple[str, ...],
    earliest_occurred_at: float | None,
    latest_occurred_at: float | None,
    event_constraint_refs: tuple[str, ...],
    environment_constraint_refs: tuple[str, ...],
    task_phase_authority_refs: tuple[str, ...],
    name: str,
) -> None:
    """Require every selector domain to have exactly one corresponding constraint."""

    constraints = {
        RecallSelectorDomain.MEMORY_TYPE: bool(memory_types),
        RecallSelectorDomain.TASK_SCOPE: bool(task_scope_ids),
        RecallSelectorDomain.ENTITY: bool(entity_constraints),
        RecallSelectorDomain.TIME: (
            earliest_occurred_at is not None or latest_occurred_at is not None
        ),
        RecallSelectorDomain.EVENT: bool(event_constraint_refs),
        RecallSelectorDomain.ENVIRONMENT: bool(environment_constraint_refs),
        RecallSelectorDomain.TASK_PHASE: bool(task_phase_authority_refs),
        RecallSelectorDomain.SHORT_HORIZON: short_horizon,
    }
    domain_set = set(domains)
    for domain, present in constraints.items():
        if (domain in domain_set) != present:
            raise ValueError(
                f"{name} selector domain {domain.value} and constraint differ"
            )


def _trusted_current_time(value: object) -> float:
    current_time = _optional_timestamp(value, "current_time")
    if current_time is None:
        raise ValueError("current_time is required")
    return current_time


def _validate_recall_disclosure(context: DisclosureContext) -> None:
    """Fail closed for any non-current, external, unknown, or untrusted authority."""

    if context.trust is not DisclosureTrust.TRUSTED_AUTHORITY:
        raise ValueError("recall requires trusted disclosure authority")
    if context.source not in {
        DisclosureSource.AUTHENTICATED_HOST,
        DisclosureSource.AUTHENTICATED_UI,
        DisclosureSource.AUDIT_ACCESS_DECISION,
    }:
        raise ValueError("recall rejects unknown or untrusted disclosure source")
    if context.generation is not DisclosureGeneration.CURRENT:
        raise ValueError("recall rejects stale, conflicted, or unknown disclosure")
    if context.purpose is DisclosurePurpose.UNKNOWN:
        raise ValueError("recall rejects unknown disclosure purpose")
    if context.recipient in {
        DeliveryRecipient.EXTERNAL_PARTY,
        DeliveryRecipient.PUBLIC,
        DeliveryRecipient.UNKNOWN,
    }:
        raise ValueError("recall rejects external or unknown recipient")
    if context.intended_audience in {
        IntendedAudience.EXTERNAL,
        IntendedAudience.PUBLIC,
        IntendedAudience.UNKNOWN,
    }:
        raise ValueError("recall rejects external or unknown audience")


def _bounded_identifiers(
    value: object,
    name: str,
    *,
    max_items: int = 128,
    max_length: int = 1024,
) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError(f"{name} must contain strings")
    result = tuple(
        _identifier(item, f"{name} item", max_length=max_length) for item in value
    )
    if len(result) > max_items:
        raise ValueError(f"{name} exceeds the item limit")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must be unique")
    return result


def _bounded_enum_tuple(
    value: object,
    name: str,
    enum_type: type[StrEnum],
    *,
    required: bool = False,
    max_items: int = 32,
) -> tuple[StrEnum, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be an array")
    try:
        result = tuple(enum_type(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} contains an unknown value") from exc
    if required and not result:
        raise ValueError(f"{name} must be non-empty")
    if len(result) > max_items or len(set(result)) != len(result):
        raise ValueError(f"{name} must be unique and bounded")
    return result


def _optional_timestamp(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric or null")
    parsed = float(value)
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise ValueError(f"{name} must be a finite non-negative timestamp or null")
    return parsed


def _validate_time_window(
    earliest: float | None, latest: float | None, *, name: str
) -> None:
    if earliest is not None and latest is not None and earliest > latest:
        raise ValueError(f"{name} time range is inverted")


@dataclass(frozen=True, slots=True)
class RecallContext:
    """Exact Host authority from which a model may only narrow recall."""

    run_id: str
    subject: str
    turn_id: str
    context_revision: int
    expires_at: float
    query: str
    active_task_scope_id: str | None
    available_memory_types: tuple[LongTermMemoryType, ...]
    short_horizon_allowed: bool
    allowed_selector_domains: tuple[RecallSelectorDomain, ...]
    allowed_retrieval_modes: tuple[RecallRetrievalMode, ...]
    allowed_task_scope_ids: tuple[str, ...]
    allowed_entity_constraints: tuple[str, ...]
    earliest_occurred_at: float | None
    latest_occurred_at: float | None
    event_constraint_refs: tuple[str, ...]
    environment_constraint_refs: tuple[str, ...]
    task_phase_authority_refs: tuple[str, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    budget: RecallBudget
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    context_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported RecallContext schema_version")
        for value, name in (
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.turn_id, "turn_id"),
        ):
            _identifier(value, name)
        _positive_int(self.context_revision, "context_revision")
        expires_at = _optional_timestamp(self.expires_at, "expires_at")
        if expires_at is None:
            raise ValueError("expires_at is required")
        _bounded_text(self.query, "query", max_bytes=16_384)
        active_scope = _optional_identifier(
            self.active_task_scope_id, "active_task_scope_id"
        )
        types = cast(
            tuple[LongTermMemoryType, ...],
            _bounded_enum_tuple(
                self.available_memory_types,
                "available_memory_types",
                LongTermMemoryType,
                required=True,
            ),
        )
        if not isinstance(self.short_horizon_allowed, bool):
            raise TypeError("short_horizon_allowed must be a boolean")
        domains = cast(
            tuple[RecallSelectorDomain, ...],
            _bounded_enum_tuple(
                self.allowed_selector_domains,
                "allowed_selector_domains",
                RecallSelectorDomain,
                required=True,
            ),
        )
        modes = cast(
            tuple[RecallRetrievalMode, ...],
            _bounded_enum_tuple(
                self.allowed_retrieval_modes,
                "allowed_retrieval_modes",
                RecallRetrievalMode,
                required=True,
            ),
        )
        scopes = _bounded_identifiers(
            self.allowed_task_scope_ids, "allowed_task_scope_ids"
        )
        entities = _bounded_tuple(
            self.allowed_entity_constraints,
            "allowed_entity_constraints",
            max_items=128,
            max_item_bytes=512,
        )
        if active_scope is not None and active_scope not in scopes:
            raise ValueError("active_task_scope_id must be in allowed_task_scope_ids")
        if self.short_horizon_allowed != (
            RecallSelectorDomain.SHORT_HORIZON in domains
        ):
            raise ValueError(
                "short_horizon_allowed and SHORT_HORIZON selector authority differ"
            )
        earliest = _optional_timestamp(
            self.earliest_occurred_at, "earliest_occurred_at"
        )
        latest = _optional_timestamp(self.latest_occurred_at, "latest_occurred_at")
        _validate_time_window(earliest, latest, name="RecallContext")
        events = _bounded_identifiers(
            self.event_constraint_refs, "event_constraint_refs"
        )
        environments = _bounded_identifiers(
            self.environment_constraint_refs, "environment_constraint_refs"
        )
        phases = _bounded_identifiers(
            self.task_phase_authority_refs, "task_phase_authority_refs"
        )
        _validate_recall_selector_consistency(
            domains,
            memory_types=types,
            short_horizon=self.short_horizon_allowed,
            task_scope_ids=scopes,
            entity_constraints=entities,
            earliest_occurred_at=earliest,
            latest_occurred_at=latest,
            event_constraint_refs=events,
            environment_constraint_refs=environments,
            task_phase_authority_refs=phases,
            name="RecallContext",
        )
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise ValueError("RecallContext requires evidence_refs")
        if not isinstance(self.budget, RecallBudget):
            raise TypeError("budget must use RecallBudget")
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "active_task_scope_id", active_scope)
        object.__setattr__(self, "available_memory_types", types)
        object.__setattr__(self, "allowed_selector_domains", domains)
        object.__setattr__(self, "allowed_retrieval_modes", modes)
        object.__setattr__(self, "allowed_task_scope_ids", scopes)
        object.__setattr__(self, "allowed_entity_constraints", entities)
        object.__setattr__(self, "earliest_occurred_at", earliest)
        object.__setattr__(self, "latest_occurred_at", latest)
        object.__setattr__(self, "event_constraint_refs", events)
        object.__setattr__(self, "environment_constraint_refs", environments)
        object.__setattr__(self, "task_phase_authority_refs", phases)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(
            self,
            "context_hash",
            _domain_hash("simple-harness/recall-context/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "turn_id": self.turn_id,
            "context_revision": self.context_revision,
            "expires_at": self.expires_at,
            "query": self.query,
            "active_task_scope_id": self.active_task_scope_id,
            "available_memory_types": [item.value for item in self.available_memory_types],
            "short_horizon_allowed": self.short_horizon_allowed,
            "allowed_selector_domains": [item.value for item in self.allowed_selector_domains],
            "allowed_retrieval_modes": [item.value for item in self.allowed_retrieval_modes],
            "allowed_task_scope_ids": list(self.allowed_task_scope_ids),
            "allowed_entity_constraints": list(self.allowed_entity_constraints),
            "earliest_occurred_at": self.earliest_occurred_at,
            "latest_occurred_at": self.latest_occurred_at,
            "event_constraint_refs": list(self.event_constraint_refs),
            "environment_constraint_refs": list(self.environment_constraint_refs),
            "task_phase_authority_refs": list(self.task_phase_authority_refs),
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "budget": self.budget.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallContext:
        expected = {
            "schema_version", "run_id", "subject", "turn_id", "context_revision",
            "expires_at", "query", "active_task_scope_id", "available_memory_types",
            "short_horizon_allowed", "allowed_selector_domains",
            "allowed_retrieval_modes", "allowed_task_scope_ids",
            "allowed_entity_constraints", "earliest_occurred_at",
            "latest_occurred_at", "event_constraint_refs",
            "environment_constraint_refs", "task_phase_authority_refs",
            "disclosure_context", "evidence_refs", "budget",
        }
        _exact_keys(value, expected, "RecallContext")
        short_horizon = value["short_horizon_allowed"]
        if not isinstance(short_horizon, bool):
            raise TypeError("short_horizon_allowed must be a boolean")
        return cls(
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            turn_id=_identifier(value["turn_id"], "turn_id"),
            context_revision=_positive_int(value["context_revision"], "context_revision"),
            expires_at=cast(float, _optional_timestamp(value["expires_at"], "expires_at")),
            query=_bounded_text(value["query"], "query", max_bytes=16_384),
            active_task_scope_id=_optional_identifier(
                value["active_task_scope_id"], "active_task_scope_id"
            ),
            available_memory_types=tuple(
                LongTermMemoryType(item)
                for item in _strings(value["available_memory_types"], "available_memory_types")
            ),
            short_horizon_allowed=short_horizon,
            allowed_selector_domains=tuple(
                RecallSelectorDomain(item)
                for item in _strings(value["allowed_selector_domains"], "allowed_selector_domains")
            ),
            allowed_retrieval_modes=tuple(
                RecallRetrievalMode(item)
                for item in _strings(value["allowed_retrieval_modes"], "allowed_retrieval_modes")
            ),
            allowed_task_scope_ids=_strings(
                value["allowed_task_scope_ids"], "allowed_task_scope_ids"
            ),
            allowed_entity_constraints=_strings(
                value["allowed_entity_constraints"], "allowed_entity_constraints"
            ),
            earliest_occurred_at=_optional_timestamp(
                value["earliest_occurred_at"], "earliest_occurred_at"
            ),
            latest_occurred_at=_optional_timestamp(
                value["latest_occurred_at"], "latest_occurred_at"
            ),
            event_constraint_refs=_strings(
                value["event_constraint_refs"], "event_constraint_refs"
            ),
            environment_constraint_refs=_strings(
                value["environment_constraint_refs"], "environment_constraint_refs"
            ),
            task_phase_authority_refs=_strings(
                value["task_phase_authority_refs"], "task_phase_authority_refs"
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            budget=RecallBudget.from_json(_object(value["budget"], "budget")),
            schema_version=_cognitive_schema_version(
                value["schema_version"], "RecallContext"
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallPlan:
    plan_id: str
    run_id: str
    subject: str
    context_hash: str
    context_revision: int
    query: str
    requested_memory_types: tuple[LongTermMemoryType, ...]
    include_short_horizon: bool
    selector_domains: tuple[RecallSelectorDomain, ...]
    retrieval_modes: tuple[RecallRetrievalMode, ...]
    task_scope_ids: tuple[str, ...]
    entity_constraints: tuple[str, ...]
    earliest_occurred_at: float | None
    latest_occurred_at: float | None
    event_constraint_refs: tuple[str, ...]
    environment_constraint_refs: tuple[str, ...]
    task_phase_authority_refs: tuple[str, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    budget: RecallBudget
    idempotency_key: str
    reason_codes: tuple[RecallReasonCode, ...]
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported RecallPlan schema_version")
        for value, name in (
            (self.plan_id, "plan_id"), (self.run_id, "run_id"),
            (self.subject, "subject"), (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _digest(self.context_hash, "context_hash")
        _positive_int(self.context_revision, "context_revision")
        _bounded_text(self.query, "query", max_bytes=16_384)
        memory_types = cast(
            tuple[LongTermMemoryType, ...],
            _bounded_enum_tuple(
                self.requested_memory_types,
                "requested_memory_types",
                LongTermMemoryType,
            ),
        )
        if not isinstance(self.include_short_horizon, bool):
            raise TypeError("include_short_horizon must be a boolean")
        if not memory_types and not self.include_short_horizon:
            raise ValueError("RecallPlan must request memory or short-horizon recall")
        domains = cast(
            tuple[RecallSelectorDomain, ...],
            _bounded_enum_tuple(
                self.selector_domains,
                "selector_domains",
                RecallSelectorDomain,
                required=True,
            ),
        )
        modes = cast(
            tuple[RecallRetrievalMode, ...],
            _bounded_enum_tuple(
                self.retrieval_modes,
                "retrieval_modes",
                RecallRetrievalMode,
                required=True,
            ),
        )
        if self.include_short_horizon and RecallSelectorDomain.SHORT_HORIZON not in domains:
            raise ValueError("short-horizon recall requires its selector domain")
        scopes = _bounded_identifiers(self.task_scope_ids, "task_scope_ids")
        entities = _bounded_tuple(
            self.entity_constraints,
            "entity_constraints",
            max_items=128,
            max_item_bytes=512,
        )
        earliest = _optional_timestamp(
            self.earliest_occurred_at, "earliest_occurred_at"
        )
        latest = _optional_timestamp(self.latest_occurred_at, "latest_occurred_at")
        _validate_time_window(earliest, latest, name="RecallPlan")
        events = _bounded_identifiers(self.event_constraint_refs, "event_constraint_refs")
        environments = _bounded_identifiers(
            self.environment_constraint_refs, "environment_constraint_refs"
        )
        phases = _bounded_identifiers(
            self.task_phase_authority_refs, "task_phase_authority_refs"
        )
        _validate_recall_selector_consistency(
            domains,
            memory_types=memory_types,
            short_horizon=self.include_short_horizon,
            task_scope_ids=scopes,
            entity_constraints=entities,
            earliest_occurred_at=earliest,
            latest_occurred_at=latest,
            event_constraint_refs=events,
            environment_constraint_refs=environments,
            task_phase_authority_refs=phases,
            name="RecallPlan",
        )
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence = _evidence_refs(self.evidence_refs)
        if not evidence:
            raise ValueError("RecallPlan requires evidence_refs")
        if not isinstance(self.budget, RecallBudget):
            raise TypeError("budget must use RecallBudget")
        reasons = cast(
            tuple[RecallReasonCode, ...],
            _bounded_enum_tuple(
                self.reason_codes,
                "reason_codes",
                RecallReasonCode,
                required=True,
            ),
        )
        object.__setattr__(self, "requested_memory_types", memory_types)
        object.__setattr__(self, "selector_domains", domains)
        object.__setattr__(self, "retrieval_modes", modes)
        object.__setattr__(self, "task_scope_ids", scopes)
        object.__setattr__(self, "entity_constraints", entities)
        object.__setattr__(self, "earliest_occurred_at", earliest)
        object.__setattr__(self, "latest_occurred_at", latest)
        object.__setattr__(self, "event_constraint_refs", events)
        object.__setattr__(self, "environment_constraint_refs", environments)
        object.__setattr__(self, "task_phase_authority_refs", phases)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self, "plan_hash", _domain_hash("simple-harness/recall-plan/v2", self.to_json())
        )

    def validate_narrowing(
        self, context: RecallContext, *, current_time: float
    ) -> None:
        """Fail closed unless every model-controlled selector narrows Host authority."""

        if not isinstance(context, RecallContext):
            raise TypeError("context must use RecallContext")
        trusted_now = _trusted_current_time(current_time)
        if context.expires_at <= trusted_now:
            raise ValueError("RecallContext is expired at trusted current_time")
        if self.run_id != context.run_id or self.subject != context.subject:
            raise ValueError("RecallPlan run/subject differs from RecallContext")
        if self.context_hash != context.context_hash:
            raise ValueError("RecallPlan context_hash differs")
        if self.context_revision != context.context_revision:
            raise ValueError("RecallPlan context_revision differs")
        if self.query != context.query:
            raise ValueError("RecallPlan query differs from RecallContext")
        subset_checks: tuple[tuple[set[object], set[object], str], ...] = (
            (set(self.requested_memory_types), set(context.available_memory_types), "memory types"),
            (set(self.selector_domains), set(context.allowed_selector_domains), "selector domains"),
            (set(self.retrieval_modes), set(context.allowed_retrieval_modes), "retrieval modes"),
            (set(self.task_scope_ids), set(context.allowed_task_scope_ids), "task scopes"),
            (set(self.entity_constraints), set(context.allowed_entity_constraints), "entities"),
            (
                set(self.event_constraint_refs),
                set(context.event_constraint_refs),
                "event constraints",
            ),
            (
                set(self.environment_constraint_refs),
                set(context.environment_constraint_refs),
                "environment constraints",
            ),
            (
                set(self.task_phase_authority_refs),
                set(context.task_phase_authority_refs),
                "task phase constraints",
            ),
        )
        for requested, allowed, name in subset_checks:
            if not requested <= allowed:
                raise ValueError(f"RecallPlan expands {name}")
        mandatory_domains = set(context.allowed_selector_domains) - {
            RecallSelectorDomain.SHORT_HORIZON
        }
        if not mandatory_domains <= set(self.selector_domains):
            raise ValueError("RecallPlan removes a mandatory Host selector domain")
        if self.include_short_horizon and not context.short_horizon_allowed:
            raise ValueError("RecallPlan expands short-horizon authority")
        if (
            context.earliest_occurred_at is not None
            and (
                self.earliest_occurred_at is None
                or self.earliest_occurred_at < context.earliest_occurred_at
            )
        ):
            raise ValueError("RecallPlan expands earliest time")
        if (
            context.latest_occurred_at is not None
            and (
                self.latest_occurred_at is None
                or self.latest_occurred_at > context.latest_occurred_at
            )
        ):
            raise ValueError("RecallPlan expands latest time")
        for name in ("max_items", "max_bytes", "max_tokens", "deadline_ms"):
            if getattr(self.budget, name) > getattr(context.budget, name):
                raise ValueError(f"RecallPlan budget expands {name}")
        if self.disclosure_context.to_json() != context.disclosure_context.to_json():
            raise ValueError("RecallPlan disclosure_context must exactly bind Host authority")
        allowed_evidence = {fingerprint_json(item.to_json()) for item in context.evidence_refs}
        if not {
            fingerprint_json(item.to_json()) for item in self.evidence_refs
        } <= allowed_evidence:
            raise ValueError("RecallPlan evidence_refs expand Host evidence")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "context_hash": self.context_hash,
            "context_revision": self.context_revision,
            "query": self.query,
            "requested_memory_types": [item.value for item in self.requested_memory_types],
            "include_short_horizon": self.include_short_horizon,
            "selector_domains": [item.value for item in self.selector_domains],
            "retrieval_modes": [item.value for item in self.retrieval_modes],
            "task_scope_ids": list(self.task_scope_ids),
            "entity_constraints": list(self.entity_constraints),
            "earliest_occurred_at": self.earliest_occurred_at,
            "latest_occurred_at": self.latest_occurred_at,
            "event_constraint_refs": list(self.event_constraint_refs),
            "environment_constraint_refs": list(self.environment_constraint_refs),
            "task_phase_authority_refs": list(self.task_phase_authority_refs),
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "budget": self.budget.to_json(),
            "idempotency_key": self.idempotency_key,
            "reason_codes": [item.value for item in self.reason_codes],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallPlan:
        expected = {
            "schema_version", "plan_id", "run_id", "subject", "context_hash",
            "context_revision", "query", "requested_memory_types",
            "include_short_horizon", "selector_domains", "retrieval_modes",
            "task_scope_ids", "entity_constraints", "earliest_occurred_at",
            "latest_occurred_at", "event_constraint_refs",
            "environment_constraint_refs", "task_phase_authority_refs",
            "disclosure_context", "evidence_refs", "budget", "idempotency_key",
            "reason_codes",
        }
        _exact_keys(value, expected, "RecallPlan")
        include_short_horizon = value["include_short_horizon"]
        if not isinstance(include_short_horizon, bool):
            raise TypeError("include_short_horizon must be a boolean")
        return cls(
            plan_id=_identifier(value["plan_id"], "plan_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            context_hash=_digest(value["context_hash"], "context_hash"),
            context_revision=_positive_int(value["context_revision"], "context_revision"),
            query=_bounded_text(value["query"], "query", max_bytes=16_384),
            requested_memory_types=tuple(
                LongTermMemoryType(item)
                for item in _strings(value["requested_memory_types"], "requested_memory_types")
            ),
            include_short_horizon=include_short_horizon,
            selector_domains=tuple(
                RecallSelectorDomain(item)
                for item in _strings(value["selector_domains"], "selector_domains")
            ),
            retrieval_modes=tuple(
                RecallRetrievalMode(item)
                for item in _strings(value["retrieval_modes"], "retrieval_modes")
            ),
            task_scope_ids=_strings(value["task_scope_ids"], "task_scope_ids"),
            entity_constraints=_strings(value["entity_constraints"], "entity_constraints"),
            earliest_occurred_at=_optional_timestamp(
                value["earliest_occurred_at"], "earliest_occurred_at"
            ),
            latest_occurred_at=_optional_timestamp(
                value["latest_occurred_at"], "latest_occurred_at"
            ),
            event_constraint_refs=_strings(
                value["event_constraint_refs"], "event_constraint_refs"
            ),
            environment_constraint_refs=_strings(
                value["environment_constraint_refs"], "environment_constraint_refs"
            ),
            task_phase_authority_refs=_strings(
                value["task_phase_authority_refs"], "task_phase_authority_refs"
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            budget=RecallBudget.from_json(_object(value["budget"], "budget")),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            reason_codes=tuple(
                RecallReasonCode(item)
                for item in _strings(value["reason_codes"], "reason_codes")
            ),
            schema_version=_cognitive_schema_version(value["schema_version"], "RecallPlan"),
        )


@dataclass(frozen=True, slots=True)
class RecallDecision:
    decision_id: str
    run_id: str
    subject: str
    context_hash: str
    context_revision: int
    plan_id: str
    plan_hash: str
    outcome: RecallDecisionOutcome
    selected_memory_types: tuple[LongTermMemoryType, ...]
    selected_memory_refs: tuple[str, ...]
    confirmation_items: tuple[RecallConfirmationItem, ...]
    filtered_candidate_count: int
    candidate_count_stage: RecallCandidateCountStage
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    reason_codes: tuple[RecallReasonCode, ...]
    decided_at: float
    schema_version: int = RECALL_DECISION_SCHEMA_VERSION
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != RECALL_DECISION_SCHEMA_VERSION:
            raise ValueError("unsupported RecallDecision schema_version")
        for value, name in (
            (self.decision_id, "decision_id"), (self.run_id, "run_id"),
            (self.subject, "subject"), (self.plan_id, "plan_id"),
        ):
            _identifier(value, name)
        _digest(self.context_hash, "context_hash")
        _positive_int(self.context_revision, "context_revision")
        _digest(self.plan_hash, "plan_hash")
        object.__setattr__(self, "outcome", RecallDecisionOutcome(self.outcome))
        types = cast(
            tuple[LongTermMemoryType, ...],
            _bounded_enum_tuple(
                self.selected_memory_types,
                "selected_memory_types",
                LongTermMemoryType,
            ),
        )
        selected = _bounded_identifiers(
            self.selected_memory_refs, "selected_memory_refs"
        )
        if not isinstance(self.confirmation_items, (tuple, list)) or not all(
            isinstance(item, RecallConfirmationItem)
            for item in self.confirmation_items
        ):
            raise TypeError(
                "confirmation_items must contain RecallConfirmationItem values"
            )
        confirmation = tuple(
            sorted(
                self.confirmation_items,
                key=lambda item: (
                    item.conflict_group_ref,
                    item.memory_type.value,
                    item.memory_ref,
                ),
            )
        )
        if len(confirmation) > 128:
            raise ValueError("confirmation_items exceeds the item limit")
        confirmation_refs = tuple(item.memory_ref for item in confirmation)
        if len(set(confirmation_refs)) != len(confirmation_refs):
            raise ValueError("confirmation memory_ref values must be globally unique")
        group_counts: dict[str, int] = {}
        for item in confirmation:
            group_counts[item.conflict_group_ref] = (
                group_counts.get(item.conflict_group_ref, 0) + 1
            )
        if any(count < 2 for count in group_counts.values()):
            raise ValueError("each confirmation conflict group requires at least two items")
        if isinstance(self.filtered_candidate_count, bool) or not isinstance(
            self.filtered_candidate_count, int
        ) or self.filtered_candidate_count < 0:
            raise ValueError("filtered_candidate_count must be a non-negative integer")
        stage = RecallCandidateCountStage(self.candidate_count_stage)
        if stage is not RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES:
            raise ValueError("candidate count must be after all eligibility gates")
        if len(selected) + len(confirmation) > self.filtered_candidate_count:
            raise ValueError("decision memories exceed post-gate candidate count")
        if self.outcome is not RecallDecisionOutcome.RECALL and (types or selected):
            raise ValueError("non-recall outcome cannot select memories")
        if self.outcome is RecallDecisionOutcome.RECALL:
            if not selected:
                raise ValueError("recall outcome requires selected memories")
            if not types:
                raise ValueError("recall outcome requires selected memory types")
            if len(types) > len(selected):
                raise ValueError(
                    "selected memory types exceed selected memory references"
                )
            if confirmation:
                raise ValueError("recall outcome cannot require confirmation")
        elif self.outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION:
            if not confirmation:
                raise ValueError(
                    "needs-user-confirmation outcome requires confirmation items"
                )
        elif confirmation:
            raise ValueError("outcome cannot include confirmation items")
        if self.outcome in {
            RecallDecisionOutcome.NO_RECALL,
            RecallDecisionOutcome.REJECTED,
        } and self.filtered_candidate_count != 0:
            raise ValueError(
                "no-recall and rejected outcomes cannot disclose candidate counts"
            )
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        if self.outcome in {
            RecallDecisionOutcome.RECALL,
            RecallDecisionOutcome.NEEDS_USER_CONFIRMATION,
        }:
            _validate_recall_disclosure(self.disclosure_context)
        evidence = _evidence_refs(self.evidence_refs)
        if not evidence:
            raise ValueError("RecallDecision requires evidence_refs")
        reasons = cast(
            tuple[RecallReasonCode, ...],
            _bounded_enum_tuple(
                self.reason_codes,
                "reason_codes",
                RecallReasonCode,
                required=True,
            ),
        )
        reason_set = set(reasons)
        if self.outcome is RecallDecisionOutcome.RECALL:
            if not reason_set <= _RECALL_DEPENDENCY_REASONS:
                raise ValueError("recall outcome requires only dependency reasons")
        elif self.outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION:
            allowed = _RECALL_DEPENDENCY_REASONS | {
                RecallReasonCode.NEEDS_USER_CONFIRMATION
            }
            if RecallReasonCode.NEEDS_USER_CONFIRMATION not in reason_set:
                raise ValueError(
                    "needs-user-confirmation outcome requires confirmation reason"
                )
            if not reason_set <= allowed:
                raise ValueError(
                    "needs-user-confirmation outcome has an incompatible reason"
                )
        elif self.outcome is RecallDecisionOutcome.NO_RECALL:
            if not reason_set <= _NO_RECALL_REASONS:
                raise ValueError("no-recall outcome has an incompatible reason")
        elif not reason_set <= _REJECTED_REASONS:
            raise ValueError("rejected outcome has an incompatible reason")
        decided_at = _optional_timestamp(self.decided_at, "decided_at")
        if decided_at is None:
            raise ValueError("decided_at is required")
        object.__setattr__(self, "selected_memory_types", types)
        object.__setattr__(self, "selected_memory_refs", selected)
        object.__setattr__(self, "confirmation_items", confirmation)
        object.__setattr__(self, "candidate_count_stage", stage)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decided_at", decided_at)
        object.__setattr__(
            self,
            "decision_hash",
            _domain_hash("simple-harness/recall-decision/v3", self.to_json()),
        )

    def validate_bindings(
        self,
        context: RecallContext,
        plan: RecallPlan,
        *,
        current_time: float,
    ) -> None:
        plan.validate_narrowing(context, current_time=current_time)
        if self.run_id != context.run_id or self.subject != context.subject:
            raise ValueError("RecallDecision run/subject differs")
        if self.context_hash != context.context_hash:
            raise ValueError("RecallDecision context_hash differs")
        if self.context_revision != context.context_revision:
            raise ValueError("RecallDecision context_revision differs")
        if self.plan_id != plan.plan_id or self.plan_hash != plan.plan_hash:
            raise ValueError("RecallDecision plan binding differs")
        if not set(self.selected_memory_types) <= set(plan.requested_memory_types):
            raise ValueError("RecallDecision selects an unrequested memory type")
        if not {
            item.memory_type for item in self.confirmation_items
        } <= set(plan.requested_memory_types):
            raise ValueError("RecallDecision confirms an unrequested memory type")
        if set(self.selected_memory_refs) & {
            item.memory_ref for item in self.confirmation_items
        }:
            raise ValueError("RecallDecision selected and confirmation refs overlap")
        if self.disclosure_context.to_json() != plan.disclosure_context.to_json():
            raise ValueError("RecallDecision disclosure differs from RecallPlan")
        plan_evidence = {
            fingerprint_json(item.to_json()) for item in plan.evidence_refs
        }
        context_evidence = {
            fingerprint_json(item.to_json()) for item in context.evidence_refs
        }
        decision_evidence = {
            fingerprint_json(item.to_json()) for item in self.evidence_refs
        }
        if decision_evidence != plan_evidence or not decision_evidence <= context_evidence:
            raise ValueError("RecallDecision evidence_refs do not bind Plan/Context")
        if self.outcome in {
            RecallDecisionOutcome.RECALL,
            RecallDecisionOutcome.NEEDS_USER_CONFIRMATION,
        }:
            _validate_recall_disclosure(self.disclosure_context)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "context_hash": self.context_hash,
            "context_revision": self.context_revision,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "outcome": self.outcome.value,
            "selected_memory_types": [item.value for item in self.selected_memory_types],
            "selected_memory_refs": list(self.selected_memory_refs),
            "confirmation_items": [item.to_json() for item in self.confirmation_items],
            "filtered_candidate_count": self.filtered_candidate_count,
            "candidate_count_stage": self.candidate_count_stage.value,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "reason_codes": [item.value for item in self.reason_codes],
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallDecision:
        schema_version: int | None = None
        if "schema_version" in value:
            schema_version = _recall_decision_schema_version(
                value["schema_version"], "RecallDecision"
            )
        expected = {
            "schema_version", "decision_id", "run_id", "subject", "context_hash",
            "context_revision", "plan_id", "plan_hash", "outcome",
            "selected_memory_types", "selected_memory_refs", "confirmation_items",
            "filtered_candidate_count",
            "candidate_count_stage", "disclosure_context", "evidence_refs",
            "reason_codes", "decided_at",
        }
        _exact_keys(value, expected, "RecallDecision")
        count = value["filtered_candidate_count"]
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("filtered_candidate_count must be an integer")
        return cls(
            decision_id=_identifier(value["decision_id"], "decision_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            context_hash=_digest(value["context_hash"], "context_hash"),
            context_revision=_positive_int(value["context_revision"], "context_revision"),
            plan_id=_identifier(value["plan_id"], "plan_id"),
            plan_hash=_digest(value["plan_hash"], "plan_hash"),
            outcome=RecallDecisionOutcome(value["outcome"]),  # type: ignore[arg-type]
            selected_memory_types=tuple(
                LongTermMemoryType(item)
                for item in _strings(value["selected_memory_types"], "selected_memory_types")
            ),
            selected_memory_refs=_strings(
                value["selected_memory_refs"], "selected_memory_refs"
            ),
            confirmation_items=tuple(
                RecallConfirmationItem.from_json(item)
                for item in _objects(value["confirmation_items"], "confirmation_items")
            ),
            filtered_candidate_count=count,
            candidate_count_stage=RecallCandidateCountStage(value["candidate_count_stage"]),  # type: ignore[arg-type]
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            reason_codes=tuple(
                RecallReasonCode(item)
                for item in _strings(value["reason_codes"], "reason_codes")
            ),
            decided_at=cast(float, _optional_timestamp(value["decided_at"], "decided_at")),
            schema_version=cast(int, schema_version),
        )


class MemoryMutationKind(StrEnum):
    CREATE = "create"
    REVISE = "revise"
    SUPERSEDE = "supersede"
    CONTEST = "contest"
    SUPPRESS = "suppress"
    NO_MUTATION = "no_mutation"


class MemoryMutationPlanOutcome(StrEnum):
    MUTATE = "mutate"
    NO_MUTATION = "no_mutation"


class MemoryMutationApplyMode(StrEnum):
    """The protocol deliberately has no partial or best-effort apply mode."""

    STRICT_ATOMIC = "strict_atomic"


@dataclass(frozen=True, slots=True)
class ExistingMemoryTarget:
    memory_id: str
    revision: int

    def __post_init__(self) -> None:
        _identifier(self.memory_id, "memory_id")
        _positive_int(self.revision, "revision")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "target_kind": "existing_memory",
            "memory_id": self.memory_id,
            "revision": self.revision,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ExistingMemoryTarget:
        _exact_keys(
            value, {"target_kind", "memory_id", "revision"}, "ExistingMemoryTarget"
        )
        if value["target_kind"] != "existing_memory":
            raise ValueError("ExistingMemoryTarget discriminator differs")
        return cls(
            _identifier(value["memory_id"], "memory_id"),
            _positive_int(value["revision"], "revision"),
        )


@dataclass(frozen=True, slots=True)
class CreatedByOperationTarget:
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "target_kind": "created_by_operation",
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> CreatedByOperationTarget:
        _exact_keys(
            value, {"target_kind", "operation_id"}, "CreatedByOperationTarget"
        )
        if value["target_kind"] != "created_by_operation":
            raise ValueError("CreatedByOperationTarget discriminator differs")
        return cls(_identifier(value["operation_id"], "operation_id"))


MemoryMutationTarget = ExistingMemoryTarget | CreatedByOperationTarget


def _mutation_target_from_json(value: object) -> MemoryMutationTarget | None:
    if value is None:
        return None
    target = _object(value, "target")
    kind = target.get("target_kind")
    if kind == "existing_memory":
        return ExistingMemoryTarget.from_json(target)
    if kind == "created_by_operation":
        return CreatedByOperationTarget.from_json(target)
    raise ValueError("target has an unknown discriminator")


class EpisodeLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    AMENDED = "amended"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


class SemanticLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


class ProcedureLifecycleState(StrEnum):
    DRAFT = "draft"
    ELIGIBLE_FOR_ACTIVATION = "eligible_for_activation"
    ACTIVE = "active"
    REINFORCED = "reinforced"
    REVISED = "revised"
    INAPPLICABLE = "inapplicable"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class ProspectiveLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    PENDING = "pending"
    TRIGGERED = "triggered"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


CognitiveLifecycleState = (
    EpisodeLifecycleState
    | SemanticLifecycleState
    | ProcedureLifecycleState
    | ProspectiveLifecycleState
)


class EpistemicStatus(StrEnum):
    EXPLICIT_USER = "explicit_user"
    VERIFIED_EXTERNAL = "verified_external"
    OBSERVED_BEHAVIOR = "observed_behavior"
    LLM_INFERENCE = "llm_inference"
    UNKNOWN = "unknown"


class ConflictStatus(StrEnum):
    UNCONTESTED = "uncontested"
    CONTESTED = "contested"
    RESOLVED = "resolved"


class VerificationState(StrEnum):
    UNVERIFIED = "unverified"
    SOURCE_BOUND = "source_bound"
    USER_CONFIRMED = "user_confirmed"
    SOURCE_VERIFIED = "source_verified"
    REPEATED_OBSERVATION = "repeated_observation"


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


_PRIVACY_ORDER = {
    PrivacyClass.PUBLIC: 0,
    PrivacyClass.PERSONAL: 1,
    PrivacyClass.SENSITIVE: 2,
    PrivacyClass.RESTRICTED: 3,
}


class InformationAttribute(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL = "goal"
    WORK = "work"
    RELATIONSHIP = "relationship"
    FAMILY = "family"
    HEALTH = "health"
    LOCATION = "location"
    FINANCIAL = "financial"
    OTHER = "other"


class ProcedureRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"


class ProspectiveTriggerKind(StrEnum):
    TIME = "time"
    EVENT = "event"


def _bounded_tuple(
    value: object,
    name: str,
    *,
    max_items: int = 64,
    max_item_bytes: int = 2048,
    required: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError(f"{name} must contain strings")
    result = tuple(
        _bounded_text(item, f"{name} item", max_bytes=max_item_bytes) for item in value
    )
    if required and not result:
        raise ValueError(f"{name} must be non-empty")
    if len(result) > max_items:
        raise ValueError(f"{name} exceeds the item limit")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must be unique")
    return result


@dataclass(frozen=True, slots=True)
class ValidTimeInterval:
    valid_from: float | None
    valid_until: float | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.valid_from, "valid_from"),
            (self.valid_until, "valid_until"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) < 0
            ):
                raise ValueError(f"{name} must be a non-negative timestamp or null")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("valid time interval is inverted")

    def to_json(self) -> dict[str, JsonValue]:
        return {"valid_from": self.valid_from, "valid_until": self.valid_until}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ValidTimeInterval:
        _exact_keys(value, {"valid_from", "valid_until"}, "ValidTimeInterval")
        parsed: list[float | None] = []
        for name in ("valid_from", "valid_until"):
            item = value[name]
            if item is None:
                parsed.append(None)
            elif isinstance(item, bool) or not isinstance(item, (int, float)):
                raise TypeError(f"{name} must be numeric or null")
            else:
                parsed.append(float(item))
        return cls(parsed[0], parsed[1])


@dataclass(frozen=True, slots=True)
class EpisodeMemoryPayload:
    title: str
    participants: tuple[str, ...]
    goals: tuple[str, ...]
    actions: tuple[str, ...]
    results: tuple[str, ...]
    impacts: tuple[str, ...]
    occurred_start: float
    occurred_end: float | None
    thread_ref: str | None

    def __post_init__(self) -> None:
        _bounded_text(self.title, "title", max_bytes=4096)
        object.__setattr__(
            self, "participants", _bounded_tuple(self.participants, "participants", required=True)
        )
        for name in ("goals", "actions", "results", "impacts"):
            object.__setattr__(self, name, _bounded_tuple(getattr(self, name), name))
        interval = ValidTimeInterval(float(self.occurred_start), self.occurred_end)
        if interval.valid_from is None:
            raise ValueError("occurred_start is required")
        object.__setattr__(self, "occurred_start", interval.valid_from)
        object.__setattr__(self, "occurred_end", interval.valid_until)
        object.__setattr__(
            self, "thread_ref", _optional_identifier(self.thread_ref, "thread_ref")
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "memory_type": LongTermMemoryType.EPISODE.value,
            "title": self.title,
            "participants": list(self.participants),
            "goals": list(self.goals),
            "actions": list(self.actions),
            "results": list(self.results),
            "impacts": list(self.impacts),
            "occurred_start": self.occurred_start,
            "occurred_end": self.occurred_end,
            "thread_ref": self.thread_ref,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> EpisodeMemoryPayload:
        _exact_keys(
            value,
            {
                "memory_type", "title", "participants", "goals", "actions",
                "results", "impacts", "occurred_start", "occurred_end", "thread_ref",
            },
            "EpisodeMemoryPayload",
        )
        if value["memory_type"] != LongTermMemoryType.EPISODE.value:
            raise ValueError("EpisodeMemoryPayload discriminator differs")
        start = value["occurred_start"]
        end = value["occurred_end"]
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise TypeError("occurred_start must be numeric")
        if end is not None and (isinstance(end, bool) or not isinstance(end, (int, float))):
            raise TypeError("occurred_end must be numeric or null")
        return cls(
            _bounded_text(value["title"], "title", max_bytes=4096),
            _bounded_tuple(value["participants"], "participants", required=True),
            _bounded_tuple(value["goals"], "goals"),
            _bounded_tuple(value["actions"], "actions"),
            _bounded_tuple(value["results"], "results"),
            _bounded_tuple(value["impacts"], "impacts"),
            float(start),
            None if end is None else float(end),
            _optional_identifier(value["thread_ref"], "thread_ref"),
        )


@dataclass(frozen=True, slots=True)
class SemanticMemoryPayload:
    subject_entity: str
    predicate: str
    object_value: JsonValue
    qualifiers: tuple[str, ...]
    object_value_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _bounded_text(self.subject_entity, "subject_entity", max_bytes=2048)
        _bounded_text(self.predicate, "predicate", max_bytes=512)
        frozen = freeze_json(self.object_value)
        thawed = thaw_json(frozen)
        encoded_hash = fingerprint_json(thawed)
        if len(canonical_json(thawed).encode("utf-8")) > 16_384:
            raise ValueError("object_value exceeds the byte limit")
        object.__setattr__(self, "object_value", frozen)
        object.__setattr__(self, "object_value_hash", encoded_hash)
        object.__setattr__(self, "qualifiers", _bounded_tuple(self.qualifiers, "qualifiers"))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "memory_type": LongTermMemoryType.SEMANTIC.value,
            "subject_entity": self.subject_entity,
            "predicate": self.predicate,
            "object_value": thaw_json(cast(FrozenJsonValue, self.object_value)),
            "object_value_hash": self.object_value_hash,
            "qualifiers": list(self.qualifiers),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SemanticMemoryPayload:
        _exact_keys(
            value,
            {
                "memory_type", "subject_entity", "predicate", "object_value",
                "object_value_hash", "qualifiers",
            },
            "SemanticMemoryPayload",
        )
        if value["memory_type"] != LongTermMemoryType.SEMANTIC.value:
            raise ValueError("SemanticMemoryPayload discriminator differs")
        expected_hash = _digest(value["object_value_hash"], "object_value_hash")
        payload = cls(
            _bounded_text(value["subject_entity"], "subject_entity", max_bytes=2048),
            _bounded_text(value["predicate"], "predicate", max_bytes=512),
            cast(JsonValue, value["object_value"]),
            _bounded_tuple(value["qualifiers"], "qualifiers"),
        )
        if payload.object_value_hash != expected_hash:
            raise ValueError("object_value_hash does not bind object_value")
        return payload


@dataclass(frozen=True, slots=True)
class ProcedureMemoryPayload:
    name: str
    applicability: tuple[str, ...]
    steps: tuple[str, ...]
    proposed_risk_level: ProcedureRiskLevel

    def __post_init__(self) -> None:
        _bounded_text(self.name, "name", max_bytes=4096)
        object.__setattr__(
            self,
            "applicability",
            _bounded_tuple(self.applicability, "applicability", required=True),
        )
        object.__setattr__(self, "steps", _bounded_tuple(self.steps, "steps", required=True))
        object.__setattr__(
            self, "proposed_risk_level", ProcedureRiskLevel(self.proposed_risk_level)
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "memory_type": LongTermMemoryType.PROCEDURE.value,
            "name": self.name,
            "applicability": list(self.applicability),
            "steps": list(self.steps),
            "proposed_risk_level": self.proposed_risk_level.value,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProcedureMemoryPayload:
        _exact_keys(
            value,
            {"memory_type", "name", "applicability", "steps", "proposed_risk_level"},
            "ProcedureMemoryPayload",
        )
        if value["memory_type"] != LongTermMemoryType.PROCEDURE.value:
            raise ValueError("ProcedureMemoryPayload discriminator differs")
        return cls(
            _bounded_text(value["name"], "name", max_bytes=4096),
            _bounded_tuple(value["applicability"], "applicability", required=True),
            _bounded_tuple(value["steps"], "steps", required=True),
            ProcedureRiskLevel(value["proposed_risk_level"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ProspectiveTimeTrigger:
    trigger_at: float
    timezone: str

    def __post_init__(self) -> None:
        trigger_at = _optional_timestamp(self.trigger_at, "trigger_at")
        if trigger_at is None:
            raise ValueError("trigger_at is required")
        object.__setattr__(self, "trigger_at", trigger_at)
        _identifier(self.timezone, "timezone", max_length=128)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "trigger_kind": ProspectiveTriggerKind.TIME.value,
            "trigger_at": self.trigger_at,
            "timezone": self.timezone,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveTimeTrigger:
        _exact_keys(
            value, {"trigger_kind", "trigger_at", "timezone"}, "ProspectiveTimeTrigger"
        )
        if value["trigger_kind"] != ProspectiveTriggerKind.TIME.value:
            raise ValueError("ProspectiveTimeTrigger discriminator differs")
        return cls(
            cast(float, _optional_timestamp(value["trigger_at"], "trigger_at")),
            _identifier(value["timezone"], "timezone", max_length=128),
        )


@dataclass(frozen=True, slots=True)
class ProspectiveEventTrigger:
    event_authority_ref: str
    condition: str
    condition_hash: str

    def __post_init__(self) -> None:
        _identifier(self.event_authority_ref, "event_authority_ref", max_length=1024)
        _bounded_text(self.condition, "condition", max_bytes=4096)
        _digest(self.condition_hash, "condition_hash")
        if fingerprint_json(self.condition) != self.condition_hash:
            raise ValueError("condition_hash does not bind condition")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "trigger_kind": ProspectiveTriggerKind.EVENT.value,
            "event_authority_ref": self.event_authority_ref,
            "condition": self.condition,
            "condition_hash": self.condition_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveEventTrigger:
        _exact_keys(
            value,
            {"trigger_kind", "event_authority_ref", "condition", "condition_hash"},
            "ProspectiveEventTrigger",
        )
        if value["trigger_kind"] != ProspectiveTriggerKind.EVENT.value:
            raise ValueError("ProspectiveEventTrigger discriminator differs")
        return cls(
            _identifier(
                value["event_authority_ref"], "event_authority_ref", max_length=1024
            ),
            _bounded_text(value["condition"], "condition", max_bytes=4096),
            _digest(value["condition_hash"], "condition_hash"),
        )


ProspectiveTrigger = ProspectiveTimeTrigger | ProspectiveEventTrigger


def _prospective_trigger_from_json(value: object) -> ProspectiveTrigger:
    trigger = _object(value, "trigger")
    kind = trigger.get("trigger_kind")
    if kind == ProspectiveTriggerKind.TIME.value:
        return ProspectiveTimeTrigger.from_json(trigger)
    if kind == ProspectiveTriggerKind.EVENT.value:
        return ProspectiveEventTrigger.from_json(trigger)
    raise ValueError("trigger has an unknown discriminator")


@dataclass(frozen=True, slots=True)
class ProspectiveMemoryPayload:
    action: str
    trigger: ProspectiveTrigger

    def __post_init__(self) -> None:
        _bounded_text(self.action, "action", max_bytes=8192)
        if not isinstance(self.trigger, (ProspectiveTimeTrigger, ProspectiveEventTrigger)):
            raise TypeError("trigger must use a strict time or event trigger")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "memory_type": LongTermMemoryType.PROSPECTIVE.value,
            "action": self.action,
            "trigger": self.trigger.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveMemoryPayload:
        _exact_keys(
            value,
            {"memory_type", "action", "trigger"},
            "ProspectiveMemoryPayload",
        )
        if value["memory_type"] != LongTermMemoryType.PROSPECTIVE.value:
            raise ValueError("ProspectiveMemoryPayload discriminator differs")
        return cls(
            _bounded_text(value["action"], "action", max_bytes=8192),
            _prospective_trigger_from_json(value["trigger"]),
        )


MemoryMutationPayload = (
    EpisodeMemoryPayload
    | SemanticMemoryPayload
    | ProcedureMemoryPayload
    | ProspectiveMemoryPayload
)


_PAYLOAD_TYPES: dict[LongTermMemoryType, type[MemoryMutationPayload]] = {
    LongTermMemoryType.EPISODE: EpisodeMemoryPayload,
    LongTermMemoryType.SEMANTIC: SemanticMemoryPayload,
    LongTermMemoryType.PROCEDURE: ProcedureMemoryPayload,
    LongTermMemoryType.PROSPECTIVE: ProspectiveMemoryPayload,
}


_LIFECYCLE_TYPES: dict[LongTermMemoryType, type[CognitiveLifecycleState]] = {
    LongTermMemoryType.EPISODE: EpisodeLifecycleState,
    LongTermMemoryType.SEMANTIC: SemanticLifecycleState,
    LongTermMemoryType.PROCEDURE: ProcedureLifecycleState,
    LongTermMemoryType.PROSPECTIVE: ProspectiveLifecycleState,
}


def _payload_memory_type(payload: MemoryMutationPayload) -> LongTermMemoryType:
    if isinstance(payload, EpisodeMemoryPayload):
        return LongTermMemoryType.EPISODE
    if isinstance(payload, SemanticMemoryPayload):
        return LongTermMemoryType.SEMANTIC
    if isinstance(payload, ProcedureMemoryPayload):
        return LongTermMemoryType.PROCEDURE
    if isinstance(payload, ProspectiveMemoryPayload):
        return LongTermMemoryType.PROSPECTIVE
    raise TypeError("payload must use a registered cognitive payload")


def _payload_from_json(value: object) -> MemoryMutationPayload:
    payload = _object(value, "payload")
    raw_type = payload.get("memory_type")
    try:
        memory_type = LongTermMemoryType(raw_type)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("payload has an unknown memory_type discriminator") from exc
    return _PAYLOAD_TYPES[memory_type].from_json(payload)  # type: ignore[attr-defined,return-value]


@dataclass(frozen=True, slots=True)
class MemoryMutationOperation:
    operation_id: str
    kind: MemoryMutationKind
    memory_type: LongTermMemoryType
    payload: MemoryMutationPayload | None
    target: MemoryMutationTarget | None
    depends_on_operation_ids: tuple[str, ...]
    lifecycle_state: CognitiveLifecycleState
    epistemic_status: EpistemicStatus
    conflict_status: ConflictStatus
    verification_state: VerificationState
    valid_time_interval: ValidTimeInterval
    proposed_privacy_class: PrivacyClass
    proposed_information_attributes: tuple[InformationAttribute, ...]
    evidence_spans: tuple[EvidenceSpanRef, ...]
    reason_code: str

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        kind = MemoryMutationKind(self.kind)
        if kind is MemoryMutationKind.NO_MUTATION:
            raise ValueError("no_mutation is a plan outcome, not an operation")
        memory_type = LongTermMemoryType(self.memory_type)
        if self.payload is not None and _payload_memory_type(self.payload) is not memory_type:
            raise ValueError("payload discriminator differs from memory_type")
        if kind is MemoryMutationKind.CREATE:
            if self.target is not None or self.payload is None:
                raise ValueError("create requires payload and a null target")
        else:
            if not isinstance(
                self.target, (ExistingMemoryTarget, CreatedByOperationTarget)
            ):
                raise ValueError("existing-memory mutation requires a strict target")
            if kind is not MemoryMutationKind.SUPPRESS and self.payload is None:
                raise ValueError("non-suppress mutation requires a typed payload")
        dependencies = _bounded_identifiers(
            self.depends_on_operation_ids, "depends_on_operation_ids"
        )
        dependencies = tuple(sorted(dependencies))
        if self.operation_id in dependencies:
            raise ValueError("operation cannot depend on self")
        if isinstance(self.target, CreatedByOperationTarget):
            if self.target.operation_id == self.operation_id:
                raise ValueError("operation cannot target itself")
            if self.target.operation_id not in dependencies:
                raise ValueError("created_by_operation target must be an explicit dependency")
        lifecycle_type = _LIFECYCLE_TYPES[memory_type]
        if not isinstance(self.lifecycle_state, lifecycle_type):
            raise ValueError(
                f"lifecycle_state is invalid for {memory_type.value} memory"
            )
        lifecycle = self.lifecycle_state
        epistemic = EpistemicStatus(self.epistemic_status)
        conflict = ConflictStatus(self.conflict_status)
        verification = VerificationState(self.verification_state)
        if not isinstance(self.valid_time_interval, ValidTimeInterval):
            raise TypeError("valid_time_interval must use ValidTimeInterval")
        privacy = PrivacyClass(self.proposed_privacy_class)
        attributes = cast(
            tuple[InformationAttribute, ...],
            _bounded_enum_tuple(
                self.proposed_information_attributes,
                "proposed_information_attributes",
                InformationAttribute,
                max_items=32,
            ),
        )
        spans = tuple(self.evidence_spans)
        if (
            not spans
            or len(spans) > 64
            or not all(isinstance(item, EvidenceSpanRef) for item in spans)
            or len({item.span_id for item in spans}) != len(spans)
        ):
            raise ValueError("evidence_spans must contain unique bounded EvidenceSpanRef values")
        _identifier(self.reason_code, "reason_code")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "memory_type", memory_type)
        object.__setattr__(self, "depends_on_operation_ids", dependencies)
        object.__setattr__(self, "lifecycle_state", lifecycle)
        object.__setattr__(self, "epistemic_status", epistemic)
        object.__setattr__(self, "conflict_status", conflict)
        object.__setattr__(self, "verification_state", verification)
        object.__setattr__(self, "proposed_privacy_class", privacy)
        object.__setattr__(self, "proposed_information_attributes", attributes)
        object.__setattr__(self, "evidence_spans", spans)

    def effective_privacy_class(
        self, *trusted_floors: PrivacyClass
    ) -> PrivacyClass:
        """Join the model proposal with every trusted policy/evidence/record floor."""

        floors = tuple(PrivacyClass(item) for item in trusted_floors)
        if not floors:
            raise ValueError("effective privacy requires at least one trusted floor")
        return max(
            (self.proposed_privacy_class, *floors),
            key=lambda item: _PRIVACY_ORDER[item],
        )

    def effective_information_attributes(
        self, *trusted_attribute_sets: tuple[InformationAttribute, ...]
    ) -> tuple[InformationAttribute, ...]:
        """Union proposal attributes with all trusted policy/evidence/record labels."""

        trusted = {
            InformationAttribute(item)
            for values in trusted_attribute_sets
            for item in values
        }
        return tuple(
            sorted(
                set(self.proposed_information_attributes)
                | trusted,
                key=lambda item: item.value,
            )
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "memory_type": self.memory_type.value,
            "payload": None if self.payload is None else self.payload.to_json(),
            "target": None if self.target is None else self.target.to_json(),
            "depends_on_operation_ids": list(self.depends_on_operation_ids),
            "lifecycle_state": self.lifecycle_state.value,
            "epistemic_status": self.epistemic_status.value,
            "conflict_status": self.conflict_status.value,
            "verification_state": self.verification_state.value,
            "valid_time_interval": self.valid_time_interval.to_json(),
            "proposed_privacy_class": self.proposed_privacy_class.value,
            "proposed_information_attributes": [
                item.value for item in self.proposed_information_attributes
            ],
            "evidence_spans": [item.to_json() for item in self.evidence_spans],
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryMutationOperation:
        expected = {
            "operation_id", "kind", "memory_type", "payload", "target",
            "depends_on_operation_ids", "lifecycle_state", "epistemic_status",
            "conflict_status", "verification_state", "valid_time_interval",
            "proposed_privacy_class", "proposed_information_attributes",
            "evidence_spans", "reason_code",
        }
        _exact_keys(value, expected, "MemoryMutationOperation")
        memory_type = LongTermMemoryType(value["memory_type"])  # type: ignore[arg-type]
        lifecycle_type = _LIFECYCLE_TYPES[memory_type]
        payload_value = value["payload"]
        return cls(
            operation_id=_identifier(value["operation_id"], "operation_id"),
            kind=MemoryMutationKind(value["kind"]),  # type: ignore[arg-type]
            memory_type=memory_type,
            payload=None if payload_value is None else _payload_from_json(payload_value),
            target=_mutation_target_from_json(value["target"]),
            depends_on_operation_ids=_strings(
                value["depends_on_operation_ids"], "depends_on_operation_ids"
            ),
            lifecycle_state=lifecycle_type(value["lifecycle_state"]),  # type: ignore[arg-type]
            epistemic_status=EpistemicStatus(value["epistemic_status"]),  # type: ignore[arg-type]
            conflict_status=ConflictStatus(value["conflict_status"]),  # type: ignore[arg-type]
            verification_state=VerificationState(value["verification_state"]),  # type: ignore[arg-type]
            valid_time_interval=ValidTimeInterval.from_json(
                _object(value["valid_time_interval"], "valid_time_interval")
            ),
            proposed_privacy_class=PrivacyClass(value["proposed_privacy_class"]),  # type: ignore[arg-type]
            proposed_information_attributes=tuple(
                InformationAttribute(item)
                for item in _strings(
                    value["proposed_information_attributes"],
                    "proposed_information_attributes",
                )
            ),
            evidence_spans=tuple(
                EvidenceSpanRef.from_json(item)
                for item in _objects(value["evidence_spans"], "evidence_spans")
            ),
            reason_code=_identifier(value["reason_code"], "reason_code"),
        )


@dataclass(frozen=True, slots=True)
class MemoryMutationPlan:
    plan_id: str
    run_id: str
    subject: str
    base_revision: int
    outcome: MemoryMutationPlanOutcome
    operations: tuple[MemoryMutationOperation, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    apply_mode: MemoryMutationApplyMode = MemoryMutationApplyMode.STRICT_ATOMIC
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryMutationPlan schema_version")
        for value, name in (
            (self.plan_id, "plan_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        apply_mode = MemoryMutationApplyMode(self.apply_mode)
        if apply_mode is not MemoryMutationApplyMode.STRICT_ATOMIC:
            raise ValueError("MemoryMutationPlan requires strict_atomic apply_mode")
        _positive_int(self.base_revision, "base_revision")
        outcome = MemoryMutationPlanOutcome(self.outcome)
        operations = tuple(self.operations)
        if not all(isinstance(item, MemoryMutationOperation) for item in operations):
            raise ValueError("operations must contain MemoryMutationOperation values")
        if len(operations) > 128:
            raise ValueError("operations exceeds the item limit")
        if len({item.operation_id for item in operations}) != len(operations):
            raise ValueError("operation_id values must be unique")
        if outcome is MemoryMutationPlanOutcome.NO_MUTATION and operations:
            raise ValueError("no_mutation outcome requires empty operations")
        if outcome is MemoryMutationPlanOutcome.MUTATE and not operations:
            raise ValueError("mutate outcome requires operations")
        operation_ids = {item.operation_id for item in operations}
        for operation in operations:
            dependencies = set(operation.depends_on_operation_ids)
            if isinstance(operation.target, CreatedByOperationTarget):
                dependencies.add(operation.target.operation_id)
            unknown = dependencies - operation_ids
            if unknown:
                raise ValueError(f"operation has unknown dependencies: {sorted(unknown)}")
        by_id = {item.operation_id: item for item in operations}
        for operation in operations:
            if isinstance(operation.target, CreatedByOperationTarget):
                producer = by_id[operation.target.operation_id]
                if producer.kind is not MemoryMutationKind.CREATE:
                    raise ValueError(
                        "created_by_operation producer must be a create operation"
                    )
                if producer.memory_type is not operation.memory_type:
                    raise ValueError(
                        "created_by_operation producer must have the same memory_type"
                    )
        operations = self._stable_topological_operations(operations)
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise ValueError("MemoryMutationPlan requires evidence_refs")
        evidence_coverage = {
            (item.evidence_id, item.content_hash) for item in evidence_refs
        }
        for operation in operations:
            for span in operation.evidence_spans:
                if (span.evidence_id, span.envelope_hash) not in evidence_coverage:
                    raise ValueError(
                        "MemoryMutationPlan evidence_refs do not cover every evidence span"
                    )
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "apply_mode", apply_mode)
        object.__setattr__(
            self,
            "plan_hash",
            _domain_hash("simple-harness/memory-mutation-plan/v2", self.to_json()),
        )

    @staticmethod
    def _stable_topological_operations(
        operations: tuple[MemoryMutationOperation, ...],
    ) -> tuple[MemoryMutationOperation, ...]:
        by_id = {item.operation_id: item for item in operations}
        remaining = {}
        for item in operations:
            dependencies = set(item.depends_on_operation_ids)
            if isinstance(item.target, CreatedByOperationTarget):
                dependencies.add(item.target.operation_id)
            remaining[item.operation_id] = dependencies
        ordered: list[MemoryMutationOperation] = []
        while remaining:
            ready = sorted(
                operation_id for operation_id, deps in remaining.items() if not deps
            )
            if not ready:
                raise ValueError("operation dependency graph contains a cycle")
            for operation_id in ready:
                ordered.append(by_id[operation_id])
                remaining.pop(operation_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(ordered)

    def topological_operations(self) -> tuple[MemoryMutationOperation, ...]:
        """Stable apply order; wire array order has no authority."""

        return self._stable_topological_operations(self.operations)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "base_revision": self.base_revision,
            "outcome": self.outcome.value,
            "operations": [operation.to_json() for operation in self.operations],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
            "apply_mode": self.apply_mode.value,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryMutationPlan:
        _exact_keys(
            value,
            {
                "schema_version",
                "plan_id",
                "run_id",
                "subject",
                "base_revision",
                "outcome",
                "operations",
                "disclosure_context",
                "evidence_refs",
                "idempotency_key",
                "apply_mode",
            },
            "MemoryMutationPlan",
        )
        return cls(
            plan_id=_identifier(value["plan_id"], "plan_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            base_revision=_positive_int(value["base_revision"], "base_revision"),
            outcome=MemoryMutationPlanOutcome(value["outcome"]),  # type: ignore[arg-type]
            operations=tuple(
                MemoryMutationOperation.from_json(item)
                for item in _objects(value["operations"], "operations")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            apply_mode=MemoryMutationApplyMode(value["apply_mode"]),  # type: ignore[arg-type]
            schema_version=_cognitive_schema_version(
                value["schema_version"], "MemoryMutationPlan"
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryMutationApplyReceiptRef:
    receipt_id: str
    receipt_hash: str

    def __post_init__(self) -> None:
        _identifier(self.receipt_id, "receipt_id")
        _digest(self.receipt_hash, "receipt_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "receipt_id": self.receipt_id,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_json(
        cls, value: Mapping[str, object]
    ) -> MemoryMutationApplyReceiptRef:
        _exact_keys(
            value,
            {"receipt_id", "receipt_hash"},
            "MemoryMutationApplyReceiptRef",
        )
        return cls(
            _identifier(value["receipt_id"], "receipt_id"),
            _digest(value["receipt_hash"], "receipt_hash"),
        )


@dataclass(frozen=True, slots=True)
class MemoryMutationApplyReceipt:
    """Authority-issued receipt for one indivisible plan transaction."""

    receipt_id: str
    authority_ref: str
    plan_id: str
    plan_hash: str
    run_id: str
    subject: str
    base_revision: int
    committed_revision: int
    canonical_operation_ids: tuple[str, ...]
    apply_mode: MemoryMutationApplyMode
    committed_at: float
    schema_version: int = COGNITIVE_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COGNITIVE_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryMutationApplyReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.authority_ref, "authority_ref"),
            (self.plan_id, "plan_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
        ):
            _identifier(value, name)
        _digest(self.plan_hash, "plan_hash")
        _positive_int(self.base_revision, "base_revision")
        _positive_int(self.committed_revision, "committed_revision")
        operation_ids = _bounded_identifiers(
            self.canonical_operation_ids,
            "canonical_operation_ids",
        )
        apply_mode = MemoryMutationApplyMode(self.apply_mode)
        if apply_mode is not MemoryMutationApplyMode.STRICT_ATOMIC:
            raise ValueError("apply receipt must use strict_atomic mode")
        committed_at = _trusted_current_time(self.committed_at)
        object.__setattr__(self, "canonical_operation_ids", operation_ids)
        object.__setattr__(self, "apply_mode", apply_mode)
        object.__setattr__(self, "committed_at", committed_at)
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash(
                "simple-harness/memory-mutation-apply-receipt/v2",
                self.to_json(),
            ),
        )

    def validate_plan(self, plan: MemoryMutationPlan) -> None:
        if not isinstance(plan, MemoryMutationPlan):
            raise TypeError("plan must use MemoryMutationPlan")
        if (
            self.plan_id != plan.plan_id
            or self.plan_hash != plan.plan_hash
            or self.run_id != plan.run_id
            or self.subject != plan.subject
            or self.apply_mode is not plan.apply_mode
        ):
            raise ValueError("apply receipt does not exactly bind mutation plan")
        if self.base_revision != plan.base_revision:
            raise ValueError("apply receipt base_revision differs from mutation plan")
        expected_ids = tuple(
            operation.operation_id for operation in plan.topological_operations()
        )
        if self.canonical_operation_ids != expected_ids:
            raise ValueError("apply receipt does not cover all canonical operations")
        expected_committed_revision = plan.base_revision + (
            1 if plan.outcome is MemoryMutationPlanOutcome.MUTATE else 0
        )
        if self.committed_revision != expected_committed_revision:
            raise ValueError(
                "apply receipt does not bind the atomic base-to-committed revision"
            )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "authority_ref": self.authority_ref,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "base_revision": self.base_revision,
            "committed_revision": self.committed_revision,
            "canonical_operation_ids": list(self.canonical_operation_ids),
            "apply_mode": self.apply_mode.value,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryMutationApplyReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "authority_ref",
                "plan_id",
                "plan_hash",
                "run_id",
                "subject",
                "base_revision",
                "committed_revision",
                "canonical_operation_ids",
                "apply_mode",
                "committed_at",
            },
            "MemoryMutationApplyReceipt",
        )
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            authority_ref=_identifier(value["authority_ref"], "authority_ref"),
            plan_id=_identifier(value["plan_id"], "plan_id"),
            plan_hash=_digest(value["plan_hash"], "plan_hash"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            base_revision=_positive_int(value["base_revision"], "base_revision"),
            committed_revision=_positive_int(
                value["committed_revision"], "committed_revision"
            ),
            canonical_operation_ids=_strings(
                value["canonical_operation_ids"], "canonical_operation_ids"
            ),
            apply_mode=MemoryMutationApplyMode(value["apply_mode"]),  # type: ignore[arg-type]
            committed_at=cast(
                float, _optional_timestamp(value["committed_at"], "committed_at")
            ),
            schema_version=_cognitive_schema_version(
                value["schema_version"], "MemoryMutationApplyReceipt"
            ),
        )


class MemoryMutationApplyAuthorityPort(Protocol):
    """Trusted Memory boundary that resolves durable apply receipts."""

    async def resolve_memory_mutation_apply_receipt(
        self, receipt_ref: MemoryMutationApplyReceiptRef
    ) -> MemoryMutationApplyReceipt: ...


async def verify_memory_mutation_apply_receipt(
    plan: MemoryMutationPlan,
    receipt_ref: MemoryMutationApplyReceiptRef,
    authority: MemoryMutationApplyAuthorityPort,
) -> MemoryMutationApplyReceipt:
    """Resolve the receipt from authority and verify an all-or-nothing commit."""

    if not isinstance(receipt_ref, MemoryMutationApplyReceiptRef):
        raise TypeError("receipt_ref must use MemoryMutationApplyReceiptRef")
    receipt = await authority.resolve_memory_mutation_apply_receipt(receipt_ref)
    if not isinstance(receipt, MemoryMutationApplyReceipt):
        raise TypeError("apply authority returned an invalid receipt")
    if (
        receipt.receipt_id != receipt_ref.receipt_id
        or receipt.receipt_hash != receipt_ref.receipt_hash
    ):
        raise ValueError("apply authority receipt differs from receipt_ref")
    receipt.validate_plan(plan)
    return receipt


def _cognitive_schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != COGNITIVE_MEMORY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _recall_decision_schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != RECALL_DECISION_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


__all__ = (
    "ConflictStatus",
    "ContextAssemblyBudget",
    "ContextAssemblyDecision",
    "ContextAssemblyReasonCode",
    "ContextFragment",
    "ContextFragmentType",
    "CreatedByOperationTarget",
    "EpisodeLifecycleState",
    "EpisodeMemoryPayload",
    "EpistemicStatus",
    "ExistingMemoryTarget",
    "InformationAttribute",
    "LongTermMemoryType",
    "MemoryMutationApplyAuthorityPort",
    "MemoryMutationApplyMode",
    "MemoryMutationApplyReceipt",
    "MemoryMutationApplyReceiptRef",
    "MemoryMutationKind",
    "MemoryMutationOperation",
    "MemoryMutationPlan",
    "MemoryMutationPlanOutcome",
    "PrivacyClass",
    "ProcedureLifecycleState",
    "ProcedureMemoryPayload",
    "ProcedureRiskLevel",
    "ProspectiveEventTrigger",
    "ProspectiveLifecycleState",
    "ProspectiveMemoryPayload",
    "ProspectiveTimeTrigger",
    "ProspectiveTriggerKind",
    "RecallBudget",
    "RecallCandidateCountStage",
    "RecallConfirmationItem",
    "RecallContext",
    "RecallDecision",
    "RecallDecisionOutcome",
    "RecallPlan",
    "RecallReasonCode",
    "RecallRetrievalMode",
    "RecallSelectorDomain",
    "RECALL_DECISION_SCHEMA_VERSION",
    "SemanticLifecycleState",
    "SemanticMemoryPayload",
    "ValidTimeInterval",
    "VerificationState",
    "WorkingMemoryRole",
    "verify_memory_mutation_apply_receipt",
)
