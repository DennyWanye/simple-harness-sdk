# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planning-decision protocol core (HTN-LLM-NATIVE §11–§17, §33–§40).

H1 makes "what the planner suggests the system should do next" a single wire
object, the ``PlanningDecisionEnvelopeV1``.  This module holds the *core* of that
protocol and nothing else:

* the decision limits of §16;
* the five closed enums of §11, §17, §33 and §36, with the exact V2 values;
* the H1 slice of the §12 phase-enablement matrix;
* the strict ``to_json`` / ``from_json`` dataclasses that are already fully
  specified — references, the request binding, feedback, the problem detail and
  the retry-budget view;
* the deterministic ``decision_id`` of §35.

The envelope, the per-type payload variants, the JSON Schema file and the golden
fixture directories are deliberately absent: they still wait on the plan author
(BL-1…BL-6).  Everything here is data; validated in ``__post_init__`` and never
executed.  Unknown keys, missing keys and wrongly-typed values raise
:class:`~.models.ContractError`, exactly as the rest of the contract package does.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .models import ContractError
from .semantic_base import (
    enum_of,
    fields_of,
    flag,
    hash_hex,
    identifier,
    index,
    optional_index,
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

MAX_PLANNING_REF_ID = 256


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
    """§17: the fifteen kinds a planning reference may carry.

    ``fact`` does not exist here — a planning fact is ``observation``.  The H1
    ``method_instance`` kind is *not* added in this slice; it waits on the plan
    author (BL-1) so the wire contract is never invented ahead of the ruling.
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
    """§20: an assumption never upgrades itself to TRUE; it carries a risk band."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


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
    "AssumptionRisk",
    "DecisionEnablement",
    "H1_DECISION_ENABLEMENT",
    "LEGACY_PLANNING_PROTOCOL",
    "MAX_PD_ALTERNATIVES",
    "MAX_PD_ARGUMENTS",
    "MAX_PD_ASSUMPTIONS",
    "MAX_PD_BINDINGS",
    "MAX_PD_BLOCKERS",
    "MAX_PD_HUMAN_OPTIONS",
    "MAX_PD_RATIONALE_CHARS",
    "MAX_PD_REASON_REFS",
    "MAX_PD_REPLAN_TRIGGERS",
    "MAX_PD_UNCERTAINTIES",
    "MAX_PD_WAIT_REFS",
    "PLANNING_DECISION_CODEC_VERSION",
    "PLANNING_DECISION_SCHEMA_VERSION",
    "PLANNING_DECISION_V1",
    "PlanningDecisionRejectionCode",
    "PlanningDecisionStatus",
    "PlanningDecisionType",
    "PlanningFeedbackV1",
    "PlanningProblemDetailV1",
    "PlanningRefKind",
    "PlanningRefV1",
    "PlanningRequestBinding",
    "PlanningRetryBudgetView",
    "compute_decision_id",
)
