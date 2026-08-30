# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""TaskScope proposals, search/open receipts, and semantic mutation plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from simple_harness.contracts import JsonValue

from .disclosure_protocol import (
    HUMAN_MEMORY_SCHEMA_VERSION,
    DisclosureContext,
    _bounded_text,
    _canonical_hash,
    _digest,
    _identifier,
    _optional_identifier,
    _positive_int,
)
from .evidence_protocol import EvidenceRef, _evidence_refs


class TaskScopeRoute(StrEnum):
    DIRECT_STANDALONE = "direct_standalone"
    MEMORY_STANDALONE = "memory_standalone"
    CONTINUE_ACTIVE = "continue_active"
    RESUME_EXISTING = "resume_existing"
    CREATE_NEW = "create_new"


class TaskScopeReasonCode(StrEnum):
    SELF_CONTAINED = "task_scope_self_contained"
    MEMORY_ONLY = "task_scope_memory_only"
    ACTIVE_TASK_CONTINUATION = "task_scope_active_task_continuation"
    HISTORICAL_TASK_REFERENCE = "task_scope_historical_task_reference"
    MULTI_STEP_TASK = "task_scope_multi_step_task"
    PROJECT_EFFECT_REQUIRED = "task_scope_project_effect_required"
    FUTURE_CONTINUATION = "task_scope_future_continuation"
    AMBIGUOUS_TARGET = "task_scope_ambiguous_target"
    EXACT_OPEN_REQUIRED = "task_scope_exact_open_required"
    BINDING_REQUIRED = "task_scope_binding_required"
    STALE_BASE_REVISION = "task_scope_stale_base_revision"
    INVALID_EVIDENCE_REF = "task_scope_invalid_evidence_ref"
    IDEMPOTENT_REPLAY = "task_scope_idempotent_replay"
    PAYLOAD_CONFLICT = "task_scope_payload_conflict"


@dataclass(frozen=True, slots=True)
class TaskScopeProposal:
    proposal_id: str
    run_id: str
    subject: str
    route: TaskScopeRoute
    target_task_scope_id: str | None
    goal: str | None
    project_hint: str | None
    confidence_millionths: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    reason_codes: tuple[TaskScopeReasonCode, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    proposal_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeProposal schema_version")
        for value, name in (
            (self.proposal_id, "proposal_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        object.__setattr__(self, "route", TaskScopeRoute(self.route))
        _optional_identifier(self.target_task_scope_id, "target_task_scope_id")
        if self.goal is not None:
            _bounded_text(self.goal, "goal", max_bytes=16_384)
        if self.project_hint is not None:
            _bounded_text(self.project_hint, "project_hint", max_bytes=2048)
        if (
            isinstance(self.confidence_millionths, bool)
            or not isinstance(self.confidence_millionths, int)
            or not 0 <= self.confidence_millionths <= 1_000_000
        ):
            raise ValueError("confidence_millionths must be between 0 and 1000000")
        if self.route is TaskScopeRoute.CREATE_NEW:
            if self.goal is None or self.target_task_scope_id is not None:
                raise ValueError("create_new requires goal and forbids target_task_scope_id")
        elif self.route in {
            TaskScopeRoute.CONTINUE_ACTIVE,
            TaskScopeRoute.RESUME_EXISTING,
        }:
            if self.target_task_scope_id is None:
                raise ValueError(f"{self.route.value} requires target_task_scope_id")
        elif self.target_task_scope_id is not None or self.goal is not None:
            raise ValueError("standalone route cannot target or create a TaskScope")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("TaskScopeProposal requires evidence_refs")
        reasons = tuple(TaskScopeReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "proposal_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "route": self.route.value,
            "target_task_scope_id": self.target_task_scope_id,
            "goal": self.goal,
            "project_hint": self.project_hint,
            "confidence_millionths": self.confidence_millionths,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }


class TaskScopeMutationOutcome(StrEnum):
    MUTATE = "mutate"
    NO_MUTATION = "no_mutation"


class TaskScopeMutationKind(StrEnum):
    GOAL_SET = "goal.set"
    GOAL_REVISE = "goal.revise"
    SCOPE_INCLUDE = "scope.include"
    SCOPE_EXCLUDE = "scope.exclude"
    DECISION_RECORD = "decision.record"
    DECISION_SUPERSEDE = "decision.supersede"
    PLAN_STEP_ADD = "plan.step.add"
    PLAN_STEP_REVISE = "plan.step.revise"
    PLAN_STEP_CANCEL = "plan.step.cancel"
    PLAN_REORDER = "plan.reorder"
    TASK_PAUSE = "task.pause"
    TASK_BLOCK = "task.block"
    TASK_RESUME = "task.resume"
    TASK_COMPLETE = "task.complete"
    RESUME_UPDATE = "resume.update"
    CHECKPOINT_REQUEST = "checkpoint.request"
    RELATION_ADD = "relation.add"


@dataclass(frozen=True, slots=True)
class TaskScopeMutationOperation:
    operation_id: str
    kind: TaskScopeMutationKind
    value: str
    evidence_refs: tuple[EvidenceRef, ...]
    reason_code: str

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        object.__setattr__(self, "kind", TaskScopeMutationKind(self.kind))
        _bounded_text(self.value, "value", max_bytes=32_768)
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("TaskScope mutation operation requires evidence_refs")
        _identifier(self.reason_code, "reason_code")
        object.__setattr__(self, "evidence_refs", refs)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "value": self.value,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class TaskScopeMutationPlan:
    plan_id: str
    run_id: str
    subject: str
    task_scope_id: str
    base_revision: int
    outcome: TaskScopeMutationOutcome
    operations: tuple[TaskScopeMutationOperation, ...]
    closure_reason: str | None
    source_turn_id: str
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    plan_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeMutationPlan schema_version")
        for value, name in (
            (self.plan_id, "plan_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.source_turn_id, "source_turn_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _positive_int(self.base_revision, "base_revision")
        object.__setattr__(self, "outcome", TaskScopeMutationOutcome(self.outcome))
        operations = tuple(self.operations)
        if not all(isinstance(item, TaskScopeMutationOperation) for item in operations):
            raise TypeError("operations must contain TaskScopeMutationOperation values")
        if len({item.operation_id for item in operations}) != len(operations):
            raise ValueError("operation_id values must be unique")
        if self.outcome is TaskScopeMutationOutcome.MUTATE and not operations:
            raise ValueError("mutate outcome requires operations")
        if self.outcome is TaskScopeMutationOutcome.NO_MUTATION and operations:
            raise ValueError("no_mutation outcome forbids operations")
        if self.closure_reason is not None:
            _bounded_text(self.closure_reason, "closure_reason", max_bytes=4096)
        if self.outcome is TaskScopeMutationOutcome.NO_MUTATION and self.closure_reason is None:
            raise ValueError("no_mutation requires closure_reason")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("TaskScopeMutationPlan requires evidence_refs")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "plan_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "base_revision": self.base_revision,
            "outcome": self.outcome.value,
            "operations": [operation.to_json() for operation in self.operations],
            "closure_reason": self.closure_reason,
            "source_turn_id": self.source_turn_id,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class TaskScopeSearchRequest:
    search_id: str
    run_id: str
    subject: str
    query: str
    max_candidates: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeSearchRequest schema_version")
        for value, name in (
            (self.search_id, "search_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _bounded_text(self.query, "query", max_bytes=8192)
        _positive_int(self.max_candidates, "max_candidates")
        if self.max_candidates > 20:
            raise ValueError("max_candidates must not exceed 20")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("TaskScopeSearchRequest requires evidence_refs")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "request_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "search_id": self.search_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "query": self.query,
            "max_candidates": self.max_candidates,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class TaskScopeCandidate:
    task_scope_id: str
    canonical_revision: int
    title: str
    status: str
    relevance_millionths: int
    project_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.task_scope_id, "task_scope_id")
        _positive_int(self.canonical_revision, "canonical_revision")
        _bounded_text(self.title, "title", max_bytes=1024)
        _identifier(self.status, "status")
        if (
            isinstance(self.relevance_millionths, bool)
            or not isinstance(self.relevance_millionths, int)
            or not 0 <= self.relevance_millionths <= 1_000_000
        ):
            raise ValueError("relevance_millionths must be between 0 and 1000000")
        refs = tuple(_identifier(item, "project_ref") for item in self.project_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("project_refs must be unique")
        object.__setattr__(self, "project_refs", refs)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "task_scope_id": self.task_scope_id,
            "canonical_revision": self.canonical_revision,
            "title": self.title,
            "status": self.status,
            "relevance_millionths": self.relevance_millionths,
            "project_refs": list(self.project_refs),
        }


@dataclass(frozen=True, slots=True)
class TaskScopeSearchReceipt:
    receipt_id: str
    run_id: str
    subject: str
    search_id: str
    request_hash: str
    candidates: tuple[TaskScopeCandidate, ...]
    permission_filter_revision: int
    reason_codes: tuple[TaskScopeReasonCode, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeSearchReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.search_id, "search_id"),
        ):
            _identifier(value, name)
        _digest(self.request_hash, "request_hash")
        candidates = tuple(self.candidates)
        if not all(isinstance(item, TaskScopeCandidate) for item in candidates):
            raise TypeError("candidates must contain TaskScopeCandidate values")
        if len({item.task_scope_id for item in candidates}) != len(candidates):
            raise ValueError("candidate task_scope_id values must be unique")
        _positive_int(self.permission_filter_revision, "permission_filter_revision")
        reasons = tuple(TaskScopeReasonCode(reason) for reason in self.reason_codes)
        if len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be unique")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "receipt_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "search_id": self.search_id,
            "request_hash": self.request_hash,
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "permission_filter_revision": self.permission_filter_revision,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }


@dataclass(frozen=True, slots=True)
class TaskScopeOpenRequest:
    open_id: str
    run_id: str
    subject: str
    task_scope_id: str
    expected_revision: int | None
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeOpenRequest schema_version")
        for value, name in (
            (self.open_id, "open_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        if self.expected_revision is not None:
            _positive_int(self.expected_revision, "expected_revision")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("TaskScopeOpenRequest requires evidence_refs")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "request_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "open_id": self.open_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "expected_revision": self.expected_revision,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class TaskScopeOpenReceipt:
    receipt_id: str
    run_id: str
    subject: str
    open_id: str
    request_hash: str
    task_scope_id: str
    canonical_revision: int
    binding_set_revision: int
    canonical_state_hash: str
    root_refs: tuple[str, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported TaskScopeOpenReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.open_id, "open_id"),
            (self.task_scope_id, "task_scope_id"),
        ):
            _identifier(value, name)
        _digest(self.request_hash, "request_hash")
        _digest(self.canonical_state_hash, "canonical_state_hash")
        _positive_int(self.canonical_revision, "canonical_revision")
        _positive_int(self.binding_set_revision, "binding_set_revision")
        roots = tuple(_identifier(item, "root_ref", max_length=1024) for item in self.root_refs)
        if not roots or len(set(roots)) != len(roots):
            raise ValueError("root_refs must be non-empty and unique")
        object.__setattr__(self, "root_refs", roots)
        object.__setattr__(self, "receipt_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "open_id": self.open_id,
            "request_hash": self.request_hash,
            "task_scope_id": self.task_scope_id,
            "canonical_revision": self.canonical_revision,
            "binding_set_revision": self.binding_set_revision,
            "canonical_state_hash": self.canonical_state_hash,
            "root_refs": list(self.root_refs),
        }


__all__ = (
    "TaskScopeCandidate",
    "TaskScopeMutationKind",
    "TaskScopeMutationOperation",
    "TaskScopeMutationOutcome",
    "TaskScopeMutationPlan",
    "TaskScopeOpenReceipt",
    "TaskScopeOpenRequest",
    "TaskScopeProposal",
    "TaskScopeReasonCode",
    "TaskScopeRoute",
    "TaskScopeSearchReceipt",
    "TaskScopeSearchRequest",
)
