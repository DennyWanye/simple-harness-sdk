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

from simple_harness.contracts import JsonValue

from .disclosure_protocol import (
    HUMAN_MEMORY_SCHEMA_VERSION,
    DisclosureContext,
    _bounded_text,
    _canonical_hash,
    _exact_keys,
    _identifier,
    _optional_identifier,
    _positive_int,
)
from .evidence_protocol import EvidenceRef, _evidence_refs


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


class RecallDecisionOutcome(StrEnum):
    NO_RECALL = "no_recall"
    RECALL = "recall"
    NEEDS_USER_CONFIRMATION = "needs_user_confirmation"
    REJECTED = "rejected"


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
class RecallContext:
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


@dataclass(frozen=True, slots=True)
class RecallPlan:
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


@dataclass(frozen=True, slots=True)
class RecallDecision:
    decision_id: str
    run_id: str
    subject: str
    plan_id: str | None
    plan_hash: str | None
    outcome: RecallDecisionOutcome
    selected_memory_types: tuple[LongTermMemoryType, ...]
    selected_memory_refs: tuple[str, ...]
    filtered_candidate_count: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    reason_codes: tuple[RecallReasonCode, ...]
    decided_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported RecallDecision schema_version")
        for value, name in (
            (self.decision_id, "decision_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
        ):
            _identifier(value, name)
        _optional_identifier(self.plan_id, "plan_id")
        if (self.plan_id is None) != (self.plan_hash is None):
            raise ValueError("plan_id and plan_hash must both be set or both be null")
        if self.plan_hash is not None:
            from .disclosure_protocol import _digest

            _digest(self.plan_hash, "plan_hash")
        object.__setattr__(self, "outcome", RecallDecisionOutcome(self.outcome))
        types = tuple(LongTermMemoryType(item) for item in self.selected_memory_types)
        refs_selected = tuple(
            _identifier(item, "selected_memory_ref") for item in self.selected_memory_refs
        )
        if len(set(types)) != len(types) or len(set(refs_selected)) != len(refs_selected):
            raise ValueError("selected memory values must be unique")
        if (
            isinstance(self.filtered_candidate_count, bool)
            or not isinstance(self.filtered_candidate_count, int)
            or self.filtered_candidate_count < 0
        ):
            raise ValueError("filtered_candidate_count must be non-negative")
        if self.outcome is RecallDecisionOutcome.NO_RECALL and (types or refs_selected):
            raise ValueError("no_recall cannot select memories")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence_refs = _evidence_refs(self.evidence_refs)
        reasons = tuple(RecallReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        if (
            isinstance(self.decided_at, bool)
            or not isinstance(self.decided_at, (int, float))
            or float(self.decided_at) < 0
        ):
            raise ValueError("decided_at must be a non-negative timestamp")
        object.__setattr__(self, "selected_memory_types", types)
        object.__setattr__(self, "selected_memory_refs", refs_selected)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decided_at", float(self.decided_at))
        object.__setattr__(self, "decision_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "outcome": self.outcome.value,
            "selected_memory_types": [item.value for item in self.selected_memory_types],
            "selected_memory_refs": list(self.selected_memory_refs),
            "filtered_candidate_count": self.filtered_candidate_count,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "reason_codes": [reason.value for reason in self.reason_codes],
            "decided_at": self.decided_at,
        }


class MemoryMutationKind(StrEnum):
    CREATE = "create"
    REVISE = "revise"
    SUPERSEDE = "supersede"
    CONTEST = "contest"
    SUPPRESS = "suppress"
    NO_MUTATION = "no_mutation"


@dataclass(frozen=True, slots=True)
class MemoryMutationOperation:
    operation_id: str
    kind: MemoryMutationKind
    memory_type: LongTermMemoryType
    target_memory_id: str | None
    claim: str | None
    evidence_refs: tuple[EvidenceRef, ...]
    reason_code: str

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        object.__setattr__(self, "kind", MemoryMutationKind(self.kind))
        object.__setattr__(self, "memory_type", LongTermMemoryType(self.memory_type))
        _optional_identifier(self.target_memory_id, "target_memory_id")
        if self.claim is not None:
            _bounded_text(self.claim, "claim", max_bytes=16_384)
        refs = _evidence_refs(self.evidence_refs)
        if self.kind is not MemoryMutationKind.NO_MUTATION and not refs:
            raise ValueError("mutation operations require evidence_refs")
        if self.kind is MemoryMutationKind.CREATE and self.target_memory_id is not None:
            raise ValueError("create operation cannot target an existing memory")
        if self.kind in {
            MemoryMutationKind.REVISE,
            MemoryMutationKind.SUPERSEDE,
            MemoryMutationKind.CONTEST,
            MemoryMutationKind.SUPPRESS,
        } and self.target_memory_id is None:
            raise ValueError("mutation of existing memory requires target_memory_id")
        _identifier(self.reason_code, "reason_code")
        object.__setattr__(self, "evidence_refs", refs)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "memory_type": self.memory_type.value,
            "target_memory_id": self.target_memory_id,
            "claim": self.claim,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class MemoryMutationPlan:
    plan_id: str
    run_id: str
    subject: str
    base_revision: int
    operations: tuple[MemoryMutationOperation, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryMutationPlan schema_version")
        for value, name in (
            (self.plan_id, "plan_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _positive_int(self.base_revision, "base_revision")
        operations = tuple(self.operations)
        if not operations or not all(
            isinstance(item, MemoryMutationOperation) for item in operations
        ):
            raise ValueError("operations must contain MemoryMutationOperation values")
        if len({item.operation_id for item in operations}) != len(operations):
            raise ValueError("operation_id values must be unique")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence_refs = _evidence_refs(self.evidence_refs)
        if not evidence_refs:
            raise ValueError("MemoryMutationPlan requires evidence_refs")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "plan_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "base_revision": self.base_revision,
            "operations": [operation.to_json() for operation in self.operations],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
        }


__all__ = (
    "LongTermMemoryType",
    "MemoryMutationKind",
    "MemoryMutationOperation",
    "MemoryMutationPlan",
    "RecallBudget",
    "RecallContext",
    "RecallDecision",
    "RecallDecisionOutcome",
    "RecallPlan",
    "RecallReasonCode",
    "WorkingMemoryRole",
)
