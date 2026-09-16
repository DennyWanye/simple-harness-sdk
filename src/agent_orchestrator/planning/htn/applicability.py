# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Safe condition evaluation and method applicability (§6.6, §18.3).

This module is pure: it walks a structured AST, reads a frozen
:class:`~agent_orchestrator.contracts.evidence_state.EvidenceSnapshot`, and returns a
report.  It never calls a tool, never touches a store and has no side effects.

The combination rules are the ones §6.6 v1.2 pinned, and they are *not* the
``(t, f)`` arithmetic of AER §8.3.  ``(t, f)`` merges evidence about **one**
proposition; between propositions the priority tables below decide, and the two
disagree in exactly the two places the tests pin:

===========================  ===================  ==================
expression                   ``(t, f)`` arithmetic  priority table
===========================  ===================  ==================
``ANY(UNKNOWN, CONFLICT)``   TRUE  (would pass)   CONFLICT (blocks)
``ALL(UNKNOWN, CONFLICT)``   FALSE               CONFLICT
===========================  ===================  ==================

``NOT`` swaps TRUE and FALSE and leaves UNKNOWN and CONFLICT alone, which is why
``ALL(p, NOT p)`` is *not* FALSE when ``p`` is UNKNOWN or CONFLICT.  That is the
deliberate conservative reading of ADR-07, not an oversight to be tidied up.

Empty ``ALL`` is TRUE and empty ``ANY`` is FALSE, but an empty expression can
never open an authorisation gate: :func:`authorization_gate` admits only a TRUE
all of whose leaves came from real observations or an authoritative closed-domain
denial.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...contracts.evidence_state import EvidenceSnapshot, TruthValue
from ...contracts.htn import (
    AllCondition,
    AnyCondition,
    ArrayValue,
    ConstantCondition,
    ConstantValue,
    MethodContract,
    MethodInstanceDraft,
    NotCondition,
    ObjectValue,
    OutputValue,
    ParameterValue,
    PredicateCondition,
    TaskSemanticBindingV1,
    condition_digest,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import enum_of, flag, identifier
from ...knowledge.predicates import (
    PredicateRegistry,
    PredicateSignature,
    WorldAssumption,
    atom_truth,
    closed_world_denial_admissible,
    proposition_key,
)


class SupportProvenance(StrEnum):
    """Where a leaf's truth came from — the basis of the authorisation rule."""

    #: A recorded observation supports (or denies) the proposition.
    OBSERVED = "observed"
    #: A closed domain produced an authoritative negative observation (§6.6 C28).
    CLOSED_WORLD_AUTHORITATIVE = "closed_world_authoritative"
    #: Nothing is recorded; the leaf is UNKNOWN.
    MISSING = "missing"
    #: Support exists but is no longer CURRENT, or is unreadable.
    STALE = "stale"
    #: A literal or an empty ALL / ANY — structural, not evidential.
    CONSTANT = "constant"
    #: The predicate or its arguments could not be resolved.
    UNRESOLVED = "unresolved"


#: Only these two ever back an authorisation decision (§6.6 rule 2).
AUTHORIZING_PROVENANCE = frozenset(
    {SupportProvenance.OBSERVED, SupportProvenance.CLOSED_WORLD_AUTHORITATIVE}
)


def not_truth(value: TruthValue) -> TruthValue:
    """§6.6 rule 1: ``NOT`` swaps TRUE/FALSE and preserves UNKNOWN and CONFLICT."""

    if value is TruthValue.TRUE:
        return TruthValue.FALSE
    if value is TruthValue.FALSE:
        return TruthValue.TRUE
    return value


def all_truth(values: tuple[TruthValue, ...]) -> TruthValue:
    """FALSE ≻ CONFLICT ≻ UNKNOWN ≻ TRUE.  Empty ALL is TRUE."""

    if TruthValue.FALSE in values:
        return TruthValue.FALSE
    if TruthValue.CONFLICT in values:
        return TruthValue.CONFLICT
    if TruthValue.UNKNOWN in values:
        return TruthValue.UNKNOWN
    return TruthValue.TRUE


def any_truth(values: tuple[TruthValue, ...]) -> TruthValue:
    """TRUE ≻ CONFLICT ≻ UNKNOWN ≻ FALSE.  Empty ANY is FALSE."""

    if TruthValue.TRUE in values:
        return TruthValue.TRUE
    if TruthValue.CONFLICT in values:
        return TruthValue.CONFLICT
    if TruthValue.UNKNOWN in values:
        return TruthValue.UNKNOWN
    return TruthValue.FALSE


@dataclass(frozen=True, slots=True)
class ConditionEvaluation:
    """The value of one condition plus what would be needed to improve it."""

    truth: TruthValue
    provenance: frozenset[SupportProvenance] = frozenset()
    type_errors: tuple[str, ...] = ()
    unknown_propositions: tuple[str, ...] = ()
    conflicting_propositions: tuple[str, ...] = ()

    def merged_with(
        self, others: tuple[ConditionEvaluation, ...], truth: TruthValue
    ) -> ConditionEvaluation:
        provenance: set[SupportProvenance] = set(self.provenance)
        type_errors: list[str] = list(self.type_errors)
        unknown: list[str] = list(self.unknown_propositions)
        conflicts: list[str] = list(self.conflicting_propositions)
        for other in others:
            provenance |= other.provenance
            type_errors.extend(other.type_errors)
            unknown.extend(other.unknown_propositions)
            conflicts.extend(other.conflicting_propositions)
        return ConditionEvaluation(
            truth=truth,
            provenance=frozenset(provenance),
            type_errors=tuple(dict.fromkeys(type_errors)),
            unknown_propositions=tuple(dict.fromkeys(unknown)),
            conflicting_propositions=tuple(dict.fromkeys(conflicts)),
        )


def _combine(children: tuple[ConditionEvaluation, ...], truth: TruthValue) -> ConditionEvaluation:
    seed = ConditionEvaluation(truth=truth)
    if not children:
        return ConditionEvaluation(truth=truth, provenance=frozenset({SupportProvenance.CONSTANT}))
    return seed.merged_with(children, truth)


def ground_value(value: Any, parameters: Mapping[str, Any], *, path: str, errors: list[str]) -> Any:
    """Resolve a structured value expression against the task's typed parameters.

    A precondition that reads a *step output* is a type error, not an UNKNOWN: at
    selection time that output does not exist, and treating it as merely unknown
    would invite an evidence hunt for something no observer can produce.
    """

    if isinstance(value, ConstantValue):
        return value.value
    if isinstance(value, ParameterValue):
        if value.name not in parameters:
            errors.append(f"{path}: no bound parameter named {value.name!r}")
            return None
        return parameters[value.name]
    if isinstance(value, OutputValue):
        errors.append(
            f"{path}: a precondition may not read step output {value.step}.{value.port} (§6.6)"
        )
        return None
    if isinstance(value, ObjectValue):
        return {
            key: ground_value(item, parameters, path=f"{path}.{key}", errors=errors)
            for key, item in value.fields.items()
        }
    if isinstance(value, ArrayValue):
        return [
            ground_value(item, parameters, path=f"{path}[{position}]", errors=errors)
            for position, item in enumerate(value.items)
        ]
    errors.append(f"{path}: unsupported value expression")
    return None


def evaluate_condition(
    condition: Any,
    *,
    registry: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    parameters: Mapping[str, Any],
    now_ms: int | None = None,
    path: str = "condition",
) -> ConditionEvaluation:
    """Evaluate one structured condition four-valued, with no side effects."""

    if isinstance(condition, ConstantCondition):
        return ConditionEvaluation(
            truth=TruthValue.TRUE if condition.value else TruthValue.FALSE,
            provenance=frozenset({SupportProvenance.CONSTANT}),
        )
    if isinstance(condition, NotCondition):
        inner = evaluate_condition(
            condition.item,
            registry=registry,
            snapshot=snapshot,
            parameters=parameters,
            now_ms=now_ms,
            path=f"{path}.not",
        )
        return inner.merged_with((), not_truth(inner.truth))
    if isinstance(condition, (AllCondition, AnyCondition)):
        children = tuple(
            evaluate_condition(
                item,
                registry=registry,
                snapshot=snapshot,
                parameters=parameters,
                now_ms=now_ms,
                path=f"{path}[{position}]",
            )
            for position, item in enumerate(condition.items)
        )
        values = tuple(child.truth for child in children)
        truth = all_truth(values) if isinstance(condition, AllCondition) else any_truth(values)
        return _combine(children, truth)
    if isinstance(condition, PredicateCondition):
        return _evaluate_atom(
            condition,
            registry=registry,
            snapshot=snapshot,
            parameters=parameters,
            now_ms=now_ms,
            path=path,
        )
    raise ContractError(
        f"{path} is not a structured condition; the interpreter never evaluates raw "
        "strings or callables (§7.3 step 1)"
    )


def _evaluate_atom(
    condition: PredicateCondition,
    *,
    registry: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    parameters: Mapping[str, Any],
    now_ms: int | None,
    path: str,
) -> ConditionEvaluation:
    signature = registry.resolve(condition.predicate_ref)
    if signature is None:
        return ConditionEvaluation(
            truth=TruthValue.UNKNOWN,
            provenance=frozenset({SupportProvenance.UNRESOLVED}),
            type_errors=(
                f"{path}: predicate {condition.predicate_ref.id!r} "
                f"v{condition.predicate_ref.version} is not registered",
            ),
        )
    errors: list[str] = []
    arguments = {
        name: ground_value(item, parameters, path=f"{path}.{name}", errors=errors)
        for name, item in condition.arguments.items()
    }
    if errors:
        return ConditionEvaluation(
            truth=TruthValue.UNKNOWN,
            provenance=frozenset({SupportProvenance.UNRESOLVED}),
            type_errors=tuple(errors),
        )
    check = registry.check_arguments(signature, arguments)
    if not check.ok:
        return ConditionEvaluation(
            truth=TruthValue.UNKNOWN,
            provenance=frozenset({SupportProvenance.UNRESOLVED}),
            type_errors=tuple(f"{path}: {message}" for message in check.messages()),
        )
    key = proposition_key(signature, arguments)
    entry = snapshot.lookup(key)
    truth = atom_truth(signature, entry, now_ms=now_ms)
    provenance = _atom_provenance(signature, entry, truth, now_ms=now_ms)
    unknown = (key,) if truth is TruthValue.UNKNOWN else ()
    conflicts = (key,) if truth is TruthValue.CONFLICT else ()
    return ConditionEvaluation(
        truth=truth,
        provenance=frozenset({provenance}),
        unknown_propositions=unknown,
        conflicting_propositions=conflicts,
    )


def _atom_provenance(
    signature: PredicateSignature,
    entry: Any,
    truth: TruthValue,
    *,
    now_ms: int | None,
) -> SupportProvenance:
    if entry is None:
        return SupportProvenance.MISSING
    if truth is TruthValue.UNKNOWN and (entry.support.t or entry.support.f):
        # There is support, but it is not usable now (stale, revoked, unreadable,
        # or past not_after) — that is different from never having looked.
        return SupportProvenance.STALE
    if truth is TruthValue.UNKNOWN:
        return SupportProvenance.MISSING
    if signature.world_assumption is WorldAssumption.CLOSED and closed_world_denial_admissible(
        signature, entry
    ):
        return SupportProvenance.CLOSED_WORLD_AUTHORITATIVE
    return SupportProvenance.OBSERVED


@dataclass(frozen=True, slots=True)
class GateDecision:
    """§6.6 rule 2: whether this evaluation may authorise a real action."""

    allowed: bool
    reason: str
    truth: TruthValue
    provenance: frozenset[SupportProvenance] = frozenset()


def authorization_gate(evaluation: ConditionEvaluation) -> GateDecision:
    """Admit only a TRUE that every leaf backed with real or authoritative evidence.

    Invariant I18: UNKNOWN and CONFLICT never open a safety gate.  Mixed
    evaluations are refused too — a result that combined a closed-domain denial
    with an open-domain UNKNOWN is not a fact about the world whatever value it
    ended up with, and neither is a TRUE that leaned on an empty ALL.
    """

    if evaluation.truth is not TruthValue.TRUE:
        return GateDecision(
            allowed=False,
            reason=f"authorisation requires TRUE, not {evaluation.truth!s}",
            truth=evaluation.truth,
            provenance=evaluation.provenance,
        )
    unsupported = evaluation.provenance - AUTHORIZING_PROVENANCE
    if unsupported:
        return GateDecision(
            allowed=False,
            reason=(
                "authorisation accepts only evidence-backed leaves; this TRUE also rests on "
                + ", ".join(sorted(str(item) for item in unsupported))
            ),
            truth=evaluation.truth,
            provenance=evaluation.provenance,
        )
    if not evaluation.provenance:
        return GateDecision(
            allowed=False,
            reason="authorisation needs at least one evidential leaf",
            truth=evaluation.truth,
            provenance=evaluation.provenance,
        )
    return GateDecision(
        allowed=True,
        reason="every leaf is supported by a current observation or an authoritative denial",
        truth=evaluation.truth,
        provenance=evaluation.provenance,
    )


# --------------------------------------------------------------------------------------
# Capabilities (§14.2)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """§14.2: registered, configured, reachable, healthy, authorised, compatible.

    They are recorded separately because they fail separately; the planner is told
    what the deployment actually has, not what a model guessed.
    """

    capability_id: str
    registered: bool = True
    configured: bool = True
    reachable: bool = True
    healthy: bool = True
    authorized: bool = True
    compatible: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability_id", identifier(self.capability_id, "capability.capability_id")
        )
        for name in (
            "registered",
            "configured",
            "reachable",
            "healthy",
            "authorized",
            "compatible",
        ):
            object.__setattr__(self, name, flag(getattr(self, name), f"capability.{name}"))

    @property
    def available(self) -> bool:
        return (
            self.registered
            and self.configured
            and self.reachable
            and self.healthy
            and self.authorized
            and self.compatible
        )

    def unavailable_reasons(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "registered",
                "configured",
                "reachable",
                "healthy",
                "authorized",
                "compatible",
            )
            if not getattr(self, name)
        )


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """The deployment facts a planner is allowed to rely on."""

    records: tuple[CapabilityRecord, ...] = field(default_factory=tuple)

    def lookup(self, capability_id: str) -> CapabilityRecord | None:
        for record in self.records:
            if record.capability_id == capability_id:
                return record
        return None

    def available(self, capability_id: str) -> bool:
        record = self.lookup(capability_id)
        return record is not None and record.available

    def missing_from(self, required: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item for item in required if not self.available(item))


# --------------------------------------------------------------------------------------
# assess_method (§18.3)
# --------------------------------------------------------------------------------------


class ApplicabilityStatus(StrEnum):
    """Why a method may or may not be selected here."""

    APPLICABLE = "APPLICABLE"
    PRECONDITION_FALSE = "PRECONDITION_FALSE"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    CONFLICT = "CONFLICT"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    TYPE_ERROR = "TYPE_ERROR"


@dataclass(frozen=True, slots=True)
class ApplicabilityReport:
    """§18.3 ``assess_method`` output.  Nothing was executed to produce it."""

    status: ApplicabilityStatus
    truth: TruthValue
    provenance: frozenset[SupportProvenance] = frozenset()
    unmet_capabilities: tuple[str, ...] = ()
    needs_evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    type_errors: tuple[str, ...] = ()
    authorization: GateDecision | None = None
    precondition_digests: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        return self.status is ApplicabilityStatus.APPLICABLE


def assess_method(
    task: TaskSemanticBindingV1,
    method: MethodContract,
    snapshot: EvidenceSnapshot,
    capabilities: CapabilitySnapshot,
    *,
    registry: PredicateRegistry,
    now_ms: int | None = None,
) -> ApplicabilityReport:
    """§18.3: decide applicable / not applicable / needs evidence / conflict / unavailable.

    Order of precedence: a structural type error first (the question is
    ill-posed), then a missing capability (a deployment fact the evidence cannot
    change), then the four-valued precondition verdict.  No tool is called and no
    state is written; exploration assumptions are evaluated but never counted as
    authorising support.
    """

    if not isinstance(task, TaskSemanticBindingV1):
        raise ContractError("assess_method expects a TaskSemanticBindingV1")
    if not isinstance(method, MethodContract):
        raise ContractError("assess_method expects a MethodContract")
    if not isinstance(snapshot, EvidenceSnapshot):
        raise ContractError("assess_method expects an EvidenceSnapshot")
    if not isinstance(capabilities, CapabilitySnapshot):
        raise ContractError("assess_method expects a CapabilitySnapshot")

    parameters = dict(task.typed_parameters)
    children = tuple(
        evaluate_condition(
            condition,
            registry=registry,
            snapshot=snapshot,
            parameters=parameters,
            now_ms=now_ms,
            path=f"applicable_when[{position}]",
        )
        for position, condition in enumerate(method.applicable_when)
    )
    values = tuple(child.truth for child in children)
    evaluation = _combine(children, all_truth(values))
    digests = tuple(condition_digest(item) for item in method.applicable_when)

    required = tuple(
        dict.fromkeys(
            list(method.required_capabilities)
            + [item for step in method.steps for item in step.required_capabilities]
        )
    )
    unmet = capabilities.missing_from(required)
    gate = authorization_gate(evaluation)

    if evaluation.type_errors:
        status = ApplicabilityStatus.TYPE_ERROR
    elif unmet:
        status = ApplicabilityStatus.CAPABILITY_UNAVAILABLE
    elif evaluation.truth is TruthValue.FALSE:
        status = ApplicabilityStatus.PRECONDITION_FALSE
    elif evaluation.truth is TruthValue.CONFLICT:
        status = ApplicabilityStatus.CONFLICT
    elif evaluation.truth is TruthValue.UNKNOWN:
        status = ApplicabilityStatus.NEEDS_EVIDENCE
    else:
        status = ApplicabilityStatus.APPLICABLE

    return ApplicabilityReport(
        status=status,
        truth=evaluation.truth,
        provenance=evaluation.provenance,
        unmet_capabilities=unmet,
        needs_evidence=evaluation.unknown_propositions,
        conflicts=evaluation.conflicting_propositions,
        type_errors=evaluation.type_errors,
        authorization=gate,
        precondition_digests=digests,
    )


class RecheckStatus(StrEnum):
    """§6.6 rule 3: the pre-dispatch re-check verdict for a method instance."""

    CONSISTENT = "CONSISTENT"
    METHOD_INSTANCE_INVALIDATED = "METHOD_INSTANCE_INVALIDATED"


@dataclass(frozen=True, slots=True)
class RecheckResult:
    status: RecheckStatus
    changed_digests: tuple[str, ...] = ()
    reason: str = ""

    @property
    def consistent(self) -> bool:
        return self.status is RecheckStatus.CONSISTENT


def recheck_method_instance(
    draft: MethodInstanceDraft,
    method: MethodContract,
    snapshot: EvidenceSnapshot,
    *,
    registry: PredicateRegistry,
    parameters: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> RecheckResult:
    """Compare the witnesses frozen at selection time against the world now.

    A disagreement invalidates *this method instance* — §6.6 rule 3 — and the
    caller takes the "method precondition overturned" branch of the §9.1 decision
    table.  It is deliberately not reported as CONFLICT (the world may be perfectly
    consistent; it simply moved) and it does not by itself trigger a global re-plan.
    """

    if not isinstance(draft, MethodInstanceDraft):
        raise ContractError("recheck_method_instance expects a MethodInstanceDraft")
    grounded = dict(parameters or {})
    for binding in draft.grounded_parameters:
        grounded.setdefault(binding.name, binding.value)
    by_digest = {condition_digest(item): item for item in method.applicable_when}
    changed: list[str] = []
    for witness in draft.precondition_witnesses:
        condition = by_digest.get(witness.condition_digest)
        if condition is None:
            changed.append(witness.condition_digest)
            continue
        current = evaluate_condition(
            condition,
            registry=registry,
            snapshot=snapshot,
            parameters=grounded,
            now_ms=now_ms,
            path="recheck",
        )
        if current.truth is not witness.truth:
            changed.append(witness.condition_digest)
    if changed:
        return RecheckResult(
            status=RecheckStatus.METHOD_INSTANCE_INVALIDATED,
            changed_digests=tuple(changed),
            reason=(
                "the snapshot witnesses disagree with the pre-dispatch re-check; "
                "this method instance is invalid (§6.6 rule 3)"
            ),
        )
    return RecheckResult(status=RecheckStatus.CONSISTENT, reason="witnesses still hold")


def truth_of(value: object, name: str = "truth") -> TruthValue:
    return enum_of(TruthValue, value, name)


__all__ = (
    "AUTHORIZING_PROVENANCE",
    "ApplicabilityReport",
    "ApplicabilityStatus",
    "CapabilityRecord",
    "CapabilitySnapshot",
    "ConditionEvaluation",
    "GateDecision",
    "RecheckResult",
    "RecheckStatus",
    "SupportProvenance",
    "all_truth",
    "any_truth",
    "assess_method",
    "authorization_gate",
    "evaluate_condition",
    "ground_value",
    "not_truth",
    "recheck_method_instance",
    "truth_of",
)
