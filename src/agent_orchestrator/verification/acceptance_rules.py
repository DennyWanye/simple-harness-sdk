# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure acceptance rules: success expression, hard gate, completeness, independence.

FULL-TARGET-1.4 slice P1.1b (§13 v1.4, §25.1 decisions 1–4; AER annex §4.1–4.3,
§5.3–5.4, §6.2, §7).  Everything here is a total function over frozen contract
values: no model call, no store read, no commit path, no ``verifier_router``.
That is the point — the rules that decide whether a goal may be declared done
must be readable, testable and impossible to satisfy by side effect.

The five rules, and what each refuses to do:

``evaluate_success_expression``
    Three-valued evaluation of the restricted AST.  ``all`` fails on one FAIL and
    only passes when every child passes; ``any`` passes on one PASS and only fails
    when every child fails; everything else is UNKNOWN.  A criterion nobody
    evaluated is UNKNOWN, never a default PASS, and an execution that errored,
    never ran, was cancelled or is still running projects to UNKNOWN rather than
    to FAIL — infrastructure trouble is not evidence against the content
    (AER §4.3, invariant I07, scenario AER-V08).
``hard_gate``
    Every HARD_CONSTRAINT is an independent AND conjunct, evaluated on its own so
    a result-level OR approved by the user's own text cannot trade it away
    (AER §4.1, scenario AER-V02).  FAIL, UNKNOWN and missing are reported apart.
``required_checks_complete``
    The record's criteria must match the package catalogue one to one, the named
    checks of ``required_evidence_policy`` must actually have run and passed, and
    their evidence must be a dispatcher / tool receipt rather than a string the
    model wrote (AER §5.4, scenario AER-V04).
``independence_ok``
    The minimum independence bar of AER §5.3: a different agent identity, no write
    access to the candidate, never reviewing a version the reviewer edited, and a
    different model is *not* by itself independent evidence.
``acceptable``
    The AER §6.2 formula, with a reason code for every unmet conjunct, plus the
    compound extras (a legal selected method, a valid contribution for every
    required occurrence, the composition obligation).
``retry_policy``
    Bounded retry, escalation to an independent reviewer, escalation to a human
    and stopping all draw on the same obligation budget; re-sampling an unchanged
    candidate until one reviewer says PASS is refused, and conflicting reviews go
    to arbitration instead of a majority vote (AER §5.4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..contracts.evidence_state import (
    Availability,
    TruthValue,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from ..contracts.htn import ChildBinding, Requiredness
from ..contracts.models import ContractError
from ..contracts.resolution import (
    AllExpr,
    AnyExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionMatch,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequirementClass,
    RequirementsRevision,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    SuccessExpression,
    WorkspaceAccess,
    match_review_criteria,
)
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of

#: AER §5.4: a receipt has to come from a dispatcher or a tool, not from prose.
#: These are the attributions the *system* binds after dispatch; a proposal may
#: only ever claim ``model`` or ``human`` for itself, so this is the primary
#: signal and ``DISPATCHER_RECEIPT_KINDS`` is only the fallback.
DISPATCHER_PROVENANCE: frozenset[Provenance] = frozenset({Provenance.SYSTEM, Provenance.TOOL})

#: The fallback signal, for refs that predate ``produced_by`` attribution.
#: ``knowledge`` / ``source`` / ``artifact`` / ``review`` refs are authored — they
#: may accompany a receipt but they never stand in for one.
DISPATCHER_RECEIPT_KINDS: frozenset[TypedRefKind] = frozenset(
    {TypedRefKind.TOOL_RECEIPT, TypedRefKind.OPERATION}
)

#: AER §4.3: only a check that actually finished carries its reported verdict.
CONCLUSIVE_EXECUTION: frozenset[CheckExecution] = frozenset({CheckExecution.SUCCEEDED})


# --------------------------------------------------------------------------------------
# Success expression (AER §4.3)
# --------------------------------------------------------------------------------------


def outcomes_by_id(
    outcomes: Iterable[CriterionOutcome],
) -> dict[str, CriterionOutcome]:
    """Index outcomes by criterion id.  A repeated id is a contract error upstream."""

    indexed: dict[str, CriterionOutcome] = {}
    for item in outcomes:
        indexed[item.criterion_id] = item
    return indexed


def project_verdict(outcome: CriterionOutcome) -> CriterionVerdict:
    """AER §4.3 / I07: project the execution axis onto the verdict axis.

    A check that errored, was cancelled, never ran or is still running yields
    UNKNOWN — including when the reviewer wrote FAIL, because an infrastructure
    failure is not a refutation of the content (AER-V08).
    """

    if outcome.check_execution not in CONCLUSIVE_EXECUTION:
        return CriterionVerdict.UNKNOWN
    return outcome.verdict


@dataclass(frozen=True, slots=True)
class ExpressionResult:
    """The three-valued verdict of a success expression, with its witness."""

    verdict: CriterionVerdict
    witness_path: tuple[str, ...] = ()
    unevaluated_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is CriterionVerdict.PASS


def _dedupe(ids: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in ids:
        seen.setdefault(item, None)
    return tuple(seen)


def _illegal_node(node: object) -> str:
    return (
        "success expression nodes are criterion / all / any only; "
        f"got {type(node).__name__} (§25.1 decision 2)"
    )


def success_expression_digest(expression: SuccessExpression) -> str:
    """The canonical identity of a success expression, for equality checks.

    Two expressions are the same requirement only when their canonical JSON is
    byte-identical; comparing objects would let a re-parsed but re-shaped tree
    pass, and comparing ids would ignore the operators entirely.
    """

    if not isinstance(expression, (CriterionExpr, AllExpr, AnyExpr)):
        raise ContractError(_illegal_node(expression))
    return content_hash_of(expression.to_json())


def evaluate_success_expression(
    expression: SuccessExpression, outcomes: Mapping[str, CriterionOutcome]
) -> ExpressionResult:
    """Evaluate the restricted success AST over the outcomes actually recorded.

    ``all``: one FAIL is FAIL, every child PASS is PASS, otherwise UNKNOWN.
    ``any``: one PASS is PASS, every child FAIL is FAIL, otherwise UNKNOWN.
    A criterion with no outcome counts as UNKNOWN and is listed in
    ``unevaluated_ids`` — a branch the reviewer deliberately declined is legitimate
    (AER §5.4) but it is never silently a PASS.
    """

    missing: list[str] = []

    def walk(node: SuccessExpression) -> tuple[CriterionVerdict, tuple[str, ...]]:
        if isinstance(node, CriterionExpr):
            outcome = outcomes.get(node.criterion_id)
            if outcome is None:
                missing.append(node.criterion_id)
                return CriterionVerdict.UNKNOWN, (node.criterion_id,)
            return project_verdict(outcome), (node.criterion_id,)
        if not isinstance(node, (AllExpr, AnyExpr)):
            raise ContractError(_illegal_node(node))
        results = [walk(child) for child in node.children]
        if isinstance(node, AnyExpr):
            for verdict, path in results:
                if verdict is CriterionVerdict.PASS:
                    return CriterionVerdict.PASS, path
            if all(verdict is CriterionVerdict.FAIL for verdict, _ in results):
                return CriterionVerdict.FAIL, _dedupe(item for _, path in results for item in path)
            return CriterionVerdict.UNKNOWN, _dedupe(
                item
                for verdict, path in results
                if verdict is not CriterionVerdict.FAIL
                for item in path
            )
        for verdict, path in results:
            if verdict is CriterionVerdict.FAIL:
                return CriterionVerdict.FAIL, path
        if all(verdict is CriterionVerdict.PASS for verdict, _ in results):
            return CriterionVerdict.PASS, _dedupe(item for _, path in results for item in path)
        return CriterionVerdict.UNKNOWN, _dedupe(
            item
            for verdict, path in results
            if verdict is not CriterionVerdict.PASS
            for item in path
        )

    verdict, path = walk(expression)
    return ExpressionResult(verdict=verdict, witness_path=path, unevaluated_ids=_dedupe(missing))


# --------------------------------------------------------------------------------------
# Hard gate (AER §4.1, scenario AER-V02)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateResult:
    """Hard constraints, evaluated as an independent AND of their own."""

    failed_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not (self.failed_ids or self.unknown_ids or self.missing_ids)


def hard_gate(
    revision: RequirementsRevision, outcomes: Mapping[str, CriterionOutcome]
) -> GateResult:
    """Every HARD_CONSTRAINT must be PASS on its own evidence.

    The success expression is not consulted at all: a hard constraint that the
    reviewer failed cannot be offset by the other side of an approved result OR.
    FAIL, UNKNOWN and "no outcome at all" are reported separately, because they
    call for different follow-up.
    """

    failed: list[str] = []
    unknown: list[str] = []
    missing: list[str] = []
    for criterion in revision.criteria:
        if criterion.requirement_class is not RequirementClass.HARD_CONSTRAINT:
            continue
        outcome = outcomes.get(criterion.criterion_id)
        if outcome is None:
            missing.append(criterion.criterion_id)
            continue
        verdict = project_verdict(outcome)
        if verdict is CriterionVerdict.FAIL:
            failed.append(criterion.criterion_id)
        elif verdict is not CriterionVerdict.PASS:
            unknown.append(criterion.criterion_id)
    return GateResult(
        failed_ids=tuple(failed), unknown_ids=tuple(unknown), missing_ids=tuple(missing)
    )


# --------------------------------------------------------------------------------------
# Required checks and the one-to-one catalogue match (AER §5.4, scenario AER-V04)
# --------------------------------------------------------------------------------------


def _needs_receipt(criterion: Criterion) -> bool:
    return bool(criterion.required_evidence_policy.required_check_ids) or (
        criterion.evaluation_kind
        in {
            EvaluationKind.EXECUTION_RECEIPT,
            EvaluationKind.DETERMINISTIC,
            EvaluationKind.FORMAL,
        }
    )


class ReceiptSource(StrEnum):
    """How well a criterion outcome's evidence is attributed (AER §5.4, §8.1)."""

    #: At least one ref the system attributed to itself or to a dispatched tool.
    DISPATCHER_ATTRIBUTED = "DISPATCHER_ATTRIBUTED"
    #: No attribution at all, but a ref whose *kind* is a receipt kind.  Accepted
    #: on the fallback path and flagged, because ``kind`` is model-writable.
    PROVENANCE_UNKNOWN = "PROVENANCE_UNKNOWN"
    #: Nothing here is a receipt — including a ``tool_receipt`` ref the model
    #: attributed to itself, which is exactly the forgery this rule exists for.
    NOT_A_RECEIPT = "NOT_A_RECEIPT"


def receipt_source(outcome: CriterionOutcome) -> ReceiptSource:
    """Classify an outcome's evidence by attribution first, kind only as fallback.

    ``TypedRef.produced_by`` is bound by the system after dispatch and a proposal
    may only claim ``model`` / ``human`` for itself, so an attributed ref is the
    trustworthy signal.  A ref with no attribution falls back to its ``kind`` —
    weaker, because a model can write ``kind: "tool_receipt"`` — and the caller is
    told so rather than being left to assume the strong reading.
    """

    refs: Sequence[TypedRef] = outcome.evidence_refs
    if any(ref.produced_by in DISPATCHER_PROVENANCE for ref in refs):
        return ReceiptSource.DISPATCHER_ATTRIBUTED
    if any(ref.produced_by is None and ref.kind in DISPATCHER_RECEIPT_KINDS for ref in refs):
        return ReceiptSource.PROVENANCE_UNKNOWN
    return ReceiptSource.NOT_A_RECEIPT


@dataclass(frozen=True, slots=True)
class CompletenessResult:
    """Whether the required checks of a review package genuinely completed."""

    match: CriterionMatch
    not_executed_ids: tuple[str, ...] = ()
    not_passed_ids: tuple[str, ...] = ()
    unsourced_ids: tuple[str, ...] = ()
    #: Required checks accepted on the weaker ``kind`` fallback because no ref
    #: carried a ``produced_by`` attribution.  Not a gap — a caveat to surface.
    provenance_unknown_ids: tuple[str, ...] = ()
    declined_ids: tuple[str, ...] = ()
    independence_required_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.match.matched and not (
            self.not_executed_ids or self.not_passed_ids or self.unsourced_ids
        )


def required_checks_complete(package: ReviewPackage, record: ReviewRecord) -> CompletenessResult:
    """AER §5.3 step 6 / §5.4: the mandatory layer must really have been exercised.

    A named check that did not run, errored, or reported a verdict other than PASS
    is not a PASS however the model phrased it (AER-V04), and a receipt has to
    come from a dispatcher or tool reference rather than from prose the reviewer
    produced.  Criteria that live only under an ``any`` branch and that the record
    deliberately left out are reported as ``declined_ids``, not as gaps.
    """

    match = match_review_criteria(record, package, allow_unevaluated_or_branches=True)
    reported = outcomes_by_id(record.criteria)
    optional_ids = package.criteria_only_under_any()

    not_executed: list[str] = []
    not_passed: list[str] = []
    unsourced: list[str] = []
    provenance_unknown: list[str] = []
    declined: list[str] = []
    independence: list[str] = []

    for criterion in package.criteria:
        if criterion.required_evidence_policy.independence_required:
            independence.append(criterion.criterion_id)
        outcome = reported.get(criterion.criterion_id)
        if outcome is None:
            if criterion.criterion_id in optional_ids:
                declined.append(criterion.criterion_id)
            continue
        if not criterion.is_required:
            continue
        gated = bool(criterion.required_evidence_policy.required_check_ids)
        if gated:
            if outcome.check_execution not in CONCLUSIVE_EXECUTION:
                not_executed.append(criterion.criterion_id)
            if project_verdict(outcome) is not CriterionVerdict.PASS:
                not_passed.append(criterion.criterion_id)
        if _needs_receipt(criterion):
            source = receipt_source(outcome)
            if source is ReceiptSource.NOT_A_RECEIPT:
                unsourced.append(criterion.criterion_id)
            elif source is ReceiptSource.PROVENANCE_UNKNOWN:
                provenance_unknown.append(criterion.criterion_id)

    return CompletenessResult(
        match=match,
        not_executed_ids=tuple(not_executed),
        not_passed_ids=tuple(not_passed),
        unsourced_ids=tuple(unsourced),
        provenance_unknown_ids=tuple(provenance_unknown),
        declined_ids=tuple(sorted(declined)),
        independence_required_ids=tuple(independence),
    )


# --------------------------------------------------------------------------------------
# Independence (AER §5.3, scenario AER-V05)
# --------------------------------------------------------------------------------------


class IndependenceReason(StrEnum):
    """Why a review does not meet the minimum independence bar."""

    SELF_REVIEW = "SELF_REVIEW"
    WRITE_ACCESS_TO_CANDIDATE = "WRITE_ACCESS_TO_CANDIDATE"
    REVIEWED_OWN_EDIT = "REVIEWED_OWN_EDIT"
    MODEL_DIVERSITY_IS_NOT_INDEPENDENCE = "MODEL_DIVERSITY_IS_NOT_INDEPENDENCE"
    #: The caller's facts and the frozen package disagree about who produced the
    #: candidate or what the reviewer may do to it.  One of them is wrong, and a
    #: review is not independent while nobody knows which.
    FACTS_CONTRADICT_PACKAGE = "INDEPENDENCE_FACTS_CONTRADICT_PACKAGE"


@dataclass(frozen=True, slots=True)
class IndependenceFacts:
    """What the caller observed about production and reviewer rights.

    ``ReviewPackage`` now carries ``producer_agent_ids`` and
    ``reviewer_workspace_access`` itself, so these are no longer the only source.
    They stay required anyway, and are *cross-checked* against the package: the
    package is a frozen anchor written at packaging time, while these are what the
    coordinator sees now, and a disagreement between them is a finding rather than
    something to resolve by preferring one side.
    """

    producer_agent_ids: tuple[str, ...] = ()
    reviewer_can_write_candidate: bool = False
    reviewed_revision_authored_by_reviewer: bool = False
    reviewer_model_id: str | None = None
    producer_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndependenceResult:
    reasons: tuple[IndependenceReason, ...] = ()

    @property
    def independent(self) -> bool:
        return not self.reasons


def independence_ok(
    package: ReviewPackage, record: ReviewRecord, *, facts: IndependenceFacts
) -> IndependenceResult:
    """AER §5.3: the minimum bar, not a score.

    A different model is deliberately *not* on the list of things that establish
    independence; when it is the only difference between producer and reviewer the
    result says so, so nobody can present model diversity as independent evidence.

    ``package`` is taken for symmetry with the other rules and so a later policy
    (``package.independence_policy_ref``) can be resolved here without changing
    every call site; the bar below is the floor and does not depend on it.
    """

    reasons: list[IndependenceReason] = []
    reviewer = record.reviewer_agent_id

    # Either source may know about authorship; neither gets to clear it alone.
    produced_here = reviewer in facts.producer_agent_ids or package.produced_by(reviewer)
    if produced_here:
        reasons.append(IndependenceReason.SELF_REVIEW)
        if (
            facts.reviewer_model_id is not None
            and facts.producer_model_id is not None
            and facts.reviewer_model_id != facts.producer_model_id
        ):
            reasons.append(IndependenceReason.MODEL_DIVERSITY_IS_NOT_INDEPENDENCE)
    if facts.reviewer_can_write_candidate:
        reasons.append(IndependenceReason.WRITE_ACCESS_TO_CANDIDATE)
    if facts.reviewed_revision_authored_by_reviewer:
        reasons.append(IndependenceReason.REVIEWED_OWN_EDIT)

    # The package is the frozen anchor; the facts are what the caller sees now.
    # A conflict is reported, never silently resolved in favour of either.
    contradicted = False
    if package.producer_agent_ids and set(package.producer_agent_ids) != set(
        facts.producer_agent_ids
    ):
        contradicted = True
    if facts.reviewer_can_write_candidate and package.reviewer_workspace_access in {
        WorkspaceAccess.NONE,
        WorkspaceAccess.READ_ONLY,
    }:
        # ``WRITE`` is refused at construction, so the package can never agree with
        # a caller who says the reviewer could edit the candidate.
        contradicted = True
    if contradicted:
        reasons.append(IndependenceReason.FACTS_CONTRADICT_PACKAGE)
    return IndependenceResult(reasons=tuple(reasons))


# --------------------------------------------------------------------------------------
# The acceptance formula (AER §6.2, §7 pure part)
# --------------------------------------------------------------------------------------


class AcceptReason(StrEnum):
    """One code per unmet conjunct of ``Acceptable(subject, now, purpose)``."""

    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    REQUIREMENTS_REVISION_MISMATCH = "REQUIREMENTS_REVISION_MISMATCH"
    REQUIREMENTS_CONTENT_MISMATCH = "REQUIREMENTS_CONTENT_MISMATCH"
    SUCCESS_EXPRESSION_MISMATCH = "SUCCESS_EXPRESSION_MISMATCH"
    HARD_CONSTRAINT_STRUCTURE = "HARD_CONSTRAINT_STRUCTURE"
    ROOT_CRITERION_MISSING = "ROOT_CRITERION_MISSING"
    HARD_CONSTRAINT_FAILED = "HARD_CONSTRAINT_FAILED"
    HARD_CONSTRAINT_UNKNOWN = "HARD_CONSTRAINT_UNKNOWN"
    SUCCESS_EXPRESSION_NOT_PASS = "SUCCESS_EXPRESSION_NOT_PASS"
    CRITERIA_NOT_MATCHED = "CRITERIA_NOT_MATCHED"
    REQUIRED_CHECKS_INCOMPLETE = "REQUIRED_CHECKS_INCOMPLETE"
    REVIEW_VERDICT_NOT_ACCEPT = "REVIEW_VERDICT_NOT_ACCEPT"
    INDEPENDENT_REVIEW_MISSING = "INDEPENDENT_REVIEW_MISSING"
    WITNESS_PURPOSE_MISMATCH = "WITNESS_PURPOSE_MISMATCH"
    WITNESS_NOT_USABLE = "WITNESS_NOT_USABLE"
    WITNESS_STALE = "WITNESS_STALE"
    CRITICAL_OPERATION_UNOWNED = "CRITICAL_OPERATION_UNOWNED"
    CANCELLATION_PENDING = "CANCELLATION_PENDING"
    METHOD_ADOPTION_STALE = "METHOD_ADOPTION_STALE"
    COMPOUND_METHOD_ILLEGAL = "COMPOUND_METHOD_ILLEGAL"
    REQUIRED_OCCURRENCE_MISSING = "REQUIRED_OCCURRENCE_MISSING"
    COMPOSITION_OBLIGATION_FAILED = "COMPOSITION_OBLIGATION_FAILED"


@dataclass(frozen=True, slots=True)
class ExecutionPosture:
    """AER §6.2 last conjunct: what in-flight work, cancellation and adoption allow."""

    unowned_critical_operation_ids: tuple[str, ...] = ()
    cancellation_requested: bool = False
    method_adoption_current: bool = True


@dataclass(frozen=True, slots=True)
class CompoundFacts:
    """AER §6.2 compound extras, for a goal satisfied through a method instance."""

    selected_method_legal: bool
    child_bindings: tuple[ChildBinding, ...] = ()
    contributing_occurrence_ids: tuple[str, ...] = ()
    composition_obligation_passed: bool = True

    def missing_required_occurrences(self) -> tuple[str, ...]:
        contributed = set(self.contributing_occurrence_ids)
        return tuple(
            str(binding.occurrence_id)
            for binding in self.child_bindings
            if binding.requiredness is Requiredness.REQUIRED
            and str(binding.occurrence_id) not in contributed
        )


@dataclass(frozen=True, slots=True)
class AcceptanceSubject:
    """Everything the formula reads, bound together so nothing is implied.

    ``independence`` and ``posture`` carry no defaults on purpose.  An
    ``IndependenceFacts()`` reads as "nobody produced this and the reviewer holds
    no rights" and an ``ExecutionPosture()`` as "nothing is in flight, nothing is
    cancelled" — both are the most permissive possible worlds, so a default would
    turn *forgetting to look* into *acceptable*.  The caller must state them.
    """

    revision: RequirementsRevision
    package: ReviewPackage
    record: ReviewRecord
    independence: IndependenceFacts
    posture: ExecutionPosture
    semantic_review_required: bool = True
    compound: CompoundFacts | None = None


@dataclass(frozen=True, slots=True)
class AcceptDecision:
    """The decision plus every intermediate result it was built from."""

    reasons: tuple[AcceptReason, ...]
    expression: ExpressionResult
    gate: GateResult
    completeness: CompletenessResult
    independence: IndependenceResult
    missing_root_ids: tuple[str, ...] = ()
    missing_occurrence_ids: tuple[str, ...] = ()
    hard_constraint_structure_ids: tuple[str, ...] = ()

    @property
    def acceptable(self) -> bool:
        return not self.reasons


def _witness_reasons(
    witness: ValidityWitness, *, now_ms: int, current_scope_epoch: int
) -> list[AcceptReason]:
    reasons: list[AcceptReason] = []
    if witness.purpose is not WitnessPurpose.ACCEPT:
        reasons.append(AcceptReason.WITNESS_PURPOSE_MISMATCH)
    if (
        witness.decision is not WitnessDecision.USABLE
        or witness.truth is not TruthValue.TRUE
        or witness.availability is not Availability.READABLE
    ):
        reasons.append(AcceptReason.WITNESS_NOT_USABLE)
    # I19: scope epoch, deadline and freshness together.  The deadline is
    # exclusive — at ``not_after_ms`` the witness is already spent — and the
    # contract's own ``is_fresh_for`` is the single source of that rule.
    if not witness.is_fresh_for(now_ms=now_ms, current_scope_epoch=current_scope_epoch):
        reasons.append(AcceptReason.WITNESS_STALE)
    return reasons


def acceptable(
    subject: AcceptanceSubject,
    *,
    now_ms: int,
    purpose: ReviewPurpose,
    witness: ValidityWitness,
    current_scope_epoch: int,
) -> AcceptDecision:
    """AER §6.2 ``Acceptable(subject, now, purpose)``, as a pure conjunction.

    Each unmet conjunct contributes its own reason code, so a caller never has to
    guess which part of the formula refused; an empty ``reasons`` is the only way
    to be acceptable.  This function decides nothing about *committing* the
    acceptance — §7's transaction, receipts and disclosure stay outside P1.1b.
    """

    revision = subject.revision
    package = subject.package
    record = subject.record
    reasons: list[AcceptReason] = []

    # 1. identity: input / artifact / requirements / review all name the same thing.
    if record.package_id != package.package_id or record.binding != package.binding:
        reasons.append(AcceptReason.IDENTITY_MISMATCH)
    if record.purpose is not package.purpose or record.purpose is not purpose:
        reasons.append(AcceptReason.PURPOSE_MISMATCH)
    if (
        package.binding.requirements_revision != revision.revision
        or package.binding.mission_id != revision.mission_id
    ):
        reasons.append(AcceptReason.REQUIREMENTS_REVISION_MISMATCH)

    # 1b. the package must review the requirement the revision actually states.
    # When the package declares which requirements text it was cut from, that
    # digest is the strong check and covers the criteria catalogue too; a package
    # that declares nothing falls back to comparing the success expression, which
    # is the part an attacker would reshape.
    if package.requirements_content_hash is not None:
        if package.requirements_content_hash != revision.content_hash():
            reasons.append(AcceptReason.REQUIREMENTS_CONTENT_MISMATCH)
    elif success_expression_digest(package.success_expression) != success_expression_digest(
        revision.success_expression
    ):
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_MISMATCH)

    # 1c. every hard constraint must still be an independent AND conjunct of the
    # package's own expression.  ``ReviewPackage`` enforces this only when it
    # declares a ``requirements_content_hash``; an undeclared package is exactly
    # the shape a tampered anchor takes, and a hard constraint moved under an
    # ``any`` there would read as a legitimately declined OR branch downstream.
    structure_violations = package.hard_constraint_violations()
    if structure_violations:
        reasons.append(AcceptReason.HARD_CONSTRAINT_STRUCTURE)

    # 2. AER-V01: a required criterion the package never carried cannot be met.
    catalogue = set(package.criterion_catalogue())
    missing_root = tuple(
        criterion_id
        for criterion_id in revision.required_criterion_ids()
        if criterion_id not in catalogue
    )
    if missing_root:
        reasons.append(AcceptReason.ROOT_CRITERION_MISSING)

    outcomes = outcomes_by_id(record.criteria)

    # 3. the hard gate, evaluated before and apart from the result expression.
    gate = hard_gate(revision, outcomes)
    if gate.failed_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_FAILED)
    if gate.unknown_ids or gate.missing_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_UNKNOWN)

    # 4. the stated success expression of the requirements revision.
    expression = evaluate_success_expression(revision.success_expression, outcomes)
    if not expression.passed:
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_NOT_PASS)

    # 5. the mandatory checks really ran, passed, and carry dispatcher receipts.
    completeness = required_checks_complete(package, record)
    if not completeness.match.matched:
        reasons.append(AcceptReason.CRITERIA_NOT_MATCHED)
    if not completeness.complete:
        reasons.append(AcceptReason.REQUIRED_CHECKS_INCOMPLETE)

    # 6. the review itself concluded ACCEPT, and did so independently when required.
    if record.verdict is not ReviewVerdict.ACCEPT:
        reasons.append(AcceptReason.REVIEW_VERDICT_NOT_ACCEPT)
    independence = independence_ok(package, record, facts=subject.independence)
    independence_required = subject.semantic_review_required or bool(
        completeness.independence_required_ids
    )
    if independence_required and not independence.independent:
        reasons.append(AcceptReason.INDEPENDENT_REVIEW_MISSING)

    # 7. the support for this use is valid, visible and usable right now.
    reasons.extend(
        _witness_reasons(witness, now_ms=now_ms, current_scope_epoch=current_scope_epoch)
    )

    # 8. in-flight execution, cancellation and method adoption allow accepting.
    posture = subject.posture
    if posture.unowned_critical_operation_ids:
        reasons.append(AcceptReason.CRITICAL_OPERATION_UNOWNED)
    if posture.cancellation_requested:
        reasons.append(AcceptReason.CANCELLATION_PENDING)
    if not posture.method_adoption_current:
        reasons.append(AcceptReason.METHOD_ADOPTION_STALE)

    # 9. compound goals: a legal method, every required occurrence, the composition.
    missing_occurrences: tuple[str, ...] = ()
    compound = subject.compound
    if compound is not None:
        if not compound.selected_method_legal:
            reasons.append(AcceptReason.COMPOUND_METHOD_ILLEGAL)
        missing_occurrences = compound.missing_required_occurrences()
        if missing_occurrences:
            reasons.append(AcceptReason.REQUIRED_OCCURRENCE_MISSING)
        if not compound.composition_obligation_passed:
            reasons.append(AcceptReason.COMPOSITION_OBLIGATION_FAILED)

    return AcceptDecision(
        reasons=tuple(reasons),
        expression=expression,
        gate=gate,
        completeness=completeness,
        independence=independence,
        missing_root_ids=missing_root,
        missing_occurrence_ids=missing_occurrences,
        hard_constraint_structure_ids=structure_violations,
    )


# --------------------------------------------------------------------------------------
# Retry, escalation and arbitration (AER §5.4)
# --------------------------------------------------------------------------------------


class RetryAction(StrEnum):
    CONCLUDE = "CONCLUDE"
    RETRY = "RETRY"
    ESCALATE_INDEPENDENT_REVIEWER = "ESCALATE_INDEPENDENT_REVIEWER"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    ARBITRATE = "ARBITRATE"
    STOP = "STOP"


class RetryReason(StrEnum):
    NO_ATTEMPT_YET = "NO_ATTEMPT_YET"
    ACCEPTED = "ACCEPTED"
    CONFLICTING_VERDICTS = "CONFLICTING_VERDICTS"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    CANDIDATE_CHANGED = "CANDIDATE_CHANGED"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    RESAMPLING_NOT_PERMITTED = "RESAMPLING_NOT_PERMITTED"
    ATTEMPT_LIMIT_REACHED = "ATTEMPT_LIMIT_REACHED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ESCALATION_EXHAUSTED = "ESCALATION_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class ReviewAttempt:
    """One review of one candidate version, with its infrastructure axis kept apart."""

    record_id: ReviewRecordId
    reviewer_agent_id: str
    verdict: ReviewVerdict
    subject_content_hash: str
    infrastructure_error: bool = False


@dataclass(frozen=True, slots=True)
class ReviewHistory:
    """What has already been spent on this obligation's review budget."""

    attempts: tuple[ReviewAttempt, ...] = ()
    budget_remaining: int = 0
    attempt_limit: int = 1
    escalated_independent: bool = False
    escalated_human: bool = False
    candidate_changed_since_last_attempt: bool = False
    policy_allows_human: bool = True


@dataclass(frozen=True, slots=True)
class RetryDecision:
    action: RetryAction
    reason: RetryReason
    consumes_budget: bool


def _conflicting(attempts: Sequence[ReviewAttempt]) -> bool:
    """Two semantic verdicts that disagree about the same candidate version."""

    by_subject: dict[str, set[ReviewVerdict]] = {}
    for item in attempts:
        if item.infrastructure_error:
            continue
        by_subject.setdefault(item.subject_content_hash, set()).add(item.verdict)
    for verdicts in by_subject.values():
        if ReviewVerdict.ACCEPT in verdicts and len(verdicts) > 1:
            return True
    return False


def _escalate(history: ReviewHistory, reason: RetryReason) -> RetryDecision:
    if not history.escalated_independent:
        return RetryDecision(
            action=RetryAction.ESCALATE_INDEPENDENT_REVIEWER,
            reason=reason,
            consumes_budget=True,
        )
    if history.policy_allows_human and not history.escalated_human:
        return RetryDecision(action=RetryAction.ESCALATE_HUMAN, reason=reason, consumes_budget=True)
    return RetryDecision(
        action=RetryAction.STOP,
        reason=RetryReason.ESCALATION_EXHAUSTED,
        consumes_budget=True,
    )


def retry_policy(history: ReviewHistory) -> RetryDecision:
    """AER §5.4: bounded retry, escalation and stopping share one obligation budget.

    Re-running a reviewer against an unchanged candidate in the hope of a PASS is
    refused outright — the ladder goes to an independent reviewer, then to a
    human, then stops.  An infrastructure failure is not a semantic verdict, so it
    may be retried.  Reviews that disagree about the same candidate go to
    arbitration; counting them would be a majority vote, which §5.4 forbids.
    """

    attempts = history.attempts
    if not attempts:
        return RetryDecision(
            action=RetryAction.RETRY, reason=RetryReason.NO_ATTEMPT_YET, consumes_budget=True
        )
    if _conflicting(attempts):
        return RetryDecision(
            action=RetryAction.ARBITRATE,
            reason=RetryReason.CONFLICTING_VERDICTS,
            consumes_budget=True,
        )
    if history.budget_remaining <= 0:
        return RetryDecision(
            action=RetryAction.STOP,
            reason=RetryReason.BUDGET_EXHAUSTED,
            consumes_budget=False,
        )

    last = attempts[-1]
    if last.infrastructure_error:
        if len(attempts) >= history.attempt_limit:
            return _escalate(history, RetryReason.ATTEMPT_LIMIT_REACHED)
        return RetryDecision(
            action=RetryAction.RETRY,
            reason=RetryReason.INFRASTRUCTURE_ERROR,
            consumes_budget=True,
        )
    if last.verdict is ReviewVerdict.ACCEPT:
        return RetryDecision(
            action=RetryAction.CONCLUDE, reason=RetryReason.ACCEPTED, consumes_budget=False
        )
    if last.verdict is ReviewVerdict.REJECTED:
        return RetryDecision(
            action=RetryAction.STOP,
            reason=RetryReason.CANDIDATE_REJECTED,
            consumes_budget=True,
        )
    if len(attempts) >= history.attempt_limit:
        return _escalate(history, RetryReason.ATTEMPT_LIMIT_REACHED)
    if last.verdict is ReviewVerdict.REWORK and history.candidate_changed_since_last_attempt:
        return RetryDecision(
            action=RetryAction.RETRY,
            reason=RetryReason.CANDIDATE_CHANGED,
            consumes_budget=True,
        )
    return _escalate(history, RetryReason.RESAMPLING_NOT_PERMITTED)


__all__ = (
    "CONCLUSIVE_EXECUTION",
    "DISPATCHER_PROVENANCE",
    "DISPATCHER_RECEIPT_KINDS",
    "AcceptDecision",
    "AcceptReason",
    "AcceptanceSubject",
    "CompletenessResult",
    "CompoundFacts",
    "ExecutionPosture",
    "ExpressionResult",
    "GateResult",
    "IndependenceFacts",
    "IndependenceReason",
    "IndependenceResult",
    "ReceiptSource",
    "RetryAction",
    "RetryDecision",
    "RetryReason",
    "ReviewAttempt",
    "ReviewHistory",
    "acceptable",
    "evaluate_success_expression",
    "hard_gate",
    "independence_ok",
    "outcomes_by_id",
    "project_verdict",
    "receipt_source",
    "required_checks_complete",
    "retry_policy",
    "success_expression_digest",
)
