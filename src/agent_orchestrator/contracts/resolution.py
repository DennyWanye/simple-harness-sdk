# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Requirements, review, acceptance and operation contracts (§13, §14.3, AER §4–6, §12).

AER §6.1 keeps four facts apart, and so does this module — there is no type here
that lets one stand in for another:

``ReviewRecord``
    who judged which candidate, and what they found, criterion by criterion.
``Acceptance``
    a Commit accepted one contribution under stated requirements and inputs.
``GoalResolution``
    a Task / Obligation is satisfied as a whole.
``DeliveryReceipt``
    how far the output actually travelled (persisted / enqueued / sent / confirmed).

The success expression is a restricted data AST — ``criterion(id)`` / ``all`` /
``any`` with non-empty children — and this module only checks its *structure*
(§25.1 decision 2).  Evaluating it is P1.1b's ``verification.acceptance_rules``;
nothing here computes a verdict, so no one can accidentally accept a goal by
importing a contract module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NewType, TypeAlias

from .evidence_state import (
    PreconditionPhase,
    QueryCompleteness,
    TemporalUse,
    Validity,
    limitation_text,
    parse_phase,
)
from .htn import GoalSignature, MethodInstanceId, ObligationId, TaskRef
from .models import ContractError
from .semantic_base import (
    MAX_REASON,
    EvidenceRef,
    TypedRef,
    content_hash_of,
    enum_of,
    fields_of,
    flag,
    hash_hex,
    identifier,
    identifiers,
    index,
    optional_hash_hex,
    optional_identifier,
    optional_index,
    reject_executable,
    schema_version,
    sequence_of,
    text,
)

GOAL_RESOLUTION_SCHEMA_VERSION = 1
REVIEW_RECORD_SCHEMA_VERSION = 1
OPERATION_ENVELOPE_SCHEMA_VERSION = 1
RECONCILIATION_RESULT_SCHEMA_VERSION = 1

#: A success expression that needs more nodes than this is refused, not walked.
MAX_SUCCESS_EXPRESSION_NODES = 512

OperationId = NewType("OperationId", str)
OperationOccurrenceId = NewType("OperationOccurrenceId", str)
RequirementsRevisionId = NewType("RequirementsRevisionId", str)
ReviewPackageId = NewType("ReviewPackageId", str)
ReviewRecordId = NewType("ReviewRecordId", str)
AcceptanceId = NewType("AcceptanceId", str)
GoalResolutionId = NewType("GoalResolutionId", str)


# --------------------------------------------------------------------------------------
# Requirements and criteria (AER §4.1)
# --------------------------------------------------------------------------------------


class CriterionOrigin(StrEnum):
    """Where a criterion came from.  A derived one never impersonates the user."""

    USER_EXPLICIT = "USER_EXPLICIT"
    POLICY_REQUIRED = "POLICY_REQUIRED"
    DERIVED = "DERIVED"


class RequirementClass(StrEnum):
    HARD_CONSTRAINT = "HARD_CONSTRAINT"
    REQUIRED_OUTCOME = "REQUIRED_OUTCOME"
    PREFERENCE = "PREFERENCE"


class EvaluationKind(StrEnum):
    SEMANTIC = "SEMANTIC"
    EXECUTION_RECEIPT = "EXECUTION_RECEIPT"
    DETERMINISTIC = "DETERMINISTIC"
    FORMAL = "FORMAL"


@dataclass(frozen=True, slots=True)
class RequiredEvidencePolicy:
    """AER §4.1: named checks with versions and coverage — not a fuzzy score."""

    required_check_ids: tuple[str, ...] = ()
    independence_required: bool = False
    coverage_statement: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_check_ids",
            identifiers(self.required_check_ids, "evidence_policy.required_check_ids"),
        )
        object.__setattr__(
            self,
            "independence_required",
            flag(self.independence_required, "evidence_policy.independence_required"),
        )
        if self.coverage_statement is not None:
            object.__setattr__(
                self,
                "coverage_statement",
                text(self.coverage_statement, "evidence_policy.coverage_statement"),
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "required_check_ids": list(self.required_check_ids),
            "independence_required": self.independence_required,
            "coverage_statement": self.coverage_statement,
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "required_evidence_policy"
    ) -> RequiredEvidencePolicy:
        data = fields_of(
            value,
            name,
            required=(),
            optional=("required_check_ids", "independence_required", "coverage_statement"),
        )
        return cls(
            required_check_ids=tuple(data.get("required_check_ids", ())),
            independence_required=data.get("independence_required", False),
            coverage_statement=data.get("coverage_statement"),
        )


@dataclass(frozen=True, slots=True)
class AmendmentPolicy:
    """AER §4.1: who may change this criterion, and whether it may be waived.

    A safety floor is never model-exemptible; the flag is data so the rule can be
    checked rather than remembered.
    """

    amendable_by: tuple[str, ...] = ()
    model_exemptible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "amendable_by", identifiers(self.amendable_by, "amendment_policy.amendable_by")
        )
        object.__setattr__(
            self,
            "model_exemptible",
            flag(self.model_exemptible, "amendment_policy.model_exemptible"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "amendable_by": list(self.amendable_by),
            "model_exemptible": self.model_exemptible,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "amendment_policy") -> AmendmentPolicy:
        data = fields_of(value, name, required=(), optional=("amendable_by", "model_exemptible"))
        return cls(
            amendable_by=tuple(data.get("amendable_by", ())),
            model_exemptible=data.get("model_exemptible", False),
        )


@dataclass(frozen=True, slots=True)
class Criterion:
    """AER §4.1: one traceable requirement with its class, evidence and phase."""

    criterion_id: str
    revision: int
    origin: CriterionOrigin
    statement: str
    requirement_class: RequirementClass
    evaluation_kind: EvaluationKind
    source_ref: TypedRef | None = None
    scope: str | None = None
    required_evidence_policy: RequiredEvidencePolicy = field(default_factory=RequiredEvidencePolicy)
    phase: PreconditionPhase | None = None
    temporal_use: TemporalUse = TemporalUse.CURRENT_AT_USE
    amendment_policy: AmendmentPolicy = field(default_factory=AmendmentPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", identifier(self.criterion_id, "criterion.criterion_id")
        )
        object.__setattr__(self, "revision", index(self.revision, "criterion.revision"))
        object.__setattr__(
            self, "origin", enum_of(CriterionOrigin, self.origin, "criterion.origin")
        )
        object.__setattr__(self, "statement", text(self.statement, "criterion.statement"))
        object.__setattr__(
            self,
            "requirement_class",
            enum_of(RequirementClass, self.requirement_class, "criterion.requirement_class"),
        )
        object.__setattr__(
            self,
            "evaluation_kind",
            enum_of(EvaluationKind, self.evaluation_kind, "criterion.evaluation_kind"),
        )
        if self.source_ref is not None and not isinstance(self.source_ref, TypedRef):
            raise ContractError("criterion.source_ref must be a TypedRef or null")
        object.__setattr__(self, "scope", optional_identifier(self.scope, "criterion.scope"))
        if not isinstance(self.required_evidence_policy, RequiredEvidencePolicy):
            raise ContractError("criterion.required_evidence_policy has the wrong type")
        if self.phase is not None:
            object.__setattr__(self, "phase", parse_phase(self.phase, "criterion.phase"))
        object.__setattr__(
            self, "temporal_use", enum_of(TemporalUse, self.temporal_use, "criterion.temporal_use")
        )
        if not isinstance(self.amendment_policy, AmendmentPolicy):
            raise ContractError("criterion.amendment_policy has the wrong type")
        if (
            self.requirement_class is RequirementClass.HARD_CONSTRAINT
            and self.amendment_policy.model_exemptible
        ):
            raise ContractError("a HARD_CONSTRAINT may not be declared model-exemptible (AER §4.1)")

    @property
    def is_required(self) -> bool:
        return self.requirement_class is not RequirementClass.PREFERENCE

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "revision": self.revision,
            "origin": str(self.origin),
            "statement": self.statement,
            "requirement_class": str(self.requirement_class),
            "evaluation_kind": str(self.evaluation_kind),
            "source_ref": None if self.source_ref is None else self.source_ref.to_json(),
            "scope": self.scope,
            "required_evidence_policy": self.required_evidence_policy.to_json(),
            "phase": None if self.phase is None else str(self.phase),
            "temporal_use": str(self.temporal_use),
            "amendment_policy": self.amendment_policy.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "criterion") -> Criterion:
        data = fields_of(
            value,
            name,
            required=(
                "criterion_id",
                "revision",
                "origin",
                "statement",
                "requirement_class",
                "evaluation_kind",
            ),
            optional=(
                "source_ref",
                "scope",
                "required_evidence_policy",
                "phase",
                "temporal_use",
                "amendment_policy",
            ),
        )
        raw_source = data.get("source_ref")
        return cls(
            criterion_id=data["criterion_id"],
            revision=data["revision"],
            origin=data["origin"],
            statement=data["statement"],
            requirement_class=data["requirement_class"],
            evaluation_kind=data["evaluation_kind"],
            source_ref=(
                None if raw_source is None else TypedRef.from_json(raw_source, f"{name}.source_ref")
            ),
            scope=data.get("scope"),
            required_evidence_policy=RequiredEvidencePolicy.from_json(
                data.get("required_evidence_policy", {}), f"{name}.required_evidence_policy"
            ),
            phase=data.get("phase"),
            temporal_use=data.get("temporal_use", TemporalUse.CURRENT_AT_USE),
            amendment_policy=AmendmentPolicy.from_json(
                data.get("amendment_policy", {}), f"{name}.amendment_policy"
            ),
        )


# --------------------------------------------------------------------------------------
# Success expression: restricted data AST, structure only (§25.1 decision 2)
# --------------------------------------------------------------------------------------

SuccessExpression: TypeAlias = "CriterionExpr | AllExpr | AnyExpr"


@dataclass(frozen=True, slots=True)
class CriterionExpr:
    criterion_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", identifier(self.criterion_id, "expression.criterion_id")
        )

    def to_json(self) -> dict[str, Any]:
        return {"op": "criterion", "criterion_id": self.criterion_id}


@dataclass(frozen=True, slots=True)
class AllExpr:
    children: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ContractError("expression.all children must not be empty (AER §4.1)")

    def to_json(self) -> dict[str, Any]:
        return {"op": "all", "children": [child.to_json() for child in self.children]}


@dataclass(frozen=True, slots=True)
class AnyExpr:
    children: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ContractError("expression.any children must not be empty (AER §4.1)")

    def to_json(self) -> dict[str, Any]:
        return {"op": "any", "children": [child.to_json() for child in self.children]}


class _ExprBudget:
    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self, name: str) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ContractError(
                f"{name} exceeds the {MAX_SUCCESS_EXPRESSION_NODES}-node expression budget"
            )


def parse_success_expression(
    value: object, name: str = "success_expression", budget: _ExprBudget | None = None
) -> SuccessExpression:
    """Decode the restricted success AST.  Only three node shapes exist."""

    budget = budget if budget is not None else _ExprBudget(MAX_SUCCESS_EXPRESSION_NODES)
    budget.spend(name)
    reject_executable(value, name)
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a structured expression object")
    op = value.get("op")
    if op == "criterion":
        data = fields_of(value, name, required=("op", "criterion_id"))
        return CriterionExpr(criterion_id=data["criterion_id"])
    if op in {"all", "any"}:
        data = fields_of(value, name, required=("op", "children"))
        children = sequence_of(
            data["children"],
            f"{name}.children",
            lambda item, where: parse_success_expression(item, where, budget),
            minimum=1,
        )
        return AllExpr(children=children) if op == "all" else AnyExpr(children=children)
    raise ContractError(f"{name}.op must be one of ['all', 'any', 'criterion']")


def expression_criterion_ids(expression: Any) -> tuple[str, ...]:
    if isinstance(expression, CriterionExpr):
        return (expression.criterion_id,)
    collected: list[str] = []
    for child in expression.children:
        collected.extend(expression_criterion_ids(child))
    return tuple(collected)


def hard_constraints_not_independent(
    expression: Any, criteria: Sequence[Criterion]
) -> tuple[str, ...]:
    """§25.1 decision 2 / AER §4.1: every hard constraint is an independent AND.

    A hard constraint that sits inside an ``any`` branch could be traded away by
    satisfying the other branch; only the result part of an expression may carry
    an OR, and only where the original text approved one.  Returns the offending
    criterion ids — this is a structural check, not an evaluation.
    """

    hard = {
        criterion.criterion_id
        for criterion in criteria
        if criterion.requirement_class is RequirementClass.HARD_CONSTRAINT
    }
    if not hard:
        return ()
    offending: list[str] = []

    def walk(node: Any, *, under_any: bool) -> None:
        if isinstance(node, CriterionExpr):
            if under_any and node.criterion_id in hard:
                offending.append(node.criterion_id)
            return
        beneath_any = under_any or isinstance(node, AnyExpr)
        for child in node.children:
            walk(child, under_any=beneath_any)

    walk(expression, under_any=False)
    missing = sorted(hard - set(expression_criterion_ids(expression)))
    return tuple(sorted(set(offending)) + missing)


def unknown_expression_criteria(expression: Any, criteria: Sequence[Criterion]) -> tuple[str, ...]:
    catalogue = {criterion.criterion_id for criterion in criteria}
    return tuple(
        sorted({item for item in expression_criterion_ids(expression) if item not in catalogue})
    )


@dataclass(frozen=True, slots=True)
class RequirementsRevision:
    """AER §4.1: one immutable version of what the user asked for."""

    revision_id: RequirementsRevisionId
    mission_id: str
    revision: int
    criteria: tuple[Criterion, ...]
    success_expression: Any
    source_text_ref: TypedRef | None = None
    authority_subject: str | None = None
    interpretation_scope: str | None = None
    delivery_contract_ref: str | None = None
    amendment_credential_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "revision_id",
            RequirementsRevisionId(identifier(self.revision_id, "requirements.revision_id")),
        )
        object.__setattr__(
            self, "mission_id", identifier(self.mission_id, "requirements.mission_id")
        )
        object.__setattr__(self, "revision", index(self.revision, "requirements.revision"))
        if not self.criteria:
            raise ContractError("requirements.criteria must not be empty")
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(set(ids)) != len(ids):
            raise ContractError("requirements.criteria must not repeat a criterion_id")
        unknown = unknown_expression_criteria(self.success_expression, self.criteria)
        if unknown:
            raise ContractError(
                f"requirements.success_expression names criteria outside the catalogue: "
                f"{list(unknown)}"
            )
        violations = hard_constraints_not_independent(self.success_expression, self.criteria)
        if violations:
            raise ContractError(
                "every HARD_CONSTRAINT must be an independent AND conjunct of the success "
                f"expression; these are not: {list(violations)}"
            )
        if self.source_text_ref is not None and not isinstance(self.source_text_ref, TypedRef):
            raise ContractError("requirements.source_text_ref must be a TypedRef or null")
        for name in (
            "authority_subject",
            "interpretation_scope",
            "delivery_contract_ref",
            "amendment_credential_ref",
        ):
            object.__setattr__(
                self, name, optional_identifier(getattr(self, name), f"requirements.{name}")
            )

    def required_criterion_ids(self) -> tuple[str, ...]:
        return tuple(criterion.criterion_id for criterion in self.criteria if criterion.is_required)

    def to_json(self) -> dict[str, Any]:
        return {
            "revision_id": str(self.revision_id),
            "mission_id": self.mission_id,
            "revision": self.revision,
            "criteria": [criterion.to_json() for criterion in self.criteria],
            "success_expression": self.success_expression.to_json(),
            "source_text_ref": (
                None if self.source_text_ref is None else self.source_text_ref.to_json()
            ),
            "authority_subject": self.authority_subject,
            "interpretation_scope": self.interpretation_scope,
            "delivery_contract_ref": self.delivery_contract_ref,
            "amendment_credential_ref": self.amendment_credential_ref,
        }

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    @classmethod
    def from_json(cls, value: object, name: str = "requirements_revision") -> RequirementsRevision:
        data = fields_of(
            value,
            name,
            required=("revision_id", "mission_id", "revision", "criteria", "success_expression"),
            optional=(
                "source_text_ref",
                "authority_subject",
                "interpretation_scope",
                "delivery_contract_ref",
                "amendment_credential_ref",
            ),
        )
        raw_source = data.get("source_text_ref")
        return cls(
            revision_id=RequirementsRevisionId(data["revision_id"]),
            mission_id=data["mission_id"],
            revision=data["revision"],
            criteria=sequence_of(
                data["criteria"],
                f"{name}.criteria",
                lambda item, where: Criterion.from_json(item, where),
                minimum=1,
            ),
            success_expression=parse_success_expression(
                data["success_expression"], f"{name}.success_expression"
            ),
            source_text_ref=(
                None
                if raw_source is None
                else TypedRef.from_json(raw_source, f"{name}.source_text_ref")
            ),
            authority_subject=data.get("authority_subject"),
            interpretation_scope=data.get("interpretation_scope"),
            delivery_contract_ref=data.get("delivery_contract_ref"),
            amendment_credential_ref=data.get("amendment_credential_ref"),
        )


# --------------------------------------------------------------------------------------
# Review (AER §5, §13 v1.4)
# --------------------------------------------------------------------------------------


class ReviewPurpose(StrEnum):
    """§13 v1.4: the six review purposes.  Each binds to its own account."""

    TASK_CONTENT = "TASK_CONTENT"
    METHOD_PLAN = "METHOD_PLAN"
    COMPOSITION = "COMPOSITION"
    ACTION_PROPOSAL = "ACTION_PROPOSAL"
    OPERATION_OUTCOME = "OPERATION_OUTCOME"
    MISSION_FINAL = "MISSION_FINAL"


class ReviewAccount(StrEnum):
    """Which budget account a review's cost lands on (§13 v1.4, §21.5 conservation)."""

    TASK = "task"
    MISSION_PLANNING = "mission_planning"
    PARENT_COMPOUND_TASK = "parent_compound_task"
    OPERATION_TASK = "operation_task"
    MISSION = "mission"


#: §13 v1.4: total, so no purpose can quietly escape budget accounting.
REVIEW_PURPOSE_ACCOUNTS: Mapping[ReviewPurpose, ReviewAccount] = {
    ReviewPurpose.TASK_CONTENT: ReviewAccount.TASK,
    ReviewPurpose.METHOD_PLAN: ReviewAccount.MISSION_PLANNING,
    ReviewPurpose.COMPOSITION: ReviewAccount.PARENT_COMPOUND_TASK,
    ReviewPurpose.ACTION_PROPOSAL: ReviewAccount.OPERATION_TASK,
    ReviewPurpose.OPERATION_OUTCOME: ReviewAccount.OPERATION_TASK,
    ReviewPurpose.MISSION_FINAL: ReviewAccount.MISSION,
}


def account_for_purpose(purpose: ReviewPurpose) -> ReviewAccount:
    resolved = enum_of(ReviewPurpose, purpose, "review.purpose")
    return REVIEW_PURPOSE_ACCOUNTS[resolved]


class WorkspaceAccess(StrEnum):
    """What the reviewer may do to the candidate's workspace (AER §5.3).

    The independence floor is that a reviewer cannot edit the thing it is judging,
    so ``WRITE`` is refused at construction rather than recorded as a fact about a
    review that should never have been packaged.
    """

    NONE = "none"
    READ_ONLY = "read_only"
    WRITE = "write"


class CriterionVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class CheckExecution(StrEnum):
    """AER §4.3: how the check *ran*, recorded apart from what it concluded.

    An infrastructure ERROR is not evidence against the content (invariant I07).
    """

    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class ReviewVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    REWORK = "REWORK"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ReviewBinding:
    """``review-record.schema.json`` ``binding``: what exactly was reviewed."""

    mission_id: str
    obligation_id: str
    subject_ref: TypedRef
    requirements_revision: int
    input_manifest_hash: str
    policy_ref: TypedRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "binding.mission_id"))
        object.__setattr__(
            self, "obligation_id", identifier(self.obligation_id, "binding.obligation_id")
        )
        for name in ("subject_ref", "policy_ref"):
            if not isinstance(getattr(self, name), TypedRef):
                raise ContractError(f"binding.{name} must be a TypedRef")
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "binding.requirements_revision"),
        )
        object.__setattr__(
            self,
            "input_manifest_hash",
            hash_hex(self.input_manifest_hash, "binding.input_manifest_hash"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "obligation_id": self.obligation_id,
            "subject_ref": self.subject_ref.to_json(),
            "requirements_revision": self.requirements_revision,
            "input_manifest_hash": self.input_manifest_hash,
            "policy_ref": self.policy_ref.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "binding") -> ReviewBinding:
        data = fields_of(
            value,
            name,
            required=(
                "mission_id",
                "obligation_id",
                "subject_ref",
                "requirements_revision",
                "input_manifest_hash",
                "policy_ref",
            ),
        )
        return cls(
            mission_id=data["mission_id"],
            obligation_id=data["obligation_id"],
            subject_ref=TypedRef.from_json(data["subject_ref"], f"{name}.subject_ref"),
            requirements_revision=data["requirements_revision"],
            input_manifest_hash=data["input_manifest_hash"],
            policy_ref=TypedRef.from_json(data["policy_ref"], f"{name}.policy_ref"),
        )


@dataclass(frozen=True, slots=True)
class CriterionOutcome:
    """One criterion's verdict, with the execution status kept on its own axis."""

    criterion_id: str
    verdict: CriterionVerdict
    check_execution: CheckExecution
    evidence_refs: tuple[TypedRef, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", identifier(self.criterion_id, "outcome.criterion_id")
        )
        object.__setattr__(
            self, "verdict", enum_of(CriterionVerdict, self.verdict, "outcome.verdict")
        )
        object.__setattr__(
            self,
            "check_execution",
            enum_of(CheckExecution, self.check_execution, "outcome.check_execution"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            sequence_of(
                self.evidence_refs,
                "outcome.evidence_refs",
                lambda item, where: (
                    item if isinstance(item, TypedRef) else TypedRef.from_json(item, where)
                ),
            ),
        )
        object.__setattr__(
            self,
            "limitations",
            sequence_of(
                self.limitations,
                "outcome.limitations",
                lambda item, where: limitation_text(item, where),
            ),
        )
        if self.verdict is CriterionVerdict.PASS and self.check_execution in {
            CheckExecution.NOT_RUN,
            CheckExecution.ERROR,
            CheckExecution.CANCELLED,
        }:
            raise ContractError(
                "invariant I07: a required check that did not run or errored is not a PASS"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "verdict": str(self.verdict),
            "check_execution": str(self.check_execution),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "criterion_outcome") -> CriterionOutcome:
        data = fields_of(
            value,
            name,
            required=(
                "criterion_id",
                "verdict",
                "check_execution",
                "evidence_refs",
                "limitations",
            ),
        )
        return cls(
            criterion_id=data["criterion_id"],
            verdict=data["verdict"],
            check_execution=data["check_execution"],
            evidence_refs=sequence_of(
                data["evidence_refs"],
                f"{name}.evidence_refs",
                lambda item, where: TypedRef.from_json(item, where),
            ),
            limitations=sequence_of(
                data["limitations"],
                f"{name}.limitations",
                lambda item, where: limitation_text(item, where),
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewPackage:
    """AER §5.2: the immutable anchor a review is bound to.

    The system fills identity, authority, account and hashes; a worker may only
    contribute references to its own candidate.  Extra evidence gathered during
    the review is appended as new records, never by rewriting this package.
    """

    package_id: ReviewPackageId
    purpose: ReviewPurpose
    binding: ReviewBinding
    criteria: tuple[Criterion, ...]
    success_expression: Any
    candidate_refs: tuple[TypedRef, ...] = ()
    child_acceptance_refs: tuple[TypedRef, ...] = ()
    defect_history_refs: tuple[TypedRef, ...] = ()
    counter_evidence_refs: tuple[TypedRef, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()
    independence_policy_ref: str | None = None
    method_instance_id: MethodInstanceId | None = None
    review_budget_ref: str | None = None
    #: The agents that produced the candidate.  Recorded on the package so the
    #: independence check reads one frozen fact instead of re-deriving authorship
    #: from whatever the record happens to mention (AER §5.3).
    producer_agent_ids: tuple[str, ...] = ()
    reviewer_workspace_access: WorkspaceAccess = WorkspaceAccess.READ_ONLY
    #: Binds the package to the exact requirements text it was cut from, so a later
    #: requirements revision cannot be read back through this anchor (AER §3.2).
    requirements_content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "package_id",
            ReviewPackageId(identifier(self.package_id, "package.package_id")),
        )
        object.__setattr__(self, "purpose", enum_of(ReviewPurpose, self.purpose, "package.purpose"))
        if not isinstance(self.binding, ReviewBinding):
            raise ContractError("package.binding must be a ReviewBinding")
        if not self.criteria:
            raise ContractError("package.criteria must not be empty")
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(set(ids)) != len(ids):
            raise ContractError("package.criteria must not repeat a criterion_id")
        unknown = unknown_expression_criteria(self.success_expression, self.criteria)
        if unknown:
            raise ContractError(
                f"package.success_expression names criteria outside its catalogue: {list(unknown)}"
            )
        object.__setattr__(
            self,
            "allowed_capabilities",
            identifiers(self.allowed_capabilities, "package.allowed_capabilities"),
        )
        object.__setattr__(
            self,
            "producer_agent_ids",
            identifiers(self.producer_agent_ids, "package.producer_agent_ids"),
        )
        object.__setattr__(
            self,
            "reviewer_workspace_access",
            enum_of(
                WorkspaceAccess,
                self.reviewer_workspace_access,
                "package.reviewer_workspace_access",
            ),
        )
        if self.reviewer_workspace_access is WorkspaceAccess.WRITE:
            raise ContractError(
                "a reviewer may not hold write access to the candidate workspace; "
                "it cannot approve a version it edited itself (AER §5.3)"
            )
        object.__setattr__(
            self,
            "requirements_content_hash",
            optional_hash_hex(self.requirements_content_hash, "package.requirements_content_hash"),
        )
        if self.requirements_content_hash is not None:
            # A package that claims to mirror a specific requirements revision must
            # mirror its structure too: the same rule RequirementsRevision enforces,
            # so a hard constraint cannot become a tradeable OR branch on the way
            # into a review (§25.1 #2).  A package that makes no such claim is still
            # constructible — that is the shape a *tampered* anchor takes, and
            # catching it is the requirements-digest check's job, not the codec's.
            violations = self.hard_constraint_violations()
            if violations:
                raise ContractError(
                    "a package bound to a requirements revision must keep every "
                    "HARD_CONSTRAINT an independent AND conjunct; these are not: "
                    f"{list(violations)}"
                )
        for name in ("independence_policy_ref", "review_budget_ref"):
            object.__setattr__(
                self, name, optional_identifier(getattr(self, name), f"package.{name}")
            )
        if self.method_instance_id is not None:
            object.__setattr__(
                self,
                "method_instance_id",
                MethodInstanceId(identifier(self.method_instance_id, "package.method_instance_id")),
            )

    @property
    def account(self) -> ReviewAccount:
        return account_for_purpose(self.purpose)

    def criterion_catalogue(self) -> tuple[str, ...]:
        return tuple(criterion.criterion_id for criterion in self.criteria)

    def hard_constraint_violations(self) -> tuple[str, ...]:
        """Hard constraints this package's expression fails to hold independently.

        Exposed so a caller that has the requirements in hand can refuse the anchor
        outright; the package itself only enforces this when it declares which
        requirements revision it mirrors.
        """

        return hard_constraints_not_independent(self.success_expression, self.criteria)

    def criteria_only_under_any(self) -> frozenset[str]:
        """Criteria a reviewer may leave unevaluated by taking the other OR branch."""

        return criteria_only_under_any(self.success_expression)

    def produced_by(self, agent_id: str) -> bool:
        """Whether this agent produced the candidate — the independence floor."""

        return agent_id in self.producer_agent_ids

    def to_json(self) -> dict[str, Any]:
        return {
            "package_id": str(self.package_id),
            "purpose": str(self.purpose),
            "binding": self.binding.to_json(),
            "criteria": [criterion.to_json() for criterion in self.criteria],
            "success_expression": self.success_expression.to_json(),
            "candidate_refs": [ref.to_json() for ref in self.candidate_refs],
            "child_acceptance_refs": [ref.to_json() for ref in self.child_acceptance_refs],
            "defect_history_refs": [ref.to_json() for ref in self.defect_history_refs],
            "counter_evidence_refs": [ref.to_json() for ref in self.counter_evidence_refs],
            "allowed_capabilities": list(self.allowed_capabilities),
            "producer_agent_ids": list(self.producer_agent_ids),
            "reviewer_workspace_access": str(self.reviewer_workspace_access),
            "requirements_content_hash": self.requirements_content_hash,
            "independence_policy_ref": self.independence_policy_ref,
            "method_instance_id": (
                None if self.method_instance_id is None else str(self.method_instance_id)
            ),
            "review_budget_ref": self.review_budget_ref,
        }

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    @classmethod
    def from_json(cls, value: object, name: str = "review_package") -> ReviewPackage:
        data = fields_of(
            value,
            name,
            required=("package_id", "purpose", "binding", "criteria", "success_expression"),
            optional=(
                "candidate_refs",
                "child_acceptance_refs",
                "defect_history_refs",
                "counter_evidence_refs",
                "allowed_capabilities",
                "producer_agent_ids",
                "reviewer_workspace_access",
                "requirements_content_hash",
                "independence_policy_ref",
                "method_instance_id",
                "review_budget_ref",
            ),
        )

        def refs(raw: object, where: str) -> tuple[TypedRef, ...]:
            return sequence_of(raw, where, lambda item, at: TypedRef.from_json(item, at))

        return cls(
            package_id=ReviewPackageId(data["package_id"]),
            purpose=data["purpose"],
            binding=ReviewBinding.from_json(data["binding"], f"{name}.binding"),
            criteria=sequence_of(
                data["criteria"],
                f"{name}.criteria",
                lambda item, where: Criterion.from_json(item, where),
                minimum=1,
            ),
            success_expression=parse_success_expression(
                data["success_expression"], f"{name}.success_expression"
            ),
            candidate_refs=refs(data.get("candidate_refs", ()), f"{name}.candidate_refs"),
            child_acceptance_refs=refs(
                data.get("child_acceptance_refs", ()), f"{name}.child_acceptance_refs"
            ),
            defect_history_refs=refs(
                data.get("defect_history_refs", ()), f"{name}.defect_history_refs"
            ),
            counter_evidence_refs=refs(
                data.get("counter_evidence_refs", ()), f"{name}.counter_evidence_refs"
            ),
            allowed_capabilities=tuple(data.get("allowed_capabilities", ())),
            producer_agent_ids=tuple(data.get("producer_agent_ids", ())),
            reviewer_workspace_access=data.get(
                "reviewer_workspace_access", WorkspaceAccess.READ_ONLY
            ),
            requirements_content_hash=data.get("requirements_content_hash"),
            independence_policy_ref=data.get("independence_policy_ref"),
            method_instance_id=(
                None
                if data.get("method_instance_id") is None
                else MethodInstanceId(data["method_instance_id"])
            ),
            review_budget_ref=data.get("review_budget_ref"),
        )


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """``review-record.schema.json``: what a reviewer concluded, criterion by criterion.

    A record is a *judgement*, not an acceptance (invariants I01, I02).
    """

    record_id: ReviewRecordId
    package_id: ReviewPackageId
    purpose: ReviewPurpose
    binding: ReviewBinding
    reviewer_agent_id: str
    reviewer_turn_id: str
    evidence_manifest_hash: str
    criteria: tuple[CriterionOutcome, ...]
    verdict: ReviewVerdict

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", ReviewRecordId(identifier(self.record_id, "record.record_id"))
        )
        object.__setattr__(
            self, "package_id", ReviewPackageId(identifier(self.package_id, "record.package_id"))
        )
        object.__setattr__(self, "purpose", enum_of(ReviewPurpose, self.purpose, "record.purpose"))
        if not isinstance(self.binding, ReviewBinding):
            raise ContractError("record.binding must be a ReviewBinding")
        object.__setattr__(
            self,
            "reviewer_agent_id",
            identifier(self.reviewer_agent_id, "record.reviewer_agent_id"),
        )
        object.__setattr__(
            self, "reviewer_turn_id", identifier(self.reviewer_turn_id, "record.reviewer_turn_id")
        )
        object.__setattr__(
            self,
            "evidence_manifest_hash",
            hash_hex(self.evidence_manifest_hash, "record.evidence_manifest_hash"),
        )
        if not self.criteria:
            raise ContractError("record.criteria must not be empty")
        ids = [outcome.criterion_id for outcome in self.criteria]
        if len(set(ids)) != len(ids):
            raise ContractError("record.criteria must not repeat a criterion_id (AER §5.4)")
        object.__setattr__(self, "verdict", enum_of(ReviewVerdict, self.verdict, "record.verdict"))

    @property
    def account(self) -> ReviewAccount:
        return account_for_purpose(self.purpose)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_RECORD_SCHEMA_VERSION,
            "record_id": str(self.record_id),
            "package_id": str(self.package_id),
            "purpose": str(self.purpose),
            "binding": self.binding.to_json(),
            "reviewer_agent_id": self.reviewer_agent_id,
            "reviewer_turn_id": self.reviewer_turn_id,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "criteria": [outcome.to_json() for outcome in self.criteria],
            "verdict": str(self.verdict),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "review_record") -> ReviewRecord:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "record_id",
                "package_id",
                "purpose",
                "binding",
                "reviewer_agent_id",
                "reviewer_turn_id",
                "evidence_manifest_hash",
                "criteria",
                "verdict",
            ),
        )
        schema_version(
            data["schema_version"], f"{name}.schema_version", expected=REVIEW_RECORD_SCHEMA_VERSION
        )
        return cls(
            record_id=ReviewRecordId(data["record_id"]),
            package_id=ReviewPackageId(data["package_id"]),
            purpose=data["purpose"],
            binding=ReviewBinding.from_json(data["binding"], f"{name}.binding"),
            reviewer_agent_id=data["reviewer_agent_id"],
            reviewer_turn_id=data["reviewer_turn_id"],
            evidence_manifest_hash=data["evidence_manifest_hash"],
            criteria=sequence_of(
                data["criteria"],
                f"{name}.criteria",
                lambda item, where: CriterionOutcome.from_json(item, where),
                minimum=1,
            ),
            verdict=data["verdict"],
        )


@dataclass(frozen=True, slots=True)
class CriterionMatch:
    """AER §5.4: a record's criteria must match the package catalogue one to one."""

    unknown: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return not (self.unknown or self.duplicated or self.missing_required)


def match_review_criteria(
    record: ReviewRecord, package: ReviewPackage, *, allow_unevaluated_or_branches: bool = True
) -> CriterionMatch:
    """Compare a record against its package: unknown, repeated or missing ids.

    Required criteria must all appear.  Criteria that only sit inside an ``any``
    branch may be left unevaluated when the reviewer explicitly chose the other
    branch (AER §5.4), which ``allow_unevaluated_or_branches`` expresses.
    """

    catalogue = package.criterion_catalogue()
    reported = [outcome.criterion_id for outcome in record.criteria]
    unknown = tuple(sorted({item for item in reported if item not in catalogue}))
    duplicated = tuple(sorted({item for item in reported if reported.count(item) > 1}))
    optional_ids: frozenset[str] = frozenset()
    if allow_unevaluated_or_branches:
        optional_ids = criteria_only_under_any(package.success_expression)
    required = {
        criterion.criterion_id
        for criterion in package.criteria
        if criterion.is_required and criterion.criterion_id not in optional_ids
    }
    missing = tuple(sorted(required - set(reported)))
    return CriterionMatch(unknown=unknown, duplicated=duplicated, missing_required=missing)


def criteria_only_under_any(expression: Any) -> frozenset[str]:
    """Criteria that appear *only* inside ``any`` branches of the expression.

    Those are the ones a reviewer may legitimately leave unevaluated by taking the
    other branch (AER §5.4).  A criterion that also appears outside an ``any`` is
    not in this set, however many ``any`` branches it additionally sits in.
    """

    under_any: set[str] = set()
    outside_any: set[str] = set()

    def walk(node: Any, *, beneath: bool) -> None:
        if isinstance(node, CriterionExpr):
            (under_any if beneath else outside_any).add(node.criterion_id)
            return
        deeper = beneath or isinstance(node, AnyExpr)
        for child in node.children:
            walk(child, beneath=deeper)

    walk(expression, beneath=False)
    return frozenset(under_any - outside_any)


# --------------------------------------------------------------------------------------
# Acceptance, GoalResolution, DeliveryReceipt (AER §6.1)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Acceptance:
    """AER §6.1: a Commit accepted one contribution under stated conditions.

    Accepting a report is not authorising an action (invariant I03).
    """

    acceptance_id: AcceptanceId
    mission_id: str
    task_id: TaskRef
    obligation_id: ObligationId
    requirements_revision: int
    contract_revision: int
    input_manifest_hash: str
    review_record_id: ReviewRecordId
    accepted_at_ms: int
    artifact_refs: tuple[EvidenceRef, ...] = ()
    policy_ref: str | None = None
    validity: Validity = Validity.CURRENT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "acceptance_id",
            AcceptanceId(identifier(self.acceptance_id, "acceptance.acceptance_id")),
        )
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "acceptance.mission_id"))
        object.__setattr__(self, "task_id", TaskRef(identifier(self.task_id, "acceptance.task_id")))
        object.__setattr__(
            self,
            "obligation_id",
            ObligationId(identifier(self.obligation_id, "acceptance.obligation_id")),
        )
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "acceptance.requirements_revision"),
        )
        object.__setattr__(
            self, "contract_revision", index(self.contract_revision, "acceptance.contract_revision")
        )
        object.__setattr__(
            self,
            "input_manifest_hash",
            hash_hex(self.input_manifest_hash, "acceptance.input_manifest_hash"),
        )
        object.__setattr__(
            self,
            "review_record_id",
            ReviewRecordId(identifier(self.review_record_id, "acceptance.review_record_id")),
        )
        object.__setattr__(
            self, "accepted_at_ms", index(self.accepted_at_ms, "acceptance.accepted_at_ms")
        )
        object.__setattr__(
            self, "policy_ref", optional_identifier(self.policy_ref, "acceptance.policy_ref")
        )
        object.__setattr__(
            self, "validity", enum_of(Validity, self.validity, "acceptance.validity")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "acceptance_id": str(self.acceptance_id),
            "mission_id": self.mission_id,
            "task_id": str(self.task_id),
            "obligation_id": str(self.obligation_id),
            "requirements_revision": self.requirements_revision,
            "contract_revision": self.contract_revision,
            "input_manifest_hash": self.input_manifest_hash,
            "review_record_id": str(self.review_record_id),
            "accepted_at_ms": self.accepted_at_ms,
            "artifact_refs": [ref.to_json() for ref in self.artifact_refs],
            "policy_ref": self.policy_ref,
            "validity": str(self.validity),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "acceptance") -> Acceptance:
        data = fields_of(
            value,
            name,
            required=(
                "acceptance_id",
                "mission_id",
                "task_id",
                "obligation_id",
                "requirements_revision",
                "contract_revision",
                "input_manifest_hash",
                "review_record_id",
                "accepted_at_ms",
            ),
            optional=("artifact_refs", "policy_ref", "validity"),
        )
        return cls(
            acceptance_id=AcceptanceId(data["acceptance_id"]),
            mission_id=data["mission_id"],
            task_id=TaskRef(data["task_id"]),
            obligation_id=ObligationId(data["obligation_id"]),
            requirements_revision=data["requirements_revision"],
            contract_revision=data["contract_revision"],
            input_manifest_hash=data["input_manifest_hash"],
            review_record_id=ReviewRecordId(data["review_record_id"]),
            accepted_at_ms=data["accepted_at_ms"],
            artifact_refs=sequence_of(
                data.get("artifact_refs", ()),
                f"{name}.artifact_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
            policy_ref=data.get("policy_ref"),
            validity=data.get("validity", Validity.CURRENT),
        )


@dataclass(frozen=True, slots=True)
class ResolutionCriterion:
    """``goal-resolution-v1`` criteria entry (no execution axis on the wire)."""

    criterion_id: str
    verdict: CriterionVerdict
    evidence_refs: tuple[EvidenceRef, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", identifier(self.criterion_id, "resolution.criterion_id")
        )
        object.__setattr__(
            self, "verdict", enum_of(CriterionVerdict, self.verdict, "resolution.verdict")
        )
        object.__setattr__(
            self,
            "limitations",
            sequence_of(
                self.limitations,
                "resolution.limitations",
                lambda item, where: text(item, where, limit=20_000),
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "verdict": str(self.verdict),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "resolution_criterion") -> ResolutionCriterion:
        data = fields_of(
            value, name, required=("criterion_id", "verdict", "evidence_refs", "limitations")
        )
        return cls(
            criterion_id=data["criterion_id"],
            verdict=data["verdict"],
            evidence_refs=sequence_of(
                data["evidence_refs"],
                f"{name}.evidence_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
            limitations=tuple(data["limitations"]),
        )


@dataclass(frozen=True, slots=True)
class GoalResolution:
    """``goal-resolution-v1``: a Task / Obligation is satisfied as a whole.

    ``validity`` is the read-side projection at serialisation time (AER §6.1); it
    does not rewrite the historical acceptance receipts this resolution binds.
    Whether ``verdict`` is *admissible* given the criteria is a domain rule, and
    lives in P1.1b — the codec accepts a structurally valid record either way.
    """

    resolution_id: GoalResolutionId
    mission_id: str
    obligation_id: str
    goal_task_id: str
    requirements_version: int
    contract_revision: int
    method_instance_id: str | None
    input_manifest_hash: str
    artifact_refs: tuple[EvidenceRef, ...]
    child_resolution_ids: tuple[str, ...]
    criteria: tuple[ResolutionCriterion, ...]
    review_receipt_id: str
    verdict: ReviewVerdict
    validity: Validity
    justification_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resolution_id",
            GoalResolutionId(identifier(self.resolution_id, "resolution.resolution_id")),
        )
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "resolution.mission_id"))
        object.__setattr__(
            self, "obligation_id", identifier(self.obligation_id, "resolution.obligation_id")
        )
        object.__setattr__(
            self, "goal_task_id", identifier(self.goal_task_id, "resolution.goal_task_id")
        )
        object.__setattr__(
            self,
            "requirements_version",
            index(self.requirements_version, "resolution.requirements_version"),
        )
        object.__setattr__(
            self, "contract_revision", index(self.contract_revision, "resolution.contract_revision")
        )
        object.__setattr__(
            self,
            "method_instance_id",
            optional_identifier(self.method_instance_id, "resolution.method_instance_id"),
        )
        object.__setattr__(
            self,
            "input_manifest_hash",
            hash_hex(self.input_manifest_hash, "resolution.input_manifest_hash"),
        )
        object.__setattr__(
            self,
            "child_resolution_ids",
            identifiers(self.child_resolution_ids, "resolution.child_resolution_ids"),
        )
        if not self.criteria:
            raise ContractError("resolution.criteria must not be empty")
        ids = [item.criterion_id for item in self.criteria]
        if len(set(ids)) != len(ids):
            raise ContractError("resolution.criteria must not repeat a criterion_id")
        object.__setattr__(
            self,
            "review_receipt_id",
            identifier(self.review_receipt_id, "resolution.review_receipt_id"),
        )
        object.__setattr__(
            self, "verdict", enum_of(ReviewVerdict, self.verdict, "resolution.verdict")
        )
        object.__setattr__(
            self, "validity", enum_of(Validity, self.validity, "resolution.validity")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": GOAL_RESOLUTION_SCHEMA_VERSION,
            "resolution_id": str(self.resolution_id),
            "mission_id": self.mission_id,
            "obligation_id": self.obligation_id,
            "goal_task_id": self.goal_task_id,
            "requirements_version": self.requirements_version,
            "contract_revision": self.contract_revision,
            "method_instance_id": self.method_instance_id,
            "input_manifest_hash": self.input_manifest_hash,
            "artifact_refs": [ref.to_json() for ref in self.artifact_refs],
            "child_resolution_ids": list(self.child_resolution_ids),
            "criteria": [item.to_json() for item in self.criteria],
            "review_receipt_id": self.review_receipt_id,
            "verdict": str(self.verdict),
            "validity": str(self.validity),
            "justification_refs": [ref.to_json() for ref in self.justification_refs],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "goal_resolution") -> GoalResolution:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "resolution_id",
                "mission_id",
                "obligation_id",
                "goal_task_id",
                "requirements_version",
                "contract_revision",
                "method_instance_id",
                "input_manifest_hash",
                "artifact_refs",
                "child_resolution_ids",
                "criteria",
                "review_receipt_id",
                "verdict",
                "validity",
                "justification_refs",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=GOAL_RESOLUTION_SCHEMA_VERSION,
        )

        def refs(raw: object, where: str) -> tuple[EvidenceRef, ...]:
            return sequence_of(raw, where, lambda item, at: EvidenceRef.from_json(item, at))

        return cls(
            resolution_id=GoalResolutionId(data["resolution_id"]),
            mission_id=data["mission_id"],
            obligation_id=data["obligation_id"],
            goal_task_id=data["goal_task_id"],
            requirements_version=data["requirements_version"],
            contract_revision=data["contract_revision"],
            method_instance_id=data["method_instance_id"],
            input_manifest_hash=data["input_manifest_hash"],
            artifact_refs=refs(data["artifact_refs"], f"{name}.artifact_refs"),
            child_resolution_ids=tuple(data["child_resolution_ids"]),
            criteria=sequence_of(
                data["criteria"],
                f"{name}.criteria",
                lambda item, where: ResolutionCriterion.from_json(item, where),
                minimum=1,
            ),
            review_receipt_id=data["review_receipt_id"],
            verdict=data["verdict"],
            validity=data["validity"],
            justification_refs=refs(data["justification_refs"], f"{name}.justification_refs"),
        )


class DeliveryStage(StrEnum):
    """AER §6.1: how far the output actually travelled."""

    PERSISTED = "PERSISTED"
    ENQUEUED = "ENQUEUED"
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """AER §6.1 / §13 v1.4: only the stage the goal asked for completes the goal."""

    receipt_id: str
    mission_id: str
    acceptance_id: AcceptanceId
    stage: DeliveryStage
    observed_at_ms: int
    operation_id: OperationId | None = None
    evidence_refs: tuple[TypedRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", identifier(self.receipt_id, "receipt.receipt_id"))
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "receipt.mission_id"))
        object.__setattr__(
            self,
            "acceptance_id",
            AcceptanceId(identifier(self.acceptance_id, "receipt.acceptance_id")),
        )
        object.__setattr__(self, "stage", enum_of(DeliveryStage, self.stage, "receipt.stage"))
        object.__setattr__(
            self, "observed_at_ms", index(self.observed_at_ms, "receipt.observed_at_ms")
        )
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                OperationId(identifier(self.operation_id, "receipt.operation_id")),
            )
        if (
            self.stage in {DeliveryStage.SENT, DeliveryStage.CONFIRMED}
            and self.operation_id is None
        ):
            raise ContractError(
                "a SENT / CONFIRMED delivery must name the operation that produced it (I05)"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "mission_id": self.mission_id,
            "acceptance_id": str(self.acceptance_id),
            "stage": str(self.stage),
            "observed_at_ms": self.observed_at_ms,
            "operation_id": None if self.operation_id is None else str(self.operation_id),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "delivery_receipt") -> DeliveryReceipt:
        data = fields_of(
            value,
            name,
            required=("receipt_id", "mission_id", "acceptance_id", "stage", "observed_at_ms"),
            optional=("operation_id", "evidence_refs"),
        )
        return cls(
            receipt_id=data["receipt_id"],
            mission_id=data["mission_id"],
            acceptance_id=AcceptanceId(data["acceptance_id"]),
            stage=data["stage"],
            observed_at_ms=data["observed_at_ms"],
            operation_id=(
                None if data.get("operation_id") is None else OperationId(data["operation_id"])
            ),
            evidence_refs=sequence_of(
                data.get("evidence_refs", ()),
                f"{name}.evidence_refs",
                lambda item, where: TypedRef.from_json(item, where),
            ),
        )


# --------------------------------------------------------------------------------------
# Operations (§14.3 v1.4, AER §12)
# --------------------------------------------------------------------------------------


class OperationKind(StrEnum):
    READ = "READ"
    STATE_WRITE = "STATE_WRITE"
    EVENT_WRITE = "EVENT_WRITE"
    COMPENSATION = "COMPENSATION"


@dataclass(frozen=True, slots=True)
class OperationEnvelope:
    """``operation-envelope.schema.json``: the frozen, immutable semantics of one operation.

    Control fields (authorisation state, generation, in-flight handoff) live in a
    separate current-state table: a retry may change those, never this.  Two
    envelopes with the same ``operation_id`` and different content are an
    ``OPERATION_PAYLOAD_CONFLICT`` and must not be executed (§14.3).
    """

    operation_id: OperationId
    operation_occurrence_id: OperationOccurrenceId
    mission_id: str
    obligation_id: str
    scope_id: str
    connector_id: str
    connector_version: str
    operation_name: str
    operation_kind: OperationKind
    target_ref: str
    expected_target_version: str | None
    parameters_artifact_ref: TypedRef
    request_hash: str
    requirements_revision: int
    review_ref: TypedRef
    accepted_input_refs: tuple[TypedRef, ...]
    effect_contract_ref: TypedRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            OperationId(identifier(self.operation_id, "envelope.operation_id")),
        )
        object.__setattr__(
            self,
            "operation_occurrence_id",
            OperationOccurrenceId(
                identifier(self.operation_occurrence_id, "envelope.operation_occurrence_id")
            ),
        )
        for name in ("mission_id", "obligation_id", "scope_id", "connector_id", "target_ref"):
            object.__setattr__(self, name, identifier(getattr(self, name), f"envelope.{name}"))
        object.__setattr__(
            self,
            "connector_version",
            identifier(self.connector_version, "envelope.connector_version"),
        )
        object.__setattr__(
            self, "operation_name", identifier(self.operation_name, "envelope.operation_name")
        )
        object.__setattr__(
            self,
            "operation_kind",
            enum_of(OperationKind, self.operation_kind, "envelope.operation_kind"),
        )
        object.__setattr__(
            self,
            "expected_target_version",
            optional_identifier(self.expected_target_version, "envelope.expected_target_version"),
        )
        for name in ("parameters_artifact_ref", "review_ref", "effect_contract_ref"):
            if not isinstance(getattr(self, name), TypedRef):
                raise ContractError(f"envelope.{name} must be a TypedRef")
        object.__setattr__(
            self, "request_hash", hash_hex(self.request_hash, "envelope.request_hash")
        )
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "envelope.requirements_revision"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": OPERATION_ENVELOPE_SCHEMA_VERSION,
            "operation_id": str(self.operation_id),
            "operation_occurrence_id": str(self.operation_occurrence_id),
            "mission_id": self.mission_id,
            "obligation_id": self.obligation_id,
            "scope_id": self.scope_id,
            "connector_id": self.connector_id,
            "connector_version": self.connector_version,
            "operation_name": self.operation_name,
            "operation_kind": str(self.operation_kind),
            "target_ref": self.target_ref,
            "expected_target_version": self.expected_target_version,
            "parameters_artifact_ref": self.parameters_artifact_ref.to_json(),
            "request_hash": self.request_hash,
            "requirements_revision": self.requirements_revision,
            "review_ref": self.review_ref.to_json(),
            "accepted_input_refs": [ref.to_json() for ref in self.accepted_input_refs],
            "effect_contract_ref": self.effect_contract_ref.to_json(),
        }

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    @classmethod
    def from_json(cls, value: object, name: str = "operation_envelope") -> OperationEnvelope:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "operation_id",
                "operation_occurrence_id",
                "mission_id",
                "obligation_id",
                "scope_id",
                "connector_id",
                "connector_version",
                "operation_name",
                "operation_kind",
                "target_ref",
                "expected_target_version",
                "parameters_artifact_ref",
                "request_hash",
                "requirements_revision",
                "review_ref",
                "accepted_input_refs",
                "effect_contract_ref",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=OPERATION_ENVELOPE_SCHEMA_VERSION,
        )
        return cls(
            operation_id=OperationId(data["operation_id"]),
            operation_occurrence_id=OperationOccurrenceId(data["operation_occurrence_id"]),
            mission_id=data["mission_id"],
            obligation_id=data["obligation_id"],
            scope_id=data["scope_id"],
            connector_id=data["connector_id"],
            connector_version=data["connector_version"],
            operation_name=data["operation_name"],
            operation_kind=data["operation_kind"],
            target_ref=data["target_ref"],
            expected_target_version=data["expected_target_version"],
            parameters_artifact_ref=TypedRef.from_json(
                data["parameters_artifact_ref"], f"{name}.parameters_artifact_ref"
            ),
            request_hash=data["request_hash"],
            requirements_revision=data["requirements_revision"],
            review_ref=TypedRef.from_json(data["review_ref"], f"{name}.review_ref"),
            accepted_input_refs=sequence_of(
                data["accepted_input_refs"],
                f"{name}.accepted_input_refs",
                lambda item, where: TypedRef.from_json(item, where),
            ),
            effect_contract_ref=TypedRef.from_json(
                data["effect_contract_ref"], f"{name}.effect_contract_ref"
            ),
        )


class OperationControl(StrEnum):
    """AER §13 axis 1: where the *orchestrator* has got to with this operation.

    It says nothing about the world.  ``CLOSED`` means this side has finished
    bookkeeping, not that the effect landed — that is :class:`EffectOutcome`.
    """

    PROPOSED = "PROPOSED"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    QUIESCING = "QUIESCING"
    CLOSED = "CLOSED"


class EffectOutcome(StrEnum):
    """AER §13 axis 2: what happened in the real world.

    ``UNKNOWN`` is a real, terminal-for-now answer and is not ``NOT_APPLIED``
    (invariant I10): a timeout says the orchestrator stopped waiting, never that
    the other side did nothing.
    """

    NOT_HANDED_OFF = "NOT_HANDED_OFF"
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class AccountingState(StrEnum):
    """AER §13 axis 3: what the money is doing.

    Separate from both other axes because a cost that has been incurred must be
    recorded even when the effect is UNKNOWN and the control flow closed
    (invariant I13); ``USAGE_UNKNOWN`` is how that is said out loud.
    """

    UNRESERVED = "UNRESERVED"
    RESERVED = "RESERVED"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    USAGE_UNKNOWN = "USAGE_UNKNOWN"


#: Control states in which the request has not yet been handed to a connector.
_PRE_HANDOFF_CONTROL = frozenset(
    {OperationControl.PROPOSED, OperationControl.AWAITING_AUTHORIZATION}
)


@dataclass(frozen=True, slots=True)
class OperationCurrentState:
    """AER §12.2 / §13: the mutable control record beside the frozen envelope.

    :class:`OperationEnvelope` is the immutable semantics of one real intent; this
    is everything a retry, an authorisation or a reconciliation may legitimately
    change.  Keeping them in separate records is what stops a retry from editing
    what is being requested while it edits how the request is being managed.
    """

    operation_id: OperationId
    authorization_state: OperationControl
    authorization_epoch: int
    dispatch_generation: int
    effect_outcome: EffectOutcome
    accounting_state: AccountingState = AccountingState.UNRESERVED
    in_flight_handoff_id: str | None = None
    budget_refs: tuple[str, ...] = ()
    next_reconcile_at_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            OperationId(identifier(self.operation_id, "operation_state.operation_id")),
        )
        object.__setattr__(
            self,
            "authorization_state",
            enum_of(
                OperationControl, self.authorization_state, "operation_state.authorization_state"
            ),
        )
        object.__setattr__(
            self,
            "authorization_epoch",
            index(self.authorization_epoch, "operation_state.authorization_epoch"),
        )
        object.__setattr__(
            self,
            "dispatch_generation",
            index(self.dispatch_generation, "operation_state.dispatch_generation"),
        )
        object.__setattr__(
            self,
            "effect_outcome",
            enum_of(EffectOutcome, self.effect_outcome, "operation_state.effect_outcome"),
        )
        object.__setattr__(
            self,
            "accounting_state",
            enum_of(AccountingState, self.accounting_state, "operation_state.accounting_state"),
        )
        object.__setattr__(
            self,
            "in_flight_handoff_id",
            optional_identifier(self.in_flight_handoff_id, "operation_state.in_flight_handoff_id"),
        )
        object.__setattr__(
            self, "budget_refs", identifiers(self.budget_refs, "operation_state.budget_refs")
        )
        object.__setattr__(
            self,
            "next_reconcile_at_ms",
            optional_index(self.next_reconcile_at_ms, "operation_state.next_reconcile_at_ms"),
        )
        if (
            self.effect_outcome is not EffectOutcome.NOT_HANDED_OFF
            and self.authorization_state in _PRE_HANDOFF_CONTROL
        ):
            raise ContractError(
                "an operation that has not been authorised cannot already have an effect; "
                f"{self.authorization_state!s} with outcome {self.effect_outcome!s} "
                "would mean approval followed the action (invariants I03, I04)"
            )
        if (
            self.effect_outcome is EffectOutcome.NOT_HANDED_OFF
            and self.in_flight_handoff_id is not None
        ):
            raise ContractError(
                "an operation with an in-flight handoff has been handed off; "
                "NOT_HANDED_OFF and a live handoff id cannot both be true"
            )

    @property
    def settled(self) -> bool:
        """Whether both the effect and the money have stopped moving."""

        return self.effect_outcome not in {
            EffectOutcome.PENDING,
            EffectOutcome.NOT_HANDED_OFF,
        } and self.accounting_state in {AccountingState.SETTLED, AccountingState.USAGE_UNKNOWN}

    def to_json(self) -> dict[str, Any]:
        return {
            "operation_id": str(self.operation_id),
            "authorization_state": str(self.authorization_state),
            "authorization_epoch": self.authorization_epoch,
            "dispatch_generation": self.dispatch_generation,
            "effect_outcome": str(self.effect_outcome),
            "accounting_state": str(self.accounting_state),
            "in_flight_handoff_id": self.in_flight_handoff_id,
            "budget_refs": list(self.budget_refs),
            "next_reconcile_at_ms": self.next_reconcile_at_ms,
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "operation_current_state"
    ) -> OperationCurrentState:
        data = fields_of(
            value,
            name,
            required=(
                "operation_id",
                "authorization_state",
                "authorization_epoch",
                "dispatch_generation",
                "effect_outcome",
            ),
            optional=(
                "accounting_state",
                "in_flight_handoff_id",
                "budget_refs",
                "next_reconcile_at_ms",
            ),
        )
        return cls(
            operation_id=OperationId(data["operation_id"]),
            authorization_state=data["authorization_state"],
            authorization_epoch=data["authorization_epoch"],
            dispatch_generation=data["dispatch_generation"],
            effect_outcome=data["effect_outcome"],
            accounting_state=data.get("accounting_state", AccountingState.UNRESERVED),
            in_flight_handoff_id=data.get("in_flight_handoff_id"),
            budget_refs=tuple(data.get("budget_refs", ())),
            next_reconcile_at_ms=data.get("next_reconcile_at_ms"),
        )


class ApprovalDecision(StrEnum):
    """Whether a human or policy approval has been given for this subject."""

    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class ApprovalState:
    """An approval and the identity and clock that bound it (§14.3).

    A grant names who gave it and when: an approval with no grantor is not an
    approval, and one that has passed ``expires_at_ms`` is not current — checked at
    use, because "it was approved once" and "it is approved now" are different
    claims (invariant I09).
    """

    decision: ApprovalDecision
    granted_by: str | None = None
    granted_at_ms: int | None = None
    expires_at_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision", enum_of(ApprovalDecision, self.decision, "approval.decision")
        )
        object.__setattr__(
            self, "granted_by", optional_identifier(self.granted_by, "approval.granted_by")
        )
        object.__setattr__(
            self, "granted_at_ms", optional_index(self.granted_at_ms, "approval.granted_at_ms")
        )
        object.__setattr__(
            self, "expires_at_ms", optional_index(self.expires_at_ms, "approval.expires_at_ms")
        )
        if self.decision is ApprovalDecision.GRANTED:
            if self.granted_by is None or self.granted_at_ms is None:
                raise ContractError(
                    "a GRANTED approval must name who granted it and when; "
                    "an unattributed grant is not an approval"
                )
        elif self.decision in {ApprovalDecision.NOT_REQUIRED, ApprovalDecision.PENDING}:
            if self.granted_by is not None or self.granted_at_ms is not None:
                raise ContractError(
                    f"a {self.decision!s} approval has not been granted and may not "
                    "carry a grantor or a grant time"
                )
        if (
            self.expires_at_ms is not None
            and self.granted_at_ms is not None
            and self.expires_at_ms < self.granted_at_ms
        ):
            raise ContractError("approval.expires_at_ms precedes granted_at_ms")

    def is_effective(self, *, now_ms: int) -> bool:
        """Whether this approval authorises an action *now*."""

        if self.decision is not ApprovalDecision.GRANTED:
            return False
        return self.expires_at_ms is None or now_ms < self.expires_at_ms

    def to_json(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "granted_by": self.granted_by,
            "granted_at_ms": self.granted_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "approval_state") -> ApprovalState:
        data = fields_of(
            value,
            name,
            required=("decision",),
            optional=("granted_by", "granted_at_ms", "expires_at_ms"),
        )
        return cls(
            decision=data["decision"],
            granted_by=data.get("granted_by"),
            granted_at_ms=data.get("granted_at_ms"),
            expires_at_ms=data.get("expires_at_ms"),
        )


#: The legacy universal candidate range (``planning/candidate_selection.py``).  ADR-08
#: replaced fixed universal limits with versioned capacity, so this is recorded as the
#: deployment's historical default, not as a ceiling the contract imposes.
LEGACY_CANDIDATE_RANGE = (1, 3)


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    """How many alternatives one goal may carry, and what synthesis costs (§10.2).

    Versioned rather than constant: ADR-08 removed the universal "3 candidates"
    rule, and a policy that cannot say which version produced a plan cannot explain
    why that plan was bounded the way it was.
    """

    policy_version: int
    max_candidates: int
    synthesis_allowed: bool = False
    reserve_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_version", index(self.policy_version, "policy.policy_version", minimum=1)
        )
        object.__setattr__(
            self, "max_candidates", index(self.max_candidates, "policy.max_candidates", minimum=1)
        )
        object.__setattr__(
            self, "synthesis_allowed", flag(self.synthesis_allowed, "policy.synthesis_allowed")
        )
        object.__setattr__(
            self, "reserve_tokens", index(self.reserve_tokens, "policy.reserve_tokens")
        )
        if self.synthesis_allowed and self.max_candidates < 2:
            raise ContractError(
                "synthesis compares candidates, so it needs at least two of them "
                "(planning/candidate_selection.py)"
            )
        if self.synthesis_allowed and self.reserve_tokens <= 0:
            raise ContractError(
                "synthesis must reserve a positive tail budget, or the final "
                "integration is the step that runs out of tokens"
            )

    @property
    def compatible_with_legacy(self) -> bool:
        """Whether this policy stays inside the legacy 1..3 candidate range."""

        low, high = LEGACY_CANDIDATE_RANGE
        return low <= self.max_candidates <= high

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "max_candidates": self.max_candidates,
            "synthesis_allowed": self.synthesis_allowed,
            "reserve_tokens": self.reserve_tokens,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "candidate_policy") -> CandidatePolicy:
        data = fields_of(
            value,
            name,
            required=("policy_version", "max_candidates"),
            optional=("synthesis_allowed", "reserve_tokens"),
        )
        return cls(
            policy_version=data["policy_version"],
            max_candidates=data["max_candidates"],
            synthesis_allowed=data.get("synthesis_allowed", False),
            reserve_tokens=data.get("reserve_tokens", 0),
        )


OPERATION_PAYLOAD_CONFLICT = "OPERATION_PAYLOAD_CONFLICT"


@dataclass(frozen=True, slots=True)
class OperationConflict:
    code: str
    operation_id: OperationId
    left_hash: str
    right_hash: str


def envelope_conflict(
    left: OperationEnvelope, right: OperationEnvelope
) -> OperationConflict | None:
    """§14.3: same ``OperationId``, different frozen semantics — refuse to execute.

    Same id and identical envelope is a retry or a status query of the same real
    intent; anything else is a different intent wearing the same identity, and a
    payment or a message must never be sent on that basis.
    """

    if left.operation_id != right.operation_id:
        return None
    left_hash = left.content_hash()
    right_hash = right.content_hash()
    if left_hash == right_hash:
        return None
    return OperationConflict(
        code=OPERATION_PAYLOAD_CONFLICT,
        operation_id=left.operation_id,
        left_hash=left_hash,
        right_hash=right_hash,
    )


class ReconciliationOutcome(StrEnum):
    APPLIED = "APPLIED"
    NOT_APPLIED_FINAL = "NOT_APPLIED_FINAL"
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """``reconciliation-result.schema.json``: what a lookup actually established.

    An empty lookup is not a proof of non-application (invariant I10); only a
    server-side proof, or a demonstrably still-valid idempotency key, lets a
    request be handed off again.
    """

    record_id: str
    operation_id: OperationId
    request_hash: str
    connector_namespace: str
    outcome: ReconciliationOutcome
    observed_at_ms: int
    evidence_refs: tuple[TypedRef, ...]
    old_request_cannot_apply: bool
    query_completeness: QueryCompleteness
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", identifier(self.record_id, "reconciliation.record_id")
        )
        object.__setattr__(
            self,
            "operation_id",
            OperationId(identifier(self.operation_id, "reconciliation.operation_id")),
        )
        object.__setattr__(
            self, "request_hash", hash_hex(self.request_hash, "reconciliation.request_hash")
        )
        object.__setattr__(
            self,
            "connector_namespace",
            identifier(self.connector_namespace, "reconciliation.connector_namespace"),
        )
        object.__setattr__(
            self,
            "outcome",
            enum_of(ReconciliationOutcome, self.outcome, "reconciliation.outcome"),
        )
        object.__setattr__(
            self, "observed_at_ms", index(self.observed_at_ms, "reconciliation.observed_at_ms")
        )
        object.__setattr__(
            self,
            "old_request_cannot_apply",
            flag(self.old_request_cannot_apply, "reconciliation.old_request_cannot_apply"),
        )
        object.__setattr__(
            self,
            "query_completeness",
            enum_of(
                QueryCompleteness, self.query_completeness, "reconciliation.query_completeness"
            ),
        )
        object.__setattr__(
            self,
            "explanation",
            text(self.explanation, "reconciliation.explanation", limit=MAX_REASON),
        )
        if (
            self.outcome is ReconciliationOutcome.NOT_APPLIED_FINAL
            and self.query_completeness is not QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
        ):
            raise ContractError(
                "NOT_APPLIED_FINAL requires an authoritative, scoped query (AER §14.3)"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_RESULT_SCHEMA_VERSION,
            "record_id": self.record_id,
            "operation_id": str(self.operation_id),
            "request_hash": self.request_hash,
            "connector_namespace": self.connector_namespace,
            "outcome": str(self.outcome),
            "observed_at_ms": self.observed_at_ms,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "old_request_cannot_apply": self.old_request_cannot_apply,
            "query_completeness": str(self.query_completeness),
            "explanation": self.explanation,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "reconciliation_result") -> ReconciliationResult:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "record_id",
                "operation_id",
                "request_hash",
                "connector_namespace",
                "outcome",
                "observed_at_ms",
                "evidence_refs",
                "old_request_cannot_apply",
                "query_completeness",
                "explanation",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=RECONCILIATION_RESULT_SCHEMA_VERSION,
        )
        return cls(
            record_id=data["record_id"],
            operation_id=OperationId(data["operation_id"]),
            request_hash=data["request_hash"],
            connector_namespace=data["connector_namespace"],
            outcome=data["outcome"],
            observed_at_ms=data["observed_at_ms"],
            evidence_refs=sequence_of(
                data["evidence_refs"],
                f"{name}.evidence_refs",
                lambda item, where: TypedRef.from_json(item, where),
            ),
            old_request_cannot_apply=data["old_request_cannot_apply"],
            query_completeness=data["query_completeness"],
            explanation=data["explanation"],
        )


def may_rehandoff(result: ReconciliationResult) -> bool:
    """AER §14.3: an empty or best-effort lookup never licenses a re-send."""

    if result.old_request_cannot_apply:
        return True
    return (
        result.outcome is ReconciliationOutcome.NOT_APPLIED_FINAL
        and result.query_completeness is QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    )


__all__ = (
    "GOAL_RESOLUTION_SCHEMA_VERSION",
    "LEGACY_CANDIDATE_RANGE",
    "MAX_SUCCESS_EXPRESSION_NODES",
    "OPERATION_ENVELOPE_SCHEMA_VERSION",
    "OPERATION_PAYLOAD_CONFLICT",
    "RECONCILIATION_RESULT_SCHEMA_VERSION",
    "REVIEW_PURPOSE_ACCOUNTS",
    "REVIEW_RECORD_SCHEMA_VERSION",
    "Acceptance",
    "AccountingState",
    "AcceptanceId",
    "AllExpr",
    "AmendmentPolicy",
    "AnyExpr",
    "ApprovalDecision",
    "ApprovalState",
    "CheckExecution",
    "CandidatePolicy",
    "Criterion",
    "CriterionExpr",
    "CriterionMatch",
    "CriterionOrigin",
    "CriterionOutcome",
    "CriterionVerdict",
    "DeliveryReceipt",
    "DeliveryStage",
    "EffectOutcome",
    "EvaluationKind",
    "GoalResolution",
    "GoalResolutionId",
    "GoalSignature",
    "OperationConflict",
    "OperationControl",
    "OperationCurrentState",
    "OperationEnvelope",
    "OperationId",
    "OperationKind",
    "OperationOccurrenceId",
    "ReconciliationOutcome",
    "ReconciliationResult",
    "RequiredEvidencePolicy",
    "RequirementClass",
    "RequirementsRevision",
    "RequirementsRevisionId",
    "ResolutionCriterion",
    "ReviewAccount",
    "ReviewBinding",
    "ReviewPackage",
    "ReviewPackageId",
    "ReviewPurpose",
    "ReviewRecord",
    "ReviewRecordId",
    "ReviewVerdict",
    "SuccessExpression",
    "WorkspaceAccess",
    "account_for_purpose",
    "criteria_only_under_any",
    "envelope_conflict",
    "expression_criterion_ids",
    "hard_constraints_not_independent",
    "match_review_criteria",
    "may_rehandoff",
    "parse_success_expression",
    "unknown_expression_criteria",
)
