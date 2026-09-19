# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic admission of a decoded ``PlanningDecisionEnvelopeV1`` (V2 §43).

This module is the step between "the model's reply decoded" and "the system may
act on it".  It is a *pure* function: it opens no store, imports no storage
reader, compiles nothing, commits nothing and emits no event.  Everything it
needs is a read-only :class:`AdmissionContext` the caller assembled from the
request record and the current world.

The §43 order is the module's control flow, and each stage either passes or
returns, so **the first failing stage wins**: two stages violated at once never
mix their codes, and the same input always yields the same feedback.

``INTERNAL_CONTRACT_ERROR`` is the one code that is not about the model: it is
raised when the caller hands admission something that is not a decoded envelope
or a well-formed context.  That is a programming error on the calling side, not
a planner mistake, and it is reported rather than thrown so the caller can
record it in the same place as every other refusal.

Everything the module refuses is reported as a :class:`PlanningFeedbackV1` —
the §39 value that goes straight back into the next request package's
``previous_feedback`` field.  Problems are located with JSON pointers (§40), and
no system-internal field is ever put in the feedback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..contracts.evidence_state import TruthValue
from ..contracts.htn import MethodRef, MethodRegistryStatus
from ..contracts.models import ContractError
from ..contracts.planning_decisions import (
    BindExistingGoalDecision,
    BindExistingGoalMode,
    DeclareBlockedDecision,
    NoChangeDecision,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionRejectionCode,
    PlanningDecisionStatus,
    PlanningDecisionType,
    PlanningFeedbackV1,
    PlanningProblemDetailV1,
    PlanningRefV1,
    PlanningRequestBinding,
    PlanningRetryBudgetView,
    RefineDecision,
    RepairProposeSuccessorDecision,
    RepairReplaceMethodDecision,
    WaitDecision,
    canonical_decision_hash,
)
from ..contracts.semantic_base import (
    enum_of,
    flag,
    hash_hex,
    identifier,
    index,
    sequence_of,
    text,
)

REJECTION = PlanningDecisionRejectionCode

#: §12 repair rows are keyed by decision type *and* sub-kind.  The enablement
#: matrix in the contract layer spells the two REPAIR rows exactly this way, so
#: admission asks the same question the package advertised.
REPAIR_ENABLEMENT_PREFIX = "REPAIR/"

#: The decision kinds that change no plan state.  They are the planner's escape
#: hatches, so an exhausted budget must not be able to silence them (§29, §30).
_STATE_FREE_TYPES = frozenset(
    {
        PlanningDecisionType.DECLARE_BLOCKED,
        PlanningDecisionType.WAIT,
        PlanningDecisionType.NO_CHANGE,
    }
)


# --------------------------------------------------------------------------------------
# The caller-assembled context (§43).  One frozen value per fact, each documented
# with its source: nothing here is ever read from the environment or the store.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MethodView:
    """How one method of the request's library looks to admission.

    Source: the registry snapshot the caller took *after* the reply arrived, so a
    method that was retired, rejected or moved to a new version while the model
    was thinking is visible here and nowhere else.
    """

    method_id: str
    version: int
    content_hash: str
    status: MethodRegistryStatus
    applies_to: tuple[str, ...]
    required_parameters: tuple[str, ...] = ()
    predicate_keys: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    requires_authorization: bool = False
    authorization_granted: bool = True
    precondition_truth: TruthValue = TruthValue.TRUE

    @property
    def ref_key(self) -> tuple[str, str, int, str]:
        return ("method", self.method_id, self.version, self.content_hash)

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_id", identifier(self.method_id, "method_view.method_id"))
        object.__setattr__(
            self, "version", index(self.version, "method_view.version", minimum=1)
        )
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "method_view.content_hash")
        )
        object.__setattr__(
            self,
            "status",
            enum_of(MethodRegistryStatus, self.status, "method_view.status"),
        )
        string_fields = (
            "applies_to",
            "required_parameters",
            "predicate_keys",
            "required_capabilities",
        )
        for name in string_fields:
            object.__setattr__(
                self,
                name,
                sequence_of(
                    getattr(self, name),
                    f"method_view.{name}",
                    lambda entry, where: identifier(entry, where),
                ),
            )
        object.__setattr__(
            self,
            "requires_authorization",
            flag(self.requires_authorization, "method_view.requires_authorization"),
        )
        object.__setattr__(
            self,
            "authorization_granted",
            flag(self.authorization_granted, "method_view.authorization_granted"),
        )
        object.__setattr__(
            self,
            "precondition_truth",
            enum_of(TruthValue, self.precondition_truth, "method_view.precondition_truth"),
        )


@dataclass(frozen=True, slots=True)
class MethodInstanceView:
    """One active method instance on one planning subject.

    Source: the request's ``planning_subjects`` plus the method instances the
    network currently has adopted, so a repair decision can be checked against
    the instance the subject really carries.
    """

    subject_key: str
    instance_id: str
    semantic_revision: int
    content_hash: str
    active: bool = True
    running_work: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_key", identifier(self.subject_key, "method_instance.subject_key")
        )
        object.__setattr__(
            self, "instance_id", identifier(self.instance_id, "method_instance.instance_id")
        )
        object.__setattr__(
            self,
            "semantic_revision",
            index(self.semantic_revision, "method_instance.semantic_revision", minimum=1),
        )
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "method_instance.content_hash")
        )
        object.__setattr__(self, "active", flag(self.active, "method_instance.active"))
        object.__setattr__(
            self, "running_work", flag(self.running_work, "method_instance.running_work")
        )

    @property
    def ref_key(self) -> tuple[str, str, int, str]:
        return ("method_instance", self.instance_id, self.semantic_revision, self.content_hash)


@dataclass(frozen=True, slots=True)
class AuthorizationView:
    """The mission's authorisation state.

    Source: the governance snapshot the caller took with the request; admission
    only reads it.  ``required_approvals`` names the approvals a real action on
    this subject still waits for.
    """

    approval_granted: bool = True
    required_approvals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "approval_granted", flag(self.approval_granted, "authorization.approval_granted")
        )
        object.__setattr__(
            self,
            "required_approvals",
            sequence_of(
                self.required_approvals,
                "authorization.required_approvals",
                lambda entry, where: identifier(entry, where),
            ),
        )


@dataclass(frozen=True, slots=True)
class CapabilityView:
    """The capability ids a live provider actually exposes right now.

    Source: the deployment's capability snapshot (§14.2), reduced to the ids.
    """

    available: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.available, frozenset):
            raise ContractError("capabilities.available must be a frozenset")
        for entry in self.available:
            identifier(entry, "capabilities.available[]")


@dataclass(frozen=True, slots=True)
class BudgetView:
    """The mission's remaining planning budget.

    Source: the retry-budget counters the caller already reads for the request
    package (``planning_rounds_remaining``), the mission's planning bound
    (``bound_remaining``) and the budget account's verdict for a real action
    (``budget_available``).
    """

    planning_rounds_remaining: int
    bound_remaining: int
    budget_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "planning_rounds_remaining",
            index(self.planning_rounds_remaining, "budget.planning_rounds_remaining"),
        )
        object.__setattr__(
            self, "bound_remaining", index(self.bound_remaining, "budget.bound_remaining")
        )
        object.__setattr__(
            self, "budget_available", flag(self.budget_available, "budget.budget_available")
        )


@dataclass(frozen=True, slots=True)
class OperationStateView:
    """The operation ledger facts admission must not step over.

    Source: the mission's operation ledger, reduced to "which operations are
    still UNKNOWN and must be reconciled before a new refinement".
    """

    unresolved_operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unresolved_operations",
            sequence_of(
                self.unresolved_operations,
                "operations.unresolved_operations",
                lambda entry, where: identifier(entry, where),
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanShapeView:
    """The structural verdicts the compiler will re-check after admission.

    Source: the caller's structural pre-checks over the plan the decision would
    produce.  Admission reports them early so the model gets a name it can act
    on instead of a failed compile; a non-empty field is a refusal, and every
    entry is one problem in the same stage.
    """

    refinement_cycle: tuple[str, ...] = ()
    order_cycle: tuple[str, ...] = ()
    data_unbound: tuple[str, ...] = ()
    coverage_gap: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("refinement_cycle", "order_cycle", "data_unbound", "coverage_gap"):
            object.__setattr__(
                self,
                name,
                sequence_of(
                    getattr(self, name),
                    f"plan_shape.{name}",
                    lambda entry, where: text(entry, where),
                ),
            )


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """Everything admission may read, assembled by the caller (§43).

    Each field names its source; none of them is fetched, derived or cached here.

    * ``binding`` — the request record's :class:`PlanningRequestBinding`; the
      identity card the reply is admitted against.
    * ``decision_id`` — the §35 id the caller minted for this attempt; it becomes
      ``PlanningFeedbackV1.previous_decision_id``.
    * ``planning_subjects`` — the subjects saved in the request record (§19).
    * ``visible_refs`` — the reference list saved in the request record (§18).
      The 2026-09-19 addendum rules this list authoritative: admission matches
      against it and never recomputes it from the package.
    * ``plan_revision`` / ``requirements_revision`` / ``scope_epoch_digest`` —
      the *current* values, compared with the binding's.
    * ``package_version`` / ``package_hash`` / ``prompt_version`` /
      ``prompt_hash`` — the package and prompt the decision was produced
      against, compared with the binding's.
    * ``enabled_decision_types`` — the phase's enabled rows, keyed the way
      ``H1_DECISION_ENABLEMENT`` keys them (``"REPAIR/REPLACE_METHOD"``).
    * ``repair_allowed`` — whether this phase may repair at all.
    * ``methods`` — the method-library view (id, version, content hash, status,
      applicability and the parameters/preconditions it needs).
    * ``predicates`` — the registered predicate names.
    * ``active_method_instances`` — the instances adopted on the subjects.
    * ``open_obligations`` / ``current_resolutions`` / ``shareable_goals`` — the
      reference sets a successor, a reuse or a sharing decision needs.
    * ``authorization`` / ``capabilities`` / ``budget`` / ``operations`` /
      ``plan_shape`` — the governance, deployment and structural facts.
    * ``retry_budgets`` — the §39 counters the feedback reports back.
    """

    binding: PlanningRequestBinding
    decision_id: str
    planning_subjects: tuple[Mapping[str, Any], ...]
    visible_refs: tuple[PlanningRefV1, ...]
    plan_revision: int
    requirements_revision: int
    scope_epoch_digest: str
    package_version: int
    package_hash: str
    prompt_version: str
    prompt_hash: str
    enabled_decision_types: frozenset[str]
    repair_allowed: bool
    methods: tuple[MethodView, ...]
    predicates: frozenset[str]
    active_method_instances: tuple[MethodInstanceView, ...]
    open_obligations: tuple[PlanningRefV1, ...]
    current_resolutions: tuple[PlanningRefV1, ...]
    shareable_goals: tuple[PlanningRefV1, ...]
    authorization: AuthorizationView
    capabilities: CapabilityView
    budget: BudgetView
    operations: OperationStateView
    plan_shape: PlanShapeView
    retry_budgets: PlanningRetryBudgetView

    def __post_init__(self) -> None:
        if not isinstance(self.binding, PlanningRequestBinding):
            raise ContractError("admission.binding must be a PlanningRequestBinding")
        object.__setattr__(
            self, "decision_id", identifier(self.decision_id, "admission.decision_id")
        )
        object.__setattr__(
            self,
            "planning_subjects",
            _mappings(self.planning_subjects, "admission.planning_subjects"),
        )
        object.__setattr__(
            self,
            "visible_refs",
            sequence_of(self.visible_refs, "admission.visible_refs", _planning_ref),
        )
        object.__setattr__(
            self, "plan_revision", index(self.plan_revision, "admission.plan_revision")
        )
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "admission.requirements_revision"),
        )
        object.__setattr__(
            self,
            "scope_epoch_digest",
            hash_hex(self.scope_epoch_digest, "admission.scope_epoch_digest"),
        )
        object.__setattr__(
            self,
            "package_version",
            index(self.package_version, "admission.package_version", minimum=1),
        )
        object.__setattr__(
            self, "package_hash", hash_hex(self.package_hash, "admission.package_hash")
        )
        object.__setattr__(
            self,
            "prompt_version",
            identifier(self.prompt_version, "admission.prompt_version"),
        )
        object.__setattr__(
            self, "prompt_hash", hash_hex(self.prompt_hash, "admission.prompt_hash")
        )
        object.__setattr__(
            self,
            "enabled_decision_types",
            _strings(self.enabled_decision_types, "admission.enabled_decision_types"),
        )
        object.__setattr__(
            self, "repair_allowed", flag(self.repair_allowed, "admission.repair_allowed")
        )
        object.__setattr__(
            self,
            "methods",
            sequence_of(self.methods, "admission.methods", _method_view),
        )
        object.__setattr__(
            self, "predicates", _strings(self.predicates, "admission.predicates")
        )
        object.__setattr__(
            self,
            "active_method_instances",
            sequence_of(
                self.active_method_instances,
                "admission.active_method_instances",
                _method_instance_view,
            ),
        )
        for name in ("open_obligations", "current_resolutions", "shareable_goals"):
            object.__setattr__(
                self, name, sequence_of(getattr(self, name), f"admission.{name}", _planning_ref)
            )
        for name, kind in (
            ("authorization", AuthorizationView),
            ("capabilities", CapabilityView),
            ("budget", BudgetView),
            ("operations", OperationStateView),
            ("plan_shape", PlanShapeView),
        ):
            value = getattr(self, name)
            if not isinstance(value, kind):
                raise ContractError(f"admission.{name} must be a {kind.__name__}")
        if not isinstance(self.retry_budgets, PlanningRetryBudgetView):
            raise ContractError("admission.retry_budgets must be a PlanningRetryBudgetView")

    @property
    def subject_keys(self) -> frozenset[str]:
        return frozenset(str(row.get("subject_key", "")) for row in self.planning_subjects)

    @property
    def visible_ref_keys(self) -> frozenset[tuple[str, str, int, str]]:
        return frozenset(_ref_key(ref) for ref in self.visible_refs)


@dataclass(frozen=True, slots=True)
class AdmittedPlanningDecision:
    """A decision that passed every §43 check — a command, not a commitment.

    ``decision`` is the decoded envelope verbatim; ``subject`` is the planning
    subject row the ``subject_key`` resolved to; ``method_refs`` and
    ``method_instances`` are the library / instance references admission
    resolved; ``canonical_hash`` is the §15 hash of the decision, so the adapter
    never has to recompute it.
    """

    decision: PlanningDecisionEnvelopeV1
    subject: Mapping[str, Any]
    method_refs: tuple[MethodRef, ...]
    method_instances: tuple[PlanningRefV1, ...]
    canonical_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PlanningDecisionEnvelopeV1):
            raise ContractError("admitted.decision must be a PlanningDecisionEnvelopeV1")
        object.__setattr__(self, "subject", MappingProxyType(dict(self.subject)))
        object.__setattr__(
            self,
            "method_refs",
            tuple(
                entry if isinstance(entry, MethodRef) else MethodRef.from_json(entry)
                for entry in self.method_refs
            ),
        )
        object.__setattr__(
            self,
            "method_instances",
            tuple(_planning_ref(entry) for entry in self.method_instances),
        )
        object.__setattr__(
            self, "canonical_hash", hash_hex(self.canonical_hash, "admitted.canonical_hash")
        )


# --------------------------------------------------------------------------------------
# Small coercion helpers (the context is caller-supplied, so it is validated)
# --------------------------------------------------------------------------------------


def _planning_ref(value: object, name: str = "planning_ref") -> PlanningRefV1:
    return value if isinstance(value, PlanningRefV1) else PlanningRefV1.from_json(value, name)


def _method_view(value: object, name: str = "method_view") -> MethodView:
    if isinstance(value, MethodView):
        return value
    if isinstance(value, Mapping):
        return MethodView(**value)
    raise ContractError(f"{name} must be a MethodView or an object")


def _method_instance_view(value: object, name: str = "method_instance_view") -> MethodInstanceView:
    if isinstance(value, MethodInstanceView):
        return value
    if isinstance(value, Mapping):
        return MethodInstanceView(**value)
    raise ContractError(f"{name} must be a MethodInstanceView or an object")


def _strings(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, (frozenset, set, list, tuple)):
        raise ContractError(f"{name} must be a collection of strings")
    return frozenset(identifier(entry, f"{name}[]") for entry in value)


def _mappings(value: object, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractError(f"{name} must be a sequence of objects")
    rows: list[Mapping[str, Any]] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ContractError(f"{name}[{position}] must be an object")
        rows.append(MappingProxyType(dict(entry)))
    return tuple(rows)


def _ref_key(ref: PlanningRefV1) -> tuple[str, str, int, str]:
    return (str(ref.kind), ref.id, ref.semantic_revision, ref.content_hash)


# --------------------------------------------------------------------------------------
# Problem accumulation: one stage, one code, one or more problems
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Stage:
    """One §43 stage.  It collects problems and returns them when it refuses."""

    context: AdmissionContext
    problems: list[PlanningProblemDetailV1] = field(default_factory=list)

    def refuse(
        self,
        code: PlanningDecisionRejectionCode,
        detail: str,
        *,
        field_path: str | None = None,
        subject_ref: PlanningRefV1 | None = None,
        expected: str | None = None,
        observed: str | None = None,
    ) -> bool:
        """Record one problem.  Always ``False``, so a check reads ``if stage...``."""

        self.problems.append(
            PlanningProblemDetailV1(
                code=code,
                subject_ref=subject_ref,
                field_path=field_path,
                detail=detail,
                expected=expected,
                observed=observed,
            )
        )
        return False

    def holds(self) -> bool:
        return not self.problems


def _feedback(context: AdmissionContext, stage: _Stage) -> PlanningFeedbackV1:
    """The refusal value: §39 shape, stable code order, deduplicated codes."""

    seen: list[PlanningDecisionRejectionCode] = []
    for problem in stage.problems:
        if problem.code not in seen:
            seen.append(problem.code)
    return PlanningFeedbackV1(
        previous_decision_id=context.decision_id,
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=tuple(seen),
        problems=tuple(stage.problems),
        changed_refs=(),
        budgets=context.retry_budgets,
    )


# --------------------------------------------------------------------------------------
# The checks, in §43 order
# --------------------------------------------------------------------------------------


def _check_request_identity(decision: object, context: object) -> bool:
    """§43 stage 1: is this a decoded decision and a usable request record?

    Nothing about the model is checked here.  A failure means the *caller* passed
    something that is not a decoded envelope or not a context, which is an
    internal contract error, not a planner mistake.
    """

    return isinstance(decision, PlanningDecisionEnvelopeV1) and isinstance(
        context, AdmissionContext
    )


def _check_package_binding(context: AdmissionContext, stage: _Stage) -> None:
    """§43 stage 2: the decision must name the package and prompt it was built for."""

    binding = context.binding
    pairs = (
        ("package_version", binding.package_version, context.package_version, "/package_version"),
        ("package_hash", binding.package_hash, context.package_hash, "/package_hash"),
        ("prompt_version", binding.prompt_version, context.prompt_version, "/prompt_version"),
        ("prompt_hash", binding.prompt_hash, context.prompt_hash, "/prompt_hash"),
    )
    for name, bound, current, pointer in pairs:
        if bound != current:
            stage.refuse(
                REJECTION.PACKAGE_HASH_MISMATCH,
                f"the request was bound to {name}={bound!r} but the decision was produced"
                f" against {current!r}",
                field_path=pointer,
                expected=str(bound),
                observed=str(current),
            )


def _check_binding_revisions(context: AdmissionContext, stage: _Stage) -> None:
    """§43 stage 3 / §34: a moved plan, requirements or scope makes the reply late.

    H1 compares directly: ``base_plan_revision != current`` is
    ``REQUEST_BINDING_STALE``, with no unrelated-change re-evaluation (that is
    H2's work).  The requirements revision and the scope-epoch digest move with
    the plan for the same reason: a decision cut against them may no longer mean
    what it meant.
    """

    binding = context.binding
    pairs = (
        ("base_plan_revision", binding.base_plan_revision, context.plan_revision, "/plan_revision"),
        (
            "requirements_revision",
            binding.requirements_revision,
            context.requirements_revision,
            "/requirements_revision",
        ),
        (
            "scope_epoch_digest",
            binding.scope_epoch_digest,
            context.scope_epoch_digest,
            "/scope_epoch_digest",
        ),
    )
    for name, bound, current, pointer in pairs:
        if bound != current:
            stage.refuse(
                REJECTION.REQUEST_BINDING_STALE,
                f"the request was bound at {name}={bound!r}; it is now {current!r}",
                field_path=pointer,
                expected=str(bound),
                observed=str(current),
            )


def _check_subject(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 4 / §19: ``subject_key`` is only meaningful inside its request."""

    if decision.subject_key not in context.subject_keys:
        stage.refuse(
            REJECTION.SUBJECT_NOT_IN_REQUEST,
            f"subject_key {decision.subject_key!r} is not one of this request's subjects",
            field_path="/subject_key",
            observed=decision.subject_key,
        )


def _decision_refs(
    decision: PlanningDecisionEnvelopeV1,
) -> tuple[tuple[str, PlanningRefV1], ...]:
    """Every reference the decision cites, with the JSON pointer that locates it."""

    found: list[tuple[str, PlanningRefV1]] = [
        (f"/reason_refs/{position}", ref) for position, ref in enumerate(decision.reason_refs)
    ]
    payload = decision.payload
    if isinstance(payload, RefineDecision):
        found.append(("/payload/method_ref", payload.method_ref))
    elif isinstance(payload, RepairReplaceMethodDecision):
        found.append(("/payload/rejected_method_instance", payload.rejected_method_instance))
        found.append(("/payload/replacement_method_ref", payload.replacement_method_ref))
    elif isinstance(payload, RepairProposeSuccessorDecision):
        found.append(("/payload/old_task_ref", payload.old_task_ref))
        found.append(("/payload/obligation_ref", payload.obligation_ref))
    elif isinstance(payload, BindExistingGoalDecision):
        found.append(
            ("/payload/consumer_method_instance_ref", payload.consumer_method_instance_ref)
        )
        found.append(("/payload/goal_ref", payload.goal_ref))
        if payload.resolution_ref is not None:
            found.append(("/payload/resolution_ref", payload.resolution_ref))
    elif isinstance(payload, WaitDecision):
        found.extend(
            (f"/payload/wait_for/{position}", ref)
            for position, ref in enumerate(payload.wait_for)
        )
    return tuple(found)


def _check_visible_refs(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 5 / §17, §18: every cited reference must byte-match the saved list.

    The comparison is over all four components; the saved list is authoritative
    (2026-09-19 addendum).  A citation that is not in it was never exposed to the
    model, and "only compare the id" is exactly the shortcut §18 forbids.
    """

    visible = context.visible_ref_keys
    for pointer, ref in _decision_refs(decision):
        if _ref_key(ref) not in visible:
            stage.refuse(
                REJECTION.REF_OUTSIDE_CONTEXT,
                f"the reference at {pointer} is not one the request exposed",
                field_path=pointer,
                subject_ref=ref,
                observed=ref.id,
            )


def _enablement_key(decision: PlanningDecisionEnvelopeV1) -> str:
    """§12: the enablement-matrix row this decision belongs to."""

    if decision.decision_type is PlanningDecisionType.REPAIR:
        kind = getattr(decision.payload, "repair_kind", None)
        return f"{REPAIR_ENABLEMENT_PREFIX}{kind}"
    return str(decision.decision_type)


def _check_phase_enablement(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 6 / §12: the phase decides which kinds may execute."""

    key = _enablement_key(decision)
    if key not in context.enabled_decision_types:
        stage.refuse(
            REJECTION.DECISION_NOT_ENABLED_IN_PHASE,
            f"{key} is not enabled in this phase",
            field_path="/decision_type",
            expected=", ".join(sorted(context.enabled_decision_types)),
            observed=key,
        )


def _check_budget_and_bound(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 7: a plan-changing decision needs bound and budget left.

    The state-free kinds (WAIT / NO_CHANGE / DECLARE_BLOCKED) are exempt: they
    are how a planner reports "nothing to do" or "I am stuck", and silencing
    them when the budget runs out would leave the mission with no voice at all.
    """

    if decision.decision_type in _STATE_FREE_TYPES:
        return
    if context.budget.bound_remaining <= 0:
        stage.refuse(
            REJECTION.PLANNING_BOUND_REACHED,
            "the mission has no planning bound left for another change",
            field_path="/decision_type",
            observed="0",
        )
        return
    if not context.budget.budget_available or context.budget.planning_rounds_remaining <= 0:
        stage.refuse(
            REJECTION.BUDGET_INSUFFICIENT,
            "the mission has no planning budget left for another change",
            field_path="/decision_type",
            observed=str(context.budget.planning_rounds_remaining),
        )


def _instance_for(context: AdmissionContext, ref: PlanningRefV1) -> MethodInstanceView | None:
    for instance in context.active_method_instances:
        if instance.ref_key == _ref_key(ref):
            return instance
    return None


def _check_repair_replace(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§25: the retired instance must be the one this subject actually carries."""

    payload = decision.payload
    assert isinstance(payload, RepairReplaceMethodDecision)  # narrowed by the caller
    if not context.repair_allowed:
        stage.refuse(
            REJECTION.REPAIR_NOT_ALLOWED,
            "this phase does not allow a repair of the adopted method",
            field_path="/payload/repair_kind",
            observed=str(payload.repair_kind),
        )
        return
    ref = payload.rejected_method_instance
    instance = _instance_for(context, ref)
    if instance is None or not instance.active or instance.subject_key != decision.subject_key:
        stage.refuse(
            REJECTION.METHOD_RETIRED,
            "the rejected method instance is not active on this subject",
            field_path="/payload/rejected_method_instance",
            subject_ref=ref,
            observed=ref.id,
        )
        return
    if instance.running_work:
        stage.refuse(
            REJECTION.RUNNING_WORK_NOT_RECONCILED,
            "running work on the retiring instance must be reconciled first",
            field_path="/payload/rejected_method_instance",
            subject_ref=ref,
            observed=ref.id,
        )


def _check_successor(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§26: a successor needs an open obligation and an acyclic refinement."""

    payload = decision.payload
    assert isinstance(payload, RepairProposeSuccessorDecision)  # narrowed by the caller
    open_keys = frozenset(_ref_key(ref) for ref in context.open_obligations)
    if _ref_key(payload.obligation_ref) not in open_keys:
        stage.refuse(
            REJECTION.OBLIGATION_NOT_OPEN,
            "the successor binds an obligation that is not open",
            field_path="/payload/obligation_ref",
            subject_ref=payload.obligation_ref,
            observed=payload.obligation_ref.id,
        )
    if context.plan_shape.refinement_cycle:
        for detail in context.plan_shape.refinement_cycle:
            stage.refuse(
                REJECTION.REFINEMENT_CYCLE,
                detail,
                field_path="/payload/old_task_ref",
                subject_ref=payload.old_task_ref,
            )


def _check_bind_existing_goal(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§27: reuse needs a CURRENT resolution; sharing needs a still-demanded goal."""

    payload = decision.payload
    assert isinstance(payload, BindExistingGoalDecision)  # narrowed by the caller
    if payload.mode is BindExistingGoalMode.REUSE_ACCEPTED:
        current = frozenset(_ref_key(ref) for ref in context.current_resolutions)
        ref = payload.resolution_ref
        if ref is None or _ref_key(ref) not in current:
            stage.refuse(
                REJECTION.REUSE_NOT_ALLOWED,
                "the resolution being reused is not CURRENT",
                field_path="/payload/resolution_ref",
                observed=None if ref is None else ref.id,
            )
    else:
        shareable = frozenset(_ref_key(ref) for ref in context.shareable_goals)
        if _ref_key(payload.goal_ref) not in shareable:
            stage.refuse(
                REJECTION.REUSE_NOT_ALLOWED,
                "the goal being shared is no longer demanded",
                field_path="/payload/goal_ref",
                subject_ref=payload.goal_ref,
                observed=payload.goal_ref.id,
            )


def _check_payload(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 8 / §24–§31: the per-type payload rules."""

    payload = decision.payload
    if isinstance(payload, RepairReplaceMethodDecision):
        _check_repair_replace(decision, context, stage)
    elif isinstance(payload, RepairProposeSuccessorDecision):
        _check_successor(decision, context, stage)
    elif isinstance(payload, BindExistingGoalDecision):
        _check_bind_existing_goal(decision, context, stage)
    elif isinstance(
        payload, (DeclareBlockedDecision, WaitDecision, NoChangeDecision, RefineDecision)
    ):
        return
    else:  # pragma: no cover - the envelope's payload is a closed set
        stage.refuse(
            REJECTION.INTERNAL_CONTRACT_ERROR,
            f"admission has no payload rules for {type(payload).__name__}",
            field_path="/payload",
        )


#: The statuses a method may not be selected under, and the code each reports.
_METHOD_STATUS_CODES: Mapping[MethodRegistryStatus, PlanningDecisionRejectionCode] = (
    MappingProxyType(
        {
            MethodRegistryStatus.RETIRED: REJECTION.METHOD_RETIRED,
            MethodRegistryStatus.REJECTED: REJECTION.METHOD_REJECTED,
            MethodRegistryStatus.SUSPENDED: REJECTION.METHOD_RETIRED,
        }
    )
)

#: The preconditions a method may not be selected under (§18.3), by truth value.
_PRECONDITION_CODES: Mapping[TruthValue, PlanningDecisionRejectionCode] = MappingProxyType(
    {
        TruthValue.FALSE: REJECTION.METHOD_INAPPLICABLE,
        TruthValue.UNKNOWN: REJECTION.EVIDENCE_REQUIRED,
        TruthValue.CONFLICT: REJECTION.EVIDENCE_CONFLICT,
    }
)


def _check_one_method(
    method_ref: PlanningRefV1,
    *,
    pointer: str,
    subject_key: str,
    bindings: Mapping[str, Any] | None,
    context: AdmissionContext,
    stage: _Stage,
) -> MethodView | None:
    """The §24/§43 method chain: identity → lifecycle → applicability → the rest."""

    library = {view.method_id: view for view in context.methods}
    view = library.get(method_ref.id)
    if view is None:
        stage.refuse(
            REJECTION.METHOD_NOT_FOUND,
            f"no method {method_ref.id!r} is in the request's method library",
            field_path=pointer,
            subject_ref=method_ref,
            observed=method_ref.id,
        )
        return None
    if (view.version, view.content_hash) != (
        method_ref.semantic_revision,
        method_ref.content_hash,
    ):
        stage.refuse(
            REJECTION.METHOD_STALE,
            f"the cited {method_ref.id!r} is not the library's current definition",
            field_path=pointer,
            subject_ref=method_ref,
            expected=str(view.version),
            observed=str(method_ref.semantic_revision),
        )
        return None
    refused = _METHOD_STATUS_CODES.get(view.status)
    if refused is not None:
        stage.refuse(
            refused,
            f"method {method_ref.id!r} is {view.status!s} and cannot be selected",
            field_path=pointer,
            subject_ref=method_ref,
            observed=str(view.status),
        )
        return None
    if subject_key not in view.applies_to:
        stage.refuse(
            REJECTION.METHOD_INAPPLICABLE,
            f"method {method_ref.id!r} does not apply to this subject",
            field_path=pointer,
            subject_ref=method_ref,
            observed=subject_key,
        )
        return None
    if bindings is not None:
        missing = sorted(name for name in view.required_parameters if name not in bindings)
        if missing:
            stage.refuse(
                REJECTION.PARAMETER_INVALID,
                f"the bindings do not fill the method's parameters: {', '.join(missing)}",
                field_path="/payload/bindings",
                subject_ref=method_ref,
                expected=", ".join(sorted(view.required_parameters)),
                observed=", ".join(sorted(str(key) for key in bindings)),
            )
            return None
    unregistered = sorted(name for name in view.predicate_keys if name not in context.predicates)
    if unregistered:
        stage.refuse(
            REJECTION.EVIDENCE_REQUIRED,
            f"the method's preconditions name predicates that are not registered:"
            f" {', '.join(unregistered)}",
            field_path=pointer,
            subject_ref=method_ref,
            observed=", ".join(unregistered),
        )
        return None
    precondition = _PRECONDITION_CODES.get(view.precondition_truth)
    if precondition is not None:
        stage.refuse(
            precondition,
            f"the method's preconditions are {view.precondition_truth!s}",
            field_path=pointer,
            subject_ref=method_ref,
            observed=str(view.precondition_truth),
        )
        return None
    if view.requires_authorization and not view.authorization_granted:
        stage.refuse(
            REJECTION.METHOD_NOT_AUTHORIZED,
            f"method {method_ref.id!r} needs an authority grant the mission lacks",
            field_path=pointer,
            subject_ref=method_ref,
            observed=method_ref.id,
        )
        return None
    if not context.authorization.approval_granted and context.authorization.required_approvals:
        stage.refuse(
            REJECTION.AUTHORIZATION_REQUIRED,
            "the decision needs an approval that has not been granted:"
            f" {', '.join(context.authorization.required_approvals)}",
            field_path=pointer,
            subject_ref=method_ref,
            observed=method_ref.id,
        )
        return None
    missing_capabilities = sorted(
        name for name in view.required_capabilities if name not in context.capabilities.available
    )
    if missing_capabilities:
        stage.refuse(
            REJECTION.CAPABILITY_MISSING,
            f"no live provider exposes the capabilities {', '.join(missing_capabilities)}",
            field_path=pointer,
            subject_ref=method_ref,
            expected=", ".join(sorted(view.required_capabilities)),
            observed=missing_capabilities[0],
        )
        return None
    return view


def _resolved_method_ref(view: MethodView) -> MethodRef:
    return MethodRef(
        method_id=view.method_id, version=view.version, content_hash=view.content_hash
    )


def _check_methods(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> tuple[MethodRef, ...]:
    """§43 stage 9: every method the decision names must still be usable here."""

    payload = decision.payload
    resolved: list[MethodRef] = []
    if isinstance(payload, RefineDecision):
        view = _check_one_method(
            payload.method_ref,
            pointer="/payload/method_ref",
            subject_key=decision.subject_key,
            bindings=payload.bindings,
            context=context,
            stage=stage,
        )
        if view is not None:
            resolved.append(_resolved_method_ref(view))
    elif isinstance(payload, RepairReplaceMethodDecision):
        view = _check_one_method(
            payload.replacement_method_ref,
            pointer="/payload/replacement_method_ref",
            subject_key=decision.subject_key,
            bindings=payload.bindings,
            context=context,
            stage=stage,
        )
        if view is not None:
            resolved.append(_resolved_method_ref(view))
    return tuple(resolved)


def _check_operation_gate(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 10: an unresolved operation must be reconciled before changing the plan."""

    if decision.decision_type in _STATE_FREE_TYPES:
        return
    for operation_id in context.operations.unresolved_operations:
        stage.refuse(
            REJECTION.OPERATION_UNRESOLVED,
            f"operation {operation_id!r} is UNKNOWN and must be reconciled first",
            field_path="/decision_type",
            observed=operation_id,
        )


def _check_plan_shape(
    decision: PlanningDecisionEnvelopeV1, context: AdmissionContext, stage: _Stage
) -> None:
    """§43 stage 11: the structural pre-checks the compiler would otherwise raise."""

    if decision.decision_type in _STATE_FREE_TYPES:
        return
    shape = context.plan_shape
    for detail in shape.order_cycle:
        stage.refuse(REJECTION.ORDER_CYCLE, detail, field_path="/payload")
    for detail in shape.data_unbound:
        stage.refuse(REJECTION.DATA_UNBOUND, detail, field_path="/payload")
    for detail in shape.coverage_gap:
        stage.refuse(REJECTION.COVERAGE_GAP, detail, field_path="/payload")


# --------------------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------------------


def admit_planning_decision(
    decision: object, *, context: object
) -> AdmittedPlanningDecision | PlanningFeedbackV1:
    """§43: admit one decoded decision, or say exactly why not.

    Strictly ordered, first failure wins, and the same input always yields the
    same answer.  A refusal is a :class:`PlanningFeedbackV1` the caller puts
    straight into the next request package; an admission is a frozen
    :class:`AdmittedPlanningDecision` the adapter turns into operations.  Passing
    is not committing.
    """

    if not _check_request_identity(decision, context):
        fallback = context if isinstance(context, AdmissionContext) else None
        problem = PlanningProblemDetailV1(
            code=REJECTION.INTERNAL_CONTRACT_ERROR,
            subject_ref=None,
            field_path=None,
            detail=(
                "admission requires a decoded PlanningDecisionEnvelopeV1 and an"
                " AdmissionContext; got"
                f" {type(decision).__name__} and {type(context).__name__}"
            ),
        )
        if fallback is not None:
            stage = _Stage(context=fallback, problems=[problem])
            return _feedback(fallback, stage)
        # No usable context: the feedback cannot report budgets, so the error is
        # raised rather than half-returned.
        raise ContractError(problem.detail)

    envelope: PlanningDecisionEnvelopeV1 = decision  # type: ignore[assignment]
    ctx: AdmissionContext = context  # type: ignore[assignment]

    stages = (
        lambda stage: _check_package_binding(ctx, stage),
        lambda stage: _check_binding_revisions(ctx, stage),
        lambda stage: _check_subject(envelope, ctx, stage),
        lambda stage: _check_visible_refs(envelope, ctx, stage),
        lambda stage: _check_phase_enablement(envelope, ctx, stage),
        lambda stage: _check_budget_and_bound(envelope, ctx, stage),
        lambda stage: _check_payload(envelope, ctx, stage),
    )
    for check in stages:
        stage = _Stage(context=ctx)
        check(stage)
        if not stage.holds():
            return _feedback(ctx, stage)

    method_stage = _Stage(context=ctx)
    method_refs = _check_methods(envelope, ctx, method_stage)
    if not method_stage.holds():
        return _feedback(ctx, method_stage)

    for check in (
        lambda stage: _check_operation_gate(envelope, ctx, stage),
        lambda stage: _check_plan_shape(envelope, ctx, stage),
    ):
        stage = _Stage(context=ctx)
        check(stage)
        if not stage.holds():
            return _feedback(ctx, stage)

    subject = next(
        row
        for row in ctx.planning_subjects
        if str(row.get("subject_key", "")) == envelope.subject_key
    )
    instances: tuple[PlanningRefV1, ...] = ()
    payload = envelope.payload
    if isinstance(payload, RepairReplaceMethodDecision):
        instances = (payload.rejected_method_instance,)
    elif isinstance(payload, BindExistingGoalDecision):
        instances = (payload.consumer_method_instance_ref,)
    return AdmittedPlanningDecision(
        decision=envelope,
        subject=subject,
        method_refs=method_refs,
        method_instances=instances,
        canonical_hash=canonical_decision_hash(envelope),
    )


__all__ = (
    "AdmissionContext",
    "AdmittedPlanningDecision",
    "AuthorizationView",
    "BudgetView",
    "CapabilityView",
    "MethodInstanceView",
    "MethodView",
    "OperationStateView",
    "PlanShapeView",
    "admit_planning_decision",
)
