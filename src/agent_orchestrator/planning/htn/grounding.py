# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Grounding a method against a compound task (§18.3 ``ground_method``).

Pure: type-check the bindings, derive stable identities for the slots, decide
which slots may bind an already existing goal occurrence, and return a
:class:`~agent_orchestrator.contracts.htn.MethodInstanceDraft`.  No store, no
clock, no model, no side effects — calling it twice with the same inputs returns
the same draft, byte for byte, which is what makes the occurrence identities
usable as a key.

Identity (TG §3.1, implementation design §5.2 step 3): every slot's occurrence,
task and derived obligation come from ``(instance_id, slot_key)`` and nothing
else.  Re-grounding the same method against the same goal with the same
parameters therefore lands on the same nodes, so a re-plan that keeps a slot keeps
its node rather than minting a twin.

Sharing (§8.3, TG §12): two slots bind the *same* occurrence only when the whole
sharing signature matches — goal contract, typed parameters, input versions,
authority scope, freshness, domain semantics and effect identity — *and* the task
type has declared that reuse is permitted.  Lexical closeness produces a
suggestion and never a binding, and a goal whose type writes anything is not
shared by default: two sends are two sends, whatever the parameters say.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...contracts.evidence_state import PreconditionPhase, PreconditionWitnessRecord, TruthValue
from ...contracts.htn import (
    Binding,
    ChildBinding,
    MethodContract,
    MethodInstanceDraft,
    MethodInstanceId,
    MethodOccurrenceBinding,
    MethodRef,
    MethodStep,
    ObligationId,
    ObligationRelation,
    OccurrenceId,
    OutputValue,
    ParameterValue,
    PlanRevision,
    PreconditionRef,
    Requiredness,
    ReusePolicy,
    SideEffectKind,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    condition_digest,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import (
    TypedRef,
    VersionedRef,
    content_hash_of,
    enum_of,
    identifier,
    text,
)
from .applicability import ApplicabilityReport, ApplicabilityStatus
from .registry import (
    READ_ONLY_EFFECTS,
    SchemaCatalog,
    TaskTypeCatalog,
    TaskTypeSpec,
    effective_reuse_policy,
    iter_values,
    statement_similarity,
)


class GroundingError(ContractError):
    """The method cannot be grounded against this task as asked.

    A ``ContractError`` because every cause is a malformed or inapplicable
    *request* — wrong parameter type, unregistered task type, a precondition the
    world says is false — and callers already handle that class uniformly.
    """


def derive_id(role: str, *parts: object) -> str:
    """A stable, collision-free derived identity.

    Hashing the JSON of the parts rather than concatenating them means a slot key
    containing the separator cannot collide with another slot (the same reason
    :mod:`...graph.task_network` length-prefixes its node ids).
    """

    digest = content_hash_of([role, *[str(part) for part in parts]])
    return f"{role}-{digest[:32]}"


def instance_identity(
    *,
    goal_id: TaskRef,
    goal_occurrence_id: OccurrenceId,
    method_ref: MethodRef,
    parameters_digest: str,
) -> MethodInstanceId:
    """The identity of one grounding.  Same inputs, same instance."""

    return MethodInstanceId(
        derive_id(
            "mi",
            goal_id,
            goal_occurrence_id,
            method_ref.method_id,
            method_ref.version,
            method_ref.content_hash,
            parameters_digest,
        )
    )


@dataclass(frozen=True, slots=True)
class SlotIdentity:
    """The derived identities of one slot of one method instance."""

    slot_key: str
    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId


def slot_identity(
    instance_id: MethodInstanceId,
    slot_key: str,
    parent_obligation: ObligationId,
    relation: ObligationRelation = ObligationRelation.REFINES_PARENT,
) -> SlotIdentity:
    """Derive a slot's occurrence, task and duty from ``(instance, slot)``.

    §6.1 decides the duty, and it is the reason splitting a task does not mint a
    fresh retry allowance: a ``refines_parent`` step *is* part of the parent's
    responsibility and therefore carries the parent's ``obligation_id``, so its
    failures, its spend and its share of the recursion fuel all land on the one
    account.  Only an ``independent_authorized`` step — work a method may add on
    its own authority — opens a derived duty of its own.

    The occurrence and the task are always fresh and always derived from
    ``(instance_id, slot_key)``: TG §12 keeps every occurrence its own position
    even when several of them answer to one duty.
    """

    slot = identifier(slot_key, "slot_key")
    relation = enum_of(ObligationRelation, relation, "obligation_relation")
    duty = (
        parent_obligation
        if relation is ObligationRelation.REFINES_PARENT
        else ObligationId(derive_id("obl", parent_obligation, instance_id, slot))
    )
    return SlotIdentity(
        slot_key=slot,
        occurrence_id=OccurrenceId(derive_id("occ", instance_id, slot)),
        task_id=TaskRef(derive_id("task", instance_id, slot)),
        obligation_id=duty,
    )


# --------------------------------------------------------------------------------------
# Sharing (§8.3, TG §12)
# --------------------------------------------------------------------------------------


class ShareVerdict(StrEnum):
    """Why two slots may or may not bind one goal occurrence."""

    SHAREABLE = "SHAREABLE"
    REUSE_NOT_PERMITTED = "REUSE_NOT_PERMITTED"
    SIDE_EFFECT_NOT_SHAREABLE = "SIDE_EFFECT_NOT_SHAREABLE"
    SIGNATURE_DIFFERS = "SIGNATURE_DIFFERS"


@dataclass(frozen=True, slots=True)
class SharingSignature:
    """TG §12: everything two slots must agree on before they may share one goal.

    Text is not on the list.  Two "send the monthly report" goals with identical
    wording and different recipients differ here; two reads of the same file at the
    same revision do not.
    """

    goal_type_ref: VersionedRef
    typed_parameters: tuple[tuple[str, Any], ...]
    input_versions: tuple[str, ...]
    authority_scope: str
    semantic_scope: str
    assurance_policy_ref: str
    freshness_policy_ref: str
    domain_semantics: str
    side_effect_kind: SideEffectKind
    effect_identity: str | None = None

    @classmethod
    def of(
        cls,
        spec: TaskTypeSpec,
        parameters: Mapping[str, Any],
        *,
        authority_scope: str,
        semantic_scope: str,
        input_versions: Sequence[str] = (),
        assurance_policy_ref: str = "assurance.default",
        freshness_policy_ref: str = "freshness.default",
    ) -> SharingSignature:
        return cls(
            goal_type_ref=spec.task_type_ref,
            typed_parameters=tuple(sorted((key, parameters[key]) for key in parameters)),
            input_versions=tuple(input_versions),
            authority_scope=identifier(authority_scope, "authority_scope"),
            semantic_scope=identifier(semantic_scope, "semantic_scope"),
            assurance_policy_ref=identifier(assurance_policy_ref, "assurance_policy_ref"),
            freshness_policy_ref=identifier(freshness_policy_ref, "freshness_policy_ref"),
            domain_semantics=spec.domain or "",
            side_effect_kind=spec.side_effect_kind,
            effect_identity=spec.effect_identity,
        )

    def digest(self) -> str:
        return content_hash_of(
            {
                "goal_type_ref": self.goal_type_ref.to_json(),
                "typed_parameters": [list(item) for item in self.typed_parameters],
                "input_versions": list(self.input_versions),
                "authority_scope": self.authority_scope,
                "semantic_scope": self.semantic_scope,
                "assurance_policy_ref": self.assurance_policy_ref,
                "freshness_policy_ref": self.freshness_policy_ref,
                "domain_semantics": self.domain_semantics,
                "side_effect_kind": str(self.side_effect_kind),
                "effect_identity": self.effect_identity,
            }
        )


@dataclass(frozen=True, slots=True)
class ShareDecision:
    verdict: ShareVerdict
    reason: str
    differing_fields: tuple[str, ...] = ()

    @property
    def shareable(self) -> bool:
        return self.verdict is ShareVerdict.SHAREABLE


def may_share(
    left: SharingSignature,
    right: SharingSignature,
    *,
    reuse_policy: ReusePolicy,
) -> ShareDecision:
    """§8.3: the whole signature must match and the type must permit reuse."""

    differing = tuple(
        name
        for name in (
            "goal_type_ref",
            "typed_parameters",
            "input_versions",
            "authority_scope",
            "semantic_scope",
            "assurance_policy_ref",
            "freshness_policy_ref",
            "domain_semantics",
            "side_effect_kind",
            "effect_identity",
        )
        if getattr(left, name) != getattr(right, name)
    )
    if differing:
        return ShareDecision(
            verdict=ShareVerdict.SIGNATURE_DIFFERS,
            reason=(
                "the sharing signature differs in "
                + ", ".join(differing)
                + "; these are two goals, not one"
            ),
            differing_fields=differing,
        )
    if reuse_policy is ReusePolicy.NEW_WORK:
        return ShareDecision(
            verdict=ShareVerdict.REUSE_NOT_PERMITTED,
            reason=(
                "the task type has not declared that its result may be reused; "
                "an occurrence stays its own work (TG §12)"
            ),
        )
    if left.side_effect_kind not in READ_ONLY_EFFECTS and left.effect_identity is None:
        return ShareDecision(
            verdict=ShareVerdict.SIDE_EFFECT_NOT_SHAREABLE,
            reason=(
                "a goal that writes outside the orchestrator is not shared by default; "
                "two identically-parameterised sends are two sends (§8.3)"
            ),
        )
    return ShareDecision(
        verdict=ShareVerdict.SHAREABLE,
        reason="full signature match and the task type permits reuse",
    )


@dataclass(frozen=True, slots=True)
class SharedGoalEntry:
    """One goal occurrence that already exists and may be bound by another slot."""

    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId
    signature: SharingSignature
    reuse_policy: ReusePolicy
    #: The exact Acceptance this occurrence's result was accepted under, when there
    #: is one.  A typed reference, not a bare id: TG decision 9 binds reuse to a
    #: specific acceptance and an id prefix does not establish which kind of thing
    #: it names.
    acceptance_ref: TypedRef | None = None

    def __post_init__(self) -> None:
        if self.acceptance_ref is not None and not isinstance(self.acceptance_ref, TypedRef):
            raise ContractError("shared_goal_entry.acceptance_ref must be a TypedRef")


@dataclass(frozen=True, slots=True)
class ShareSuggestion:
    """A merge *candidate*.  §8.3: similarity never binds anything by itself."""

    occurrence_id: OccurrenceId
    similarity: float
    reason: str
    decision: ShareDecision
    advisory_only: bool = True


class SharedGoalIndex:
    """Known goal occurrences, addressed by their full sharing signature.

    A plain dict keyed by :meth:`SharingSignature.digest`.  There is no fuzzy
    lookup on this path on purpose: :meth:`suggest` exists for the fuzzy question
    and returns suggestions, so nothing can slip from "looks alike" to "is the
    same" by taking one more code path.
    """

    def __init__(self, entries: Sequence[SharedGoalEntry] = ()) -> None:
        self._by_digest: dict[str, SharedGoalEntry] = {}
        for entry in entries:
            self.add(entry)

    def add(self, entry: SharedGoalEntry) -> None:
        if not isinstance(entry, SharedGoalEntry):
            raise ContractError("add expects a SharedGoalEntry")
        self._by_digest.setdefault(entry.signature.digest(), entry)

    def entries(self) -> tuple[SharedGoalEntry, ...]:
        return tuple(self._by_digest[key] for key in sorted(self._by_digest))

    def lookup(
        self, signature: SharingSignature, *, reuse_policy: ReusePolicy
    ) -> tuple[SharedGoalEntry | None, ShareDecision]:
        entry = self._by_digest.get(signature.digest())
        if entry is None:
            return None, ShareDecision(
                verdict=ShareVerdict.SIGNATURE_DIFFERS,
                reason="no existing occurrence carries this sharing signature",
            )
        decision = may_share(entry.signature, signature, reuse_policy=reuse_policy)
        return (entry if decision.shareable else None), decision

    def suggest(
        self, signature: SharingSignature, *, statement: str = "", minimum: float = 0.5
    ) -> tuple[ShareSuggestion, ...]:
        """Occurrences that merely *look* related, each with the reason it was not bound."""

        out: list[ShareSuggestion] = []
        for entry in self.entries():
            if entry.signature.digest() == signature.digest():
                continue
            similarity = statement_similarity(
                statement or signature.goal_type_ref.id, entry.signature.goal_type_ref.id
            )
            if similarity < minimum:
                continue
            out.append(
                ShareSuggestion(
                    occurrence_id=entry.occurrence_id,
                    similarity=similarity,
                    reason="lexically close goal type; a human or planner decides (§8.3)",
                    decision=may_share(entry.signature, signature, reuse_policy=entry.reuse_policy),
                )
            )
        return tuple(sorted(out, key=lambda item: (-item.similarity, str(item.occurrence_id))))


# --------------------------------------------------------------------------------------
# ground_method (§18.3)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """What grounding decided about one slot, before the draft is assembled."""

    step: MethodStep
    identity: SlotIdentity
    spec: TaskTypeSpec
    requiredness: Requiredness
    reuse_policy: ReusePolicy
    bound_occurrence_id: OccurrenceId
    bound_task_id: TaskRef
    bound_obligation_id: ObligationId
    signature: SharingSignature
    share_decision: ShareDecision | None = None
    acceptance_ref: TypedRef | None = None
    arguments: Mapping[str, Any] = None  # type: ignore[assignment]

    @property
    def shared(self) -> bool:
        return self.bound_occurrence_id != self.identity.occurrence_id


def requiredness_of(step: MethodStep) -> Requiredness:
    """§6.1: a step that refines the parent duty is required by this method.

    ``INDEPENDENT_AUTHORIZED`` is the "authorised optional" branch: work a method
    may legitimately add, which does not gate the parent.  A planner cannot turn a
    user's required outcome into an optional one, because the relation is a field
    of the immutable method definition, not a choice made at grounding time.
    """

    if step.obligation_relation is ObligationRelation.REFINES_PARENT:
        return Requiredness.REQUIRED
    return Requiredness.OPTIONAL_AUTHORIZED


def resolve_arguments(
    step: MethodStep, parameters: Mapping[str, Any], *, path: str
) -> dict[str, Any]:
    """Resolve a step's argument expressions against the grounded parameters.

    ``OutputValue`` is left symbolic: a step output does not exist at grounding
    time and becomes a DATA requirement in the compiler, not a value here.
    """

    out: dict[str, Any] = {}
    for name, argument in step.arguments.items():
        if isinstance(argument, OutputValue):
            continue
        out[name] = _resolve_value(argument, parameters, path=f"{path}.{name}")
    return out


def _resolve_value(value: Any, parameters: Mapping[str, Any], *, path: str) -> Any:
    from ...contracts.htn import ArrayValue, ConstantValue, ObjectValue

    if isinstance(value, ConstantValue):
        return value.value
    if isinstance(value, ParameterValue):
        if value.name not in parameters:
            raise GroundingError(f"{path}: no bound parameter named {value.name!r}")
        return parameters[value.name]
    if isinstance(value, ObjectValue):
        return {
            key: _resolve_value(item, parameters, path=f"{path}.{key}")
            for key, item in value.fields.items()
        }
    if isinstance(value, ArrayValue):
        return [
            _resolve_value(item, parameters, path=f"{path}[{position}]")
            for position, item in enumerate(value.items)
        ]
    if isinstance(value, OutputValue):
        raise GroundingError(
            f"{path}: a step output has no value at grounding time; it becomes a DATA "
            "requirement in the compiler"
        )
    raise GroundingError(f"{path}: unsupported value expression")


def ground_method(
    task: TaskSemanticBindingV1,
    method: MethodContract,
    bindings: Mapping[str, Any],
    assessment: ApplicabilityReport,
    *,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    sharing: SharedGoalIndex | None = None,
    reuse_acceptances: Mapping[str, TypedRef] | None = None,
    plan_revision: PlanRevision = PlanRevision(0),
    goal_occurrence_id: OccurrenceId | None = None,
    world_snapshot_id: str | None = None,
) -> MethodInstanceDraft:
    """§18.3: type-check, bind variables, derive identities.  No database effects.

    Refuses rather than repairs.  An inapplicable assessment, a parameter of the
    wrong type, a step whose task type is not registered — each is a
    :class:`GroundingError`, because a draft built on any of them would carry a
    precondition witness for a world that does not hold.
    """

    if not isinstance(task, TaskSemanticBindingV1):
        raise GroundingError("ground_method expects a TaskSemanticBindingV1")
    if not isinstance(method, MethodContract):
        raise GroundingError("ground_method expects a MethodContract")
    if not isinstance(assessment, ApplicabilityReport):
        raise GroundingError("ground_method expects an ApplicabilityReport")
    if task.form is not TaskForm.COMPOUND:
        raise GroundingError(
            f"task {task.task_id!s} is primitive; only a compound task is refined by a "
            "method (§6.2)"
        )
    goal_type = catalog.resolve(method.goal_type_ref)
    if goal_type is None:
        raise GroundingError(
            f"goal type {method.goal_type_ref.id!r} v{method.goal_type_ref.version} is not "
            "registered at that content hash"
        )
    if goal_type.goal_signature.signature_id != task.goal_signature.signature_id:
        raise GroundingError(
            f"method {method.method_id!r} decomposes goal signature "
            f"{goal_type.goal_signature.signature_id!r}, but the task is "
            f"{task.goal_signature.signature_id!r}"
        )

    if assessment.status is not ApplicabilityStatus.APPLICABLE:
        raise GroundingError(
            f"method {method.method_id!r} is {assessment.status!s} for task "
            f"{task.task_id!s}; a method is grounded only where it applies (§7.2)"
        )
    gate = assessment.authorization
    if method.applicable_when and (gate is None or not gate.allowed):
        raise GroundingError(
            f"method {method.method_id!r} has preconditions whose TRUE is not "
            "evidence-backed; UNKNOWN and CONFLICT never open a gate (§6.6 rule 2)"
        )

    parameters = _grounded_parameters(task, method, bindings, schemas)
    parameters_digest = content_hash_of(
        [{"name": key, "value": parameters[key]} for key in sorted(parameters)]
    )
    parent_occurrence = (
        goal_occurrence_id if goal_occurrence_id is not None else OccurrenceId(str(task.task_id))
    )
    instance_id = instance_identity(
        goal_id=task.task_id,
        goal_occurrence_id=parent_occurrence,
        method_ref=method.method_ref(),
        parameters_digest=parameters_digest,
    )

    plans = plan_slots(
        task,
        method,
        parameters,
        instance_id=instance_id,
        catalog=catalog,
        sharing=sharing,
        reuse_acceptances=reuse_acceptances or {},
    )
    child_bindings = tuple(
        ChildBinding(
            instance_id=instance_id,
            slot_key=plan.identity.slot_key,
            occurrence_id=plan.bound_occurrence_id,
            obligation_id=plan.bound_obligation_id,
            requiredness=plan.requiredness,
            reuse_policy=plan.reuse_policy,
            goal_occurrence_id=plan.bound_occurrence_id,
            acceptance_ref=plan.acceptance_ref
            if plan.reuse_policy is ReusePolicy.REUSE_ACCEPTED
            else None,
        )
        for plan in plans
    )

    # Every applicable_when is TRUE here: ALL is TRUE only when every member is, and
    # the assessment above is APPLICABLE.  Freezing the digest with that truth is
    # what lets recheck_method_instance say later that the world moved (§6.6 rule 3).
    witnesses = tuple(
        PreconditionWitnessRecord(
            condition_digest=condition_digest(condition),
            phase=PreconditionPhase.SELECT,
            truth=TruthValue.TRUE,
        )
        for condition in method.applicable_when
    )

    return MethodInstanceDraft(
        instance_id=instance_id,
        goal_id=task.task_id,
        obligation_id=task.obligation_id,
        method_ref=method.method_ref(),
        grounded_parameters=tuple(
            Binding(name=key, value=parameters[key]) for key in sorted(parameters)
        ),
        world_snapshot_id=world_snapshot_id,
        precondition_witnesses=witnesses,
        assumption_refs=method.basis_refs,
        child_bindings=child_bindings,
        plan_revision=plan_revision,
        goal_occurrence_id=parent_occurrence,
    )


def _grounded_parameters(
    task: TaskSemanticBindingV1,
    method: MethodContract,
    bindings: Mapping[str, Any],
    schemas: SchemaCatalog,
) -> dict[str, Any]:
    """Merge the task's typed parameters with the caller's bindings and type-check."""

    if not isinstance(bindings, Mapping):
        raise GroundingError("ground_method expects a mapping of parameter bindings")
    merged: dict[str, Any] = dict(task.typed_parameters)
    for name, value in bindings.items():
        merged[identifier(name, "binding name")] = value
    schema = schemas.resolve(method.parameter_schema_ref)
    if schema is None:
        raise GroundingError(
            f"parameter schema {method.parameter_schema_ref.id!r} "
            f"v{method.parameter_schema_ref.version} is not registered at that content hash"
        )
    # Only the declared fields travel into the instance.  Anything else the task
    # happens to carry is not a parameter of *this* method, and silently passing it
    # through would make the parameters digest depend on unrelated state.
    surplus = sorted(set(merged) - {item.name for item in schema.fields})
    scoped = {name: merged[name] for name in merged if name not in surplus}
    check = schema.check(scoped)
    if not check.ok:
        raise GroundingError(
            f"method {method.method_id!r} parameters do not type-check: "
            + "; ".join(check.messages())
        )
    return scoped


def plan_slots(
    task: TaskSemanticBindingV1,
    method: MethodContract,
    parameters: Mapping[str, Any],
    *,
    instance_id: MethodInstanceId,
    catalog: TaskTypeCatalog,
    sharing: SharedGoalIndex | None,
    reuse_acceptances: Mapping[str, TypedRef],
) -> tuple[SlotPlan, ...]:
    """Decide, per slot, whether it creates work or binds an existing occurrence."""

    plans: list[SlotPlan] = []
    for step in method.steps:
        spec = catalog.resolve(step.task_type_ref)
        if spec is None:
            raise GroundingError(
                f"step {step.local_id!r} names task type {step.task_type_ref.id!r} "
                f"v{step.task_type_ref.version}, which is not registered"
            )
        if spec.form is not step.form:
            raise GroundingError(
                f"step {step.local_id!r} declares form {step.form!s} but task type "
                f"{step.task_type_ref.id!r} is {spec.form!s}"
            )
        identity = slot_identity(
            instance_id, step.local_id, task.obligation_id, step.obligation_relation
        )
        arguments = resolve_arguments(step, parameters, path=f"step[{step.local_id}]")
        signature = SharingSignature.of(
            spec,
            arguments,
            authority_scope=task.semantic_scope,
            semantic_scope=task.semantic_scope,
        )
        permitted = effective_reuse_policy(step, spec)
        entry: SharedGoalEntry | None = None
        decision = None
        if sharing is not None:
            entry, decision = sharing.lookup(signature, reuse_policy=permitted)
        if entry is None:
            plans.append(
                SlotPlan(
                    step=step,
                    identity=identity,
                    spec=spec,
                    requiredness=requiredness_of(step),
                    reuse_policy=ReusePolicy.NEW_WORK,
                    bound_occurrence_id=identity.occurrence_id,
                    bound_task_id=identity.task_id,
                    bound_obligation_id=identity.obligation_id,
                    signature=signature,
                    share_decision=decision,
                    arguments=arguments,
                )
            )
            continue
        acceptance = reuse_acceptances.get(step.local_id, entry.acceptance_ref)
        policy = ReusePolicy.REUSE_ACCEPTED if acceptance is not None else ReusePolicy.SHARE_ACTIVE
        plans.append(
            SlotPlan(
                step=step,
                identity=identity,
                spec=spec,
                requiredness=requiredness_of(step),
                reuse_policy=policy,
                bound_occurrence_id=entry.occurrence_id,
                bound_task_id=entry.task_id,
                bound_obligation_id=entry.obligation_id,
                signature=signature,
                share_decision=decision,
                acceptance_ref=acceptance,
                arguments=arguments,
            )
        )
    return tuple(plans)


def child_task_bindings(
    draft: MethodInstanceDraft,
    method: MethodContract,
    task: TaskSemanticBindingV1,
    *,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    sharing: SharedGoalIndex | None = None,
    reuse_acceptances: Mapping[str, TypedRef] | None = None,
) -> tuple[TaskSemanticBindingV1, ...]:
    """The semantic bindings of the slots this draft *creates*.

    A shared slot binds an occurrence that already exists, so it contributes no
    binding here: TG §3.2 keeps ``TaskSemanticBindingV1`` the single authority for
    a task, and minting a second one for a borrowed occurrence is how a shared
    sub-goal acquires two owners.
    """

    parameters = {binding.name: binding.value for binding in draft.grounded_parameters}
    plans = plan_slots(
        task,
        method,
        parameters,
        instance_id=draft.instance_id,
        catalog=catalog,
        sharing=sharing,
        reuse_acceptances=reuse_acceptances or {},
    )
    out: list[TaskSemanticBindingV1] = []
    for plan in plans:
        if plan.shared:
            continue
        out.append(task_binding_for(plan, draft, task, schemas=schemas))
    return tuple(out)


def task_binding_for(
    plan: SlotPlan,
    draft: MethodInstanceDraft,
    parent: TaskSemanticBindingV1,
    *,
    schemas: SchemaCatalog,
) -> TaskSemanticBindingV1:
    """Build the semantic binding of one newly created slot."""

    spec = plan.spec
    del schemas  # the shape is already checked; kept in the signature for symmetry
    contract_payload = {
        "task_type": spec.task_type_ref.to_json(),
        "parameters": {key: plan.arguments[key] for key in sorted(plan.arguments)},
        "instance": str(draft.instance_id),
        "slot": plan.identity.slot_key,
    }
    return TaskSemanticBindingV1(
        task_id=plan.identity.task_id,
        obligation_id=plan.identity.obligation_id,
        contract_revision=parent.contract_revision,
        contract_hash=content_hash_of(contract_payload),
        form=spec.form,
        goal_signature=spec.goal_signature,
        typed_parameters=dict(plan.arguments),
        requirement_refs=parent.requirement_refs,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        operator_ref=spec.operator_ref,
        semantic_scope=parent.semantic_scope,
        capability_requirements=spec.required_capabilities,
        precondition_refs=tuple(
            PreconditionRef(
                condition_digest=witness.condition_digest, phase=PreconditionPhase.SELECT
            )
            for witness in draft.precondition_witnesses
        )
        if spec.form is TaskForm.PRIMITIVE
        else (),
        occurrence_binding=MethodOccurrenceBinding(
            method_instance_id=draft.instance_id,
            occurrence_id=plan.identity.occurrence_id,
            slot_key=plan.identity.slot_key,
        ),
        resource_reads=spec.resource_reads if spec.form is TaskForm.PRIMITIVE else (),
        resource_writes=spec.resource_writes if spec.form is TaskForm.PRIMITIVE else (),
        side_effect_kind=spec.side_effect_kind if spec.form is TaskForm.PRIMITIVE else None,
    )


def data_flows(method: MethodContract) -> tuple[tuple[str, str, str, str], ...]:
    """``(producer_step, output_port, consumer_step, input_port)`` for each DATA link.

    This is the single reading of "a step argument reads another step's output";
    the admission check, the compiler and the HDDL export all consult it, so none
    of them can disagree about what the method's data flow is.
    """

    out: list[tuple[str, str, str, str]] = []
    for step in method.steps:
        for name, argument in step.arguments.items():
            for node in iter_values(argument):
                if isinstance(node, OutputValue) and node.step != step.local_id:
                    entry = (node.step, node.port, step.local_id, name)
                    if entry not in out:
                        out.append(entry)
    return tuple(out)


def describe_draft(draft: MethodInstanceDraft) -> str:
    """A short, deterministic description for diagnostics and journals."""

    return text(
        f"{draft.method_ref.method_id}@{draft.method_ref.version} refines "
        f"{draft.goal_id!s} at {draft.effective_goal_occurrence_id!s} "
        f"({len(draft.child_bindings)} slots)",
        "description",
    )


__all__ = (
    "GroundingError",
    "ShareDecision",
    "ShareSuggestion",
    "ShareVerdict",
    "SharedGoalEntry",
    "SharedGoalIndex",
    "SharingSignature",
    "SlotIdentity",
    "SlotPlan",
    "child_task_bindings",
    "data_flows",
    "derive_id",
    "describe_draft",
    "ground_method",
    "instance_identity",
    "may_share",
    "plan_slots",
    "requiredness_of",
    "resolve_arguments",
    "slot_identity",
    "task_binding_for",
)
