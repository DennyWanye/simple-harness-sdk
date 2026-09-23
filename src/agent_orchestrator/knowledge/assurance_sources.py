# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Typed Assurance propositions, fresh anchor selection and admitted rules.

Pure evaluation over rows the orchestrator validity service has already read
from one consistent snapshot and source-checked. Nothing here reads a Store,
grants access, or accepts a prior closure/witness as a seed: every call
re-selects anchors from the supplied observations and current check results
and recomputes the bounded grounded and clean closures (AER §9.2, plan §8.3).

The conclusion of an ACCEPT use is a registered system predicate whose only
admitted rule is the fixed acceptance rule below: the official review must be
acceptable under the *current* typed check results and every check it consumed
must currently PASS. Deployment observations and stored justification sets
take part only through registered signatures and explicitly admitted rules; an
unregistered predicate or an unadmitted rule is recorded as rejected and never
fires (plan cases V01–V05).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..assurance.checks import Grade
from ..assurance.codec import AssuranceError, fingerprint, integer, text
from ..assurance.grounding import GroundedSupport, compute_grounded_support
from ..assurance.refs import AssuranceRef, Pin
from ..contracts.evidence_state import (
    ObservationRecord,
    QueryCompleteness,
    TruthValue,
    WitnessPurpose,
)
from ..contracts.models import ContractError
from ..contracts.semantic_base import (
    EvidenceRef,
    EvidenceRefKind,
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
)
from .justifications import (
    Anchor,
    AnchorCandidate,
    AnchorOrigin,
    AnchorSelection,
    AnchorSelector,
    Atom,
    EvidencePremise,
    JustificationSet,
    Polarity,
    PropositionPremise,
    RejectedAnchor,
    SupportGraph,
    SupportWitness,
    WitnessKind,
)
from .predicates import ArgumentType, PredicateParameter, PredicateSignature, proposition_key

#: Fixed system observer identity for review/check anchors. It is not a Host or
#: model identity and is never accepted from a request.
ASSURANCE_OBSERVER = "assurance-validity-v1"
ACCEPT_RULE_ID = "assurance-accept-v1"
ACCEPT_RULE_VERSION = "assurance-accept-v1"

#: Every deployment-admitted predicate carries a content hash; the system
#: predicates hash their own frozen declaration so a renamed or re-typed
#: declaration is a different predicate, never a silent replacement.


def _system_predicate(
    predicate_id: str, parameters: tuple[str, ...], statement: str
) -> PredicateSignature:
    declaration = {"id": predicate_id, "version": 1, "parameters": list(parameters)}
    return PredicateSignature(
        predicate_ref=VersionedRef(predicate_id, 1, content_hash_of(declaration)),
        parameters=tuple(PredicateParameter(name, ArgumentType.STRING) for name in parameters),
        observer_ids=(ASSURANCE_OBSERVER,),
        statement=statement,
    )


REVIEW_ACCEPTED = _system_predicate(
    "assurance.review-accepted",
    ("mission_id", "record_id"),
    "the official Assurance review record is acceptable under current typed checks",
)
CHECK_PASSED = _system_predicate(
    "assurance.check-passed",
    ("mission_id", "check_binding_id"),
    "the exact imported check binding currently normalizes to PASS",
)
CONTENT_ACCEPTABLE = _system_predicate(
    "assurance.content-acceptable",
    ("mission_id", "scope_id", "subject_hash"),
    "the reviewed subject may be accepted for this completion scope",
)

SYSTEM_PREDICATES: Mapping[str, PredicateSignature] = {
    signature.predicate_ref.id: signature
    for signature in (REVIEW_ACCEPTED, CHECK_PASSED, CONTENT_ACCEPTABLE)
}


def _is_system_key(proposition: str) -> bool:
    predicate_id, _, _ = proposition.partition("@")
    return predicate_id in SYSTEM_PREDICATES


def review_accepted_key(mission_id: str, record_id: str) -> str:
    return proposition_key(
        REVIEW_ACCEPTED, {"mission_id": text(mission_id), "record_id": text(record_id)}
    )


def check_passed_key(mission_id: str, check_binding_id: str) -> str:
    return proposition_key(
        CHECK_PASSED,
        {"mission_id": text(mission_id), "check_binding_id": text(check_binding_id)},
    )


def content_acceptable_key(mission_id: str, scope_id: str, subject_hash: str) -> str:
    return proposition_key(
        CONTENT_ACCEPTABLE,
        {
            "mission_id": text(mission_id),
            "scope_id": text(scope_id),
            "subject_hash": text(subject_hash),
        },
    )


@dataclass(frozen=True, slots=True)
class AdmittedRule:
    """A deployment-approved rule shape for stored justification sets.

    ``subject_kind`` is the stored set's subject kind; the set's subject id is
    the single typed argument of ``conclusion``. Admission is an explicit
    deployment/approval fact, never inferred from a stored ``rule_ref`` alone.
    """

    rule_id: str
    rule_version: str
    subject_kind: str
    conclusion: PredicateSignature

    def __post_init__(self) -> None:
        text(self.rule_id)
        text(self.rule_version)
        text(self.subject_kind)
        if not isinstance(self.conclusion, PredicateSignature) or [
            parameter.name for parameter in self.conclusion.parameters
        ] != ["subject_id"]:
            raise AssuranceError("RULE_ADMISSION_INVALID", self.rule_id)


@dataclass(frozen=True, slots=True)
class ReviewAnchorInput:
    """The authenticated official review, re-decided under current checks."""

    record_ref: AssuranceRef
    acceptable: bool
    observed_at_ms: int

    def __post_init__(self) -> None:
        if self.record_ref.kind != "review" or type(self.acceptable) is not bool:
            raise AssuranceError("REVIEW_ANCHOR_INVALID")
        integer(self.observed_at_ms)


@dataclass(frozen=True, slots=True)
class CheckAnchorInput:
    """One consumed check binding with its *current* normalized grade."""

    binding_ref: AssuranceRef
    grade: Grade
    observed_at_ms: int
    not_after_ms: int | None

    def __post_init__(self) -> None:
        if self.binding_ref.kind != "check_binding" or not isinstance(self.grade, Grade):
            raise AssuranceError("CHECK_ANCHOR_INVALID")
        integer(self.observed_at_ms)
        if self.not_after_ms is not None:
            integer(self.not_after_ms)


@dataclass(frozen=True, slots=True)
class ObservationInput:
    """One stored observation row: the decoded record, its scope and exact hash."""

    record: ObservationRecord
    scope_id: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class JustificationInput:
    """One stored justification set row with its members (subject → members)."""

    set_id: str
    subject_kind: str
    subject_id: str
    rule_ref: str | None
    members: tuple[tuple[TypedRef, bool], ...]


@dataclass(frozen=True, slots=True)
class SupportEvaluation:
    conclusion_key: str
    truth: TruthValue
    usable: bool
    clean_support_refs: tuple[AssuranceRef, ...]
    admitted_anchor_ids: tuple[str, ...]
    rejected_anchors: tuple[tuple[str, str], ...]
    rejected_rules: tuple[tuple[str, str], ...]
    earliest_expiry_ms: int | None
    reasons: tuple[str, ...]


def _purpose(value: str) -> WitnessPurpose:
    try:
        return WitnessPurpose(value)
    except ValueError as error:
        raise AssuranceError("USE_PURPOSE_INVALID", value) from error


def _review_candidate(
    review: ReviewAnchorInput, *, mission_id: str, scope_id: str
) -> AnchorCandidate:
    key = review_accepted_key(mission_id, review.record_ref.pin.id)
    return AnchorCandidate(
        observation=ObservationRecord(
            observation_id="assurance-review:" + review.record_ref.pin.id,
            proposition_key=key,
            polarity=review.acceptable,
            source_ref=TypedRef(
                kind=TypedRefKind.REVIEW,
                id=review.record_ref.pin.id,
                revision=review.record_ref.pin.revision,
                content_hash=review.record_ref.pin.content_hash,
            ),
            observed_at_ms=review.observed_at_ms,
            recorded_at_ms=review.observed_at_ms,
            coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE,
            coverage_scope=scope_id,
            query_watermark_ms=review.observed_at_ms,
            observer_id=ASSURANCE_OBSERVER,
        ),
        scope_id=scope_id,
        source_group="assurance-review:" + review.record_ref.pin.id,
        origin=AnchorOrigin.CHECK,
        evidence_ref=EvidenceRef(
            kind=EvidenceRefKind.REVIEW,
            id=review.record_ref.pin.id,
            revision=review.record_ref.pin.revision,
            content_hash=review.record_ref.pin.content_hash,
        ),
        observer_id=ASSURANCE_OBSERVER,
    )


def _check_candidate(
    check: CheckAnchorInput, *, mission_id: str, scope_id: str
) -> AnchorCandidate | None:
    # UNKNOWN is not a denial: it contributes no literal at all (§6.6 C28).
    if check.grade is Grade.UNKNOWN:
        return None
    key = check_passed_key(mission_id, check.binding_ref.pin.id)
    return AnchorCandidate(
        observation=ObservationRecord(
            observation_id="assurance-check:" + check.binding_ref.pin.id,
            proposition_key=key,
            polarity=check.grade is Grade.PASS,
            source_ref=TypedRef(
                kind=TypedRefKind.TOOL_RECEIPT,
                id=check.binding_ref.pin.id,
                revision=check.binding_ref.pin.revision,
                content_hash=check.binding_ref.pin.content_hash,
            ),
            observed_at_ms=check.observed_at_ms,
            recorded_at_ms=check.observed_at_ms,
            coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE,
            coverage_scope=scope_id,
            query_watermark_ms=check.observed_at_ms,
            valid_until_ms=check.not_after_ms,
            observer_id=ASSURANCE_OBSERVER,
        ),
        scope_id=scope_id,
        source_group="assurance-check:" + check.binding_ref.pin.id,
        origin=AnchorOrigin.CHECK,
        evidence_ref=EvidenceRef(
            kind=EvidenceRefKind.TOOL_RECEIPT,
            id=check.binding_ref.pin.id,
            revision=check.binding_ref.pin.revision,
            content_hash=check.binding_ref.pin.content_hash,
        ),
        observer_id=ASSURANCE_OBSERVER,
    )


def _observation_candidate(row: ObservationInput) -> AnchorCandidate:
    record = row.record
    return AnchorCandidate(
        observation=record,
        scope_id=row.scope_id,
        source_group=f"{record.source_ref.kind!s}:{record.source_ref.id}",
        origin=AnchorOrigin.OBSERVATION,
        evidence_ref=EvidenceRef(
            kind=EvidenceRefKind.OBSERVATION,
            id=record.observation_id,
            revision=1,
            content_hash=row.content_hash,
        ),
        observer_id=record.observer_id,
    )


def _select(
    candidates: Sequence[AnchorCandidate],
    *,
    purpose: WitnessPurpose,
    now_ms: int,
    signatures: Mapping[str, PredicateSignature],
) -> AnchorSelection:
    """One selector per scope; selection never crosses scopes."""
    anchors: list[Anchor] = []
    rejected: list[RejectedAnchor] = []
    by_scope: dict[str, list[AnchorCandidate]] = {}
    for candidate in candidates:
        by_scope.setdefault(candidate.scope_id, []).append(candidate)
    for scope_id in sorted(by_scope):
        selection = AnchorSelector(
            scope_id=scope_id, purpose=purpose, as_of_ms=now_ms, signatures=signatures
        ).select(by_scope[scope_id])
        anchors.extend(selection.anchors)
        rejected.extend(selection.rejected)
    return AnchorSelection(anchors=tuple(anchors), rejected=tuple(rejected))


def _admit_stored_rules(
    stored: Sequence[JustificationInput],
    *,
    observations: Mapping[str, ObservationInput],
    admitted_rules: Mapping[str, AdmittedRule],
) -> tuple[list[JustificationSet], list[tuple[str, str]]]:
    rules: list[JustificationSet] = []
    rejected: list[tuple[str, str]] = []
    for row in stored:
        rule = None if row.rule_ref is None else admitted_rules.get(row.rule_ref)
        if rule is None:
            rejected.append((row.set_id, "RULE_NOT_ADMITTED"))
            continue
        if rule.subject_kind != row.subject_kind:
            rejected.append((row.set_id, "RULE_SUBJECT_KIND_MISMATCH"))
            continue
        premises: list[PropositionPremise | EvidencePremise] = []
        reason = None
        for reference, polarity in row.members:
            if reference.kind is TypedRefKind.OBSERVATION:
                observation = observations.get(reference.id)
                if observation is None or observation.content_hash != reference.content_hash:
                    reason = "MEMBER_OBSERVATION_UNAVAILABLE"
                    break
                premises.append(
                    PropositionPremise(
                        Atom(
                            key=observation.record.proposition_key,
                            polarity=Polarity.POSITIVE if polarity else Polarity.NEGATIVE,
                        )
                    )
                )
                continue
            if not polarity:
                # A negative non-observation member has no admitted denial source.
                reason = "NEGATIVE_MEMBER_UNSUPPORTED"
                break
            try:
                premises.append(
                    EvidencePremise(
                        EvidenceRef(
                            kind=EvidenceRefKind(str(reference.kind)),
                            id=reference.id,
                            revision=reference.revision,
                            content_hash=reference.content_hash,
                        )
                    )
                )
            except (ValueError, ContractError):
                reason = "MEMBER_KIND_UNSUPPORTED"
                break
        if reason is not None:
            rejected.append((row.set_id, reason))
            continue
        try:
            rules.append(
                JustificationSet(
                    conclusion=proposition_key(rule.conclusion, {"subject_id": row.subject_id}),
                    premises=tuple(premises),
                    rule_version=rule.rule_version,
                    source_group=None,
                )
            )
        except ContractError:
            rejected.append((row.set_id, "RULE_SHAPE_INVALID"))
    return rules, rejected


def _anchor_refs(
    support: GroundedSupport, conclusion: Atom, refs_by_anchor: Mapping[str, AssuranceRef]
) -> tuple[AssuranceRef, ...]:
    """Anchors actually reached on the clean support paths of the conclusion."""
    found: set[AssuranceRef] = set()
    visited: set[Atom] = set()
    pending = [conclusion]
    steps = 0
    while pending:
        atom = pending.pop()
        if atom in visited:
            continue
        visited.add(atom)
        for witness in support.clean.witnesses_for(atom):
            steps += 1
            if steps > 100_000:
                raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE")
            if witness.kind is WitnessKind.ANCHOR:
                anchor_id = witness.signature.removeprefix("anchor:")
                ref = refs_by_anchor.get(anchor_id)
                if ref is not None:
                    found.add(ref)
                continue
            for premise in witness.premises:
                if isinstance(premise, PropositionPremise):
                    pending.append(premise.atom)
    return tuple(sorted(found, key=lambda ref: ref.key))


def _admitted_witnesses(support: GroundedSupport) -> tuple[SupportWitness, ...]:
    return tuple(witness for witnesses in support.clean.witnesses.values() for witness in witnesses)


def evaluate_acceptance_support(
    *,
    mission_id: str,
    scope_id: str,
    purpose: str,
    now_ms: int,
    subject_hash: str,
    review: ReviewAnchorInput,
    checks: Sequence[CheckAnchorInput],
    observations: Sequence[ObservationInput] = (),
    justification_sets: Sequence[JustificationInput] = (),
    resolve_signature: Callable[[str], PredicateSignature | None] | None = None,
    admitted_rules: Mapping[str, AdmittedRule] | None = None,
) -> SupportEvaluation:
    """Fresh anchors + admitted rules → bounded supported/clean closure → conclusion.

    ``checks`` must be exactly the bindings the official record consumed; each one
    is a premise of the fixed acceptance rule, so a binding that no longer
    normalizes to PASS makes the conclusion UNKNOWN (no anchor) or FALSE
    (authoritative FAIL anchor), never a cached PASS.
    """
    text(mission_id)
    text(scope_id)
    text(subject_hash)
    integer(now_ms)
    witness_purpose = _purpose(purpose)
    if len({check.binding_ref for check in checks}) != len(checks) or len(checks) > 256:
        raise AssuranceError("CHECK_ANCHOR_INVALID")
    if len(observations) > 10_000 or len(justification_sets) > 20_000:
        raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE")
    by_observation: dict[str, ObservationInput] = {}
    for row in observations:
        if by_observation.setdefault(row.record.observation_id, row) != row:
            raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE", "duplicate observation")

    # Signatures: the fixed system predicates plus whatever the deployment
    # registry resolves for the stored observation keys. Unresolved keys are
    # rejected by the selector as UNREGISTERED_PREDICATE.
    signatures: dict[str, PredicateSignature] = {}
    candidates: list[AnchorCandidate] = []
    refs_by_anchor: dict[str, AssuranceRef] = {}
    review_candidate = _review_candidate(review, mission_id=mission_id, scope_id=scope_id)
    signatures[review_candidate.observation.proposition_key] = REVIEW_ACCEPTED
    candidates.append(review_candidate)
    refs_by_anchor[review_candidate.observation.observation_id] = review.record_ref
    for check in checks:
        candidate = _check_candidate(check, mission_id=mission_id, scope_id=scope_id)
        # A check whose current grade is UNKNOWN contributes no anchor and no
        # registered signature: nothing stored may stand in for it.
        if candidate is not None:
            signatures[check_passed_key(mission_id, check.binding_ref.pin.id)] = CHECK_PASSED
            candidates.append(candidate)
            refs_by_anchor[candidate.observation.observation_id] = check.binding_ref
    impersonated: list[tuple[str, str]] = []
    for row in observations:
        key = row.record.proposition_key
        # System predicates are only ever anchored by this evaluator's own
        # review/check candidates. A stored observation naming one of them, or
        # claiming the system observer, cannot license anything.
        if _is_system_key(key) or row.record.observer_id == ASSURANCE_OBSERVER:
            impersonated.append((row.record.observation_id, "SYSTEM_PREDICATE_IMPERSONATION"))
            continue
        if key not in signatures and resolve_signature is not None:
            resolved = resolve_signature(key)
            if resolved is not None:
                if not isinstance(resolved, PredicateSignature):
                    raise AssuranceError("PREDICATE_REGISTRY_INVALID")
                if resolved.predicate_ref.id in SYSTEM_PREDICATES or ASSURANCE_OBSERVER in (
                    resolved.observer_ids
                ):
                    impersonated.append(
                        (row.record.observation_id, "SYSTEM_PREDICATE_IMPERSONATION")
                    )
                    continue
                signatures[key] = resolved
        candidate = _observation_candidate(row)
        candidates.append(candidate)
        refs_by_anchor[row.record.observation_id] = AssuranceRef(
            "observation", Pin(row.record.observation_id, 0, row.content_hash)
        )
    selection = _select(candidates, purpose=witness_purpose, now_ms=now_ms, signatures=signatures)

    conclusion_key = content_acceptable_key(mission_id, scope_id, subject_hash)
    premises: list[PropositionPremise] = [
        PropositionPremise(Atom(key=review_candidate.observation.proposition_key))
    ]
    premises.extend(
        PropositionPremise(Atom(key=check_passed_key(mission_id, check.binding_ref.pin.id)))
        for check in sorted(checks, key=lambda check: check.binding_ref.key)
    )
    accept_rule = JustificationSet(
        conclusion=conclusion_key,
        premises=tuple(premises),
        rule_version=ACCEPT_RULE_VERSION,
        source_group=None,
    )
    stored_rules, rejected_rules = _admit_stored_rules(
        justification_sets, observations=by_observation, admitted_rules=admitted_rules or {}
    )
    graph = SupportGraph((accept_rule, *stored_rules))
    support = compute_grounded_support(graph, selection)

    conclusion = Atom(key=conclusion_key)
    truth = support.supported.truth_for(conclusion_key)
    usable = support.usable(conclusion_key)
    clean_refs = _anchor_refs(support, conclusion, refs_by_anchor) if usable else ()
    admitted_ids = tuple(sorted(selection.admitted_ids()))
    # Expiry: the earliest deadline of any admitted anchor on the clean support.
    deadlines: list[int] = []
    clean_anchor_ids = {
        witness.signature.removeprefix("anchor:")
        for witness in _admitted_witnesses(support)
        if witness.kind is WitnessKind.ANCHOR
    }
    for candidate in candidates:
        observation = candidate.observation
        if (
            observation.observation_id in clean_anchor_ids
            and observation.valid_until_ms is not None
        ):
            deadlines.append(observation.valid_until_ms)
    rejected_anchors = tuple(
        sorted(
            [
                (entry.candidate.observation.observation_id, str(entry.reason))
                for entry in selection.rejected
            ]
            + impersonated
        )
    )
    reasons = [
        f"conclusion:{truth!s}",
        f"clean:{'USABLE' if usable else 'NOT_USABLE'}",
        f"review:{'ACCEPTABLE' if review.acceptable else 'NOT_ACCEPTABLE'}",
        f"checks:{len(checks)}",
        f"anchors:{len(admitted_ids)}",
        f"rejected_anchors:{len(rejected_anchors)}",
        f"rules:{len(graph)}",
        f"rejected_rules:{len(rejected_rules)}",
    ]
    return SupportEvaluation(
        conclusion_key,
        truth,
        usable,
        tuple(clean_refs),
        admitted_ids,
        rejected_anchors,
        tuple(sorted(rejected_rules)),
        min(deadlines) if deadlines else None,
        tuple(reasons),
    )


def support_fingerprint(evaluation: SupportEvaluation) -> str:
    """Stable digest of a support evaluation for certificate reasons/diagnostics."""
    return fingerprint(
        {
            "conclusion": evaluation.conclusion_key,
            "truth": str(evaluation.truth),
            "usable": evaluation.usable,
            "clean_support": [ref.to_json() for ref in evaluation.clean_support_refs],
            "anchors": list(evaluation.admitted_anchor_ids),
            "rejected_anchors": [list(item) for item in evaluation.rejected_anchors],
            "rejected_rules": [list(item) for item in evaluation.rejected_rules],
        }
    )


__all__ = (
    "ACCEPT_RULE_ID",
    "ACCEPT_RULE_VERSION",
    "ASSURANCE_OBSERVER",
    "CHECK_PASSED",
    "CONTENT_ACCEPTABLE",
    "REVIEW_ACCEPTED",
    "SYSTEM_PREDICATES",
    "AdmittedRule",
    "CheckAnchorInput",
    "JustificationInput",
    "ObservationInput",
    "ReviewAnchorInput",
    "SupportEvaluation",
    "check_passed_key",
    "content_acceptable_key",
    "evaluate_acceptance_support",
    "review_accepted_key",
    "support_fingerprint",
)
