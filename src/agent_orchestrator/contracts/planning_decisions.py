# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planning-decision protocol core and envelope (HTN-LLM-NATIVE V2).

H1 makes "what the planner suggests the system should do next" a single wire
object, the ``PlanningDecisionEnvelopeV1``.  This module owns the protocol:

* the decision limits of section 16;
* every closed enum: sections 11, 17, 33, 36 and the addendum-2 small enums;
* the H1 slice of the section 12 phase-enablement matrix;
* the strict ``to_json`` / ``from_json`` dataclasses: references, the request
  binding, feedback, the problem detail, the retry-budget view, the four
  sub-structures (sections 20-23), all ten payloads (sections 24-31) and the
  ten-field envelope (section 13);
* the deterministic ``decision_id`` of section 35 and the canonical decision
  JSON / hash of section 15.

The JSON Schema file, the golden fixture directories and the packaging work are
deliberately absent: they belong to the H1-A2b slice.  Everything here is data;
validated in ``__post_init__`` and never executed.  Unknown keys, missing keys
and wrongly-typed values raise :class:`~.models.ContractError`, exactly as the
rest of the contract package does.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from simple_harness.contracts import canonical_json

from .models import ContractError
from .semantic_base import (
    enum_of,
    fields_of,
    flag,
    hash_hex,
    identifier,
    index,
    json_object,
    optional_index,
    schema_version,
    sequence_of,
    text,
)

# --------------------------------------------------------------------------------------
# Protocol identity (§13, §35) and limits (§16)
# --------------------------------------------------------------------------------------

PLANNING_DECISION_SCHEMA_VERSION = 1
PLANNING_DECISION_V1 = "planning-decision-v1"
LEGACY_PLANNING_PROTOCOL = "legacy-plan-proposal-v1"
PLANNING_DECISION_CODEC_VERSION = "planning-decision-codec-v1"

MAX_PD_RATIONALE_CHARS = 4_000
MAX_PD_REASON_REFS = 32
MAX_PD_ASSUMPTIONS = 16
MAX_PD_ALTERNATIVES = 8
MAX_PD_UNCERTAINTIES = 16
MAX_PD_REPLAN_TRIGGERS = 16
MAX_PD_BINDINGS = 64
MAX_PD_WAIT_REFS = 32
MAX_PD_BLOCKERS = 16
MAX_PD_HUMAN_OPTIONS = 12
MAX_PD_ARGUMENTS = 32
MIN_PD_EVIDENCE_QUESTIONS = 1
MAX_PD_EVIDENCE_QUESTIONS = 8

MAX_PLANNING_REF_ID = 256
MAX_SUBJECT_KEY_CHARS = 256


# --------------------------------------------------------------------------------------
# Enums (§11, §17, §33, §36)
# --------------------------------------------------------------------------------------


class PlanningDecisionType(StrEnum):
    """§11: the nine things a planner reply may propose."""

    REFINE = "REFINE"
    PROPOSE_METHOD = "PROPOSE_METHOD"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    REPAIR = "REPAIR"
    BIND_EXISTING_GOAL = "BIND_EXISTING_GOAL"
    DECLARE_BLOCKED = "DECLARE_BLOCKED"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    WAIT = "WAIT"
    NO_CHANGE = "NO_CHANGE"


class PlanningRefKind(StrEnum):
    """Section 17 plus BL-1: the sixteen kinds a planning reference may name.

    ``fact`` does not exist here; a planning fact is ``observation``.
    ``method_instance`` is the sixteenth kind added by the V2 addendum so a
    REPLACE_METHOD payload can name the instance it retires.
    """

    TASK = "task"
    OBLIGATION = "obligation"
    METHOD = "method"
    REQUIREMENTS = "requirements"
    ARTIFACT = "artifact"
    SOURCE = "source"
    OBSERVATION = "observation"
    REVIEW = "review"
    ACCEPTANCE = "acceptance"
    RESOLUTION = "resolution"
    OPERATION = "operation"
    TOOL_RECEIPT = "tool_receipt"
    KNOWLEDGE = "knowledge"
    AUTHORITY = "authority"
    CAPABILITY = "capability"
    METHOD_INSTANCE = "method_instance"


class PlanningDecisionRejectionCode(StrEnum):
    """§33: the complete, closed list of thirty-six rejection codes."""

    DECISION_BLOCK_MISSING = "DECISION_BLOCK_MISSING"
    MULTIPLE_DECISIONS = "MULTIPLE_DECISIONS"
    MIXED_PROTOCOL_BLOCKS = "MIXED_PROTOCOL_BLOCKS"
    MALFORMED_DECISION = "MALFORMED_DECISION"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MODEL_SET_SYSTEM_FIELD = "MODEL_SET_SYSTEM_FIELD"
    DECISION_TYPE_UNKNOWN = "DECISION_TYPE_UNKNOWN"
    DECISION_NOT_ENABLED_IN_PHASE = "DECISION_NOT_ENABLED_IN_PHASE"
    SUBJECT_NOT_IN_REQUEST = "SUBJECT_NOT_IN_REQUEST"
    REF_OUTSIDE_CONTEXT = "REF_OUTSIDE_CONTEXT"
    REQUEST_BINDING_STALE = "REQUEST_BINDING_STALE"
    PACKAGE_HASH_MISMATCH = "PACKAGE_HASH_MISMATCH"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    METHOD_STALE = "METHOD_STALE"
    METHOD_RETIRED = "METHOD_RETIRED"
    METHOD_REJECTED = "METHOD_REJECTED"
    METHOD_INAPPLICABLE = "METHOD_INAPPLICABLE"
    METHOD_NOT_AUTHORIZED = "METHOD_NOT_AUTHORIZED"
    PARAMETER_INVALID = "PARAMETER_INVALID"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    DATA_UNBOUND = "DATA_UNBOUND"
    STRUCTURE_INVALID = "STRUCTURE_INVALID"
    ORDER_CYCLE = "ORDER_CYCLE"
    REFINEMENT_CYCLE = "REFINEMENT_CYCLE"
    COVERAGE_GAP = "COVERAGE_GAP"
    BUDGET_INSUFFICIENT = "BUDGET_INSUFFICIENT"
    OBLIGATION_NOT_OPEN = "OBLIGATION_NOT_OPEN"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    OPERATION_UNRESOLVED = "OPERATION_UNRESOLVED"
    RUNNING_WORK_NOT_RECONCILED = "RUNNING_WORK_NOT_RECONCILED"
    REPAIR_NOT_ALLOWED = "REPAIR_NOT_ALLOWED"
    REUSE_NOT_ALLOWED = "REUSE_NOT_ALLOWED"
    PLANNING_BOUND_REACHED = "PLANNING_BOUND_REACHED"
    INTERNAL_CONTRACT_ERROR = "INTERNAL_CONTRACT_ERROR"


class PlanningDecisionStatus(StrEnum):
    """§36: the durable status of one evaluated decision."""

    UNREADABLE = "UNREADABLE"
    DECODED = "DECODED"
    REJECTED = "REJECTED"
    ADMITTED = "ADMITTED"
    COMPILED = "COMPILED"
    COMMIT_REJECTED = "COMMIT_REJECTED"
    COMMITTED = "COMMITTED"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"


class AssumptionRisk(StrEnum):
    """Section 20: an assumption never upgrades itself to TRUE; it carries a risk band."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class UncertaintySeverity(StrEnum):
    """Addendum 2 section 2: how much an uncertainty should worry a reader."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AlternativeDisposition(StrEnum):
    """Addendum 2 section 2: what happened to a considered alternative."""

    CONSIDERED = "CONSIDERED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class BlockerCode(StrEnum):
    """Addendum 2 section 2: the closed set of reasons a decision may declare."""

    NO_USABLE_METHOD = "NO_USABLE_METHOD"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    AUTHORIZATION_MISSING = "AUTHORIZATION_MISSING"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    STRUCTURE_UNSAT = "STRUCTURE_UNSAT"
    OTHER = "OTHER"


class ResumableIf(StrEnum):
    """Addendum 2 section 2: the closed set of events that may resume a blocked plan."""

    NEW_METHOD_ADMITTED = "new_method_admitted"
    EVIDENCE_UPDATED = "evidence_updated"
    AUTHORIZATION_GRANTED = "authorization_granted"
    HUMAN_RESOLVED = "human_resolved"
    PLAN_REVISION_CHANGED = "plan_revision_changed"


class RepairKind(StrEnum):
    """Section 25/26: the H1 repair sub-kinds.  H4 may add more."""

    REPLACE_METHOD = "REPLACE_METHOD"
    PROPOSE_SUCCESSOR = "PROPOSE_SUCCESSOR"


class BindExistingGoalMode(StrEnum):
    """Section 27: why an existing goal is being bound."""

    REUSE_ACCEPTED = "REUSE_ACCEPTED"
    SHARE_ACTIVE = "SHARE_ACTIVE"


# --------------------------------------------------------------------------------------
# Phase enablement (§12, H1 column)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionEnablement:
    """Whether a decision kind can be decoded, admitted and executed in a phase."""

    decodable: bool
    admissible: bool
    executable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "decodable", flag(self.decodable, "enablement.decodable"))
        object.__setattr__(self, "admissible", flag(self.admissible, "enablement.admissible"))
        object.__setattr__(self, "executable", flag(self.executable, "enablement.executable"))


_EXECUTABLE = DecisionEnablement(decodable=True, admissible=True, executable=True)
_DECODE_ONLY = DecisionEnablement(decodable=True, admissible=False, executable=False)

#: §12 H1 column, keyed by decision type and (for REPAIR) its sub-kind.  The three
#: decode-only kinds parse and persist, then return ``DECISION_NOT_ENABLED_IN_PHASE``
#: rather than silently falling back to legacy behaviour.
H1_DECISION_ENABLEMENT: Mapping[str, DecisionEnablement] = MappingProxyType(
    {
        "REFINE": _EXECUTABLE,
        "REPAIR/REPLACE_METHOD": _EXECUTABLE,
        "REPAIR/PROPOSE_SUCCESSOR": _EXECUTABLE,
        "BIND_EXISTING_GOAL": _EXECUTABLE,
        "DECLARE_BLOCKED": _EXECUTABLE,
        "WAIT": _EXECUTABLE,
        "NO_CHANGE": _EXECUTABLE,
        "REQUEST_EVIDENCE": _DECODE_ONLY,
        "REQUEST_HUMAN": _DECODE_ONLY,
        "PROPOSE_METHOD": _DECODE_ONLY,
    }
)


# --------------------------------------------------------------------------------------
# Helpers local to this module
# --------------------------------------------------------------------------------------


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else text(value, name)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ContractError(f"{name} must be a finite number")
    return float(value)


def _hash(value: object, name: str) -> str:
    return hash_hex(value, name)


# --------------------------------------------------------------------------------------
# PlanningRefV1 (§17)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningRefV1:
    """A reference that must byte-match a visible ref: kind, id, revision, hash."""

    kind: PlanningRefKind
    id: str
    semantic_revision: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", enum_of(PlanningRefKind, self.kind, "planning_ref.kind"))
        object.__setattr__(
            self, "id", identifier(self.id, "planning_ref.id", limit=MAX_PLANNING_REF_ID)
        )
        object.__setattr__(
            self,
            "semantic_revision",
            index(self.semantic_revision, "planning_ref.semantic_revision", minimum=1),
        )
        object.__setattr__(
            self, "content_hash", _hash(self.content_hash, "planning_ref.content_hash")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "semantic_revision": self.semantic_revision,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "planning_ref") -> PlanningRefV1:
        data = fields_of(
            value, name, required=("kind", "id", "semantic_revision", "content_hash")
        )
        return cls(
            kind=data["kind"],
            id=data["id"],
            semantic_revision=data["semantic_revision"],
            content_hash=data["content_hash"],
        )


def _planning_ref(value: object, name: str) -> PlanningRefV1:
    if isinstance(value, PlanningRefV1):
        return value
    return PlanningRefV1.from_json(value, name)


def _optional_planning_ref(value: object, name: str) -> PlanningRefV1 | None:
    return None if value is None else _planning_ref(value, name)


def _problem(value: object, name: str) -> PlanningProblemDetailV1:
    if isinstance(value, PlanningProblemDetailV1):
        return value
    return PlanningProblemDetailV1.from_json(value, name)


def _retry_budget(value: object, name: str) -> PlanningRetryBudgetView:
    if isinstance(value, PlanningRetryBudgetView):
        return value
    return PlanningRetryBudgetView.from_json(value, name)


# --------------------------------------------------------------------------------------
# PlanningRequestBinding (§34, plus intent_id per BL-7)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningRequestBinding:
    """The immutable binding between a planner request and the package it saw.

    ``intent_id`` is carried here per the conflict check BL-7; the rest are the
    §34 fields verbatim.  A binding is stale the moment the plan revision moves
    (H1 compares ``base_plan_revision`` to the current revision directly).
    """

    request_id: str
    mission_id: str
    protocol_version: str
    package_version: int
    package_hash: str
    base_plan_revision: int
    requirements_revision: int
    scope_epoch_digest: str
    subject_bindings_hash: str
    visible_refs_digest: str
    prompt_version: str
    prompt_hash: str
    created_at: float
    intent_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", identifier(self.request_id, "binding.request_id"))
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "binding.mission_id"))
        object.__setattr__(
            self, "protocol_version", identifier(self.protocol_version, "binding.protocol_version")
        )
        object.__setattr__(
            self,
            "package_version",
            index(self.package_version, "binding.package_version", minimum=1),
        )
        object.__setattr__(self, "package_hash", _hash(self.package_hash, "binding.package_hash"))
        object.__setattr__(
            self,
            "base_plan_revision",
            index(self.base_plan_revision, "binding.base_plan_revision", minimum=0),
        )
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "binding.requirements_revision", minimum=0),
        )
        object.__setattr__(
            self,
            "scope_epoch_digest",
            _hash(self.scope_epoch_digest, "binding.scope_epoch_digest"),
        )
        object.__setattr__(
            self,
            "subject_bindings_hash",
            _hash(self.subject_bindings_hash, "binding.subject_bindings_hash"),
        )
        object.__setattr__(
            self,
            "visible_refs_digest",
            _hash(self.visible_refs_digest, "binding.visible_refs_digest"),
        )
        object.__setattr__(
            self, "prompt_version", identifier(self.prompt_version, "binding.prompt_version")
        )
        object.__setattr__(self, "prompt_hash", _hash(self.prompt_hash, "binding.prompt_hash"))
        object.__setattr__(
            self, "created_at", _finite_number(self.created_at, "binding.created_at")
        )
        object.__setattr__(self, "intent_id", identifier(self.intent_id, "binding.intent_id"))

    _FIELDS = (
        "request_id",
        "mission_id",
        "protocol_version",
        "package_version",
        "package_hash",
        "base_plan_revision",
        "requirements_revision",
        "scope_epoch_digest",
        "subject_bindings_hash",
        "visible_refs_digest",
        "prompt_version",
        "prompt_hash",
        "created_at",
        "intent_id",
    )

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._FIELDS}

    @classmethod
    def from_json(
        cls, value: object, name: str = "planning_request_binding"
    ) -> PlanningRequestBinding:
        data = fields_of(value, name, required=cls._FIELDS)
        return cls(
            request_id=data["request_id"],
            mission_id=data["mission_id"],
            protocol_version=data["protocol_version"],
            package_version=data["package_version"],
            package_hash=data["package_hash"],
            base_plan_revision=data["base_plan_revision"],
            requirements_revision=data["requirements_revision"],
            scope_epoch_digest=data["scope_epoch_digest"],
            subject_bindings_hash=data["subject_bindings_hash"],
            visible_refs_digest=data["visible_refs_digest"],
            prompt_version=data["prompt_version"],
            prompt_hash=data["prompt_hash"],
            created_at=data["created_at"],
            intent_id=data["intent_id"],
        )


# --------------------------------------------------------------------------------------
# PlanningProblemDetailV1 (§40)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningProblemDetailV1:
    """One rejected-decision problem, optionally anchored to a ref and a field path."""

    code: PlanningDecisionRejectionCode
    subject_ref: PlanningRefV1 | None
    field_path: str | None
    detail: str
    expected: str | None = None
    observed: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "code", enum_of(PlanningDecisionRejectionCode, self.code, "problem.code")
        )
        object.__setattr__(
            self,
            "subject_ref",
            _optional_planning_ref(self.subject_ref, "problem.subject_ref"),
        )
        object.__setattr__(
            self,
            "field_path",
            None
            if self.field_path is None
            else identifier(self.field_path, "problem.field_path"),
        )
        object.__setattr__(self, "detail", text(self.detail, "problem.detail"))
        object.__setattr__(self, "expected", _optional_text(self.expected, "problem.expected"))
        object.__setattr__(self, "observed", _optional_text(self.observed, "problem.observed"))

    def to_json(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "subject_ref": None if self.subject_ref is None else self.subject_ref.to_json(),
            "field_path": self.field_path,
            "detail": self.detail,
            "expected": self.expected,
            "observed": self.observed,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "planning_problem") -> PlanningProblemDetailV1:
        data = fields_of(
            value,
            name,
            required=("code", "subject_ref", "field_path", "detail"),
            optional=("expected", "observed"),
        )
        return cls(
            code=data["code"],
            subject_ref=data["subject_ref"],
            field_path=data["field_path"],
            detail=data["detail"],
            expected=data.get("expected"),
            observed=data.get("observed"),
        )


# --------------------------------------------------------------------------------------
# PlanningRetryBudgetView (§39)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningRetryBudgetView:
    """The retry counters reported back to the planner with feedback (§39)."""

    same_request_format_retries_remaining: int
    planning_rounds_remaining: int
    synthesis_asks_remaining: int
    root_review_repairs_remaining: int
    repeated_failure_before_escalation_remaining: int | None

    _FIELDS = (
        "same_request_format_retries_remaining",
        "planning_rounds_remaining",
        "synthesis_asks_remaining",
        "root_review_repairs_remaining",
        "repeated_failure_before_escalation_remaining",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS[:4]:
            object.__setattr__(
                self, name, index(getattr(self, name), name, minimum=0)
            )
        object.__setattr__(
            self,
            "repeated_failure_before_escalation_remaining",
            optional_index(
                self.repeated_failure_before_escalation_remaining,
                "repeated_failure_before_escalation_remaining",
                minimum=0,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._FIELDS}

    @classmethod
    def from_json(
        cls, value: object, name: str = "planning_retry_budget"
    ) -> PlanningRetryBudgetView:
        data = fields_of(value, name, required=cls._FIELDS)
        return cls(
            same_request_format_retries_remaining=data["same_request_format_retries_remaining"],
            planning_rounds_remaining=data["planning_rounds_remaining"],
            synthesis_asks_remaining=data["synthesis_asks_remaining"],
            root_review_repairs_remaining=data["root_review_repairs_remaining"],
            repeated_failure_before_escalation_remaining=data[
                "repeated_failure_before_escalation_remaining"
            ],
        )


# --------------------------------------------------------------------------------------
# PlanningFeedbackV1 (§39)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningFeedbackV1:
    """Admission's answer for a previous decision; a value, not an event (§39)."""

    previous_decision_id: str
    status: PlanningDecisionStatus
    rejection_codes: tuple[PlanningDecisionRejectionCode, ...]
    problems: tuple[PlanningProblemDetailV1, ...]
    changed_refs: tuple[PlanningRefV1, ...]
    budgets: PlanningRetryBudgetView

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "previous_decision_id",
            identifier(self.previous_decision_id, "feedback.previous_decision_id"),
        )
        object.__setattr__(
            self, "status", enum_of(PlanningDecisionStatus, self.status, "feedback.status")
        )
        object.__setattr__(
            self,
            "rejection_codes",
            sequence_of(
                self.rejection_codes,
                "feedback.rejection_codes",
                lambda entry, where: enum_of(PlanningDecisionRejectionCode, entry, where),
                limit=len(PlanningDecisionRejectionCode),
            ),
        )
        object.__setattr__(
            self,
            "problems",
            sequence_of(
                self.problems,
                "feedback.problems",
                _problem,
            ),
        )
        object.__setattr__(
            self,
            "changed_refs",
            sequence_of(
                self.changed_refs,
                "feedback.changed_refs",
                _planning_ref,
            ),
        )
        object.__setattr__(
            self, "budgets", _retry_budget(self.budgets, "feedback.budgets")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "previous_decision_id": self.previous_decision_id,
            "status": str(self.status),
            "rejection_codes": [str(code) for code in self.rejection_codes],
            "problems": [problem.to_json() for problem in self.problems],
            "changed_refs": [ref.to_json() for ref in self.changed_refs],
            "budgets": self.budgets.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "planning_feedback") -> PlanningFeedbackV1:
        data = fields_of(
            value,
            name,
            required=(
                "previous_decision_id",
                "status",
                "rejection_codes",
                "problems",
                "changed_refs",
                "budgets",
            ),
        )
        return cls(
            previous_decision_id=data["previous_decision_id"],
            status=data["status"],
            rejection_codes=data["rejection_codes"],
            problems=data["problems"],
            changed_refs=data["changed_refs"],
            budgets=data["budgets"],
        )


# --------------------------------------------------------------------------------------
# Sub-structures (V2 section 20-23) and the versioned type reference (addendum 1)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionedTypeRefV1:
    """A type reference to ``(id, version, content_hash)`` (V2 section 26).

    It is deliberately distinct from :class:`~.semantic_base.VersionedRef`: the
    two shapes are identical on the wire but only this type is the ``goal_type_ref``
    of a PROPOSE_SUCCESSOR payload, registered as ``$defs/versionedTypeRef``.
    """

    id: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", identifier(self.id, "versioned_type_ref.id"))
        object.__setattr__(
            self, "version", index(self.version, "versioned_type_ref.version", minimum=1)
        )
        object.__setattr__(
            self,
            "content_hash",
            hash_hex(self.content_hash, "versioned_type_ref.content_hash"),
        )

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "content_hash": self.content_hash}

    @classmethod
    def from_json(cls, value: object, name: str = "versioned_type_ref") -> VersionedTypeRefV1:
        data = fields_of(value, name, required=("id", "version", "content_hash"))
        return cls(
            id=data["id"],
            version=data["version"],
            content_hash=data["content_hash"],
        )


def _versioned_type_ref(value: object, name: str) -> VersionedTypeRefV1:
    if isinstance(value, VersionedTypeRefV1):
        return value
    return VersionedTypeRefV1.from_json(value, name)


def _decision_type(value: object, name: str) -> PlanningDecisionType:
    return enum_of(PlanningDecisionType, value, name)


@dataclass(frozen=True, slots=True)
class AssumptionV1:
    """V2 section 20: one assumption.  It never upgrades itself to TRUE."""

    key: str
    statement: str
    required_for: tuple[PlanningDecisionType, ...]
    risk: AssumptionRisk
    suggested_predicate_key: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", identifier(self.key, "assumption.key"))
        object.__setattr__(self, "statement", text(self.statement, "assumption.statement"))
        object.__setattr__(
            self,
            "required_for",
            sequence_of(
                self.required_for,
                "assumption.required_for",
                _decision_type,
                limit=len(PlanningDecisionType),
            ),
        )
        object.__setattr__(
            self, "risk", enum_of(AssumptionRisk, self.risk, "assumption.risk")
        )
        object.__setattr__(
            self,
            "suggested_predicate_key",
            None
            if self.suggested_predicate_key is None
            else identifier(self.suggested_predicate_key, "assumption.suggested_predicate_key"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "statement": self.statement,
            "required_for": [str(entry) for entry in self.required_for],
            "risk": str(self.risk),
            "suggested_predicate_key": self.suggested_predicate_key,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "assumption") -> AssumptionV1:
        data = fields_of(
            value,
            name,
            required=("key", "statement", "required_for", "risk", "suggested_predicate_key"),
        )
        return cls(
            key=data["key"],
            statement=data["statement"],
            required_for=data["required_for"],
            risk=data["risk"],
            suggested_predicate_key=data["suggested_predicate_key"],
        )


def _assumption(value: object, name: str) -> AssumptionV1:
    if isinstance(value, AssumptionV1):
        return value
    return AssumptionV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class PlanningUncertaintyV1:
    """V2 section 21: trace/UI/review context only; never a safety verdict."""

    statement: str
    severity: UncertaintySeverity
    affects: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "statement", text(self.statement, "uncertainty.statement"))
        object.__setattr__(
            self, "severity", enum_of(UncertaintySeverity, self.severity, "uncertainty.severity")
        )
        object.__setattr__(
            self,
            "affects",
            sequence_of(
                self.affects,
                "uncertainty.affects",
                lambda entry, where: identifier(entry, where),
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "severity": str(self.severity),
            "affects": list(self.affects),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "uncertainty") -> PlanningUncertaintyV1:
        data = fields_of(value, name, required=("statement", "severity", "affects"))
        return cls(
            statement=data["statement"],
            severity=data["severity"],
            affects=data["affects"],
        )


def _uncertainty(value: object, name: str) -> PlanningUncertaintyV1:
    if isinstance(value, PlanningUncertaintyV1):
        return value
    return PlanningUncertaintyV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class AlternativeSummaryV1:
    """V2 section 22: one alternative that was not selected.  Not an admission input."""

    method_ref: PlanningRefV1 | None
    label: str
    disposition: AlternativeDisposition
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method_ref",
            _optional_planning_ref(self.method_ref, "alternative.method_ref"),
        )
        object.__setattr__(self, "label", text(self.label, "alternative.label"))
        object.__setattr__(
            self,
            "disposition",
            enum_of(AlternativeDisposition, self.disposition, "alternative.disposition"),
        )
        object.__setattr__(self, "reason", text(self.reason, "alternative.reason"))

    def to_json(self) -> dict[str, Any]:
        return {
            "method_ref": None if self.method_ref is None else self.method_ref.to_json(),
            "label": self.label,
            "disposition": str(self.disposition),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "alternative") -> AlternativeSummaryV1:
        data = fields_of(
            value, name, required=("method_ref", "label", "disposition", "reason")
        )
        return cls(
            method_ref=data["method_ref"],
            label=data["label"],
            disposition=data["disposition"],
            reason=data["reason"],
        )


def _alternative(value: object, name: str) -> AlternativeSummaryV1:
    if isinstance(value, AlternativeSummaryV1):
        return value
    return AlternativeSummaryV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class ReplanTriggerHintV1:
    """V2 section 23: an H1 hint only; no callback is installed from it."""

    description: str
    referenced_predicates: tuple[str, ...]
    suggested_decision: PlanningDecisionType

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "description", text(self.description, "replan_trigger.description")
        )
        object.__setattr__(
            self,
            "referenced_predicates",
            sequence_of(
                self.referenced_predicates,
                "replan_trigger.referenced_predicates",
                lambda entry, where: identifier(entry, where),
            ),
        )
        object.__setattr__(
            self,
            "suggested_decision",
            enum_of(
                PlanningDecisionType,
                self.suggested_decision,
                "replan_trigger.suggested_decision",
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "referenced_predicates": list(self.referenced_predicates),
            "suggested_decision": str(self.suggested_decision),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "replan_trigger") -> ReplanTriggerHintV1:
        data = fields_of(
            value,
            name,
            required=("description", "referenced_predicates", "suggested_decision"),
        )
        return cls(
            description=data["description"],
            referenced_predicates=data["referenced_predicates"],
            suggested_decision=data["suggested_decision"],
        )


def _replan_trigger(value: object, name: str) -> ReplanTriggerHintV1:
    if isinstance(value, ReplanTriggerHintV1):
        return value
    return ReplanTriggerHintV1.from_json(value, name)


# --------------------------------------------------------------------------------------
# Payload helpers: arbitrary JSON maps are never key-scanned (V2 section 32)
# --------------------------------------------------------------------------------------


def _json_arg_map(value: object, name: str, *, limit: int) -> Mapping[str, Any]:
    """A model-supplied ``bindings`` / ``arguments`` map.

    The keys are *domain* names: the contract never inspects them for system
    fields (V2 section 32).  Only the entry count and JSON-plainness are enforced.
    """

    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    if len(value) > limit:
        raise ContractError(f"{name} has more than {limit} entries")
    return MappingProxyType(dict(json_object(value, name)))


def _has_method_instance_kind(ref: PlanningRefV1, name: str) -> PlanningRefV1:
    if ref.kind is not PlanningRefKind.METHOD_INSTANCE:
        raise ContractError(f"{name} must have kind=method_instance")
    return ref


# --------------------------------------------------------------------------------------
# The seven executable payloads plus three decode-only payloads (V2 section 24-31)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefineDecision:
    """V2 section 24: refine with one registered method."""

    method_ref: PlanningRefV1
    bindings: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_ref", _planning_ref(self.method_ref, "refine.method_ref"))
        object.__setattr__(
            self, "bindings", _json_arg_map(self.bindings, "refine.bindings", limit=MAX_PD_BINDINGS)
        )

    def to_json(self) -> dict[str, Any]:
        return {"method_ref": self.method_ref.to_json(), "bindings": dict(self.bindings)}

    @classmethod
    def from_json(cls, value: object, name: str = "refine") -> RefineDecision:
        data = fields_of(value, name, required=("method_ref", "bindings"))
        return cls(method_ref=data["method_ref"], bindings=data["bindings"])


@dataclass(frozen=True, slots=True)
class RepairReplaceMethodDecision:
    """V2 section 25: REPAIR / REPLACE_METHOD."""

    repair_kind: RepairKind
    rejected_method_instance: PlanningRefV1
    replacement_method_ref: PlanningRefV1
    bindings: Mapping[str, Any]

    def __post_init__(self) -> None:
        repair_kind = enum_of(RepairKind, self.repair_kind, "repair_replace.repair_kind")
        if repair_kind is not RepairKind.REPLACE_METHOD:
            raise ContractError("repair_replace.repair_kind must be REPLACE_METHOD")
        object.__setattr__(self, "repair_kind", repair_kind)
        object.__setattr__(
            self,
            "rejected_method_instance",
            _has_method_instance_kind(
                _planning_ref(
                    self.rejected_method_instance, "repair_replace.rejected_method_instance"
                ),
                "repair_replace.rejected_method_instance",
            ),
        )
        object.__setattr__(
            self,
            "replacement_method_ref",
            _planning_ref(
                self.replacement_method_ref, "repair_replace.replacement_method_ref"
            ),
        )
        object.__setattr__(
            self,
            "bindings",
            _json_arg_map(self.bindings, "repair_replace.bindings", limit=MAX_PD_BINDINGS),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "repair_kind": str(self.repair_kind),
            "rejected_method_instance": self.rejected_method_instance.to_json(),
            "replacement_method_ref": self.replacement_method_ref.to_json(),
            "bindings": dict(self.bindings),
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "repair_replace"
    ) -> RepairReplaceMethodDecision:
        data = fields_of(
            value,
            name,
            required=(
                "repair_kind",
                "rejected_method_instance",
                "replacement_method_ref",
                "bindings",
            ),
        )
        return cls(
            repair_kind=data["repair_kind"],
            rejected_method_instance=data["rejected_method_instance"],
            replacement_method_ref=data["replacement_method_ref"],
            bindings=data["bindings"],
        )


@dataclass(frozen=True, slots=True)
class RepairProposeSuccessorDecision:
    """V2 section 26: REPAIR / PROPOSE_SUCCESSOR."""

    repair_kind: RepairKind
    old_task_ref: PlanningRefV1
    obligation_ref: PlanningRefV1
    goal_type_ref: VersionedTypeRefV1
    bindings: Mapping[str, Any]

    def __post_init__(self) -> None:
        repair_kind = enum_of(RepairKind, self.repair_kind, "repair_successor.repair_kind")
        if repair_kind is not RepairKind.PROPOSE_SUCCESSOR:
            raise ContractError("repair_successor.repair_kind must be PROPOSE_SUCCESSOR")
        object.__setattr__(self, "repair_kind", repair_kind)
        object.__setattr__(
            self,
            "old_task_ref",
            _planning_ref(self.old_task_ref, "repair_successor.old_task_ref"),
        )
        object.__setattr__(
            self,
            "obligation_ref",
            _planning_ref(self.obligation_ref, "repair_successor.obligation_ref"),
        )
        object.__setattr__(
            self,
            "goal_type_ref",
            _versioned_type_ref(self.goal_type_ref, "repair_successor.goal_type_ref"),
        )
        object.__setattr__(
            self,
            "bindings",
            _json_arg_map(self.bindings, "repair_successor.bindings", limit=MAX_PD_BINDINGS),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "repair_kind": str(self.repair_kind),
            "old_task_ref": self.old_task_ref.to_json(),
            "obligation_ref": self.obligation_ref.to_json(),
            "goal_type_ref": self.goal_type_ref.to_json(),
            "bindings": dict(self.bindings),
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "repair_successor"
    ) -> RepairProposeSuccessorDecision:
        data = fields_of(
            value,
            name,
            required=("repair_kind", "old_task_ref", "obligation_ref", "goal_type_ref", "bindings"),
        )
        return cls(
            repair_kind=data["repair_kind"],
            old_task_ref=data["old_task_ref"],
            obligation_ref=data["obligation_ref"],
            goal_type_ref=data["goal_type_ref"],
            bindings=data["bindings"],
        )


@dataclass(frozen=True, slots=True)
class BindExistingGoalDecision:
    """V2 section 27: bind an existing goal.

    ``REUSE_ACCEPTED`` requires a CURRENT resolution; ``SHARE_ACTIVE`` must carry a
    null ``resolution_ref`` because nothing is being reused.
    """

    mode: BindExistingGoalMode
    consumer_method_instance_ref: PlanningRefV1
    step: str
    goal_ref: PlanningRefV1
    resolution_ref: PlanningRefV1 | None

    def __post_init__(self) -> None:
        mode = enum_of(BindExistingGoalMode, self.mode, "bind_goal.mode")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "consumer_method_instance_ref",
            _has_method_instance_kind(
                _planning_ref(
                    self.consumer_method_instance_ref, "bind_goal.consumer_method_instance_ref"
                ),
                "bind_goal.consumer_method_instance_ref",
            ),
        )
        object.__setattr__(self, "step", identifier(self.step, "bind_goal.step"))
        object.__setattr__(
            self, "goal_ref", _planning_ref(self.goal_ref, "bind_goal.goal_ref")
        )
        resolution_ref = _optional_planning_ref(self.resolution_ref, "bind_goal.resolution_ref")
        if mode is BindExistingGoalMode.SHARE_ACTIVE:
            if resolution_ref is not None:
                raise ContractError("bind_goal.resolution_ref must be null when mode=SHARE_ACTIVE")
        elif resolution_ref is None:
            raise ContractError(
                "bind_goal.resolution_ref is required when mode=REUSE_ACCEPTED"
            )
        object.__setattr__(self, "resolution_ref", resolution_ref)

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "consumer_method_instance_ref": self.consumer_method_instance_ref.to_json(),
            "step": self.step,
            "goal_ref": self.goal_ref.to_json(),
            "resolution_ref": (
                None if self.resolution_ref is None else self.resolution_ref.to_json()
            ),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "bind_goal") -> BindExistingGoalDecision:
        data = fields_of(
            value,
            name,
            required=(
                "mode",
                "consumer_method_instance_ref",
                "step",
                "goal_ref",
                "resolution_ref",
            ),
        )
        return cls(
            mode=data["mode"],
            consumer_method_instance_ref=data["consumer_method_instance_ref"],
            step=data["step"],
            goal_ref=data["goal_ref"],
            resolution_ref=data["resolution_ref"],
        )


@dataclass(frozen=True, slots=True)
class BlockedItemV1:
    """One entry of a DECLARE_BLOCKED payload; ``OTHER`` must carry a detail."""

    code: BlockerCode
    detail: str | None

    def __post_init__(self) -> None:
        code = enum_of(BlockerCode, self.code, "blocked_item.code")
        object.__setattr__(self, "code", code)
        detail = None if self.detail is None else text(self.detail, "blocked_item.detail")
        if code is BlockerCode.OTHER and detail is None:
            raise ContractError("blocked_item.detail is required when code=OTHER")
        object.__setattr__(self, "detail", detail)

    def to_json(self) -> dict[str, Any]:
        return {"code": str(self.code), "detail": self.detail}

    @classmethod
    def from_json(cls, value: object, name: str = "blocked_item") -> BlockedItemV1:
        data = fields_of(value, name, required=("code",), optional=("detail",))
        return cls(code=data["code"], detail=data.get("detail"))


def _blocked_item(value: object, name: str) -> BlockedItemV1:
    if isinstance(value, BlockedItemV1):
        return value
    return BlockedItemV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class DeclareBlockedDecision:
    """V2 section 28: record a blockage; never a Mission FAIL by itself."""

    blockers: tuple[BlockedItemV1, ...]
    resumable_if: tuple[ResumableIf, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "blockers",
            sequence_of(
                self.blockers,
                "declare_blocked.blockers",
                _blocked_item,
                limit=MAX_PD_BLOCKERS,
            ),
        )
        object.__setattr__(
            self,
            "resumable_if",
            sequence_of(
                self.resumable_if,
                "declare_blocked.resumable_if",
                lambda entry, where: enum_of(ResumableIf, entry, where),
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "blockers": [item.to_json() for item in self.blockers],
            "resumable_if": [str(item) for item in self.resumable_if],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "declare_blocked") -> DeclareBlockedDecision:
        data = fields_of(value, name, required=("blockers", "resumable_if"))
        return cls(blockers=data["blockers"], resumable_if=data["resumable_if"])


@dataclass(frozen=True, slots=True)
class WaitDecision:
    """V2 section 29: wait for already-dispatched work; no plan change."""

    wait_for: tuple[PlanningRefV1, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wait_for",
            sequence_of(self.wait_for, "wait.wait_for", _planning_ref, limit=MAX_PD_WAIT_REFS),
        )
        object.__setattr__(self, "reason", text(self.reason, "wait.reason"))

    def to_json(self) -> dict[str, Any]:
        return {"wait_for": [ref.to_json() for ref in self.wait_for], "reason": self.reason}

    @classmethod
    def from_json(cls, value: object, name: str = "wait") -> WaitDecision:
        data = fields_of(value, name, required=("wait_for", "reason"))
        return cls(wait_for=data["wait_for"], reason=data["reason"])


@dataclass(frozen=True, slots=True)
class NoChangeDecision:
    """V2 section 30: no state change at all; the payload carries one reason."""

    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", text(self.reason, "no_change.reason"))

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason}

    @classmethod
    def from_json(cls, value: object, name: str = "no_change") -> NoChangeDecision:
        data = fields_of(value, name, required=("reason",))
        return cls(reason=data["reason"])


@dataclass(frozen=True, slots=True)
class EvidenceQuestionV1:
    """One entry of a REQUEST_EVIDENCE payload (addendum 2 section 3)."""

    predicate_key: str
    arguments: Mapping[str, Any]
    purpose: str
    blocking: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "predicate_key", identifier(self.predicate_key, "question.predicate_key")
        )
        object.__setattr__(
            self,
            "arguments",
            _json_arg_map(self.arguments, "question.arguments", limit=MAX_PD_ARGUMENTS),
        )
        object.__setattr__(self, "purpose", text(self.purpose, "question.purpose"))
        object.__setattr__(self, "blocking", flag(self.blocking, "question.blocking"))

    def to_json(self) -> dict[str, Any]:
        return {
            "predicate_key": self.predicate_key,
            "arguments": dict(self.arguments),
            "purpose": self.purpose,
            "blocking": self.blocking,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "question") -> EvidenceQuestionV1:
        data = fields_of(
            value, name, required=("predicate_key", "arguments", "purpose", "blocking")
        )
        return cls(
            predicate_key=data["predicate_key"],
            arguments=data["arguments"],
            purpose=data["purpose"],
            blocking=data["blocking"],
        )


def _evidence_question(value: object, name: str) -> EvidenceQuestionV1:
    if isinstance(value, EvidenceQuestionV1):
        return value
    return EvidenceQuestionV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class RequestEvidenceDecision:
    """Addendum 2 section 3: decode-only in H1; 1-8 questions."""

    questions: tuple[EvidenceQuestionV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "questions",
            sequence_of(
                self.questions,
                "request_evidence.questions",
                _evidence_question,
                limit=MAX_PD_EVIDENCE_QUESTIONS,
                minimum=MIN_PD_EVIDENCE_QUESTIONS,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {"questions": [question.to_json() for question in self.questions]}

    @classmethod
    def from_json(cls, value: object, name: str = "request_evidence") -> RequestEvidenceDecision:
        data = fields_of(value, name, required=("questions",))
        return cls(questions=data["questions"])


@dataclass(frozen=True, slots=True)
class HumanOptionV1:
    """One selectable option of a REQUEST_HUMAN payload (addendum 2 section 3)."""

    key: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", identifier(self.key, "human_option.key"))
        object.__setattr__(self, "label", text(self.label, "human_option.label"))

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label}

    @classmethod
    def from_json(cls, value: object, name: str = "human_option") -> HumanOptionV1:
        data = fields_of(value, name, required=("key", "label"))
        return cls(key=data["key"], label=data["label"])


def _human_option(value: object, name: str) -> HumanOptionV1:
    if isinstance(value, HumanOptionV1):
        return value
    return HumanOptionV1.from_json(value, name)


@dataclass(frozen=True, slots=True)
class RequestHumanDecision:
    """Addendum 2 section 3: decode-only in H1; 0-12 options."""

    question: str
    options: tuple[HumanOptionV1, ...]
    blocking: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", text(self.question, "request_human.question"))
        object.__setattr__(
            self,
            "options",
            sequence_of(
                self.options,
                "request_human.options",
                _human_option,
                limit=MAX_PD_HUMAN_OPTIONS,
            ),
        )
        object.__setattr__(self, "blocking", flag(self.blocking, "request_human.blocking"))

    def to_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": [option.to_json() for option in self.options],
            "blocking": self.blocking,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "request_human") -> RequestHumanDecision:
        data = fields_of(value, name, required=("question", "options", "blocking"))
        return cls(
            question=data["question"],
            options=data["options"],
            blocking=data["blocking"],
        )


@dataclass(frozen=True, slots=True)
class ProposeMethodDecision:
    """Addendum 2 section 3: decode-only in H1.

    The contract layer deliberately does not import the planning package: the
    inner ``method_proposal`` is only required to be a JSON object and is kept
    verbatim.  Validating it against ``MethodProposal`` is H1-C's job.
    """

    method_proposal: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method_proposal",
            MappingProxyType(json_object(self.method_proposal, "propose_method.method_proposal")),
        )

    def to_json(self) -> dict[str, Any]:
        return {"method_proposal": dict(self.method_proposal)}

    @classmethod
    def from_json(cls, value: object, name: str = "propose_method") -> ProposeMethodDecision:
        data = fields_of(value, name, required=("method_proposal",))
        return cls(method_proposal=data["method_proposal"])


# --------------------------------------------------------------------------------------
# PlanningDecisionEnvelopeV1 (V2 section 13), canonical JSON / hash (V2 section 15)
# --------------------------------------------------------------------------------------


PAYLOAD_BY_DECISION_TYPE: Mapping[PlanningDecisionType, type[Any]] = MappingProxyType(
    {
        PlanningDecisionType.REFINE: RefineDecision,
        PlanningDecisionType.BIND_EXISTING_GOAL: BindExistingGoalDecision,
        PlanningDecisionType.DECLARE_BLOCKED: DeclareBlockedDecision,
        PlanningDecisionType.WAIT: WaitDecision,
        PlanningDecisionType.NO_CHANGE: NoChangeDecision,
        PlanningDecisionType.REQUEST_EVIDENCE: RequestEvidenceDecision,
        PlanningDecisionType.REQUEST_HUMAN: RequestHumanDecision,
        PlanningDecisionType.PROPOSE_METHOD: ProposeMethodDecision,
    }
)

REPAIR_PAYLOAD_BY_KIND: Mapping[RepairKind, type[Any]] = MappingProxyType(
    {
        RepairKind.REPLACE_METHOD: RepairReplaceMethodDecision,
        RepairKind.PROPOSE_SUCCESSOR: RepairProposeSuccessorDecision,
    }
)


def _payload_class(
    decision_type: PlanningDecisionType, payload: object, name: str
) -> type[Any]:
    if decision_type is PlanningDecisionType.REPAIR:
        if isinstance(payload, RepairReplaceMethodDecision):
            return RepairReplaceMethodDecision
        if isinstance(payload, RepairProposeSuccessorDecision):
            return RepairProposeSuccessorDecision
        if not isinstance(payload, Mapping):
            raise ContractError(f"{name} must be an object")
        try:
            kind = RepairKind(str(payload.get("repair_kind")))
        except (ValueError, TypeError) as error:
            raise ContractError(f"{name}.repair_kind must be a known RepairKind") from error
        return REPAIR_PAYLOAD_BY_KIND[kind]
    try:
        return PAYLOAD_BY_DECISION_TYPE[decision_type]
    except KeyError as error:  # pragma: no cover - decision_type is a closed enum
        raise ContractError(f"{name} has no payload for {decision_type}") from error


_ALL_PAYLOAD_CLASSES = (
    RefineDecision,
    RepairReplaceMethodDecision,
    RepairProposeSuccessorDecision,
    BindExistingGoalDecision,
    DeclareBlockedDecision,
    WaitDecision,
    NoChangeDecision,
    RequestEvidenceDecision,
    RequestHumanDecision,
    ProposeMethodDecision,
)


def _decode_payload(
    decision_type: PlanningDecisionType, payload: object, name: str
) -> object:
    kind = _payload_class(decision_type, payload, name)
    if isinstance(payload, kind):
        return payload
    if isinstance(payload, _ALL_PAYLOAD_CLASSES):
        raise ContractError(f"{name} does not match decision type {decision_type}")
    return kind.from_json(payload, name)  # type: ignore[attr-defined]


ENVELOPE_FIELDS = (
    "schema_version",
    "decision_type",
    "subject_key",
    "rationale",
    "reason_refs",
    "assumptions",
    "payload",
    "uncertainties",
    "alternatives",
    "replan_triggers",
)


@dataclass(frozen=True, slots=True)
class PlanningDecisionEnvelopeV1:
    """V2 section 13: the ten-field wire object a planner reply decodes into."""

    schema_version: int
    decision_type: PlanningDecisionType
    subject_key: str
    rationale: str
    reason_refs: tuple[PlanningRefV1, ...]
    assumptions: tuple[AssumptionV1, ...]
    payload: object
    uncertainties: tuple[PlanningUncertaintyV1, ...]
    alternatives: tuple[AlternativeSummaryV1, ...]
    replan_triggers: tuple[ReplanTriggerHintV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "decision.schema_version",
                expected=PLANNING_DECISION_SCHEMA_VERSION,
            ),
        )
        decision_type = enum_of(PlanningDecisionType, self.decision_type, "decision.decision_type")
        object.__setattr__(self, "decision_type", decision_type)
        object.__setattr__(
            self,
            "subject_key",
            identifier(self.subject_key, "decision.subject_key", limit=MAX_SUBJECT_KEY_CHARS),
        )
        object.__setattr__(
            self,
            "rationale",
            text(self.rationale, "decision.rationale", limit=MAX_PD_RATIONALE_CHARS),
        )
        object.__setattr__(
            self,
            "reason_refs",
            _unique_refs(self.reason_refs, "decision.reason_refs", limit=MAX_PD_REASON_REFS),
        )
        object.__setattr__(
            self,
            "assumptions",
            sequence_of(
                self.assumptions,
                "decision.assumptions",
                _assumption,
                limit=MAX_PD_ASSUMPTIONS,
            ),
        )
        object.__setattr__(
            self,
            "payload",
            _decode_payload(decision_type, self.payload, "decision.payload"),
        )
        object.__setattr__(
            self,
            "uncertainties",
            sequence_of(
                self.uncertainties,
                "decision.uncertainties",
                _uncertainty,
                limit=MAX_PD_UNCERTAINTIES,
            ),
        )
        object.__setattr__(
            self,
            "alternatives",
            sequence_of(
                self.alternatives,
                "decision.alternatives",
                _alternative,
                limit=MAX_PD_ALTERNATIVES,
            ),
        )
        object.__setattr__(
            self,
            "replan_triggers",
            sequence_of(
                self.replan_triggers,
                "decision.replan_triggers",
                _replan_trigger,
                limit=MAX_PD_REPLAN_TRIGGERS,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_type": str(self.decision_type),
            "subject_key": self.subject_key,
            "rationale": self.rationale,
            "reason_refs": [ref.to_json() for ref in self.reason_refs],
            "assumptions": [assumption.to_json() for assumption in self.assumptions],
            "payload": self.payload.to_json(),  # type: ignore[attr-defined]
            "uncertainties": [item.to_json() for item in self.uncertainties],
            "alternatives": [item.to_json() for item in self.alternatives],
            "replan_triggers": [item.to_json() for item in self.replan_triggers],
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "planning_decision"
    ) -> PlanningDecisionEnvelopeV1:
        data = fields_of(value, name, required=ENVELOPE_FIELDS)
        return cls(
            schema_version=data["schema_version"],
            decision_type=data["decision_type"],
            subject_key=data["subject_key"],
            rationale=data["rationale"],
            reason_refs=data["reason_refs"],
            assumptions=data["assumptions"],
            payload=data["payload"],
            uncertainties=data["uncertainties"],
            alternatives=data["alternatives"],
            replan_triggers=data["replan_triggers"],
        )



def _unique_refs(value: object, name: str, *, limit: int) -> tuple[PlanningRefV1, ...]:
    refs = sequence_of(value, name, _planning_ref, limit=limit)
    seen: set[tuple[str, str, int, str]] = set()
    for ref in refs:
        identity = (str(ref.kind), ref.id, ref.semantic_revision, ref.content_hash)
        if identity in seen:
            raise ContractError(f"{name} must not contain duplicate references")
        seen.add(identity)
    return refs


def canonical_decision_json(envelope: PlanningDecisionEnvelopeV1) -> str:
    """V2 section 15: canonical JSON of the decoded decision's ``to_json``.

    Object key order never changes the result; array order always does.
    """

    if not isinstance(envelope, PlanningDecisionEnvelopeV1):
        raise ContractError("canonical_decision_json requires a PlanningDecisionEnvelopeV1")
    return canonical_json(envelope.to_json())


def canonical_decision_hash(envelope: PlanningDecisionEnvelopeV1) -> str:
    """V2 section 15: ``sha256`` of the canonical decision JSON, lowercase hex."""

    return hashlib.sha256(canonical_decision_json(envelope).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# decision_id (§35)
# --------------------------------------------------------------------------------------


def compute_decision_id(request_id: str, attempt_ordinal: int, raw_output_hash: str) -> str:
    """Deterministically derive the ``pd-`` id for one planner attempt (§35).

    The codec version is the trailing separator-delimited segment, so a change to
    the codec's canonicalisation can never collide with an id minted by the old
    one.  The same ``(request_id, attempt_ordinal, raw_output_hash)`` always yields
    the same id; a different raw output on the same ordinal is a store conflict.
    """

    request_id = identifier(request_id, "decision_id.request_id")
    attempt_ordinal = index(attempt_ordinal, "decision_id.attempt_ordinal", minimum=0)
    raw_output_hash = _hash(raw_output_hash, "decision_id.raw_output_hash")
    material = (
        request_id
        + "\x1f"
        + str(attempt_ordinal)
        + "\x1f"
        + raw_output_hash
        + "\x1f"
        + PLANNING_DECISION_CODEC_VERSION
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "pd-" + digest[:24]


__all__ = (
    "AlternativeDisposition",
    "AlternativeSummaryV1",
    "AssumptionRisk",
    "AssumptionV1",
    "BindExistingGoalDecision",
    "BindExistingGoalMode",
    "BlockerCode",
    "BlockedItemV1",
    "DecisionEnablement",
    "DeclareBlockedDecision",
    "ENVELOPE_FIELDS",
    "EvidenceQuestionV1",
    "H1_DECISION_ENABLEMENT",
    "HumanOptionV1",
    "LEGACY_PLANNING_PROTOCOL",
    "MAX_PD_ALTERNATIVES",
    "MAX_PD_ARGUMENTS",
    "MAX_PD_ASSUMPTIONS",
    "MAX_PD_EVIDENCE_QUESTIONS",
    "MAX_PD_BINDINGS",
    "MAX_PD_BLOCKERS",
    "MAX_PD_HUMAN_OPTIONS",
    "MAX_PD_RATIONALE_CHARS",
    "MAX_PD_REASON_REFS",
    "MAX_PD_REPLAN_TRIGGERS",
    "MAX_PD_UNCERTAINTIES",
    "MAX_PD_WAIT_REFS",
    "MAX_PLANNING_REF_ID",
    "MIN_PD_EVIDENCE_QUESTIONS",
    "MAX_SUBJECT_KEY_CHARS",
    "NoChangeDecision",
    "PAYLOAD_BY_DECISION_TYPE",
    "PLANNING_DECISION_CODEC_VERSION",
    "PLANNING_DECISION_SCHEMA_VERSION",
    "PLANNING_DECISION_V1",
    "PlanningDecisionEnvelopeV1",
    "PlanningDecisionRejectionCode",
    "PlanningDecisionStatus",
    "PlanningDecisionType",
    "PlanningFeedbackV1",
    "PlanningProblemDetailV1",
    "PlanningRefKind",
    "PlanningRefV1",
    "PlanningRequestBinding",
    "PlanningRetryBudgetView",
    "PlanningUncertaintyV1",
    "ProposeMethodDecision",
    "REPAIR_PAYLOAD_BY_KIND",
    "RefineDecision",
    "ReplanTriggerHintV1",
    "RepairKind",
    "RepairProposeSuccessorDecision",
    "RepairReplaceMethodDecision",
    "RequestEvidenceDecision",
    "RequestHumanDecision",
    "ResumableIf",
    "UncertaintySeverity",
    "VersionedTypeRefV1",
    "WaitDecision",
    "canonical_decision_hash",
    "canonical_decision_json",
    "compute_decision_id",
)
