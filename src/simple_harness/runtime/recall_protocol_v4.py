# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict Human Memory recall authority and Context binding contracts.

These values are immutable wire records.  They deliberately do not expose a
ref-only read capability: content can enter Context only through a durable,
hash-bound recall result and a page or context-use receipt.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    fingerprint_json,
    freeze_json,
    thaw_json,
)

from .disclosure_protocol import (
    DisclosureContext,
    _digest,
    _domain_hash,
    _exact_keys,
    _identifier,
    _object,
    _objects,
    _optional_identifier,
    _positive_int,
    _strings,
)
from .evidence_protocol import EvidenceRef, _evidence_refs, _refs_from_json
from .information_classification_protocol import InformationAttribute, PrivacyClass
from .memory_protocol import (
    _NO_RECALL_REASONS,
    _RECALL_DEPENDENCY_REASONS,
    _REJECTED_REASONS,
    ContextAssemblyBudget,
    ContextAssemblyReasonCode,
    ContextFragmentType,
    LongTermMemoryType,
    RecallBudget,
    RecallCandidateCountStage,
    RecallContext,
    RecallDecisionOutcome,
    RecallPlan,
    RecallReasonCode,
    _bounded_enum_tuple,
    _bounded_identifiers,
    _optional_timestamp,
    _validate_recall_disclosure,
)

RECALL_DECISION_SCHEMA_VERSION_V4 = 4
RECALL_RESULT_SCHEMA_VERSION = 1
CONTEXT_FRAGMENT_SCHEMA_VERSION = 2
CONTEXT_ASSEMBLY_SCHEMA_VERSION = 2
RECALL_CONTEXT_USE_SCHEMA_VERSION = 1

RECALL_MAX_ITEMS = 32
RECALL_MAX_BYTES = 65_536
RECALL_MAX_TOKENS = 8_192
RECALL_MAX_DEADLINE_MS = 2_000
RECALL_MAX_RESULT_ITEMS = 128


def _schema(value: object, expected: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bounded_ordinal(value: object, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def _finite_score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


def _payload(value: object, name: str) -> FrozenJsonValue:
    try:
        return freeze_json(cast(JsonValue, value))
    except (TypeError, ValueError):
        try:
            return freeze_json(thaw_json(cast(FrozenJsonValue, value)))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{name} must be a JSON value") from exc


def _payload_json(value: FrozenJsonValue) -> JsonValue:
    return thaw_json(value)


def _payload_hash(value: FrozenJsonValue) -> str:
    return fingerprint_json(_payload_json(value))


class RecallSourceKind(StrEnum):
    COGNITIVE_MEMORY = "cognitive_memory"
    SHORT_HORIZON = "short_horizon"


class RecallItemKind(StrEnum):
    SELECTED = "selected"
    CONFIRMATION_MEMBER = "confirmation_member"


RecallBudgetV1 = RecallBudget


@dataclass(frozen=True, slots=True)
class RecallSelectedItemV4:
    item_id: str
    ordinal: int
    item_kind: RecallItemKind
    source_kind: RecallSourceKind
    source_ref: str
    source_content_hash: str
    public_payload_hash: str
    memory_type: LongTermMemoryType | None
    source_revision: int | None
    chunk_ref: str | None
    item_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _positive_int(self.ordinal, "ordinal")
        kind = RecallItemKind(self.item_kind)
        if kind is not RecallItemKind.SELECTED:
            raise ValueError("RecallSelectedItemV4 item_kind must be selected")
        source_kind = RecallSourceKind(self.source_kind)
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _digest(self.source_content_hash, "source_content_hash")
        _digest(self.public_payload_hash, "public_payload_hash")
        memory_type = None if self.memory_type is None else LongTermMemoryType(self.memory_type)
        chunk_ref = _optional_identifier(self.chunk_ref, "chunk_ref", max_length=1024)
        revision = self.source_revision
        if source_kind is RecallSourceKind.COGNITIVE_MEMORY:
            if memory_type is None or revision is None:
                raise ValueError("cognitive memory requires memory_type and exact revision")
            _positive_int(revision, "source_revision")
            if chunk_ref is not None:
                raise ValueError("cognitive memory cannot carry chunk_ref")
        else:
            if memory_type is not None or revision is not None:
                raise ValueError("short-horizon item cannot carry memory_type or revision")
            if chunk_ref is None or chunk_ref != self.source_ref:
                raise ValueError("short-horizon item requires chunk_ref equal to source_ref")
        object.__setattr__(self, "item_kind", kind)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "memory_type", memory_type)
        object.__setattr__(self, "chunk_ref", chunk_ref)
        object.__setattr__(
            self,
            "item_hash",
            _domain_hash("simple-harness/recall-selected-item/v4", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "item_id": self.item_id,
            "ordinal": self.ordinal,
            "item_kind": self.item_kind.value,
            "source_kind": self.source_kind.value,
            "source_ref": self.source_ref,
            "source_content_hash": self.source_content_hash,
            "public_payload_hash": self.public_payload_hash,
            "memory_type": None if self.memory_type is None else self.memory_type.value,
            "source_revision": self.source_revision,
            "chunk_ref": self.chunk_ref,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallSelectedItemV4:
        _exact_keys(
            value,
            {
                "item_id",
                "ordinal",
                "item_kind",
                "source_kind",
                "source_ref",
                "source_content_hash",
                "public_payload_hash",
                "memory_type",
                "source_revision",
                "chunk_ref",
            },
            "RecallSelectedItemV4",
        )
        memory_type = value["memory_type"]
        revision = value["source_revision"]
        if memory_type is not None and not isinstance(memory_type, str):
            raise TypeError("memory_type must be a string or null")
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise TypeError("source_revision must be an integer or null")
        return cls(
            item_id=_identifier(value["item_id"], "item_id"),
            ordinal=_positive_int(value["ordinal"], "ordinal"),
            item_kind=RecallItemKind(value["item_kind"]),  # type: ignore[arg-type]
            source_kind=RecallSourceKind(value["source_kind"]),  # type: ignore[arg-type]
            source_ref=_identifier(value["source_ref"], "source_ref", max_length=1024),
            source_content_hash=_digest(value["source_content_hash"], "source_content_hash"),
            public_payload_hash=_digest(value["public_payload_hash"], "public_payload_hash"),
            memory_type=None if memory_type is None else LongTermMemoryType(memory_type),
            source_revision=revision,
            chunk_ref=_optional_identifier(value["chunk_ref"], "chunk_ref", max_length=1024),
        )


@dataclass(frozen=True, slots=True)
class RecallConfirmationMemberV4:
    item_id: str
    ordinal: int
    item_kind: RecallItemKind
    source_ref: str
    source_revision: int
    memory_type: LongTermMemoryType
    source_content_hash: str
    public_payload_hash: str
    item_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _positive_int(self.ordinal, "ordinal")
        kind = RecallItemKind(self.item_kind)
        if kind is not RecallItemKind.CONFIRMATION_MEMBER:
            raise ValueError("confirmation member item_kind differs")
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _positive_int(self.source_revision, "source_revision")
        object.__setattr__(self, "memory_type", LongTermMemoryType(self.memory_type))
        _digest(self.source_content_hash, "source_content_hash")
        _digest(self.public_payload_hash, "public_payload_hash")
        object.__setattr__(self, "item_kind", kind)
        object.__setattr__(
            self,
            "item_hash",
            _domain_hash("simple-harness/recall-confirmation-member/v4", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "item_id": self.item_id,
            "ordinal": self.ordinal,
            "item_kind": self.item_kind.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "memory_type": self.memory_type.value,
            "source_content_hash": self.source_content_hash,
            "public_payload_hash": self.public_payload_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallConfirmationMemberV4:
        _exact_keys(
            value,
            {
                "item_id",
                "ordinal",
                "item_kind",
                "source_ref",
                "source_revision",
                "memory_type",
                "source_content_hash",
                "public_payload_hash",
            },
            "RecallConfirmationMemberV4",
        )
        return cls(
            _identifier(value["item_id"], "item_id"),
            _positive_int(value["ordinal"], "ordinal"),
            RecallItemKind(value["item_kind"]),  # type: ignore[arg-type]
            _identifier(value["source_ref"], "source_ref", max_length=1024),
            _positive_int(value["source_revision"], "source_revision"),
            LongTermMemoryType(value["memory_type"]),  # type: ignore[arg-type]
            _digest(value["source_content_hash"], "source_content_hash"),
            _digest(value["public_payload_hash"], "public_payload_hash"),
        )


@dataclass(frozen=True, slots=True)
class RecallConfirmationGroupV4:
    conflict_group_id: str
    conflict_group_hash: str
    ordinal: int
    members: tuple[RecallConfirmationMemberV4, ...]
    confirmation_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.conflict_group_id, "conflict_group_id")
        _digest(self.conflict_group_hash, "conflict_group_hash")
        _positive_int(self.ordinal, "ordinal")
        if not isinstance(self.members, (tuple, list)) or not all(
            isinstance(item, RecallConfirmationMemberV4) for item in self.members
        ):
            raise TypeError("members must contain RecallConfirmationMemberV4 values")
        members = tuple(self.members)
        if len(members) < 2 or len(members) > RECALL_MAX_RESULT_ITEMS:
            raise ValueError("confirmation group requires 2..128 members")
        if tuple(item.ordinal for item in members) != tuple(range(1, len(members) + 1)):
            raise ValueError("confirmation member ordinals must be contiguous from one")
        if len({item.item_id for item in members}) != len(members):
            raise ValueError("confirmation member item_id values must be unique")
        exact_sources = {(item.source_ref, item.source_revision) for item in members}
        if len(exact_sources) != len(members):
            raise ValueError("confirmation members must bind distinct exact revisions")
        if len({item.memory_type for item in members}) != 1:
            raise ValueError("confirmation members must share one memory type")
        object.__setattr__(self, "members", members)
        object.__setattr__(
            self,
            "confirmation_hash",
            _domain_hash("simple-harness/recall-confirmation-group/v4", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "conflict_group_id": self.conflict_group_id,
            "conflict_group_hash": self.conflict_group_hash,
            "ordinal": self.ordinal,
            "members": [item.to_json() for item in self.members],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallConfirmationGroupV4:
        _exact_keys(
            value,
            {"conflict_group_id", "conflict_group_hash", "ordinal", "members"},
            "RecallConfirmationGroupV4",
        )
        return cls(
            _identifier(value["conflict_group_id"], "conflict_group_id"),
            _digest(value["conflict_group_hash"], "conflict_group_hash"),
            _positive_int(value["ordinal"], "ordinal"),
            tuple(
                RecallConfirmationMemberV4.from_json(item)
                for item in _objects(value["members"], "members")
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallDecisionV4:
    decision_id: str
    run_id: str
    subject: str
    context_hash: str
    context_revision: int
    plan_id: str
    plan_hash: str
    outcome: RecallDecisionOutcome
    selected_items: tuple[RecallSelectedItemV4, ...]
    confirmation_groups: tuple[RecallConfirmationGroupV4, ...]
    filtered_candidate_count: int
    candidate_count_stage: RecallCandidateCountStage
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    reason_codes: tuple[RecallReasonCode, ...]
    decided_at: float
    schema_version: int = RECALL_DECISION_SCHEMA_VERSION_V4
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(self.schema_version, RECALL_DECISION_SCHEMA_VERSION_V4, "RecallDecisionV4")
        for identifier_value, name in (
            (self.decision_id, "decision_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.plan_id, "plan_id"),
        ):
            _identifier(identifier_value, name)
        _digest(self.context_hash, "context_hash")
        _positive_int(self.context_revision, "context_revision")
        _digest(self.plan_hash, "plan_hash")
        outcome = RecallDecisionOutcome(self.outcome)
        if not isinstance(self.selected_items, (tuple, list)) or not all(
            isinstance(item, RecallSelectedItemV4) for item in self.selected_items
        ):
            raise TypeError("selected_items must contain RecallSelectedItemV4 values")
        selected = tuple(self.selected_items)
        if not isinstance(self.confirmation_groups, (tuple, list)) or not all(
            isinstance(item, RecallConfirmationGroupV4) for item in self.confirmation_groups
        ):
            raise TypeError("confirmation_groups must contain RecallConfirmationGroupV4 values")
        groups = tuple(self.confirmation_groups)
        if tuple(item.ordinal for item in selected) != tuple(range(1, len(selected) + 1)):
            raise ValueError("selected item ordinals must be contiguous from one")
        if tuple(item.ordinal for item in groups) != tuple(range(1, len(groups) + 1)):
            raise ValueError("confirmation group ordinals must be contiguous from one")
        all_item_ids = [item.item_id for item in selected]
        all_item_ids.extend(member.item_id for group in groups for member in group.members)
        if len(set(all_item_ids)) != len(all_item_ids):
            raise ValueError("decision item_id values must be globally unique")
        selected_exact = {
            (item.source_ref, item.source_revision)
            for item in selected
            if item.source_kind is RecallSourceKind.COGNITIVE_MEMORY
        }
        confirmation_exact = {
            (member.source_ref, member.source_revision)
            for group in groups
            for member in group.members
        }
        if selected_exact & confirmation_exact:
            raise ValueError("confirmation members cannot enter ordinary selected items")
        count = _non_negative_int(self.filtered_candidate_count, "filtered_candidate_count")
        stage = RecallCandidateCountStage(self.candidate_count_stage)
        if stage is not RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES:
            raise ValueError("candidate count must follow all eligibility gates")
        disclosed_count = len(selected) + sum(len(group.members) for group in groups)
        if disclosed_count > count:
            raise ValueError("decision items exceed post-gate candidate count")
        if outcome is RecallDecisionOutcome.RECALL:
            if not selected or groups:
                raise ValueError("recall requires selected_items and forbids confirmation_groups")
        elif outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION:
            if selected or not groups:
                raise ValueError("confirmation outcome requires only complete groups")
        elif selected or groups:
            raise ValueError("non-content outcome cannot carry items or confirmation groups")
        if outcome in {RecallDecisionOutcome.NO_RECALL, RecallDecisionOutcome.REJECTED} and count:
            raise ValueError("no-recall/rejected cannot disclose candidate count")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        if outcome in {RecallDecisionOutcome.RECALL, RecallDecisionOutcome.NEEDS_USER_CONFIRMATION}:
            _validate_recall_disclosure(self.disclosure_context)
        evidence = _evidence_refs(self.evidence_refs)
        if not evidence:
            raise ValueError("RecallDecisionV4 requires evidence_refs")
        reasons = cast(
            tuple[RecallReasonCode, ...],
            _bounded_enum_tuple(self.reason_codes, "reason_codes", RecallReasonCode, required=True),
        )
        reason_set = set(reasons)
        if outcome is RecallDecisionOutcome.RECALL and not reason_set <= _RECALL_DEPENDENCY_REASONS:
            raise ValueError("recall outcome requires dependency reasons")
        if outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION:
            allowed = _RECALL_DEPENDENCY_REASONS | {RecallReasonCode.NEEDS_USER_CONFIRMATION}
            if (
                RecallReasonCode.NEEDS_USER_CONFIRMATION not in reason_set
                or not reason_set <= allowed
            ):
                raise ValueError("confirmation outcome requires compatible confirmation reason")
        if outcome is RecallDecisionOutcome.NO_RECALL and not reason_set <= _NO_RECALL_REASONS:
            raise ValueError("no-recall outcome has incompatible reason")
        if outcome is RecallDecisionOutcome.REJECTED and not reason_set <= _REJECTED_REASONS:
            raise ValueError("rejected outcome has incompatible reason")
        decided_at = _optional_timestamp(self.decided_at, "decided_at")
        if decided_at is None:
            raise ValueError("decided_at is required")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "selected_items", selected)
        object.__setattr__(self, "confirmation_groups", groups)
        object.__setattr__(self, "candidate_count_stage", stage)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "decided_at", decided_at)
        object.__setattr__(
            self,
            "decision_hash",
            _domain_hash("simple-harness/recall-decision/v4", self.to_json()),
        )

    def validate_bindings(
        self, context: RecallContext, plan: RecallPlan, *, current_time: float
    ) -> None:
        plan.validate_narrowing(context, current_time=current_time)
        if self.run_id != context.run_id or self.subject != context.subject:
            raise ValueError("RecallDecisionV4 run/subject differs")
        if (
            self.context_hash != context.context_hash
            or self.context_revision != context.context_revision
        ):
            raise ValueError("RecallDecisionV4 context binding differs")
        if self.plan_id != plan.plan_id or self.plan_hash != plan.plan_hash:
            raise ValueError("RecallDecisionV4 plan binding differs")
        for item in self.selected_items:
            if item.source_kind is RecallSourceKind.COGNITIVE_MEMORY:
                if item.memory_type not in plan.requested_memory_types:
                    raise ValueError("decision selects an unrequested memory type")
            elif not plan.include_short_horizon:
                raise ValueError("decision selects unrequested short-horizon content")
        if any(
            member.memory_type not in plan.requested_memory_types
            for group in self.confirmation_groups
            for member in group.members
        ):
            raise ValueError("decision confirms an unrequested memory type")
        if self.disclosure_context.to_json() != plan.disclosure_context.to_json():
            raise ValueError("RecallDecisionV4 disclosure differs from RecallPlan")
        plan_evidence = {fingerprint_json(item.to_json()) for item in plan.evidence_refs}
        context_evidence = {fingerprint_json(item.to_json()) for item in context.evidence_refs}
        decision_evidence = {fingerprint_json(item.to_json()) for item in self.evidence_refs}
        if decision_evidence != plan_evidence or not decision_evidence <= context_evidence:
            raise ValueError("RecallDecisionV4 evidence_refs do not bind Plan/Context")

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
            "selected_items": [item.to_json() for item in self.selected_items],
            "confirmation_groups": [item.to_json() for item in self.confirmation_groups],
            "filtered_candidate_count": self.filtered_candidate_count,
            "candidate_count_stage": self.candidate_count_stage.value,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "reason_codes": [item.value for item in self.reason_codes],
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallDecisionV4:
        expected = {
            "schema_version",
            "decision_id",
            "run_id",
            "subject",
            "context_hash",
            "context_revision",
            "plan_id",
            "plan_hash",
            "outcome",
            "selected_items",
            "confirmation_groups",
            "filtered_candidate_count",
            "candidate_count_stage",
            "disclosure_context",
            "evidence_refs",
            "reason_codes",
            "decided_at",
        }
        _exact_keys(value, expected, "RecallDecisionV4")
        _schema(value["schema_version"], RECALL_DECISION_SCHEMA_VERSION_V4, "RecallDecisionV4")
        count = _non_negative_int(value["filtered_candidate_count"], "filtered_candidate_count")
        return cls(
            _identifier(value["decision_id"], "decision_id"),
            _identifier(value["run_id"], "run_id"),
            _identifier(value["subject"], "subject"),
            _digest(value["context_hash"], "context_hash"),
            _positive_int(value["context_revision"], "context_revision"),
            _identifier(value["plan_id"], "plan_id"),
            _digest(value["plan_hash"], "plan_hash"),
            RecallDecisionOutcome(value["outcome"]),  # type: ignore[arg-type]
            tuple(
                RecallSelectedItemV4.from_json(item)
                for item in _objects(value["selected_items"], "selected_items")
            ),
            tuple(
                RecallConfirmationGroupV4.from_json(item)
                for item in _objects(value["confirmation_groups"], "confirmation_groups")
            ),
            count,
            RecallCandidateCountStage(value["candidate_count_stage"]),  # type: ignore[arg-type]
            DisclosureContext.from_json(_object(value["disclosure_context"], "disclosure_context")),
            _refs_from_json(value["evidence_refs"]),
            tuple(
                RecallReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            cast(float, _optional_timestamp(value["decided_at"], "decided_at")),
            _schema(value["schema_version"], RECALL_DECISION_SCHEMA_VERSION_V4, "RecallDecisionV4"),
        )


@dataclass(frozen=True, slots=True)
class RecallItemBindingV1:
    item_id: str
    item_hash: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _digest(self.item_hash, "item_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {"item_id": self.item_id, "item_hash": self.item_hash}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallItemBindingV1:
        _exact_keys(value, {"item_id", "item_hash"}, "RecallItemBindingV1")
        return cls(
            _identifier(value["item_id"], "item_id"), _digest(value["item_hash"], "item_hash")
        )


@dataclass(frozen=True, slots=True)
class TypedRecallResultItemV1:
    selected_item: RecallSelectedItemV4
    public_payload: FrozenJsonValue
    effective_privacy_class: PrivacyClass
    information_attributes: tuple[InformationAttribute, ...]
    score: float
    evidence_manifest_hash: str
    source_task_scope_ids: tuple[str, ...]
    active_task_scope_id: str | None
    cross_scope: bool
    result_item_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selected_item, RecallSelectedItemV4):
            raise TypeError("selected_item must use RecallSelectedItemV4")
        payload = _payload(self.public_payload, "public_payload")
        if _payload_hash(payload) != self.selected_item.public_payload_hash:
            raise ValueError("public_payload differs from selected item hash")
        privacy = PrivacyClass(self.effective_privacy_class)
        attributes = tuple(
            sorted(
                {InformationAttribute(item) for item in self.information_attributes},
                key=lambda item: item.value,
            )
        )
        score = _finite_score(self.score, "score")
        _digest(self.evidence_manifest_hash, "evidence_manifest_hash")
        scopes = _bounded_identifiers(self.source_task_scope_ids, "source_task_scope_ids")
        active_scope = _optional_identifier(self.active_task_scope_id, "active_task_scope_id")
        if not isinstance(self.cross_scope, bool):
            raise TypeError("cross_scope must be a boolean")
        expected_cross_scope = bool(scopes) and (
            active_scope is None or any(scope != active_scope for scope in scopes)
        )
        if self.cross_scope != expected_cross_scope:
            raise ValueError("cross_scope differs from exact TaskScope provenance")
        object.__setattr__(self, "public_payload", payload)
        object.__setattr__(self, "effective_privacy_class", privacy)
        object.__setattr__(self, "information_attributes", attributes)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "source_task_scope_ids", scopes)
        object.__setattr__(self, "active_task_scope_id", active_scope)
        object.__setattr__(
            self,
            "result_item_hash",
            _domain_hash("simple-harness/typed-recall-result-item/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "selected_item": self.selected_item.to_json(),
            "public_payload": _payload_json(self.public_payload),
            "effective_privacy_class": self.effective_privacy_class.value,
            "information_attributes": [item.value for item in self.information_attributes],
            "score": self.score,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "source_task_scope_ids": list(self.source_task_scope_ids),
            "active_task_scope_id": self.active_task_scope_id,
            "cross_scope": self.cross_scope,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TypedRecallResultItemV1:
        _exact_keys(
            value,
            {
                "selected_item",
                "public_payload",
                "effective_privacy_class",
                "information_attributes",
                "score",
                "evidence_manifest_hash",
                "source_task_scope_ids",
                "active_task_scope_id",
                "cross_scope",
            },
            "TypedRecallResultItemV1",
        )
        cross_scope = value["cross_scope"]
        if not isinstance(cross_scope, bool):
            raise TypeError("cross_scope must be a boolean")
        return cls(
            RecallSelectedItemV4.from_json(_object(value["selected_item"], "selected_item")),
            _payload(value["public_payload"], "public_payload"),
            PrivacyClass(value["effective_privacy_class"]),  # type: ignore[arg-type]
            tuple(
                InformationAttribute(item)
                for item in _strings(value["information_attributes"], "information_attributes")
            ),
            _finite_score(value["score"], "score"),
            _digest(value["evidence_manifest_hash"], "evidence_manifest_hash"),
            _strings(value["source_task_scope_ids"], "source_task_scope_ids"),
            _optional_identifier(value["active_task_scope_id"], "active_task_scope_id"),
            cross_scope,
        )


@dataclass(frozen=True, slots=True)
class TypedRecallConfirmationMemberV1:
    member: RecallConfirmationMemberV4
    public_payload: FrozenJsonValue
    effective_privacy_class: PrivacyClass
    information_attributes: tuple[InformationAttribute, ...]
    evidence_manifest_hash: str
    source_task_scope_ids: tuple[str, ...]
    active_task_scope_id: str | None
    cross_scope: bool
    result_member_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.member, RecallConfirmationMemberV4):
            raise TypeError("member must use RecallConfirmationMemberV4")
        payload = _payload(self.public_payload, "public_payload")
        if _payload_hash(payload) != self.member.public_payload_hash:
            raise ValueError("confirmation public_payload hash differs")
        privacy = PrivacyClass(self.effective_privacy_class)
        attrs = tuple(
            sorted(
                {InformationAttribute(item) for item in self.information_attributes},
                key=lambda item: item.value,
            )
        )
        _digest(self.evidence_manifest_hash, "evidence_manifest_hash")
        scopes = _bounded_identifiers(self.source_task_scope_ids, "source_task_scope_ids")
        active = _optional_identifier(self.active_task_scope_id, "active_task_scope_id")
        if not isinstance(self.cross_scope, bool):
            raise TypeError("cross_scope must be a boolean")
        expected = bool(scopes) and (active is None or any(scope != active for scope in scopes))
        if self.cross_scope != expected:
            raise ValueError("cross_scope differs from exact TaskScope provenance")
        object.__setattr__(self, "public_payload", payload)
        object.__setattr__(self, "effective_privacy_class", privacy)
        object.__setattr__(self, "information_attributes", attrs)
        object.__setattr__(self, "source_task_scope_ids", scopes)
        object.__setattr__(self, "active_task_scope_id", active)
        object.__setattr__(
            self,
            "result_member_hash",
            _domain_hash("simple-harness/typed-recall-confirmation-member/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "member": self.member.to_json(),
            "public_payload": _payload_json(self.public_payload),
            "effective_privacy_class": self.effective_privacy_class.value,
            "information_attributes": [item.value for item in self.information_attributes],
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "source_task_scope_ids": list(self.source_task_scope_ids),
            "active_task_scope_id": self.active_task_scope_id,
            "cross_scope": self.cross_scope,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TypedRecallConfirmationMemberV1:
        _exact_keys(
            value,
            {
                "member",
                "public_payload",
                "effective_privacy_class",
                "information_attributes",
                "evidence_manifest_hash",
                "source_task_scope_ids",
                "active_task_scope_id",
                "cross_scope",
            },
            "TypedRecallConfirmationMemberV1",
        )
        cross_scope = value["cross_scope"]
        if not isinstance(cross_scope, bool):
            raise TypeError("cross_scope must be a boolean")
        return cls(
            RecallConfirmationMemberV4.from_json(_object(value["member"], "member")),
            _payload(value["public_payload"], "public_payload"),
            PrivacyClass(value["effective_privacy_class"]),  # type: ignore[arg-type]
            tuple(
                InformationAttribute(item)
                for item in _strings(value["information_attributes"], "information_attributes")
            ),
            _digest(value["evidence_manifest_hash"], "evidence_manifest_hash"),
            _strings(value["source_task_scope_ids"], "source_task_scope_ids"),
            _optional_identifier(value["active_task_scope_id"], "active_task_scope_id"),
            cross_scope,
        )


@dataclass(frozen=True, slots=True)
class TypedRecallConfirmationGroupV1:
    group: RecallConfirmationGroupV4
    members: tuple[TypedRecallConfirmationMemberV1, ...]
    result_group_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.group, RecallConfirmationGroupV4):
            raise TypeError("group must use RecallConfirmationGroupV4")
        if not isinstance(self.members, (tuple, list)) or not all(
            isinstance(item, TypedRecallConfirmationMemberV1) for item in self.members
        ):
            raise TypeError("members must contain typed confirmation members")
        members = tuple(self.members)
        if tuple(item.member.item_hash for item in members) != tuple(
            item.item_hash for item in self.group.members
        ):
            raise ValueError("typed confirmation group is not complete or ordered")
        object.__setattr__(self, "members", members)
        object.__setattr__(
            self,
            "result_group_hash",
            _domain_hash("simple-harness/typed-recall-confirmation-group/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {"group": self.group.to_json(), "members": [item.to_json() for item in self.members]}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TypedRecallConfirmationGroupV1:
        _exact_keys(value, {"group", "members"}, "TypedRecallConfirmationGroupV1")
        return cls(
            RecallConfirmationGroupV4.from_json(_object(value["group"], "group")),
            tuple(
                TypedRecallConfirmationMemberV1.from_json(item)
                for item in _objects(value["members"], "members")
            ),
        )


@dataclass(frozen=True, slots=True)
class TypedRecallResultV1:
    result_id: str
    decision_id: str
    decision_hash: str
    authority_epoch: int
    policy_hash: str
    evaluated_at: float
    authority_expires_at: float
    items: tuple[TypedRecallResultItemV1, ...]
    confirmation_groups: tuple[TypedRecallConfirmationGroupV1, ...]
    truncated: bool
    reason_codes: tuple[RecallReasonCode, ...]
    schema_version: int = RECALL_RESULT_SCHEMA_VERSION
    result_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(self.schema_version, RECALL_RESULT_SCHEMA_VERSION, "TypedRecallResultV1")
        _identifier(self.result_id, "result_id")
        _identifier(self.decision_id, "decision_id")
        _digest(self.decision_hash, "decision_hash")
        _positive_int(self.authority_epoch, "authority_epoch")
        _digest(self.policy_hash, "policy_hash")
        evaluated = _optional_timestamp(self.evaluated_at, "evaluated_at")
        expires = _optional_timestamp(self.authority_expires_at, "authority_expires_at")
        if evaluated is None or expires is None or expires <= evaluated:
            raise ValueError("result authority requires a future expiry")
        if not isinstance(self.items, (tuple, list)) or not all(
            isinstance(item, TypedRecallResultItemV1) for item in self.items
        ):
            raise TypeError("items must contain TypedRecallResultItemV1 values")
        items = tuple(self.items)
        if not isinstance(self.confirmation_groups, (tuple, list)) or not all(
            isinstance(item, TypedRecallConfirmationGroupV1) for item in self.confirmation_groups
        ):
            raise TypeError("confirmation_groups must contain typed groups")
        groups = tuple(self.confirmation_groups)
        if len(items) + sum(len(group.members) for group in groups) > RECALL_MAX_RESULT_ITEMS:
            raise ValueError("typed recall result exceeds item limit")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be a boolean")
        reasons = cast(
            tuple[RecallReasonCode, ...],
            _bounded_enum_tuple(self.reason_codes, "reason_codes", RecallReasonCode, required=True),
        )
        object.__setattr__(self, "evaluated_at", evaluated)
        object.__setattr__(self, "authority_expires_at", expires)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "confirmation_groups", groups)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self,
            "result_hash",
            _domain_hash("simple-harness/typed-recall-result/v1", self.to_json()),
        )

    def validate_decision(self, decision: RecallDecisionV4) -> None:
        if self.decision_id != decision.decision_id or self.decision_hash != decision.decision_hash:
            raise ValueError("result decision binding differs")
        if tuple(item.selected_item.item_hash for item in self.items) != tuple(
            item.item_hash for item in decision.selected_items
        ):
            raise ValueError("result selected items are incomplete or reordered")
        if tuple(group.group.confirmation_hash for group in self.confirmation_groups) != tuple(
            group.confirmation_hash for group in decision.confirmation_groups
        ):
            raise ValueError("result confirmation groups are incomplete or reordered")
        if decision.outcome is RecallDecisionOutcome.RECALL and (
            not self.items or self.confirmation_groups
        ):
            raise ValueError("recall result shape differs from recall decision")
        if decision.outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION and (
            self.items or not self.confirmation_groups
        ):
            raise ValueError("confirmation result shape differs from decision")
        if decision.outcome in {
            RecallDecisionOutcome.NO_RECALL,
            RecallDecisionOutcome.REJECTED,
        } and (self.items or self.confirmation_groups):
            raise ValueError("non-content decision cannot produce result content")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "decision_id": self.decision_id,
            "decision_hash": self.decision_hash,
            "authority_epoch": self.authority_epoch,
            "policy_hash": self.policy_hash,
            "evaluated_at": self.evaluated_at,
            "authority_expires_at": self.authority_expires_at,
            "items": [item.to_json() for item in self.items],
            "confirmation_groups": [item.to_json() for item in self.confirmation_groups],
            "truncated": self.truncated,
            "reason_codes": [item.value for item in self.reason_codes],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TypedRecallResultV1:
        _exact_keys(
            value,
            {
                "schema_version",
                "result_id",
                "decision_id",
                "decision_hash",
                "authority_epoch",
                "policy_hash",
                "evaluated_at",
                "authority_expires_at",
                "items",
                "confirmation_groups",
                "truncated",
                "reason_codes",
            },
            "TypedRecallResultV1",
        )
        truncated = value["truncated"]
        if not isinstance(truncated, bool):
            raise TypeError("truncated must be a boolean")
        return cls(
            _identifier(value["result_id"], "result_id"),
            _identifier(value["decision_id"], "decision_id"),
            _digest(value["decision_hash"], "decision_hash"),
            _positive_int(value["authority_epoch"], "authority_epoch"),
            _digest(value["policy_hash"], "policy_hash"),
            cast(float, _optional_timestamp(value["evaluated_at"], "evaluated_at")),
            cast(float, _optional_timestamp(value["authority_expires_at"], "authority_expires_at")),
            tuple(
                TypedRecallResultItemV1.from_json(item)
                for item in _objects(value["items"], "items")
            ),
            tuple(
                TypedRecallConfirmationGroupV1.from_json(item)
                for item in _objects(value["confirmation_groups"], "confirmation_groups")
            ),
            truncated,
            tuple(
                RecallReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            _schema(value["schema_version"], RECALL_RESULT_SCHEMA_VERSION, "TypedRecallResultV1"),
        )


@dataclass(frozen=True, slots=True)
class RecallResultPageRequestV1:
    result_id: str
    result_hash: str
    page_ordinal: int
    item_offset: int
    max_items: int
    max_bytes: int
    requested_at: float

    def __post_init__(self) -> None:
        _identifier(self.result_id, "result_id")
        _digest(self.result_hash, "result_hash")
        _positive_int(self.page_ordinal, "page_ordinal")
        _non_negative_int(self.item_offset, "item_offset")
        if _positive_int(self.max_items, "max_items") > RECALL_MAX_ITEMS:
            raise ValueError("max_items exceeds page maximum")
        if _positive_int(self.max_bytes, "max_bytes") > RECALL_MAX_BYTES:
            raise ValueError("max_bytes exceeds page maximum")
        if _optional_timestamp(self.requested_at, "requested_at") is None:
            raise ValueError("requested_at is required")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "result_id": self.result_id,
            "result_hash": self.result_hash,
            "page_ordinal": self.page_ordinal,
            "item_offset": self.item_offset,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallResultPageRequestV1:
        _exact_keys(
            value,
            {
                "result_id",
                "result_hash",
                "page_ordinal",
                "item_offset",
                "max_items",
                "max_bytes",
                "requested_at",
            },
            "RecallResultPageRequestV1",
        )
        return cls(
            _identifier(value["result_id"], "result_id"),
            _digest(value["result_hash"], "result_hash"),
            _positive_int(value["page_ordinal"], "page_ordinal"),
            _non_negative_int(value["item_offset"], "item_offset"),
            _positive_int(value["max_items"], "max_items"),
            _positive_int(value["max_bytes"], "max_bytes"),
            cast(float, _optional_timestamp(value["requested_at"], "requested_at")),
        )


class RecallPageBindingKind(StrEnum):
    SELECTED_ITEM = "selected_item"
    CONFIRMATION_GROUP = "confirmation_group"


@dataclass(frozen=True, slots=True)
class RecallPageSelectedItemBindingV1:
    binding_kind: RecallPageBindingKind
    ordinal: int
    item_id: str
    item_hash: str

    def __post_init__(self) -> None:
        kind = RecallPageBindingKind(self.binding_kind)
        if kind is not RecallPageBindingKind.SELECTED_ITEM:
            raise ValueError("selected page binding discriminator differs")
        _positive_int(self.ordinal, "ordinal")
        _identifier(self.item_id, "item_id")
        _digest(self.item_hash, "item_hash")
        object.__setattr__(self, "binding_kind", kind)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "binding_kind": self.binding_kind.value,
            "ordinal": self.ordinal,
            "item_id": self.item_id,
            "item_hash": self.item_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallPageSelectedItemBindingV1:
        _exact_keys(
            value,
            {"binding_kind", "ordinal", "item_id", "item_hash"},
            "RecallPageSelectedItemBindingV1",
        )
        return cls(
            RecallPageBindingKind(value["binding_kind"]),  # type: ignore[arg-type]
            _positive_int(value["ordinal"], "ordinal"),
            _identifier(value["item_id"], "item_id"),
            _digest(value["item_hash"], "item_hash"),
        )


@dataclass(frozen=True, slots=True)
class RecallPageConfirmationMemberBindingV1:
    ordinal: int
    item_id: str
    item_hash: str

    def __post_init__(self) -> None:
        _positive_int(self.ordinal, "ordinal")
        _identifier(self.item_id, "item_id")
        _digest(self.item_hash, "item_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {"ordinal": self.ordinal, "item_id": self.item_id, "item_hash": self.item_hash}

    @classmethod
    def from_json(
        cls, value: Mapping[str, object]
    ) -> RecallPageConfirmationMemberBindingV1:
        _exact_keys(
            value,
            {"ordinal", "item_id", "item_hash"},
            "RecallPageConfirmationMemberBindingV1",
        )
        return cls(
            _positive_int(value["ordinal"], "ordinal"),
            _identifier(value["item_id"], "item_id"),
            _digest(value["item_hash"], "item_hash"),
        )


@dataclass(frozen=True, slots=True)
class RecallPageConfirmationGroupBindingV1:
    binding_kind: RecallPageBindingKind
    group: RecallConfirmationGroupV4
    result_group_hash: str
    member_bindings: tuple[RecallPageConfirmationMemberBindingV1, ...]

    def __post_init__(self) -> None:
        kind = RecallPageBindingKind(self.binding_kind)
        if kind is not RecallPageBindingKind.CONFIRMATION_GROUP:
            raise ValueError("confirmation page binding discriminator differs")
        if not isinstance(self.group, RecallConfirmationGroupV4):
            raise TypeError("group must use RecallConfirmationGroupV4")
        _digest(self.result_group_hash, "result_group_hash")
        if not isinstance(self.member_bindings, (tuple, list)) or not all(
            isinstance(item, RecallPageConfirmationMemberBindingV1)
            for item in self.member_bindings
        ):
            raise TypeError("member_bindings must contain typed confirmation members")
        members = tuple(self.member_bindings)
        if tuple((item.ordinal, item.item_id) for item in members) != tuple(
            (item.ordinal, item.item_id) for item in self.group.members
        ):
            raise ValueError("confirmation page group must be complete and ordered")
        object.__setattr__(self, "binding_kind", kind)
        object.__setattr__(self, "member_bindings", members)

    @property
    def ordinal(self) -> int:
        return self.group.ordinal

    @property
    def conflict_group_id(self) -> str:
        return self.group.conflict_group_id

    @property
    def confirmation_hash(self) -> str:
        return self.group.confirmation_hash

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "binding_kind": self.binding_kind.value,
            "group": self.group.to_json(),
            "result_group_hash": self.result_group_hash,
            "member_bindings": [item.to_json() for item in self.member_bindings],
        }

    @classmethod
    def from_json(
        cls, value: Mapping[str, object]
    ) -> RecallPageConfirmationGroupBindingV1:
        _exact_keys(
            value,
            {
                "binding_kind",
                "group",
                "result_group_hash",
                "member_bindings",
            },
            "RecallPageConfirmationGroupBindingV1",
        )
        return cls(
            RecallPageBindingKind(value["binding_kind"]),  # type: ignore[arg-type]
            RecallConfirmationGroupV4.from_json(_object(value["group"], "group")),
            _digest(value["result_group_hash"], "result_group_hash"),
            tuple(
                RecallPageConfirmationMemberBindingV1.from_json(item)
                for item in _objects(value["member_bindings"], "member_bindings")
            ),
        )


RecallResultPageBindingV1 = (
    RecallPageSelectedItemBindingV1 | RecallPageConfirmationGroupBindingV1
)


def _page_binding_from_json(value: Mapping[str, object]) -> RecallResultPageBindingV1:
    binding_kind = value.get("binding_kind")
    if binding_kind == RecallPageBindingKind.SELECTED_ITEM.value:
        return RecallPageSelectedItemBindingV1.from_json(value)
    if binding_kind == RecallPageBindingKind.CONFIRMATION_GROUP.value:
        return RecallPageConfirmationGroupBindingV1.from_json(value)
    raise ValueError("unsupported recall page binding_kind")


@dataclass(frozen=True, slots=True)
class RecallResultPageV1:
    page_id: str
    result_id: str
    result_hash: str
    page_ordinal: int
    item_offset: int
    bindings: tuple[RecallResultPageBindingV1, ...]
    byte_count: int
    complete: bool
    page_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.page_id, "page_id")
        _identifier(self.result_id, "result_id")
        _digest(self.result_hash, "result_hash")
        _positive_int(self.page_ordinal, "page_ordinal")
        _non_negative_int(self.item_offset, "item_offset")
        if not isinstance(self.bindings, (tuple, list)) or not all(
            isinstance(
                item,
                (RecallPageSelectedItemBindingV1, RecallPageConfirmationGroupBindingV1),
            )
            for item in self.bindings
        ):
            raise TypeError("bindings must contain typed recall page bindings")
        bindings = tuple(self.bindings)
        if not bindings or len(bindings) > RECALL_MAX_ITEMS:
            raise ValueError("page bindings must be non-empty and bounded")
        expected_ordinals = tuple(range(self.item_offset + 1, self.item_offset + len(bindings) + 1))
        if tuple(item.ordinal for item in bindings) != expected_ordinals:
            raise ValueError("page binding ordinals must be contiguous from item_offset")
        carrier_ids = tuple(
            item.item_id
            if isinstance(item, RecallPageSelectedItemBindingV1)
            else item.conflict_group_id
            for item in bindings
        )
        if len(set(carrier_ids)) != len(carrier_ids):
            raise ValueError("page binding carrier IDs must be unique")
        _non_negative_int(self.byte_count, "byte_count")
        if self.byte_count > RECALL_MAX_BYTES:
            raise ValueError("page byte_count exceeds maximum")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(
            self, "page_hash", _domain_hash("simple-harness/recall-result-page/v1", self.to_json())
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "page_id": self.page_id,
            "result_id": self.result_id,
            "result_hash": self.result_hash,
            "page_ordinal": self.page_ordinal,
            "item_offset": self.item_offset,
            "bindings": [item.to_json() for item in self.bindings],
            "byte_count": self.byte_count,
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallResultPageV1:
        _exact_keys(
            value,
            {
                "page_id",
                "result_id",
                "result_hash",
                "page_ordinal",
                "item_offset",
                "bindings",
                "byte_count",
                "complete",
            },
            "RecallResultPageV1",
        )
        complete = value["complete"]
        if not isinstance(complete, bool):
            raise TypeError("complete must be a boolean")
        return cls(
            _identifier(value["page_id"], "page_id"),
            _identifier(value["result_id"], "result_id"),
            _digest(value["result_hash"], "result_hash"),
            _positive_int(value["page_ordinal"], "page_ordinal"),
            _non_negative_int(value["item_offset"], "item_offset"),
            tuple(
                _page_binding_from_json(item)
                for item in _objects(value["bindings"], "bindings")
            ),
            _non_negative_int(value["byte_count"], "byte_count"),
            complete,
        )


@dataclass(frozen=True, slots=True)
class ContextFragmentBindingV2:
    fragment_id: str
    fragment_hash: str

    def __post_init__(self) -> None:
        _identifier(self.fragment_id, "fragment_id")
        _digest(self.fragment_hash, "fragment_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {"fragment_id": self.fragment_id, "fragment_hash": self.fragment_hash}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextFragmentBindingV2:
        _exact_keys(value, {"fragment_id", "fragment_hash"}, "ContextFragmentBindingV2")
        return cls(
            _identifier(value["fragment_id"], "fragment_id"),
            _digest(value["fragment_hash"], "fragment_hash"),
        )


@dataclass(frozen=True, slots=True)
class RecallContextUseAuthorizationRequestV1:
    subject: str
    run_id: str
    turn_id: str
    provider_attempt_id: str
    decision_id: str
    decision_hash: str
    result_id: str
    result_hash: str
    item_bindings: tuple[RecallItemBindingV1, ...]
    snapshot_fragment_bindings: tuple[ContextFragmentBindingV2, ...]
    snapshot_manifest_hash: str
    requested_at: float
    schema_version: int = RECALL_CONTEXT_USE_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(
            self.schema_version,
            RECALL_CONTEXT_USE_SCHEMA_VERSION,
            "RecallContextUseAuthorizationRequestV1",
        )
        for identifier_value, name in (
            (self.subject, "subject"),
            (self.run_id, "run_id"),
            (self.turn_id, "turn_id"),
            (self.provider_attempt_id, "provider_attempt_id"),
            (self.decision_id, "decision_id"),
            (self.result_id, "result_id"),
        ):
            _identifier(identifier_value, name)
        _digest(self.decision_hash, "decision_hash")
        _digest(self.result_hash, "result_hash")
        if not isinstance(self.item_bindings, (tuple, list)) or not all(
            isinstance(item, RecallItemBindingV1) for item in self.item_bindings
        ):
            raise TypeError("item_bindings must contain RecallItemBindingV1 values")
        items = tuple(self.item_bindings)
        if not items or len({item.item_id for item in items}) != len(items):
            raise ValueError("item_bindings must be non-empty and unique")
        if not isinstance(self.snapshot_fragment_bindings, (tuple, list)) or not all(
            isinstance(item, ContextFragmentBindingV2) for item in self.snapshot_fragment_bindings
        ):
            raise TypeError("snapshot_fragment_bindings must contain ContextFragmentBindingV2")
        fragments = tuple(self.snapshot_fragment_bindings)
        if not fragments or len({item.fragment_id for item in fragments}) != len(fragments):
            raise ValueError("snapshot_fragment_bindings must be non-empty and unique")
        _digest(self.snapshot_manifest_hash, "snapshot_manifest_hash")
        expected_manifest = fingerprint_json([item.to_json() for item in fragments])
        if expected_manifest != self.snapshot_manifest_hash:
            raise ValueError("snapshot_manifest_hash differs from fragment bindings")
        if _optional_timestamp(self.requested_at, "requested_at") is None:
            raise ValueError("requested_at is required")
        object.__setattr__(self, "item_bindings", items)
        object.__setattr__(self, "snapshot_fragment_bindings", fragments)
        object.__setattr__(
            self,
            "request_hash",
            _domain_hash("simple-harness/recall-context-use-request/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "provider_attempt_id": self.provider_attempt_id,
            "decision_id": self.decision_id,
            "decision_hash": self.decision_hash,
            "result_id": self.result_id,
            "result_hash": self.result_hash,
            "item_bindings": [item.to_json() for item in self.item_bindings],
            "snapshot_fragment_bindings": [
                item.to_json() for item in self.snapshot_fragment_bindings
            ],
            "snapshot_manifest_hash": self.snapshot_manifest_hash,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallContextUseAuthorizationRequestV1:
        _exact_keys(
            value,
            {
                "schema_version",
                "subject",
                "run_id",
                "turn_id",
                "provider_attempt_id",
                "decision_id",
                "decision_hash",
                "result_id",
                "result_hash",
                "item_bindings",
                "snapshot_fragment_bindings",
                "snapshot_manifest_hash",
                "requested_at",
            },
            "RecallContextUseAuthorizationRequestV1",
        )
        return cls(
            _identifier(value["subject"], "subject"),
            _identifier(value["run_id"], "run_id"),
            _identifier(value["turn_id"], "turn_id"),
            _identifier(value["provider_attempt_id"], "provider_attempt_id"),
            _identifier(value["decision_id"], "decision_id"),
            _digest(value["decision_hash"], "decision_hash"),
            _identifier(value["result_id"], "result_id"),
            _digest(value["result_hash"], "result_hash"),
            tuple(
                RecallItemBindingV1.from_json(item)
                for item in _objects(value["item_bindings"], "item_bindings")
            ),
            tuple(
                ContextFragmentBindingV2.from_json(item)
                for item in _objects(
                    value["snapshot_fragment_bindings"], "snapshot_fragment_bindings"
                )
            ),
            _digest(value["snapshot_manifest_hash"], "snapshot_manifest_hash"),
            cast(float, _optional_timestamp(value["requested_at"], "requested_at")),
            _schema(
                value["schema_version"],
                RECALL_CONTEXT_USE_SCHEMA_VERSION,
                "RecallContextUseAuthorizationRequestV1",
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallContextUseReceiptV1:
    receipt_id: str
    request_hash: str
    subject: str
    run_id: str
    turn_id: str
    provider_attempt_id: str
    decision_id: str
    decision_hash: str
    result_id: str
    result_hash: str
    item_bindings: tuple[RecallItemBindingV1, ...]
    snapshot_manifest_hash: str
    authority_epoch: int
    policy_hash: str
    authorized_at: float
    expires_at: float
    schema_version: int = RECALL_CONTEXT_USE_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(self.schema_version, RECALL_CONTEXT_USE_SCHEMA_VERSION, "RecallContextUseReceiptV1")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.subject, "subject"),
            (self.run_id, "run_id"),
            (self.turn_id, "turn_id"),
            (self.provider_attempt_id, "provider_attempt_id"),
            (self.decision_id, "decision_id"),
            (self.result_id, "result_id"),
        ):
            _identifier(value, name)
        for value, name in (
            (self.request_hash, "request_hash"),
            (self.decision_hash, "decision_hash"),
            (self.result_hash, "result_hash"),
            (self.snapshot_manifest_hash, "snapshot_manifest_hash"),
            (self.policy_hash, "policy_hash"),
        ):
            _digest(value, name)
        if not isinstance(self.item_bindings, (tuple, list)) or not all(
            isinstance(item, RecallItemBindingV1) for item in self.item_bindings
        ):
            raise TypeError("item_bindings must contain RecallItemBindingV1 values")
        items = tuple(self.item_bindings)
        if not items or len({item.item_id for item in items}) != len(items):
            raise ValueError("receipt item_bindings must be non-empty and unique")
        _positive_int(self.authority_epoch, "authority_epoch")
        authorized = _optional_timestamp(self.authorized_at, "authorized_at")
        expires = _optional_timestamp(self.expires_at, "expires_at")
        if authorized is None or expires is None or expires <= authorized:
            raise ValueError("receipt requires a future expiry")
        object.__setattr__(self, "item_bindings", items)
        object.__setattr__(self, "authorized_at", authorized)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash("simple-harness/recall-context-use-receipt/v1", self.to_json()),
        )

    def validate_request(self, request: RecallContextUseAuthorizationRequestV1) -> None:
        if self.request_hash != request.request_hash:
            raise ValueError("receipt request_hash differs")
        exact = (
            self.subject,
            self.run_id,
            self.turn_id,
            self.provider_attempt_id,
            self.decision_id,
            self.decision_hash,
            self.result_id,
            self.result_hash,
            self.item_bindings,
            self.snapshot_manifest_hash,
        )
        expected = (
            request.subject,
            request.run_id,
            request.turn_id,
            request.provider_attempt_id,
            request.decision_id,
            request.decision_hash,
            request.result_id,
            request.result_hash,
            request.item_bindings,
            request.snapshot_manifest_hash,
        )
        if exact != expected:
            raise ValueError("receipt request bindings differ")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "request_hash": self.request_hash,
            "subject": self.subject,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "provider_attempt_id": self.provider_attempt_id,
            "decision_id": self.decision_id,
            "decision_hash": self.decision_hash,
            "result_id": self.result_id,
            "result_hash": self.result_hash,
            "item_bindings": [item.to_json() for item in self.item_bindings],
            "snapshot_manifest_hash": self.snapshot_manifest_hash,
            "authority_epoch": self.authority_epoch,
            "policy_hash": self.policy_hash,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallContextUseReceiptV1:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "request_hash",
                "subject",
                "run_id",
                "turn_id",
                "provider_attempt_id",
                "decision_id",
                "decision_hash",
                "result_id",
                "result_hash",
                "item_bindings",
                "snapshot_manifest_hash",
                "authority_epoch",
                "policy_hash",
                "authorized_at",
                "expires_at",
            },
            "RecallContextUseReceiptV1",
        )
        return cls(
            _identifier(value["receipt_id"], "receipt_id"),
            _digest(value["request_hash"], "request_hash"),
            _identifier(value["subject"], "subject"),
            _identifier(value["run_id"], "run_id"),
            _identifier(value["turn_id"], "turn_id"),
            _identifier(value["provider_attempt_id"], "provider_attempt_id"),
            _identifier(value["decision_id"], "decision_id"),
            _digest(value["decision_hash"], "decision_hash"),
            _identifier(value["result_id"], "result_id"),
            _digest(value["result_hash"], "result_hash"),
            tuple(
                RecallItemBindingV1.from_json(item)
                for item in _objects(value["item_bindings"], "item_bindings")
            ),
            _digest(value["snapshot_manifest_hash"], "snapshot_manifest_hash"),
            _positive_int(value["authority_epoch"], "authority_epoch"),
            _digest(value["policy_hash"], "policy_hash"),
            cast(float, _optional_timestamp(value["authorized_at"], "authorized_at")),
            cast(float, _optional_timestamp(value["expires_at"], "expires_at")),
            _schema(
                value["schema_version"],
                RECALL_CONTEXT_USE_SCHEMA_VERSION,
                "RecallContextUseReceiptV1",
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallFragmentAuthorityBindingV1:
    decision_id: str
    decision_hash: str
    result_id: str
    result_hash: str
    item_id: str | None
    item_hash: str | None
    conflict_group_id: str | None
    confirmation_hash: str | None
    result_group_hash: str | None
    page_id: str | None
    page_hash: str | None
    use_receipt_id: str | None
    use_receipt_hash: str | None
    public_payload_hash: str
    binding_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, name in ((self.decision_id, "decision_id"), (self.result_id, "result_id")):
            _identifier(value, name)
        for value, name in (
            (self.decision_hash, "decision_hash"),
            (self.result_hash, "result_hash"),
            (self.public_payload_hash, "public_payload_hash"),
        ):
            _digest(value, name)
        item_id = _optional_identifier(self.item_id, "item_id")
        group_id = _optional_identifier(self.conflict_group_id, "conflict_group_id")
        if (item_id is None) == (group_id is None):
            raise ValueError("binding requires exactly one item or confirmation group")
        if (self.item_hash is None) != (item_id is None):
            raise ValueError("item_id and item_hash must appear together")
        if (self.confirmation_hash is None) != (group_id is None) or (
            self.result_group_hash is None
        ) != (group_id is None):
            raise ValueError(
                "conflict_group_id, confirmation_hash, and result_group_hash must appear together"
            )
        if self.item_hash is not None:
            _digest(self.item_hash, "item_hash")
        if self.confirmation_hash is not None:
            _digest(self.confirmation_hash, "confirmation_hash")
        if self.result_group_hash is not None:
            _digest(self.result_group_hash, "result_group_hash")
        page_id = _optional_identifier(self.page_id, "page_id")
        receipt_id = _optional_identifier(self.use_receipt_id, "use_receipt_id")
        if (page_id is None) == (receipt_id is None):
            raise ValueError("binding requires exactly one page or context-use receipt")
        if (self.page_hash is None) != (page_id is None):
            raise ValueError("page_id and page_hash must appear together")
        if (self.use_receipt_hash is None) != (receipt_id is None):
            raise ValueError("use_receipt_id and use_receipt_hash must appear together")
        if self.page_hash is not None:
            _digest(self.page_hash, "page_hash")
        if self.use_receipt_hash is not None:
            _digest(self.use_receipt_hash, "use_receipt_hash")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "conflict_group_id", group_id)
        object.__setattr__(self, "page_id", page_id)
        object.__setattr__(self, "use_receipt_id", receipt_id)
        object.__setattr__(
            self,
            "binding_hash",
            _domain_hash("simple-harness/recall-fragment-binding/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "decision_id": self.decision_id,
            "decision_hash": self.decision_hash,
            "result_id": self.result_id,
            "result_hash": self.result_hash,
            "item_id": self.item_id,
            "item_hash": self.item_hash,
            "conflict_group_id": self.conflict_group_id,
            "confirmation_hash": self.confirmation_hash,
            "result_group_hash": self.result_group_hash,
            "page_id": self.page_id,
            "page_hash": self.page_hash,
            "use_receipt_id": self.use_receipt_id,
            "use_receipt_hash": self.use_receipt_hash,
            "public_payload_hash": self.public_payload_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RecallFragmentAuthorityBindingV1:
        _exact_keys(
            value,
            {
                "decision_id",
                "decision_hash",
                "result_id",
                "result_hash",
                "item_id",
                "item_hash",
                "conflict_group_id",
                "confirmation_hash",
                "result_group_hash",
                "page_id",
                "page_hash",
                "use_receipt_id",
                "use_receipt_hash",
                "public_payload_hash",
            },
            "RecallFragmentAuthorityBindingV1",
        )
        return cls(
            _identifier(value["decision_id"], "decision_id"),
            _digest(value["decision_hash"], "decision_hash"),
            _identifier(value["result_id"], "result_id"),
            _digest(value["result_hash"], "result_hash"),
            _optional_identifier(value["item_id"], "item_id"),
            None if value["item_hash"] is None else _digest(value["item_hash"], "item_hash"),
            _optional_identifier(value["conflict_group_id"], "conflict_group_id"),
            None
            if value["confirmation_hash"] is None
            else _digest(value["confirmation_hash"], "confirmation_hash"),
            None
            if value["result_group_hash"] is None
            else _digest(value["result_group_hash"], "result_group_hash"),
            _optional_identifier(value["page_id"], "page_id"),
            None if value["page_hash"] is None else _digest(value["page_hash"], "page_hash"),
            _optional_identifier(value["use_receipt_id"], "use_receipt_id"),
            None
            if value["use_receipt_hash"] is None
            else _digest(value["use_receipt_hash"], "use_receipt_hash"),
            _digest(value["public_payload_hash"], "public_payload_hash"),
        )


@dataclass(frozen=True, slots=True)
class ContextFragmentV2:
    fragment_id: str
    run_id: str
    subject: str
    fragment_type: ContextFragmentType
    source_ref: str
    source_revision: int
    public_payload: FrozenJsonValue
    public_payload_hash: str
    token_estimate: int
    byte_estimate: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    recall_binding: RecallFragmentAuthorityBindingV1 | None
    schema_version: int = CONTEXT_FRAGMENT_SCHEMA_VERSION
    fragment_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(self.schema_version, CONTEXT_FRAGMENT_SCHEMA_VERSION, "ContextFragmentV2")
        for identifier_value, name in (
            (self.fragment_id, "fragment_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
        ):
            _identifier(identifier_value, name)
        fragment_type = ContextFragmentType(self.fragment_type)
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _positive_int(self.source_revision, "source_revision")
        payload = _payload(self.public_payload, "public_payload")
        _digest(self.public_payload_hash, "public_payload_hash")
        if _payload_hash(payload) != self.public_payload_hash:
            raise ValueError("public_payload_hash differs from payload")
        for estimate_value, name in (
            (self.token_estimate, "token_estimate"),
            (self.byte_estimate, "byte_estimate"),
        ):
            _non_negative_int(estimate_value, name)
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        evidence = _evidence_refs(self.evidence_refs)
        recalled = fragment_type in {
            ContextFragmentType.RECALLED_MEMORY,
            ContextFragmentType.SHORT_HORIZON,
            ContextFragmentType.RECALL_CONFIRMATION,
        }
        if recalled:
            if not isinstance(self.recall_binding, RecallFragmentAuthorityBindingV1):
                raise ValueError("recalled ContextFragmentV2 requires recall binding")
            if self.recall_binding.public_payload_hash != self.public_payload_hash:
                raise ValueError("fragment payload differs from recall authority")
            if (
                fragment_type is ContextFragmentType.RECALL_CONFIRMATION
                and self.recall_binding.conflict_group_id is None
            ):
                raise ValueError("confirmation fragment requires conflict group binding")
            if (
                fragment_type is not ContextFragmentType.RECALL_CONFIRMATION
                and self.recall_binding.item_id is None
            ):
                raise ValueError("ordinary recalled fragment requires item binding")
        elif self.recall_binding is not None:
            raise ValueError("non-recall fragment cannot carry recall authority")
        object.__setattr__(self, "fragment_type", fragment_type)
        object.__setattr__(self, "public_payload", payload)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(
            self,
            "fragment_hash",
            _domain_hash("simple-harness/context-fragment/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "fragment_id": self.fragment_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "fragment_type": self.fragment_type.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "public_payload": _payload_json(self.public_payload),
            "public_payload_hash": self.public_payload_hash,
            "token_estimate": self.token_estimate,
            "byte_estimate": self.byte_estimate,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "recall_binding": None
            if self.recall_binding is None
            else self.recall_binding.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextFragmentV2:
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
                "public_payload",
                "public_payload_hash",
                "token_estimate",
                "byte_estimate",
                "disclosure_context",
                "evidence_refs",
                "recall_binding",
            },
            "ContextFragmentV2",
        )
        binding = value["recall_binding"]
        return cls(
            _identifier(value["fragment_id"], "fragment_id"),
            _identifier(value["run_id"], "run_id"),
            _identifier(value["subject"], "subject"),
            ContextFragmentType(value["fragment_type"]),  # type: ignore[arg-type]
            _identifier(value["source_ref"], "source_ref", max_length=1024),
            _positive_int(value["source_revision"], "source_revision"),
            _payload(value["public_payload"], "public_payload"),
            _digest(value["public_payload_hash"], "public_payload_hash"),
            _non_negative_int(value["token_estimate"], "token_estimate"),
            _non_negative_int(value["byte_estimate"], "byte_estimate"),
            DisclosureContext.from_json(_object(value["disclosure_context"], "disclosure_context")),
            _refs_from_json(value["evidence_refs"]),
            None
            if binding is None
            else RecallFragmentAuthorityBindingV1.from_json(_object(binding, "recall_binding")),
            _schema(value["schema_version"], CONTEXT_FRAGMENT_SCHEMA_VERSION, "ContextFragmentV2"),
        )


@dataclass(frozen=True, slots=True)
class ContextAssemblyDecisionV2:
    decision_id: str
    run_id: str
    subject: str
    selected_fragment_bindings: tuple[ContextFragmentBindingV2, ...]
    omitted_fragment_bindings: tuple[ContextFragmentBindingV2, ...]
    snapshot_refs: tuple[str, ...]
    budget: ContextAssemblyBudget
    selected_token_estimate: int
    selected_byte_estimate: int
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    reason_codes: tuple[ContextAssemblyReasonCode, ...]
    idempotency_key: str
    schema_version: int = CONTEXT_ASSEMBLY_SCHEMA_VERSION
    decision_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _schema(self.schema_version, CONTEXT_ASSEMBLY_SCHEMA_VERSION, "ContextAssemblyDecisionV2")
        for value, name in (
            (self.decision_id, "decision_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        if not isinstance(self.selected_fragment_bindings, (tuple, list)) or not all(
            isinstance(item, ContextFragmentBindingV2) for item in self.selected_fragment_bindings
        ):
            raise TypeError("selected_fragment_bindings must contain bindings")
        if not isinstance(self.omitted_fragment_bindings, (tuple, list)) or not all(
            isinstance(item, ContextFragmentBindingV2) for item in self.omitted_fragment_bindings
        ):
            raise TypeError("omitted_fragment_bindings must contain bindings")
        selected = tuple(self.selected_fragment_bindings)
        omitted = tuple(self.omitted_fragment_bindings)
        if len({item.fragment_id for item in selected}) != len(selected) or len(
            {item.fragment_id for item in omitted}
        ) != len(omitted):
            raise ValueError("fragment ids must be unique within each outcome")
        if {item.fragment_id for item in selected} & {item.fragment_id for item in omitted}:
            raise ValueError("fragment cannot be selected and omitted")
        snapshots = _bounded_identifiers(self.snapshot_refs, "snapshot_refs")
        if not snapshots:
            raise ValueError("snapshot_refs must be non-empty")
        if not isinstance(self.budget, ContextAssemblyBudget):
            raise TypeError("budget must use ContextAssemblyBudget")
        selected_tokens = _non_negative_int(self.selected_token_estimate, "selected_token_estimate")
        selected_bytes = _non_negative_int(self.selected_byte_estimate, "selected_byte_estimate")
        available = (
            self.budget.max_total_tokens
            - self.budget.generation_reserve_tokens
            - self.budget.safety_reserve_tokens
        )
        if selected_tokens > available or selected_bytes > self.budget.max_total_bytes:
            raise ValueError("selected estimates exceed Context assembly budget")
        if (
            not isinstance(self.disclosure_context, DisclosureContext)
            or self.disclosure_context.run_id != self.run_id
        ):
            raise ValueError("disclosure_context must bind assembly run")
        evidence = _evidence_refs(self.evidence_refs)
        reasons = tuple(ContextAssemblyReasonCode(item) for item in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        object.__setattr__(self, "selected_fragment_bindings", selected)
        object.__setattr__(self, "omitted_fragment_bindings", omitted)
        object.__setattr__(self, "snapshot_refs", snapshots)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self,
            "decision_hash",
            _domain_hash("simple-harness/context-assembly-decision/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "selected_fragment_bindings": [
                item.to_json() for item in self.selected_fragment_bindings
            ],
            "omitted_fragment_bindings": [
                item.to_json() for item in self.omitted_fragment_bindings
            ],
            "snapshot_refs": list(self.snapshot_refs),
            "budget": self.budget.to_json(),
            "selected_token_estimate": self.selected_token_estimate,
            "selected_byte_estimate": self.selected_byte_estimate,
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "reason_codes": [item.value for item in self.reason_codes],
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ContextAssemblyDecisionV2:
        _exact_keys(
            value,
            {
                "schema_version",
                "decision_id",
                "run_id",
                "subject",
                "selected_fragment_bindings",
                "omitted_fragment_bindings",
                "snapshot_refs",
                "budget",
                "selected_token_estimate",
                "selected_byte_estimate",
                "disclosure_context",
                "evidence_refs",
                "reason_codes",
                "idempotency_key",
            },
            "ContextAssemblyDecisionV2",
        )
        return cls(
            _identifier(value["decision_id"], "decision_id"),
            _identifier(value["run_id"], "run_id"),
            _identifier(value["subject"], "subject"),
            tuple(
                ContextFragmentBindingV2.from_json(item)
                for item in _objects(
                    value["selected_fragment_bindings"], "selected_fragment_bindings"
                )
            ),
            tuple(
                ContextFragmentBindingV2.from_json(item)
                for item in _objects(
                    value["omitted_fragment_bindings"], "omitted_fragment_bindings"
                )
            ),
            _strings(value["snapshot_refs"], "snapshot_refs"),
            ContextAssemblyBudget.from_json(_object(value["budget"], "budget")),
            _non_negative_int(value["selected_token_estimate"], "selected_token_estimate"),
            _non_negative_int(value["selected_byte_estimate"], "selected_byte_estimate"),
            DisclosureContext.from_json(_object(value["disclosure_context"], "disclosure_context")),
            _refs_from_json(value["evidence_refs"]),
            tuple(
                ContextAssemblyReasonCode(item)
                for item in _strings(value["reason_codes"], "reason_codes")
            ),
            _identifier(value["idempotency_key"], "idempotency_key"),
            _schema(
                value["schema_version"],
                CONTEXT_ASSEMBLY_SCHEMA_VERSION,
                "ContextAssemblyDecisionV2",
            ),
        )


__all__ = (
    "CONTEXT_ASSEMBLY_SCHEMA_VERSION",
    "CONTEXT_FRAGMENT_SCHEMA_VERSION",
    "RECALL_CONTEXT_USE_SCHEMA_VERSION",
    "RECALL_DECISION_SCHEMA_VERSION_V4",
    "RECALL_MAX_BYTES",
    "RECALL_MAX_DEADLINE_MS",
    "RECALL_MAX_ITEMS",
    "RECALL_MAX_TOKENS",
    "RECALL_RESULT_SCHEMA_VERSION",
    "ContextAssemblyDecisionV2",
    "ContextFragmentBindingV2",
    "ContextFragmentV2",
    "RecallBudgetV1",
    "RecallConfirmationGroupV4",
    "RecallConfirmationMemberV4",
    "RecallContextUseAuthorizationRequestV1",
    "RecallContextUseReceiptV1",
    "RecallDecisionV4",
    "RecallFragmentAuthorityBindingV1",
    "RecallItemBindingV1",
    "RecallItemKind",
    "RecallPageBindingKind",
    "RecallPageConfirmationGroupBindingV1",
    "RecallPageConfirmationMemberBindingV1",
    "RecallPageSelectedItemBindingV1",
    "RecallResultPageBindingV1",
    "RecallResultPageRequestV1",
    "RecallResultPageV1",
    "RecallSelectedItemV4",
    "RecallSourceKind",
    "TypedRecallConfirmationGroupV1",
    "TypedRecallConfirmationMemberV1",
    "TypedRecallResultItemV1",
    "TypedRecallResultV1",
)
