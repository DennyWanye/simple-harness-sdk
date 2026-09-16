# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""HTN planning contracts: identities, method definitions, plan proposals (§6, §18.1).

Everything here is data.  A method is an immutable, versioned *definition*, never a
piece of Python with execution rights (§6.3), and a condition is a structured AST
the safe interpreter walks — never a string an evaluator would run (§7.3 step 1).

Two naming rules from §18.3 are enforced by the types themselves:

``PlanProposal``
    what a model proposes.  Unchecked, not committable; mirrors
    ``plan-revision-proposal-v1``.
``ProposedPlanDelta``
    what the compiler emits after structural and coverage checks.  Only this may
    be handed to a Commit.

They are different classes with different required fields, so one cannot stand in
for the other by accident or by re-labelling a payload.

The five version axes of §6.4 / TG §3.3 are separate ``NewType``s so a
``plan_revision`` cannot be passed where a ``dispatch_generation`` belongs, and
typed references carry their ``kind`` on the wire so no id prefix can buy a role.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NewType, TypeAlias

from .evidence_state import PreconditionPhase, PreconditionWitnessRecord, parse_phase
from .models import ContractError
from .semantic_base import (
    MAX_TEXT,
    EvidenceRef,
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
    enum_of,
    fields_of,
    flag,
    hash_hex,
    identifier,
    identifiers,
    index,
    json_object,
    json_value,
    optional_identifier,
    reject_executable,
    reject_model_claimed_provenance,
    schema_version,
    sequence_of,
    text,
)

METHOD_CONTRACT_SCHEMA_VERSION = 1
PLAN_REVISION_PROPOSAL_SCHEMA_VERSION = 1
EXECUTION_FEEDBACK_SCHEMA_VERSION = 1

#: A structured condition may not grow without bound; a proposal that needs more
#: than this is refused at the boundary rather than interpreted (§7.3 step 1).
MAX_CONDITION_NODES = 512

# --------------------------------------------------------------------------------------
# Identities (TG §3.1) and the five version axes (§6.4 / TG §3.3)
# --------------------------------------------------------------------------------------

MissionRef = NewType("MissionRef", str)
TaskRef = NewType("TaskRef", str)
ObligationId = NewType("ObligationId", str)
MethodInstanceId = NewType("MethodInstanceId", str)
OccurrenceId = NewType("OccurrenceId", str)
AttemptRef = NewType("AttemptRef", str)
DataBindingId = NewType("DataBindingId", str)
ResultRef = NewType("ResultRef", str)
AcceptanceRef = NewType("AcceptanceRef", str)
ArtifactRef = NewType("ArtifactRef", str)
SchemaRef = NewType("SchemaRef", str)
PolicyRef = NewType("PolicyRef", str)

#: §6.4: goal / success-criteria / input-contract changes.
ContractRevision = NewType("ContractRevision", int)
#: §6.4: atomic record state CAS.
RecordVersion = NewType("RecordVersion", int)
#: §6.4: execution right or input superseded / cancelled.
DispatchGeneration = NewType("DispatchGeneration", int)
#: §6.4: adopted method, members, ORDER / DATA structure.
PlanRevision = NewType("PlanRevision", int)
#: §6.4: current usability of observations, supports and acceptances.
ValidityRevision = NewType("ValidityRevision", int)
#: TG §3.3: the local generation of this task's resolved inputs.
InputBindingRevision = NewType("InputBindingRevision", int)


def mission_ref(value: object, name: str = "mission_id") -> MissionRef:
    return MissionRef(identifier(value, name))


def task_ref(value: object, name: str = "task_id") -> TaskRef:
    return TaskRef(identifier(value, name))


def obligation_id(value: object, name: str = "obligation_id") -> ObligationId:
    return ObligationId(identifier(value, name))


def method_instance_id(value: object, name: str = "instance_id") -> MethodInstanceId:
    return MethodInstanceId(identifier(value, name))


def occurrence_id(value: object, name: str = "occurrence_id") -> OccurrenceId:
    return OccurrenceId(identifier(value, name))


def contract_revision(value: object, name: str = "contract_revision") -> ContractRevision:
    return ContractRevision(index(value, name))


def record_version(value: object, name: str = "record_version") -> RecordVersion:
    return RecordVersion(index(value, name))


def dispatch_generation(value: object, name: str = "dispatch_generation") -> DispatchGeneration:
    return DispatchGeneration(index(value, name))


def plan_revision(value: object, name: str = "plan_revision") -> PlanRevision:
    return PlanRevision(index(value, name))


def validity_revision(value: object, name: str = "validity_revision") -> ValidityRevision:
    return ValidityRevision(index(value, name))


def input_binding_revision(
    value: object, name: str = "input_binding_revision"
) -> InputBindingRevision:
    return InputBindingRevision(index(value, name))


def task_ref_from_typed(value: object, name: str) -> TaskRef:
    """Decode a ``TaskRef`` from its typed wire form ``{"kind": "task", "id": ...}``.

    TG §3.1: a bare string — whatever prefix it carries — is refused here.  The
    discriminator is the only thing that says which kind of subject this is, so
    an obligation id cannot be spent as a goal id by spelling it like one.
    """

    if isinstance(value, str):
        raise ContractError(
            f"{name} must be a typed reference object, not a bare string; "
            "an id prefix does not establish the kind"
        )
    ref = TypedRef.of_kind(TypedRefKind.TASK, value, name)
    return TaskRef(ref.id)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class TaskForm(StrEnum):
    """§6.2: the refinement axis.  Orthogonal to the existing ``kind``."""

    COMPOUND = "compound"
    PRIMITIVE = "primitive"


class RelationKind(StrEnum):
    """§6.5 / TG §4: one meaning per edge kind; ``dependencies`` used to carry five."""

    REFINEMENT = "refinement"
    SATISFIES = "satisfies"
    ORDER = "ORDER"
    DATA = "DATA"
    SUPPORT = "SUPPORT"
    ASSUMPTION = "ASSUMPTION"
    SUPERVISION = "supervision"
    FUNDING = "funding"
    SUPERSEDES = "supersedes"


class ReleaseCondition(StrEnum):
    """TG decision 1: an ORDER edge releases on acceptance, not on "it stopped".

    ``settled_terminal`` exists only for clean-up / convergence contracts, and an
    UNKNOWN outcome never settles.
    """

    ACCEPTED = "accepted"
    SETTLED_TERMINAL = "settled_terminal"


class Requiredness(StrEnum):
    """§6.1: necessary, authorised-optional, or required only by a given method."""

    REQUIRED = "required"
    OPTIONAL_AUTHORIZED = "optional_authorized"
    CONDITIONAL = "conditional"


class ReusePolicy(StrEnum):
    """TG decision 9: the four kinds of de-duplication stay apart.

    Reusing an accepted result, sharing live work and starting fresh work are
    different decisions with different accounting.
    """

    NEW_WORK = "new_work"
    REUSE_ACCEPTED = "reuse_accepted"
    SHARE_ACTIVE = "share_active"


class ObligationRelation(StrEnum):
    """``method-contract-v1`` step field: does the step refine the parent duty?"""

    REFINES_PARENT = "refines_parent"
    INDEPENDENT_AUTHORIZED = "independent_authorized"


class SourceRevisionPolicy(StrEnum):
    """TG §4.3: pinned historical input vs. follow-the-authorised-revision."""

    PINNED = "PINNED"
    FOLLOW_AUTHORIZED_REVISION = "FOLLOW_AUTHORIZED_REVISION"


class PortCardinality(StrEnum):
    """TG §4.3: a single-valued port has exactly one binding; a set port is ordered."""

    SINGLE = "single"
    SET = "set"


class PortOrdering(StrEnum):
    """TG §4.3: how the bindings feeding a set-valued port are ordered.

    A set port that leaves this undeclared is the "whoever wrote last wins" bug the
    annex names; :func:`undeclared_set_ports` reports those so the compiler can
    refuse them.  The contract keeps the declaration optional because a port list
    can be assembled before the ordering decision is made.
    """

    BY_PRODUCER_ORDINAL = "by_producer_ordinal"
    BY_KEY = "by_key"
    EXPLICIT = "explicit"


class SideEffectKind(StrEnum):
    """§6.6: what a primitive operator does to the world outside the orchestrator."""

    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_STATE_WRITE = "external_state_write"
    EXTERNAL_EVENT_WRITE = "external_event_write"


class ApplicabilityCheckPolicy(StrEnum):
    """When this task's preconditions are re-checked (§6.6 v1.3)."""

    SELECT_ONLY = "select_only"
    SELECT_AND_ACCEPT = "select_and_accept"
    MAINTAIN_CONTINUOUS = "maintain_continuous"


class MethodRegistryStatus(StrEnum):
    """§7.3 lifecycle, written by the registry service — never by a planner."""

    DRAFT = "DRAFT"
    STRUCTURALLY_VALID = "STRUCTURALLY_VALID"
    TRIAL_ADMITTED = "TRIAL_ADMITTED"
    EVALUATED = "EVALUATED"
    ADMITTED = "ADMITTED"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class RegistryAuthor(StrEnum):
    SYSTEM = "system"
    MODEL = "model"


#: §6.3 / §7.3: the only status a model-authored submission may carry.
MODEL_SUBMITTABLE_STATUS = frozenset({MethodRegistryStatus.DRAFT})


class RunningWorkPolicy(StrEnum):
    """``plan-revision-proposal-v1``: what happens to work already in flight."""

    RETAIN_IF_BINDINGS_UNCHANGED = "retain_if_bindings_unchanged"
    REQUEST_STOP_THEN_RECONCILE = "request_stop_then_reconcile"
    EXPLICIT_PER_SUBJECT_IN_COMMIT = "explicit_per_subject_in_commit"


class ReadItemKind(StrEnum):
    """``plan-revision-proposal-v1`` read_set entry kinds."""

    REQUIREMENTS = "requirements"
    OBLIGATION = "obligation"
    TASK = "task"
    METHOD = "method"
    FACT = "fact"
    ACCEPTANCE = "acceptance"
    OPERATION = "operation"
    AUTHORITY = "authority"
    CAPABILITY = "capability"


# --------------------------------------------------------------------------------------
# Value and condition AST (``method-contract-v1`` $defs/value and $defs/condition)
# --------------------------------------------------------------------------------------

ValueExpr: TypeAlias = "ParameterValue | ConstantValue | OutputValue | ObjectValue | ArrayValue"
Condition: TypeAlias = (
    "PredicateCondition | AllCondition | AnyCondition | NotCondition | ConstantCondition"
)


@dataclass(frozen=True, slots=True)
class ParameterValue:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "value.parameter.name"))

    def to_json(self) -> dict[str, Any]:
        return {"op": "parameter", "name": self.name}


@dataclass(frozen=True, slots=True)
class ConstantValue:
    value: Any

    def __post_init__(self) -> None:
        reject_executable(self.value, "value.constant")
        object.__setattr__(self, "value", json_value(self.value, "value.constant"))

    def to_json(self) -> dict[str, Any]:
        return {"op": "constant", "value": self.value}


@dataclass(frozen=True, slots=True)
class OutputValue:
    step: str
    port: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", identifier(self.step, "value.output.step"))
        object.__setattr__(self, "port", identifier(self.port, "value.output.port"))

    def to_json(self) -> dict[str, Any]:
        return {"op": "output", "step": self.step, "port": self.port}


@dataclass(frozen=True, slots=True)
class ObjectValue:
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise ContractError("value.object.fields must be an object")

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "object",
            "fields": {key: item.to_json() for key, item in self.fields.items()},
        }


@dataclass(frozen=True, slots=True)
class ArrayValue:
    items: tuple[Any, ...]

    def to_json(self) -> dict[str, Any]:
        return {"op": "array", "items": [item.to_json() for item in self.items]}


class StructureBudget:
    """A shared node counter so a whole contract, not one branch, is bounded."""

    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self, name: str) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ContractError(f"{name} exceeds the {MAX_CONDITION_NODES}-node structure budget")


def parse_value(value: object, name: str, budget: StructureBudget) -> ValueExpr:
    budget.spend(name)
    reject_executable(value, name)
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a structured value object")
    op = value.get("op")
    if op == "parameter":
        data = fields_of(value, name, required=("op", "name"))
        return ParameterValue(name=data["name"])
    if op == "constant":
        data = fields_of(value, name, required=("op", "value"))
        return ConstantValue(value=data["value"])
    if op == "output":
        data = fields_of(value, name, required=("op", "step", "port"))
        return OutputValue(step=data["step"], port=data["port"])
    if op == "object":
        data = fields_of(value, name, required=("op", "fields"))
        raw = data["fields"]
        if not isinstance(raw, Mapping):
            raise ContractError(f"{name}.fields must be an object")
        return ObjectValue(
            fields={
                identifier(key, f"{name}.fields.key"): parse_value(item, f"{name}.{key}", budget)
                for key, item in raw.items()
            }
        )
    if op == "array":
        data = fields_of(value, name, required=("op", "items"))
        return ArrayValue(
            items=sequence_of(
                data["items"],
                f"{name}.items",
                lambda item, where: parse_value(item, where, budget),
            )
        )
    raise ContractError(
        f"{name}.op must be one of ['array', 'constant', 'object', 'output', 'parameter']"
    )


def parse_arguments(value: object, name: str, budget: StructureBudget) -> dict[str, ValueExpr]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return {
        identifier(key, f"{name}.key"): parse_value(item, f"{name}.{key}", budget)
        for key, item in value.items()
    }


def arguments_to_json(arguments: Mapping[str, ValueExpr]) -> dict[str, Any]:
    return {key: item.to_json() for key, item in sorted(arguments.items())}


@dataclass(frozen=True, slots=True)
class PredicateCondition:
    predicate_ref: VersionedRef
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.predicate_ref, VersionedRef):
            raise ContractError("condition.predicate_ref must be a VersionedRef")
        if not isinstance(self.arguments, Mapping):
            raise ContractError("condition.arguments must be an object")

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "predicate",
            "predicate_ref": self.predicate_ref.to_json(),
            "arguments": arguments_to_json(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class AllCondition:
    items: tuple[Any, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"op": "all", "items": [item.to_json() for item in self.items]}


@dataclass(frozen=True, slots=True)
class AnyCondition:
    items: tuple[Any, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"op": "any", "items": [item.to_json() for item in self.items]}


@dataclass(frozen=True, slots=True)
class NotCondition:
    item: Any

    def to_json(self) -> dict[str, Any]:
        return {"op": "not", "item": self.item.to_json()}


@dataclass(frozen=True, slots=True)
class ConstantCondition:
    value: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", flag(self.value, "condition.constant.value"))

    def to_json(self) -> dict[str, Any]:
        return {"op": "constant", "value": self.value}


def parse_condition(
    value: object, name: str = "condition", budget: StructureBudget | None = None
) -> Condition:
    """Decode a structured condition.

    Only the five documented node shapes are accepted.  A string, a callable, a
    SQL fragment or an unknown ``op`` is a ``ContractError``: the interpreter has
    no path that would ever run model-supplied code (§6.6, §7.3 step 1).
    """

    budget = budget if budget is not None else StructureBudget(MAX_CONDITION_NODES)
    budget.spend(name)
    reject_executable(value, name)
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a structured condition object")
    op = value.get("op")
    if op == "predicate":
        data = fields_of(value, name, required=("op", "predicate_ref", "arguments"))
        return PredicateCondition(
            predicate_ref=VersionedRef.from_json(data["predicate_ref"], f"{name}.predicate_ref"),
            arguments=parse_arguments(data["arguments"], f"{name}.arguments", budget),
        )
    if op in {"all", "any"}:
        data = fields_of(value, name, required=("op", "items"))
        items = sequence_of(
            data["items"],
            f"{name}.items",
            lambda item, where: parse_condition(item, where, budget),
        )
        return AllCondition(items=items) if op == "all" else AnyCondition(items=items)
    if op == "not":
        data = fields_of(value, name, required=("op", "item"))
        return NotCondition(item=parse_condition(data["item"], f"{name}.item", budget))
    if op == "constant":
        data = fields_of(value, name, required=("op", "value"))
        return ConstantCondition(value=data["value"])
    raise ContractError(f"{name}.op must be one of ['all', 'any', 'constant', 'not', 'predicate']")


def parse_conditions(
    value: object, name: str, budget: StructureBudget | None = None
) -> tuple[Any, ...]:
    budget = budget if budget is not None else StructureBudget(MAX_CONDITION_NODES)
    return sequence_of(value, name, lambda item, where: parse_condition(item, where, budget))


def condition_node_count(condition: Any) -> int:
    if isinstance(condition, (AllCondition, AnyCondition)):
        return 1 + sum(condition_node_count(item) for item in condition.items)
    if isinstance(condition, NotCondition):
        return 1 + condition_node_count(condition.item)
    return 1


def condition_digest(condition: Any) -> str:
    """A stable identity for one condition, used to pin precondition witnesses."""

    return content_hash_of(condition.to_json())


def is_empty_expression(conditions: tuple[Any, ...]) -> bool:
    """§6.6 v1.4: an empty expression is a lint warning, never a silent pass.

    Empty ALL is TRUE and empty ANY is FALSE by the priority tables, but such a
    TRUE never satisfies :func:`planning.htn.applicability.authorization_gate`.
    """

    return len(conditions) == 0


# --------------------------------------------------------------------------------------
# Method definition (``method-contract-v1``)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphStructureBudget:
    """ADR-08: versioned, frozen size bounds for one deployment's task network.

    Not :class:`StructureBudget`, which is a mutable node counter for one condition
    parse.  These are declarative and versioned by ``budget_version``: exceeding one
    is reported as ``BOUND_REACHED`` with the structure built so far, never repaired
    by silently dropping nodes and never restated as "the goal is impossible".

    The dimensions are deliberately separate, because they bound different things and
    fail for different reasons:

    ``max_live_tasks``
        tasks that are open at one moment — the concurrency-and-memory bound.
    ``max_depth``
        refinement depth from the root — how far a plan may nest.
    ``max_expanded_nodes``
        cumulative expansions over the mission's life, including retired branches;
        this is what stops a search that keeps re-planning without deepening.
    ``max_candidates``
        alternative methods considered for one goal at one time.
    ``max_recursion_fuel``
        the per-obligation default handed to :class:`~.obligations.ObligationLedger`.
    ``max_nodes`` / ``max_edges`` / ``max_fan_out``
        the *projection* bounds: how large one materialised execution view may get,
        and how many edges may leave a single node.  A plan can be within its
        planning bounds and still project to something no scheduler should be handed.
    """

    budget_version: int
    max_live_tasks: int
    max_depth: int
    max_expanded_nodes: int
    max_candidates: int
    max_recursion_fuel: int
    max_nodes: int
    max_edges: int
    max_fan_out: int

    def __post_init__(self) -> None:
        for name in (
            "budget_version",
            "max_live_tasks",
            "max_depth",
            "max_expanded_nodes",
            "max_candidates",
            "max_recursion_fuel",
            "max_nodes",
            "max_edges",
            "max_fan_out",
        ):
            object.__setattr__(
                self, name, index(getattr(self, name), f"graph_budget.{name}", minimum=1)
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "budget_version": self.budget_version,
            "max_live_tasks": self.max_live_tasks,
            "max_depth": self.max_depth,
            "max_expanded_nodes": self.max_expanded_nodes,
            "max_candidates": self.max_candidates,
            "max_recursion_fuel": self.max_recursion_fuel,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "max_fan_out": self.max_fan_out,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "graph_structure_budget") -> GraphStructureBudget:
        data = fields_of(
            value,
            name,
            required=(
                "budget_version",
                "max_live_tasks",
                "max_depth",
                "max_expanded_nodes",
                "max_candidates",
                "max_recursion_fuel",
                "max_nodes",
                "max_edges",
                "max_fan_out",
            ),
        )
        return cls(
            budget_version=data["budget_version"],
            max_live_tasks=data["max_live_tasks"],
            max_depth=data["max_depth"],
            max_expanded_nodes=data["max_expanded_nodes"],
            max_candidates=data["max_candidates"],
            max_recursion_fuel=data["max_recursion_fuel"],
            max_nodes=data["max_nodes"],
            max_edges=data["max_edges"],
            max_fan_out=data["max_fan_out"],
        )


@dataclass(frozen=True, slots=True)
class MethodRef:
    """§6.4: the immutable identity of a method definition."""

    method_id: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_id", identifier(self.method_id, "method_ref.method_id"))
        object.__setattr__(self, "version", index(self.version, "method_ref.version", minimum=1))
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "method_ref.content_hash")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "method_id": self.method_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }

    def as_versioned_ref(self) -> VersionedRef:
        return VersionedRef(id=self.method_id, version=self.version, content_hash=self.content_hash)

    @classmethod
    def from_json(cls, value: object, name: str = "method_ref") -> MethodRef:
        data = fields_of(value, name, required=("method_id", "version", "content_hash"))
        return cls(
            method_id=data["method_id"],
            version=data["version"],
            content_hash=data["content_hash"],
        )


@dataclass(frozen=True, slots=True)
class MethodStep:
    local_id: str
    task_type_ref: VersionedRef
    form: TaskForm
    arguments: Mapping[str, Any]
    required_capabilities: tuple[str, ...]
    obligation_relation: ObligationRelation

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_id", identifier(self.local_id, "step.local_id"))
        if not isinstance(self.task_type_ref, VersionedRef):
            raise ContractError("step.task_type_ref must be a VersionedRef")
        object.__setattr__(self, "form", enum_of(TaskForm, self.form, "step.form"))
        object.__setattr__(
            self,
            "required_capabilities",
            identifiers(self.required_capabilities, "step.required_capabilities"),
        )
        object.__setattr__(
            self,
            "obligation_relation",
            enum_of(ObligationRelation, self.obligation_relation, "step.obligation_relation"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "local_id": self.local_id,
            "task_type_ref": self.task_type_ref.to_json(),
            "form": str(self.form),
            "arguments": arguments_to_json(self.arguments),
            "required_capabilities": list(self.required_capabilities),
            "obligation_relation": str(self.obligation_relation),
        }

    @classmethod
    def from_json(cls, value: object, name: str, budget: StructureBudget) -> MethodStep:
        data = fields_of(
            value,
            name,
            required=(
                "local_id",
                "task_type_ref",
                "form",
                "arguments",
                "required_capabilities",
                "obligation_relation",
            ),
        )
        return cls(
            local_id=data["local_id"],
            task_type_ref=VersionedRef.from_json(data["task_type_ref"], f"{name}.task_type_ref"),
            form=data["form"],
            arguments=parse_arguments(data["arguments"], f"{name}.arguments", budget),
            required_capabilities=tuple(data["required_capabilities"]),
            obligation_relation=data["obligation_relation"],
        )


@dataclass(frozen=True, slots=True)
class MethodOrdering:
    before: str
    after: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "before", identifier(self.before, "ordering.before"))
        object.__setattr__(self, "after", identifier(self.after, "ordering.after"))
        if self.before == self.after:
            raise ContractError("ordering.before and ordering.after must differ")

    def to_json(self) -> dict[str, Any]:
        return {"before": self.before, "after": self.after}

    @classmethod
    def from_json(cls, value: object, name: str) -> MethodOrdering:
        data = fields_of(value, name, required=("before", "after"))
        return cls(before=data["before"], after=data["after"])


@dataclass(frozen=True, slots=True)
class CriterionLink:
    """§6.3: how a parent criterion is covered by a child step's criterion."""

    parent_criterion_id: str
    child_step: str | None
    child_criterion_id: str | None
    evidence_requirement: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parent_criterion_id",
            identifier(self.parent_criterion_id, "criterion_link.parent_criterion_id"),
        )
        object.__setattr__(
            self, "child_step", optional_identifier(self.child_step, "criterion_link.child_step")
        )
        object.__setattr__(
            self,
            "child_criterion_id",
            optional_identifier(self.child_criterion_id, "criterion_link.child_criterion_id"),
        )
        object.__setattr__(
            self,
            "evidence_requirement",
            text(self.evidence_requirement, "criterion_link.evidence_requirement", limit=MAX_TEXT),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "parent_criterion_id": self.parent_criterion_id,
            "child_step": self.child_step,
            "child_criterion_id": self.child_criterion_id,
            "evidence_requirement": self.evidence_requirement,
        }

    @classmethod
    def from_json(cls, value: object, name: str) -> CriterionLink:
        data = fields_of(
            value,
            name,
            required=(
                "parent_criterion_id",
                "child_step",
                "child_criterion_id",
                "evidence_requirement",
            ),
        )
        return cls(
            parent_criterion_id=data["parent_criterion_id"],
            child_step=data["child_step"],
            child_criterion_id=data["child_criterion_id"],
            evidence_requirement=data["evidence_requirement"],
        )


@dataclass(frozen=True, slots=True)
class MethodComposition:
    criterion_links: tuple[CriterionLink, ...]
    outputs: Mapping[str, Any]
    finalizer_step: str | None
    independent_review_required: bool = True

    def __post_init__(self) -> None:
        if not self.criterion_links:
            raise ContractError("composition.criterion_links must not be empty")
        object.__setattr__(
            self,
            "finalizer_step",
            optional_identifier(self.finalizer_step, "composition.finalizer_step"),
        )
        if self.independent_review_required is not True:
            raise ContractError(
                "composition.independent_review_required must be true "
                "(ADR-04: open content is accepted only after an independent review)"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion_links": [link.to_json() for link in self.criterion_links],
            "outputs": arguments_to_json(self.outputs),
            "finalizer_step": self.finalizer_step,
            "independent_review_required": True,
        }

    @classmethod
    def from_json(cls, value: object, name: str, budget: StructureBudget) -> MethodComposition:
        data = fields_of(
            value,
            name,
            required=(
                "criterion_links",
                "outputs",
                "finalizer_step",
                "independent_review_required",
            ),
        )
        return cls(
            criterion_links=sequence_of(
                data["criterion_links"],
                f"{name}.criterion_links",
                lambda item, where: CriterionLink.from_json(item, where),
                minimum=1,
            ),
            outputs=parse_arguments(data["outputs"], f"{name}.outputs", budget),
            finalizer_step=data["finalizer_step"],
            independent_review_required=data["independent_review_required"],
        )


@dataclass(frozen=True, slots=True)
class MethodContract:
    """§6.3 / ``method-contract-v1``: immutable, versioned decomposition knowledge.

    The registry status is deliberately *not* a field: a method definition cannot
    carry its own admission.  Use :func:`admit_method`, which refuses to let a
    model-authored submission claim anything past DRAFT (§6.3, §7.3).
    """

    method_id: str
    method_version: int
    goal_type_ref: VersionedRef
    parameter_schema_ref: VersionedRef
    output_schema_ref: VersionedRef
    applicable_when: tuple[Any, ...]
    exploration_assumptions: tuple[Any, ...]
    steps: tuple[MethodStep, ...]
    ordering: tuple[MethodOrdering, ...]
    required_capabilities: tuple[str, ...]
    expected_effects: tuple[Any, ...]
    composition: MethodComposition
    basis_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_id", identifier(self.method_id, "method.method_id"))
        object.__setattr__(
            self, "method_version", index(self.method_version, "method.method_version", minimum=1)
        )
        for name in ("goal_type_ref", "parameter_schema_ref", "output_schema_ref"):
            if not isinstance(getattr(self, name), VersionedRef):
                raise ContractError(f"method.{name} must be a VersionedRef")
        if not isinstance(self.composition, MethodComposition):
            raise ContractError("method.composition must be a MethodComposition")
        object.__setattr__(
            self,
            "required_capabilities",
            identifiers(self.required_capabilities, "method.required_capabilities"),
        )
        locals_seen = [step.local_id for step in self.steps]
        if len(set(locals_seen)) != len(locals_seen):
            raise ContractError("method.steps must not repeat a local_id")
        for order in self.ordering:
            for endpoint in (order.before, order.after):
                if endpoint not in locals_seen:
                    raise ContractError(f"method.ordering refers to unknown step {endpoint!r}")
        if self.composition.finalizer_step is not None:
            if self.composition.finalizer_step not in locals_seen:
                raise ContractError("method.composition.finalizer_step is not a declared step")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": METHOD_CONTRACT_SCHEMA_VERSION,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "goal_type_ref": self.goal_type_ref.to_json(),
            "parameter_schema_ref": self.parameter_schema_ref.to_json(),
            "output_schema_ref": self.output_schema_ref.to_json(),
            "applicable_when": [item.to_json() for item in self.applicable_when],
            "exploration_assumptions": [item.to_json() for item in self.exploration_assumptions],
            "steps": [step.to_json() for step in self.steps],
            "ordering": [order.to_json() for order in self.ordering],
            "required_capabilities": list(self.required_capabilities),
            "expected_effects": [item.to_json() for item in self.expected_effects],
            "composition": self.composition.to_json(),
            "basis_refs": [ref.to_json() for ref in self.basis_refs],
        }

    def method_ref(self) -> MethodRef:
        return MethodRef(
            method_id=self.method_id,
            version=self.method_version,
            content_hash=content_hash_of(self.to_json()),
        )

    @classmethod
    def from_json(cls, value: object, name: str = "method_contract") -> MethodContract:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "method_id",
                "method_version",
                "goal_type_ref",
                "parameter_schema_ref",
                "output_schema_ref",
                "applicable_when",
                "exploration_assumptions",
                "steps",
                "ordering",
                "required_capabilities",
                "expected_effects",
                "composition",
                "basis_refs",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=METHOD_CONTRACT_SCHEMA_VERSION,
        )
        budget = StructureBudget(MAX_CONDITION_NODES)
        return cls(
            method_id=data["method_id"],
            method_version=data["method_version"],
            goal_type_ref=VersionedRef.from_json(data["goal_type_ref"], f"{name}.goal_type_ref"),
            parameter_schema_ref=VersionedRef.from_json(
                data["parameter_schema_ref"], f"{name}.parameter_schema_ref"
            ),
            output_schema_ref=VersionedRef.from_json(
                data["output_schema_ref"], f"{name}.output_schema_ref"
            ),
            applicable_when=parse_conditions(
                data["applicable_when"], f"{name}.applicable_when", budget
            ),
            exploration_assumptions=parse_conditions(
                data["exploration_assumptions"], f"{name}.exploration_assumptions", budget
            ),
            steps=sequence_of(
                data["steps"],
                f"{name}.steps",
                lambda item, where: MethodStep.from_json(item, where, budget),
            ),
            ordering=sequence_of(
                data["ordering"],
                f"{name}.ordering",
                lambda item, where: MethodOrdering.from_json(item, where),
            ),
            required_capabilities=tuple(data["required_capabilities"]),
            expected_effects=parse_conditions(
                data["expected_effects"], f"{name}.expected_effects", budget
            ),
            composition=MethodComposition.from_json(
                data["composition"], f"{name}.composition", budget
            ),
            basis_refs=sequence_of(
                data["basis_refs"],
                f"{name}.basis_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
        )


@dataclass(frozen=True, slots=True)
class MethodRegistration:
    """The registry's own record; the definition never carries its own admission."""

    method_ref: MethodRef
    status: MethodRegistryStatus
    author: RegistryAuthor
    admission_receipt_ref: TypedRef | None = None
    trial_scope_mission: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method_ref, MethodRef):
            raise ContractError("registration.method_ref must be a MethodRef")
        object.__setattr__(
            self, "status", enum_of(MethodRegistryStatus, self.status, "registration.status")
        )
        object.__setattr__(
            self, "author", enum_of(RegistryAuthor, self.author, "registration.author")
        )
        object.__setattr__(
            self,
            "trial_scope_mission",
            optional_identifier(self.trial_scope_mission, "registration.trial_scope_mission"),
        )
        if self.author is RegistryAuthor.MODEL and self.status not in MODEL_SUBMITTABLE_STATUS:
            raise ContractError(
                "a model-authored method may only be submitted as DRAFT; "
                "registry_status past DRAFT is written by the registry service (§7.3)"
            )
        if self.status is MethodRegistryStatus.TRIAL_ADMITTED and self.trial_scope_mission is None:
            raise ContractError("TRIAL_ADMITTED is scoped to one mission (§7.3 v1.2)")

    def to_json(self) -> dict[str, Any]:
        return {
            "method_ref": self.method_ref.to_json(),
            "status": str(self.status),
            "author": str(self.author),
            "admission_receipt_ref": (
                None if self.admission_receipt_ref is None else self.admission_receipt_ref.to_json()
            ),
            "trial_scope_mission": self.trial_scope_mission,
        }


def admit_method(
    ref: MethodRef,
    status: MethodRegistryStatus,
    *,
    author: RegistryAuthor,
    admission_receipt_ref: TypedRef | None = None,
    trial_scope_mission: str | None = None,
) -> MethodRegistration:
    """§7.3: only the registry service promotes a method past DRAFT."""

    return MethodRegistration(
        method_ref=ref,
        status=status,
        author=author,
        admission_receipt_ref=admission_receipt_ref,
        trial_scope_mission=trial_scope_mission,
    )


# --------------------------------------------------------------------------------------
# Goals, ports, typed relations
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoalSignature:
    """§6.4: a typed goal, its parameter/output schemas and the criteria it covers."""

    signature_id: str
    version: int
    parameter_schema_ref: VersionedRef
    output_schema_ref: VersionedRef
    statement: str
    coverage_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "signature_id", identifier(self.signature_id, "goal_signature.signature_id")
        )
        object.__setattr__(
            self, "version", index(self.version, "goal_signature.version", minimum=1)
        )
        for name in ("parameter_schema_ref", "output_schema_ref"):
            if not isinstance(getattr(self, name), VersionedRef):
                raise ContractError(f"goal_signature.{name} must be a VersionedRef")
        object.__setattr__(self, "statement", text(self.statement, "goal_signature.statement"))
        object.__setattr__(
            self,
            "coverage_criteria",
            identifiers(self.coverage_criteria, "goal_signature.coverage_criteria"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "signature_id": self.signature_id,
            "version": self.version,
            "parameter_schema_ref": self.parameter_schema_ref.to_json(),
            "output_schema_ref": self.output_schema_ref.to_json(),
            "statement": self.statement,
            "coverage_criteria": list(self.coverage_criteria),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "goal_signature") -> GoalSignature:
        data = fields_of(
            value,
            name,
            required=(
                "signature_id",
                "version",
                "parameter_schema_ref",
                "output_schema_ref",
                "statement",
            ),
            optional=("coverage_criteria",),
        )
        return cls(
            signature_id=data["signature_id"],
            version=data["version"],
            parameter_schema_ref=VersionedRef.from_json(
                data["parameter_schema_ref"], f"{name}.parameter_schema_ref"
            ),
            output_schema_ref=VersionedRef.from_json(
                data["output_schema_ref"], f"{name}.output_schema_ref"
            ),
            statement=data["statement"],
            coverage_criteria=tuple(data.get("coverage_criteria", ())),
        )


@dataclass(frozen=True, slots=True)
class PortSpec:
    """TG §4.3: an explicit data port, not "whatever the ancestors wrote"."""

    port_key: str
    schema_ref: VersionedRef
    cardinality: PortCardinality = PortCardinality.SINGLE
    required: bool = True
    ordering: PortOrdering | None = None
    order_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "port_key", identifier(self.port_key, "port.port_key"))
        if not isinstance(self.schema_ref, VersionedRef):
            raise ContractError("port.schema_ref must be a VersionedRef")
        object.__setattr__(
            self, "cardinality", enum_of(PortCardinality, self.cardinality, "port.cardinality")
        )
        object.__setattr__(self, "required", flag(self.required, "port.required"))
        if self.ordering is not None:
            object.__setattr__(
                self, "ordering", enum_of(PortOrdering, self.ordering, "port.ordering")
            )
        object.__setattr__(self, "order_key", optional_identifier(self.order_key, "port.order_key"))
        if self.cardinality is PortCardinality.SINGLE and self.ordering is not None:
            raise ContractError(
                "a single-valued port has exactly one binding and therefore no ordering"
            )
        if self.ordering is PortOrdering.BY_KEY and self.order_key is None:
            raise ContractError("port.ordering BY_KEY needs an order_key to sort on")
        if self.ordering is not PortOrdering.BY_KEY and self.order_key is not None:
            raise ContractError("port.order_key is only meaningful for BY_KEY ordering")

    @property
    def set_order_declared(self) -> bool:
        """TG §4.3: a set port needs a declared order; this says whether it has one."""

        return self.cardinality is not PortCardinality.SET or self.ordering is not None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "port_key": self.port_key,
            "schema_ref": self.schema_ref.to_json(),
            "cardinality": str(self.cardinality),
            "required": self.required,
        }
        # Omitted when unset, so a port written before this field existed keeps its
        # bytes and its enclosing binding keeps its content hash.
        if self.ordering is not None:
            payload["ordering"] = str(self.ordering)
        if self.order_key is not None:
            payload["order_key"] = self.order_key
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "port") -> PortSpec:
        data = fields_of(
            value,
            name,
            required=("port_key", "schema_ref"),
            optional=("cardinality", "required", "ordering", "order_key"),
        )
        return cls(
            port_key=data["port_key"],
            schema_ref=VersionedRef.from_json(data["schema_ref"], f"{name}.schema_ref"),
            cardinality=data.get("cardinality", PortCardinality.SINGLE),
            required=data.get("required", True),
            ordering=data.get("ordering"),
            order_key=data.get("order_key"),
        )


def undeclared_set_ports(ports: tuple[PortSpec, ...]) -> tuple[str, ...]:
    """The set-valued ports that have not said how their bindings are ordered.

    TG §4.3 requires every set port to define one.  The contract keeps ``ordering``
    optional so a port list can be built before that decision is made; this is the
    check the compiler runs before a network is admitted.
    """

    return tuple(port.port_key for port in ports if not port.set_order_declared)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """§6.6 / TG decision 4: a real object a primitive reads or writes.

    Identity is ``(namespace, normalized object id)`` — never a bare path, because
    the same spelling in two workspaces is two objects and the same object reached
    by two spellings is one.  Two occurrences with no task dependency are not
    thereby parallel-safe: a write/write or read/write overlap here is what the
    scheduler needs in order to serialise them.
    """

    namespace: str
    object_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", identifier(self.namespace, "resource.namespace"))
        object.__setattr__(self, "object_id", identifier(self.object_id, "resource.object_id"))

    @property
    def key(self) -> tuple[str, str]:
        return (self.namespace, self.object_id)

    def to_json(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "object_id": self.object_id}

    @classmethod
    def from_json(cls, value: object, name: str = "resource_ref") -> ResourceRef:
        data = fields_of(value, name, required=("namespace", "object_id"))
        return cls(namespace=data["namespace"], object_id=data["object_id"])


def resource_conflicts(
    left_reads: tuple[ResourceRef, ...],
    left_writes: tuple[ResourceRef, ...],
    right_reads: tuple[ResourceRef, ...],
    right_writes: tuple[ResourceRef, ...],
) -> tuple[ResourceRef, ...]:
    """Resources two occurrences cannot touch concurrently (§6.6).

    Write/write and read/write overlap; read/read does not.  Declaring nothing is
    not a declaration of independence — an occurrence with no declared set is
    simply unanalysed, and the caller should serialise conservatively.
    """

    left_write_keys = {ref.key for ref in left_writes}
    right_write_keys = {ref.key for ref in right_writes}
    clashing = left_write_keys & right_write_keys
    clashing |= left_write_keys & {ref.key for ref in right_reads}
    clashing |= right_write_keys & {ref.key for ref in left_reads}
    by_key = {ref.key: ref for ref in (*left_reads, *left_writes, *right_reads, *right_writes)}
    return tuple(by_key[key] for key in sorted(clashing))


@dataclass(frozen=True, slots=True)
class OrderConstraint:
    """TG decision 1: ``before`` must reach its release condition before ``after`` starts."""

    before: OccurrenceId
    after: OccurrenceId
    release_condition: ReleaseCondition = ReleaseCondition.ACCEPTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "before", occurrence_id(self.before, "order.before"))
        object.__setattr__(self, "after", occurrence_id(self.after, "order.after"))
        if self.before == self.after:
            raise ContractError("order.before and order.after must differ")
        object.__setattr__(
            self,
            "release_condition",
            enum_of(ReleaseCondition, self.release_condition, "order.release_condition"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "before": str(self.before),
            "after": str(self.after),
            "release_condition": str(self.release_condition),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "order_constraint") -> OrderConstraint:
        data = fields_of(value, name, required=("before", "after"), optional=("release_condition",))
        return cls(
            before=OccurrenceId(data["before"]),
            after=OccurrenceId(data["after"]),
            release_condition=data.get("release_condition", ReleaseCondition.ACCEPTED),
        )


@dataclass(frozen=True, slots=True)
class DataRequirement:
    """TG §4.3: a planned port-to-port contract; resolved to a :class:`BoundInput`."""

    requirement_id: str
    producer_occurrence: OccurrenceId
    output_port: str
    consumer_occurrence: OccurrenceId
    input_port: str
    schema_ref: VersionedRef
    assurance_policy_ref: str
    freshness_policy_ref: str
    source_revision_policy: SourceRevisionPolicy = SourceRevisionPolicy.PINNED

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirement_id", identifier(self.requirement_id, "data.requirement_id")
        )
        object.__setattr__(
            self,
            "producer_occurrence",
            occurrence_id(self.producer_occurrence, "data.producer_occurrence"),
        )
        object.__setattr__(
            self,
            "consumer_occurrence",
            occurrence_id(self.consumer_occurrence, "data.consumer_occurrence"),
        )
        if self.producer_occurrence == self.consumer_occurrence:
            raise ContractError("data requirement must connect two different occurrences")
        object.__setattr__(self, "output_port", identifier(self.output_port, "data.output_port"))
        object.__setattr__(self, "input_port", identifier(self.input_port, "data.input_port"))
        if not isinstance(self.schema_ref, VersionedRef):
            raise ContractError("data.schema_ref must be a VersionedRef")
        object.__setattr__(
            self,
            "assurance_policy_ref",
            identifier(self.assurance_policy_ref, "data.assurance_policy_ref"),
        )
        object.__setattr__(
            self,
            "freshness_policy_ref",
            identifier(self.freshness_policy_ref, "data.freshness_policy_ref"),
        )
        object.__setattr__(
            self,
            "source_revision_policy",
            enum_of(
                SourceRevisionPolicy, self.source_revision_policy, "data.source_revision_policy"
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "producer_occurrence": str(self.producer_occurrence),
            "output_port": self.output_port,
            "consumer_occurrence": str(self.consumer_occurrence),
            "input_port": self.input_port,
            "schema_ref": self.schema_ref.to_json(),
            "assurance_policy_ref": self.assurance_policy_ref,
            "freshness_policy_ref": self.freshness_policy_ref,
            "source_revision_policy": str(self.source_revision_policy),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "data_requirement") -> DataRequirement:
        data = fields_of(
            value,
            name,
            required=(
                "requirement_id",
                "producer_occurrence",
                "output_port",
                "consumer_occurrence",
                "input_port",
                "schema_ref",
                "assurance_policy_ref",
                "freshness_policy_ref",
            ),
            optional=("source_revision_policy",),
        )
        return cls(
            requirement_id=data["requirement_id"],
            producer_occurrence=OccurrenceId(data["producer_occurrence"]),
            output_port=data["output_port"],
            consumer_occurrence=OccurrenceId(data["consumer_occurrence"]),
            input_port=data["input_port"],
            schema_ref=VersionedRef.from_json(data["schema_ref"], f"{name}.schema_ref"),
            assurance_policy_ref=data["assurance_policy_ref"],
            freshness_policy_ref=data["freshness_policy_ref"],
            source_revision_policy=data.get("source_revision_policy", SourceRevisionPolicy.PINNED),
        )


@dataclass(frozen=True, slots=True)
class BoundInput:
    """TG §4.3: a requirement resolved to one accepted, hashed artefact revision."""

    requirement_id: str
    producer_result_id: str
    acceptance_id: str
    artifact_id: str
    content_hash: str
    schema_ref: VersionedRef
    source_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirement_id", identifier(self.requirement_id, "bound_input.requirement_id")
        )
        object.__setattr__(
            self,
            "producer_result_id",
            identifier(self.producer_result_id, "bound_input.producer_result_id"),
        )
        object.__setattr__(
            self, "acceptance_id", identifier(self.acceptance_id, "bound_input.acceptance_id")
        )
        object.__setattr__(
            self, "artifact_id", identifier(self.artifact_id, "bound_input.artifact_id")
        )
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "bound_input.content_hash")
        )
        if not isinstance(self.schema_ref, VersionedRef):
            raise ContractError("bound_input.schema_ref must be a VersionedRef")
        object.__setattr__(
            self, "source_revision", identifier(self.source_revision, "bound_input.source_revision")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "producer_result_id": self.producer_result_id,
            "acceptance_id": self.acceptance_id,
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "schema_ref": self.schema_ref.to_json(),
            "source_revision": self.source_revision,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "bound_input") -> BoundInput:
        data = fields_of(
            value,
            name,
            required=(
                "requirement_id",
                "producer_result_id",
                "acceptance_id",
                "artifact_id",
                "content_hash",
                "schema_ref",
                "source_revision",
            ),
        )
        return cls(
            requirement_id=data["requirement_id"],
            producer_result_id=data["producer_result_id"],
            acceptance_id=data["acceptance_id"],
            artifact_id=data["artifact_id"],
            content_hash=data["content_hash"],
            schema_ref=VersionedRef.from_json(data["schema_ref"], f"{name}.schema_ref"),
            source_revision=data["source_revision"],
        )


@dataclass(frozen=True, slots=True)
class PreconditionRef:
    """A precondition of a task binding, with the phase at which it is checked."""

    condition_digest: str
    phase: PreconditionPhase | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "condition_digest",
            hash_hex(self.condition_digest, "precondition.condition_digest"),
        )
        if self.phase is not None:
            object.__setattr__(self, "phase", parse_phase(self.phase, "precondition.phase"))

    def to_json(self) -> dict[str, Any]:
        return {
            "condition_digest": self.condition_digest,
            "phase": None if self.phase is None else str(self.phase),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "precondition_ref") -> PreconditionRef:
        data = fields_of(value, name, required=("condition_digest",), optional=("phase",))
        return cls(condition_digest=data["condition_digest"], phase=data.get("phase"))


# --------------------------------------------------------------------------------------
# Bindings and method instances (§6.4)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Binding:
    """One grounded method parameter."""

    name: str
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "binding.name"))
        reject_executable(self.value, "binding.value")
        object.__setattr__(self, "value", json_value(self.value, "binding.value"))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_json(cls, value: object, name: str = "binding") -> Binding:
        data = fields_of(value, name, required=("name", "value"))
        return cls(name=data["name"], value=data["value"])


@dataclass(frozen=True, slots=True)
class ChildBinding:
    """§6.4: one slot of a method instance bound to a concrete occurrence and duty."""

    instance_id: MethodInstanceId
    slot_key: str
    occurrence_id: OccurrenceId
    obligation_id: ObligationId
    requiredness: Requiredness = Requiredness.REQUIRED
    reuse_policy: ReusePolicy = ReusePolicy.NEW_WORK
    #: TG §12: when this slot consumes a *shared* compound goal, the occurrence of
    #: that goal it binds to.  Defaults to this slot's own occurrence, which is the
    #: unshared case; naming another lets two adopting slots point at one shared
    #: goal without either of them owning it.
    goal_occurrence_id: OccurrenceId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instance_id", method_instance_id(self.instance_id, "child_binding.instance_id")
        )
        object.__setattr__(self, "slot_key", identifier(self.slot_key, "child_binding.slot_key"))
        object.__setattr__(
            self,
            "occurrence_id",
            occurrence_id(self.occurrence_id, "child_binding.occurrence_id"),
        )
        object.__setattr__(
            self,
            "obligation_id",
            obligation_id(self.obligation_id, "child_binding.obligation_id"),
        )
        object.__setattr__(
            self,
            "requiredness",
            enum_of(Requiredness, self.requiredness, "child_binding.requiredness"),
        )
        object.__setattr__(
            self,
            "reuse_policy",
            enum_of(ReusePolicy, self.reuse_policy, "child_binding.reuse_policy"),
        )
        if self.goal_occurrence_id is not None:
            object.__setattr__(
                self,
                "goal_occurrence_id",
                occurrence_id(self.goal_occurrence_id, "child_binding.goal_occurrence_id"),
            )
        if (
            self.reuse_policy is ReusePolicy.NEW_WORK
            and self.goal_occurrence_id is not None
            and self.goal_occurrence_id != self.occurrence_id
        ):
            raise ContractError(
                "a slot that binds another goal occurrence is reusing or sharing it; "
                "NEW_WORK means this slot creates the work itself (TG decision 9)"
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": str(self.instance_id),
            "slot_key": self.slot_key,
            "occurrence_id": str(self.occurrence_id),
            "obligation_id": str(self.obligation_id),
            "requiredness": str(self.requiredness),
            "reuse_policy": str(self.reuse_policy),
        }
        if self.goal_occurrence_id is not None:
            payload["goal_occurrence_id"] = str(self.goal_occurrence_id)
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "child_binding") -> ChildBinding:
        data = fields_of(
            value,
            name,
            required=("instance_id", "slot_key", "occurrence_id", "obligation_id"),
            optional=("requiredness", "reuse_policy", "goal_occurrence_id"),
        )
        raw_goal_occurrence = data.get("goal_occurrence_id")
        return cls(
            instance_id=MethodInstanceId(data["instance_id"]),
            slot_key=data["slot_key"],
            occurrence_id=OccurrenceId(data["occurrence_id"]),
            obligation_id=ObligationId(data["obligation_id"]),
            requiredness=data.get("requiredness", Requiredness.REQUIRED),
            reuse_policy=data.get("reuse_policy", ReusePolicy.NEW_WORK),
            goal_occurrence_id=(
                None if raw_goal_occurrence is None else OccurrenceId(raw_goal_occurrence)
            ),
        )


@dataclass(frozen=True, slots=True)
class MethodInstanceDraft:
    """§6.4: one grounding of a method against a compound task.

    ``goal_id`` is the typed ``TaskRef`` of the compound task being refined — TG
    decision 7.  The duty is referenced separately by ``obligation_id``; the two
    are different identities and a draft that conflates them is refused.
    """

    instance_id: MethodInstanceId
    goal_id: TaskRef
    obligation_id: ObligationId
    method_ref: MethodRef
    grounded_parameters: tuple[Binding, ...] = ()
    world_snapshot_id: str | None = None
    precondition_witnesses: tuple[PreconditionWitnessRecord, ...] = ()
    assumption_refs: tuple[EvidenceRef, ...] = ()
    child_bindings: tuple[ChildBinding, ...] = ()
    plan_revision: PlanRevision = PlanRevision(0)
    #: TG §12: which *occurrence* of ``goal_id`` this instance refines.  A shared
    #: compound goal can appear at several occurrences, each adopted by a different
    #: slot; without this the refinements of two consumers would be indistinguishable.
    #: Defaults to the occurrence derived from ``goal_id`` — the unshared case.
    goal_occurrence_id: OccurrenceId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instance_id", method_instance_id(self.instance_id, "draft.instance_id")
        )
        object.__setattr__(self, "goal_id", task_ref(self.goal_id, "draft.goal_id"))
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "draft.obligation_id")
        )
        if not isinstance(self.method_ref, MethodRef):
            raise ContractError("draft.method_ref must be a MethodRef")
        names = [binding.name for binding in self.grounded_parameters]
        if len(set(names)) != len(names):
            raise ContractError("draft.grounded_parameters must not repeat a parameter name")
        slots = [binding.slot_key for binding in self.child_bindings]
        if len(set(slots)) != len(slots):
            raise ContractError("draft.child_bindings must not repeat a slot_key")
        for binding in self.child_bindings:
            if binding.instance_id != self.instance_id:
                raise ContractError("draft.child_bindings must belong to this method instance")
        object.__setattr__(
            self,
            "world_snapshot_id",
            optional_identifier(self.world_snapshot_id, "draft.world_snapshot_id"),
        )
        object.__setattr__(
            self, "plan_revision", plan_revision(self.plan_revision, "draft.plan_revision")
        )
        if self.goal_occurrence_id is not None:
            object.__setattr__(
                self,
                "goal_occurrence_id",
                occurrence_id(self.goal_occurrence_id, "draft.goal_occurrence_id"),
            )

    @property
    def effective_goal_occurrence_id(self) -> OccurrenceId:
        """The occurrence this instance refines, derived from ``goal_id`` when unset."""

        if self.goal_occurrence_id is not None:
            return self.goal_occurrence_id
        return OccurrenceId(str(self.goal_id))

    def parameters_digest(self) -> str:
        """The identity of this grounding's parameters, for repeat-expansion checks."""

        return content_hash_of([binding.to_json() for binding in self.grounded_parameters])

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": str(self.instance_id),
            "goal_id": {
                "kind": str(TypedRefKind.TASK),
                "id": str(self.goal_id),
                "revision": 0,
                "content_hash": content_hash_of(str(self.goal_id)),
            },
            "obligation_id": str(self.obligation_id),
            "method_ref": self.method_ref.to_json(),
            "grounded_parameters": [binding.to_json() for binding in self.grounded_parameters],
            "world_snapshot_id": self.world_snapshot_id,
            "precondition_witnesses": [
                witness.to_json() for witness in self.precondition_witnesses
            ],
            "assumption_refs": [ref.to_json() for ref in self.assumption_refs],
            "child_bindings": [binding.to_json() for binding in self.child_bindings],
            "plan_revision": int(self.plan_revision),
        }
        if self.goal_occurrence_id is not None:
            payload["goal_occurrence_id"] = str(self.goal_occurrence_id)
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "method_instance_draft") -> MethodInstanceDraft:
        data = fields_of(
            value,
            name,
            required=("instance_id", "goal_id", "obligation_id", "method_ref"),
            optional=(
                "grounded_parameters",
                "world_snapshot_id",
                "precondition_witnesses",
                "assumption_refs",
                "child_bindings",
                "plan_revision",
                "goal_occurrence_id",
            ),
        )
        return cls(
            instance_id=MethodInstanceId(data["instance_id"]),
            goal_id=task_ref_from_typed(data["goal_id"], f"{name}.goal_id"),
            obligation_id=ObligationId(data["obligation_id"]),
            method_ref=MethodRef.from_json(data["method_ref"], f"{name}.method_ref"),
            grounded_parameters=sequence_of(
                data.get("grounded_parameters", ()),
                f"{name}.grounded_parameters",
                lambda item, where: Binding.from_json(item, where),
            ),
            world_snapshot_id=data.get("world_snapshot_id"),
            precondition_witnesses=sequence_of(
                data.get("precondition_witnesses", ()),
                f"{name}.precondition_witnesses",
                lambda item, where: PreconditionWitnessRecord.from_json(item, where),
            ),
            assumption_refs=sequence_of(
                data.get("assumption_refs", ()),
                f"{name}.assumption_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
            child_bindings=sequence_of(
                data.get("child_bindings", ()),
                f"{name}.child_bindings",
                lambda item, where: ChildBinding.from_json(item, where),
            ),
            plan_revision=PlanRevision(data.get("plan_revision", 0)),
            goal_occurrence_id=(
                None
                if data.get("goal_occurrence_id") is None
                else OccurrenceId(data["goal_occurrence_id"])
            ),
        )


# --------------------------------------------------------------------------------------
# Task semantic binding (TG §3.2 / §18.5)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MethodOccurrenceBinding:
    """Where this task sits inside the method instance that produced it."""

    method_instance_id: MethodInstanceId
    occurrence_id: OccurrenceId
    slot_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method_instance_id",
            method_instance_id(self.method_instance_id, "binding.method_instance_id"),
        )
        object.__setattr__(
            self, "occurrence_id", occurrence_id(self.occurrence_id, "binding.occurrence_id")
        )
        object.__setattr__(self, "slot_key", identifier(self.slot_key, "binding.slot_key"))

    def to_json(self) -> dict[str, Any]:
        return {
            "method_instance_id": str(self.method_instance_id),
            "occurrence_id": str(self.occurrence_id),
            "slot_key": self.slot_key,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "occurrence_binding") -> MethodOccurrenceBinding:
        data = fields_of(value, name, required=("method_instance_id", "occurrence_id", "slot_key"))
        return cls(
            method_instance_id=MethodInstanceId(data["method_instance_id"]),
            occurrence_id=OccurrenceId(data["occurrence_id"]),
            slot_key=data["slot_key"],
        )


@dataclass(frozen=True, slots=True)
class TaskSemanticBindingV1:
    """TG §3.2: the versioned semantic record that sits beside an existing Task.

    The legacy Task keeps the work facts (status, attempts, artefacts); this record
    carries the meaning.  ``form`` is the compound/primitive axis and never reuses
    ``kind``; ``operator_ref`` exists only for a primitive, because a compound task
    has no operator to dispatch.
    """

    task_id: TaskRef
    obligation_id: ObligationId
    contract_revision: ContractRevision
    contract_hash: str
    form: TaskForm
    goal_signature: GoalSignature
    typed_parameters: Mapping[str, Any] = field(default_factory=dict)
    requirement_refs: tuple[str, ...] = ()
    input_ports: tuple[PortSpec, ...] = ()
    output_ports: tuple[PortSpec, ...] = ()
    operator_ref: VersionedRef | None = None
    semantic_scope: str = "mission"
    capability_requirements: tuple[str, ...] = ()
    precondition_refs: tuple[PreconditionRef, ...] = ()
    applicability_check_policy: ApplicabilityCheckPolicy = (
        ApplicabilityCheckPolicy.SELECT_AND_ACCEPT
    )
    occurrence_binding: MethodOccurrenceBinding | None = None
    adopted_method_instance_id: MethodInstanceId | None = None
    input_binding_revision: InputBindingRevision = InputBindingRevision(0)
    dispatch_generation: DispatchGeneration = DispatchGeneration(0)
    #: §6.6: what this primitive's operator reads and writes, and what it does to
    #: the outside world.  A compound task declares neither — it is refined, not
    #: dispatched, so it has no operator whose effects these could describe.
    resource_reads: tuple[ResourceRef, ...] = ()
    resource_writes: tuple[ResourceRef, ...] = ()
    side_effect_kind: SideEffectKind | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", task_ref(self.task_id, "binding.task_id"))
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "binding.obligation_id")
        )
        object.__setattr__(
            self,
            "contract_revision",
            contract_revision(self.contract_revision, "binding.contract_revision"),
        )
        object.__setattr__(
            self, "contract_hash", hash_hex(self.contract_hash, "binding.contract_hash")
        )
        object.__setattr__(self, "form", enum_of(TaskForm, self.form, "binding.form"))
        if not isinstance(self.goal_signature, GoalSignature):
            raise ContractError("binding.goal_signature must be a GoalSignature")
        object.__setattr__(
            self,
            "typed_parameters",
            json_object(self.typed_parameters, "binding.typed_parameters"),
        )
        object.__setattr__(
            self, "requirement_refs", identifiers(self.requirement_refs, "binding.requirement_refs")
        )
        for label, ports in (
            ("input_ports", self.input_ports),
            ("output_ports", self.output_ports),
        ):
            keys = [port.port_key for port in ports]
            if len(set(keys)) != len(keys):
                raise ContractError(f"binding.{label} must not repeat a port_key")
        if self.form is TaskForm.PRIMITIVE and self.operator_ref is None:
            raise ContractError("a primitive task binding needs an operator_ref (§6.2)")
        if self.form is TaskForm.COMPOUND and self.operator_ref is not None:
            raise ContractError(
                "a compound task binding must not carry an operator_ref; "
                "it is refined by a MethodInstance, not dispatched (§6.2)"
            )
        object.__setattr__(
            self, "semantic_scope", identifier(self.semantic_scope, "binding.semantic_scope")
        )
        object.__setattr__(
            self,
            "capability_requirements",
            identifiers(self.capability_requirements, "binding.capability_requirements"),
        )
        object.__setattr__(
            self,
            "applicability_check_policy",
            enum_of(
                ApplicabilityCheckPolicy,
                self.applicability_check_policy,
                "binding.applicability_check_policy",
            ),
        )
        if self.adopted_method_instance_id is not None:
            object.__setattr__(
                self,
                "adopted_method_instance_id",
                method_instance_id(
                    self.adopted_method_instance_id, "binding.adopted_method_instance_id"
                ),
            )
            if self.form is TaskForm.PRIMITIVE:
                raise ContractError("a primitive task is not refined by a method instance")
        object.__setattr__(
            self,
            "input_binding_revision",
            input_binding_revision(self.input_binding_revision, "binding.input_binding_revision"),
        )
        object.__setattr__(
            self,
            "dispatch_generation",
            dispatch_generation(self.dispatch_generation, "binding.dispatch_generation"),
        )
        for label in ("resource_reads", "resource_writes"):
            declared = sequence_of(
                getattr(self, label),
                f"binding.{label}",
                lambda item, where: (
                    item if isinstance(item, ResourceRef) else ResourceRef.from_json(item, where)
                ),
            )
            resource_keys = {ref.key for ref in declared}
            if len(resource_keys) != len(declared):
                raise ContractError(f"binding.{label} must not repeat a resource")
            object.__setattr__(self, label, declared)
        if self.side_effect_kind is not None:
            object.__setattr__(
                self,
                "side_effect_kind",
                enum_of(SideEffectKind, self.side_effect_kind, "binding.side_effect_kind"),
            )
        if self.form is TaskForm.COMPOUND and (
            self.resource_reads or self.resource_writes or self.side_effect_kind is not None
        ):
            raise ContractError(
                "a compound task binding declares no resources or side effects; "
                "those belong to the primitive operators it is refined into (§6.2)"
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": str(self.task_id),
            "obligation_id": str(self.obligation_id),
            "contract_revision": int(self.contract_revision),
            "contract_hash": self.contract_hash,
            "form": str(self.form),
            "goal_signature": self.goal_signature.to_json(),
            "typed_parameters": dict(self.typed_parameters),
            "requirement_refs": list(self.requirement_refs),
            "input_ports": [port.to_json() for port in self.input_ports],
            "output_ports": [port.to_json() for port in self.output_ports],
            "operator_ref": None if self.operator_ref is None else self.operator_ref.to_json(),
            "semantic_scope": self.semantic_scope,
            "capability_requirements": list(self.capability_requirements),
            "precondition_refs": [ref.to_json() for ref in self.precondition_refs],
            "applicability_check_policy": str(self.applicability_check_policy),
            "occurrence_binding": (
                None if self.occurrence_binding is None else self.occurrence_binding.to_json()
            ),
            "adopted_method_instance_id": (
                None
                if self.adopted_method_instance_id is None
                else str(self.adopted_method_instance_id)
            ),
            "input_binding_revision": int(self.input_binding_revision),
            "dispatch_generation": int(self.dispatch_generation),
        }
        # Omitted when unused so a binding written before these fields existed keeps
        # its bytes and therefore its content hash.
        if self.resource_reads:
            payload["resource_reads"] = [ref.to_json() for ref in self.resource_reads]
        if self.resource_writes:
            payload["resource_writes"] = [ref.to_json() for ref in self.resource_writes]
        if self.side_effect_kind is not None:
            payload["side_effect_kind"] = str(self.side_effect_kind)
        return payload

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    @classmethod
    def from_json(cls, value: object, name: str = "task_semantic_binding") -> TaskSemanticBindingV1:
        data = fields_of(
            value,
            name,
            required=(
                "task_id",
                "obligation_id",
                "contract_revision",
                "contract_hash",
                "form",
                "goal_signature",
            ),
            optional=(
                "typed_parameters",
                "requirement_refs",
                "input_ports",
                "output_ports",
                "operator_ref",
                "semantic_scope",
                "capability_requirements",
                "precondition_refs",
                "applicability_check_policy",
                "occurrence_binding",
                "adopted_method_instance_id",
                "input_binding_revision",
                "dispatch_generation",
                "resource_reads",
                "resource_writes",
                "side_effect_kind",
            ),
        )
        raw_operator = data.get("operator_ref")
        raw_occurrence = data.get("occurrence_binding")
        return cls(
            task_id=TaskRef(data["task_id"]),
            obligation_id=ObligationId(data["obligation_id"]),
            contract_revision=ContractRevision(data["contract_revision"]),
            contract_hash=data["contract_hash"],
            form=data["form"],
            goal_signature=GoalSignature.from_json(
                data["goal_signature"], f"{name}.goal_signature"
            ),
            typed_parameters=data.get("typed_parameters", {}),
            requirement_refs=tuple(data.get("requirement_refs", ())),
            input_ports=sequence_of(
                data.get("input_ports", ()),
                f"{name}.input_ports",
                lambda item, where: PortSpec.from_json(item, where),
            ),
            output_ports=sequence_of(
                data.get("output_ports", ()),
                f"{name}.output_ports",
                lambda item, where: PortSpec.from_json(item, where),
            ),
            operator_ref=(
                None
                if raw_operator is None
                else VersionedRef.from_json(raw_operator, f"{name}.operator_ref")
            ),
            semantic_scope=data.get("semantic_scope", "mission"),
            capability_requirements=tuple(data.get("capability_requirements", ())),
            precondition_refs=sequence_of(
                data.get("precondition_refs", ()),
                f"{name}.precondition_refs",
                lambda item, where: PreconditionRef.from_json(item, where),
            ),
            applicability_check_policy=data.get(
                "applicability_check_policy", ApplicabilityCheckPolicy.SELECT_AND_ACCEPT
            ),
            occurrence_binding=(
                None
                if raw_occurrence is None
                else MethodOccurrenceBinding.from_json(raw_occurrence, f"{name}.occurrence_binding")
            ),
            adopted_method_instance_id=(
                None
                if data.get("adopted_method_instance_id") is None
                else MethodInstanceId(data["adopted_method_instance_id"])
            ),
            input_binding_revision=InputBindingRevision(data.get("input_binding_revision", 0)),
            dispatch_generation=DispatchGeneration(data.get("dispatch_generation", 0)),
            resource_reads=sequence_of(
                data.get("resource_reads", ()),
                f"{name}.resource_reads",
                lambda item, where: ResourceRef.from_json(item, where),
            ),
            resource_writes=sequence_of(
                data.get("resource_writes", ()),
                f"{name}.resource_writes",
                lambda item, where: ResourceRef.from_json(item, where),
            ),
            side_effect_kind=data.get("side_effect_kind"),
        )


# --------------------------------------------------------------------------------------
# Read sets (ADR-13, TG §11.2)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadItem:
    """One wire read-set entry of ``plan-revision-proposal-v1``."""

    kind: ReadItemKind
    id: str
    semantic_revision: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", enum_of(ReadItemKind, self.kind, "read_item.kind"))
        object.__setattr__(self, "id", identifier(self.id, "read_item.id"))
        object.__setattr__(
            self, "semantic_revision", index(self.semantic_revision, "read_item.semantic_revision")
        )
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "read_item.content_hash")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "semantic_revision": self.semantic_revision,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "read_item") -> ReadItem:
        data = fields_of(value, name, required=("kind", "id", "semantic_revision", "content_hash"))
        return cls(
            kind=data["kind"],
            id=data["id"],
            semantic_revision=data["semantic_revision"],
            content_hash=data["content_hash"],
        )


@dataclass(frozen=True, slots=True)
class SupportSetRead:
    """ADR-13 C29: the *set* of supports read, with a digest over its members.

    Without the member digest, adding a counter-observation while leaving the
    positive supports untouched would leave the recorded revision unchanged and an
    old review would pass the gate (AER scenario I02).
    """

    support_set_id: str
    revision: int
    member_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "support_set_id", identifier(self.support_set_id, "support_set.support_set_id")
        )
        object.__setattr__(self, "revision", index(self.revision, "support_set.revision"))
        object.__setattr__(
            self, "member_digest", hash_hex(self.member_digest, "support_set.member_digest")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "support_set_id": self.support_set_id,
            "revision": self.revision,
            "member_digest": self.member_digest,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "support_set_read") -> SupportSetRead:
        data = fields_of(value, name, required=("support_set_id", "revision", "member_digest"))
        return cls(
            support_set_id=data["support_set_id"],
            revision=data["revision"],
            member_digest=data["member_digest"],
        )


@dataclass(frozen=True, slots=True)
class ScopeEpochRead:
    scope_id: str
    validity_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", identifier(self.scope_id, "scope_epoch.scope_id"))
        object.__setattr__(
            self, "validity_epoch", index(self.validity_epoch, "scope_epoch.validity_epoch")
        )

    def to_json(self) -> dict[str, Any]:
        return {"scope_id": self.scope_id, "validity_epoch": self.validity_epoch}

    @classmethod
    def from_json(cls, value: object, name: str = "scope_epoch_read") -> ScopeEpochRead:
        data = fields_of(value, name, required=("scope_id", "validity_epoch"))
        return cls(scope_id=data["scope_id"], validity_epoch=data["validity_epoch"])


@dataclass(frozen=True, slots=True)
class AbsenceRead:
    """TG §11.2: "there is no such object / edge / writer" is also a read fact."""

    predicate: str
    scope_id: str
    range_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "predicate", identifier(self.predicate, "absence.predicate"))
        object.__setattr__(self, "scope_id", identifier(self.scope_id, "absence.scope_id"))
        object.__setattr__(
            self, "range_revision", index(self.range_revision, "absence.range_revision")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate,
            "scope_id": self.scope_id,
            "range_revision": self.range_revision,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "absence_read") -> AbsenceRead:
        data = fields_of(value, name, required=("predicate", "scope_id", "range_revision"))
        return cls(
            predicate=data["predicate"],
            scope_id=data["scope_id"],
            range_revision=data["range_revision"],
        )


@dataclass(frozen=True, slots=True)
class SemanticReadSet:
    """ADR-13: the semantic read-set carried beside the integer graph version.

    It never replaces the integer optimistic gate; a commit passes the integer
    gate first and is then checked item by item, with no automatic rebase.
    """

    requirements_revision: int
    goal_revisions: tuple[ReadItem, ...] = ()
    method_revisions: tuple[ReadItem, ...] = ()
    observation_revisions: tuple[ReadItem, ...] = ()
    acceptance_revisions: tuple[ReadItem, ...] = ()
    manager_epoch: int = 0
    budget_grant_revision: int = 0
    support_sets: tuple[SupportSetRead, ...] = ()
    scope_epochs: tuple[ScopeEpochRead, ...] = ()
    absences: tuple[AbsenceRead, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "read_set.requirements_revision"),
        )
        object.__setattr__(
            self, "manager_epoch", index(self.manager_epoch, "read_set.manager_epoch")
        )
        object.__setattr__(
            self,
            "budget_grant_revision",
            index(self.budget_grant_revision, "read_set.budget_grant_revision"),
        )
        for label, expected in (
            ("goal_revisions", ReadItemKind.TASK),
            ("method_revisions", ReadItemKind.METHOD),
            ("observation_revisions", ReadItemKind.FACT),
            ("acceptance_revisions", ReadItemKind.ACCEPTANCE),
        ):
            for item in getattr(self, label):
                if item.kind is not expected:
                    raise ContractError(f"read_set.{label} entries must be of kind {expected!s}")

    def to_json(self) -> dict[str, Any]:
        return {
            "requirements_revision": self.requirements_revision,
            "goal_revisions": [item.to_json() for item in self.goal_revisions],
            "method_revisions": [item.to_json() for item in self.method_revisions],
            "observation_revisions": [item.to_json() for item in self.observation_revisions],
            "acceptance_revisions": [item.to_json() for item in self.acceptance_revisions],
            "manager_epoch": self.manager_epoch,
            "budget_grant_revision": self.budget_grant_revision,
            "support_sets": [item.to_json() for item in self.support_sets],
            "scope_epochs": [item.to_json() for item in self.scope_epochs],
            "absences": [item.to_json() for item in self.absences],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "semantic_read_set") -> SemanticReadSet:
        data = fields_of(
            value,
            name,
            required=("requirements_revision",),
            optional=(
                "goal_revisions",
                "method_revisions",
                "observation_revisions",
                "acceptance_revisions",
                "manager_epoch",
                "budget_grant_revision",
                "support_sets",
                "scope_epochs",
                "absences",
            ),
        )

        def read_items(raw: object, where: str) -> tuple[ReadItem, ...]:
            return sequence_of(raw, where, lambda item, at: ReadItem.from_json(item, at))

        return cls(
            requirements_revision=data["requirements_revision"],
            goal_revisions=read_items(data.get("goal_revisions", ()), f"{name}.goal_revisions"),
            method_revisions=read_items(
                data.get("method_revisions", ()), f"{name}.method_revisions"
            ),
            observation_revisions=read_items(
                data.get("observation_revisions", ()), f"{name}.observation_revisions"
            ),
            acceptance_revisions=read_items(
                data.get("acceptance_revisions", ()), f"{name}.acceptance_revisions"
            ),
            manager_epoch=data.get("manager_epoch", 0),
            budget_grant_revision=data.get("budget_grant_revision", 0),
            support_sets=sequence_of(
                data.get("support_sets", ()),
                f"{name}.support_sets",
                lambda item, where: SupportSetRead.from_json(item, where),
            ),
            scope_epochs=sequence_of(
                data.get("scope_epochs", ()),
                f"{name}.scope_epochs",
                lambda item, where: ScopeEpochRead.from_json(item, where),
            ),
            absences=sequence_of(
                data.get("absences", ()),
                f"{name}.absences",
                lambda item, where: AbsenceRead.from_json(item, where),
            ),
        )


# --------------------------------------------------------------------------------------
# PlanProposal (model side) and ProposedPlanDelta (compiler side) — §18.3
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefineOperation:
    goal_id: str
    obligation_id: str
    method_ref: VersionedRef
    bindings: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "refine",
            "goal_id": self.goal_id,
            "obligation_id": self.obligation_id,
            "method_ref": self.method_ref.to_json(),
            "bindings": dict(self.bindings),
        }


@dataclass(frozen=True, slots=True)
class RetireMethodOperation:
    method_instance_id: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "retire_method",
            "method_instance_id": self.method_instance_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BindSharedGoalOperation:
    consumer_method_instance_id: str
    step: str
    goal_id: str
    resolution_id: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "bind_shared_goal",
            "consumer_method_instance_id": self.consumer_method_instance_id,
            "step": self.step,
            "goal_id": self.goal_id,
            "resolution_id": self.resolution_id,
        }


@dataclass(frozen=True, slots=True)
class ProposeSuccessorOperation:
    old_task_id: str
    obligation_id: str
    goal_type_ref: VersionedRef
    bindings: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "op": "propose_successor",
            "old_task_id": self.old_task_id,
            "obligation_id": self.obligation_id,
            "goal_type_ref": self.goal_type_ref.to_json(),
            "bindings": dict(self.bindings),
        }


PlanOperation: TypeAlias = (
    "RefineOperation | RetireMethodOperation | BindSharedGoalOperation | ProposeSuccessorOperation"
)


def parse_plan_operation(value: object, name: str) -> PlanOperation:
    reject_executable(value, name)
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    op = value.get("op")
    if op == "refine":
        data = fields_of(
            value, name, required=("op", "goal_id", "obligation_id", "method_ref", "bindings")
        )
        return RefineOperation(
            goal_id=identifier(data["goal_id"], f"{name}.goal_id"),
            obligation_id=identifier(data["obligation_id"], f"{name}.obligation_id"),
            method_ref=VersionedRef.from_json(data["method_ref"], f"{name}.method_ref"),
            bindings=json_object(data["bindings"], f"{name}.bindings"),
        )
    if op == "retire_method":
        data = fields_of(value, name, required=("op", "method_instance_id", "reason"))
        return RetireMethodOperation(
            method_instance_id=identifier(data["method_instance_id"], f"{name}.method_instance_id"),
            reason=text(data["reason"], f"{name}.reason"),
        )
    if op == "bind_shared_goal":
        data = fields_of(
            value,
            name,
            required=("op", "consumer_method_instance_id", "step", "goal_id", "resolution_id"),
        )
        return BindSharedGoalOperation(
            consumer_method_instance_id=identifier(
                data["consumer_method_instance_id"], f"{name}.consumer_method_instance_id"
            ),
            step=identifier(data["step"], f"{name}.step"),
            goal_id=identifier(data["goal_id"], f"{name}.goal_id"),
            resolution_id=optional_identifier(data["resolution_id"], f"{name}.resolution_id"),
        )
    if op == "propose_successor":
        data = fields_of(
            value,
            name,
            required=("op", "old_task_id", "obligation_id", "goal_type_ref", "bindings"),
        )
        return ProposeSuccessorOperation(
            old_task_id=identifier(data["old_task_id"], f"{name}.old_task_id"),
            obligation_id=identifier(data["obligation_id"], f"{name}.obligation_id"),
            goal_type_ref=VersionedRef.from_json(data["goal_type_ref"], f"{name}.goal_type_ref"),
            bindings=json_object(data["bindings"], f"{name}.bindings"),
        )
    raise ContractError(
        f"{name}.op must be one of "
        "['bind_shared_goal', 'propose_successor', 'refine', 'retire_method']"
    )


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """§18.3: the *unchecked* model-side proposal (``plan-revision-proposal-v1``).

    Holding one of these proves nothing about coverage, ports, cycles or budgets.
    Only :class:`ProposedPlanDelta`, produced by the compiler, may reach a Commit.
    """

    proposal_id: str
    mission_id: MissionRef
    expected_plan_revision: PlanRevision
    trigger_refs: tuple[EvidenceRef, ...]
    read_set: tuple[ReadItem, ...]
    operations: tuple[Any, ...]
    rationale: str
    running_work_policy: RunningWorkPolicy

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "proposal_id", identifier(self.proposal_id, "proposal.proposal_id")
        )
        object.__setattr__(self, "mission_id", mission_ref(self.mission_id, "proposal.mission_id"))
        object.__setattr__(
            self,
            "expected_plan_revision",
            plan_revision(self.expected_plan_revision, "proposal.expected_plan_revision"),
        )
        if not self.read_set:
            raise ContractError("proposal.read_set must not be empty (ADR-13)")
        if not self.operations:
            raise ContractError("proposal.operations must not be empty")
        object.__setattr__(self, "rationale", text(self.rationale, "proposal.rationale"))
        object.__setattr__(
            self,
            "running_work_policy",
            enum_of(RunningWorkPolicy, self.running_work_policy, "proposal.running_work_policy"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_REVISION_PROPOSAL_SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "mission_id": str(self.mission_id),
            "expected_plan_revision": int(self.expected_plan_revision),
            "trigger_refs": [ref.to_json() for ref in self.trigger_refs],
            "read_set": [item.to_json() for item in self.read_set],
            "operations": [operation.to_json() for operation in self.operations],
            "rationale": self.rationale,
            "running_work_policy": str(self.running_work_policy),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "plan_revision_proposal") -> PlanProposal:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "proposal_id",
                "mission_id",
                "expected_plan_revision",
                "trigger_refs",
                "read_set",
                "operations",
                "rationale",
                "running_work_policy",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=PLAN_REVISION_PROPOSAL_SCHEMA_VERSION,
        )
        # This is definitionally the model side (§18.3), so an attribution to the
        # system or to a real tool cannot be accepted from it.
        reject_model_claimed_provenance(data, name)
        return cls(
            proposal_id=data["proposal_id"],
            mission_id=MissionRef(data["mission_id"]),
            expected_plan_revision=PlanRevision(data["expected_plan_revision"]),
            trigger_refs=sequence_of(
                data["trigger_refs"],
                f"{name}.trigger_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
            read_set=sequence_of(
                data["read_set"],
                f"{name}.read_set",
                lambda item, where: ReadItem.from_json(item, where),
                minimum=1,
            ),
            operations=sequence_of(
                data["operations"],
                f"{name}.operations",
                lambda item, where: parse_plan_operation(item, where),
                minimum=1,
            ),
            rationale=data["rationale"],
            running_work_policy=data["running_work_policy"],
        )


@dataclass(frozen=True, slots=True)
class OccurrenceSpec:
    """One node the compiler wants to add to the network.

    ``obligation_id`` and ``form`` are *copies* of what the task's
    :class:`TaskSemanticBindingV1` says; the binding remains the authority and
    :func:`assert_occurrences_match_bindings` refuses any disagreement.
    """

    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId
    form: TaskForm
    requiredness: Requiredness = Requiredness.REQUIRED

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", occurrence_id(self.occurrence_id, "occurrence.occurrence_id")
        )
        object.__setattr__(self, "task_id", task_ref(self.task_id, "occurrence.task_id"))
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "occurrence.obligation_id")
        )
        object.__setattr__(self, "form", enum_of(TaskForm, self.form, "occurrence.form"))
        object.__setattr__(
            self,
            "requiredness",
            enum_of(Requiredness, self.requiredness, "occurrence.requiredness"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "occurrence_id": str(self.occurrence_id),
            "task_id": str(self.task_id),
            "obligation_id": str(self.obligation_id),
            "form": str(self.form),
            "requiredness": str(self.requiredness),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "occurrence_spec") -> OccurrenceSpec:
        data = fields_of(
            value,
            name,
            required=("occurrence_id", "task_id", "obligation_id", "form"),
            optional=("requiredness",),
        )
        return cls(
            occurrence_id=OccurrenceId(data["occurrence_id"]),
            task_id=TaskRef(data["task_id"]),
            obligation_id=ObligationId(data["obligation_id"]),
            form=data["form"],
            requiredness=data.get("requiredness", Requiredness.REQUIRED),
        )


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    """Which criteria of a duty the added occurrences are claimed to cover."""

    obligation_id: ObligationId
    criterion_ids: tuple[str, ...]
    covered_by: tuple[OccurrenceId, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "coverage.obligation_id")
        )
        object.__setattr__(
            self, "criterion_ids", identifiers(self.criterion_ids, "coverage.criterion_ids")
        )
        object.__setattr__(
            self,
            "covered_by",
            tuple(
                OccurrenceId(item) for item in identifiers(self.covered_by, "coverage.covered_by")
            ),
        )
        if not self.criterion_ids:
            raise ContractError("coverage.criterion_ids must not be empty")
        if not self.covered_by:
            raise ContractError("coverage.covered_by must not be empty")

    def to_json(self) -> dict[str, Any]:
        return {
            "obligation_id": str(self.obligation_id),
            "criterion_ids": list(self.criterion_ids),
            "covered_by": [str(item) for item in self.covered_by],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "obligation_coverage") -> ObligationCoverage:
        data = fields_of(value, name, required=("obligation_id", "criterion_ids", "covered_by"))
        return cls(
            obligation_id=ObligationId(data["obligation_id"]),
            criterion_ids=tuple(data["criterion_ids"]),
            covered_by=tuple(data["covered_by"]),
        )


@dataclass(frozen=True, slots=True)
class ProposedPlanDelta:
    """§18.3: the compiler's checked output — the only shape a Commit accepts.

    A ``PlanProposal`` cannot be re-labelled into one: the delta carries the
    compiled occurrences, the typed edges, the coverage claim and the semantic
    read-set that :func:`require_commit_ready` insists on.
    """

    delta_id: str
    mission_id: MissionRef
    base_plan_revision: PlanRevision
    read_set: SemanticReadSet
    method_instances: tuple[MethodInstanceDraft, ...] = ()
    occurrences: tuple[OccurrenceSpec, ...] = ()
    retired_instance_ids: tuple[MethodInstanceId, ...] = ()
    order_constraints: tuple[OrderConstraint, ...] = ()
    data_requirements: tuple[DataRequirement, ...] = ()
    obligation_coverage: tuple[ObligationCoverage, ...] = ()
    referenced_occurrences: tuple[OccurrenceId, ...] = ()
    compiled_from_proposal_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta_id", identifier(self.delta_id, "delta.delta_id"))
        object.__setattr__(self, "mission_id", mission_ref(self.mission_id, "delta.mission_id"))
        object.__setattr__(
            self,
            "base_plan_revision",
            plan_revision(self.base_plan_revision, "delta.base_plan_revision"),
        )
        if not isinstance(self.read_set, SemanticReadSet):
            raise ContractError("delta.read_set must be a SemanticReadSet (ADR-13)")
        object.__setattr__(
            self,
            "referenced_occurrences",
            tuple(
                OccurrenceId(item)
                for item in identifiers(self.referenced_occurrences, "delta.referenced_occurrences")
            ),
        )
        known = {spec.occurrence_id for spec in self.occurrences} | set(self.referenced_occurrences)
        for order in self.order_constraints:
            for endpoint in (order.before, order.after):
                if endpoint not in known:
                    raise ContractError(
                        f"delta.order_constraints refers to unknown occurrence {endpoint!r}"
                    )
        for requirement in self.data_requirements:
            for endpoint in (
                requirement.producer_occurrence,
                requirement.consumer_occurrence,
            ):
                if endpoint not in known:
                    raise ContractError(
                        f"delta.data_requirements refers to unknown occurrence {endpoint!r}"
                    )
        ports = [
            (requirement.consumer_occurrence, requirement.input_port)
            for requirement in self.data_requirements
        ]
        if len(set(ports)) != len(ports):
            raise ContractError(
                "a single-valued input port accepts one binding; "
                "delta.data_requirements binds one twice (TG decision 3)"
            )
        object.__setattr__(
            self,
            "compiled_from_proposal_id",
            optional_identifier(self.compiled_from_proposal_id, "delta.compiled_from_proposal_id"),
        )

    def assert_consistent_with(self, bindings: Mapping[TaskRef, TaskSemanticBindingV1]) -> None:
        """Check every occurrence against the task binding that is its authority."""

        assert_occurrences_match_bindings(self.occurrences, bindings)
        assert_method_instances_match_occurrences(self.method_instances, self.occurrences)

    def to_json(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "mission_id": str(self.mission_id),
            "base_plan_revision": int(self.base_plan_revision),
            "read_set": self.read_set.to_json(),
            "method_instances": [item.to_json() for item in self.method_instances],
            "occurrences": [item.to_json() for item in self.occurrences],
            "retired_instance_ids": [str(item) for item in self.retired_instance_ids],
            "order_constraints": [item.to_json() for item in self.order_constraints],
            "data_requirements": [item.to_json() for item in self.data_requirements],
            "obligation_coverage": [item.to_json() for item in self.obligation_coverage],
            "referenced_occurrences": [str(item) for item in self.referenced_occurrences],
            "compiled_from_proposal_id": self.compiled_from_proposal_id,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "proposed_plan_delta") -> ProposedPlanDelta:
        data = fields_of(
            value,
            name,
            required=("delta_id", "mission_id", "base_plan_revision", "read_set"),
            optional=(
                "method_instances",
                "occurrences",
                "retired_instance_ids",
                "order_constraints",
                "data_requirements",
                "obligation_coverage",
                "referenced_occurrences",
                "compiled_from_proposal_id",
            ),
        )
        return cls(
            delta_id=data["delta_id"],
            mission_id=MissionRef(data["mission_id"]),
            base_plan_revision=PlanRevision(data["base_plan_revision"]),
            read_set=SemanticReadSet.from_json(data["read_set"], f"{name}.read_set"),
            method_instances=sequence_of(
                data.get("method_instances", ()),
                f"{name}.method_instances",
                lambda item, where: MethodInstanceDraft.from_json(item, where),
            ),
            occurrences=sequence_of(
                data.get("occurrences", ()),
                f"{name}.occurrences",
                lambda item, where: OccurrenceSpec.from_json(item, where),
            ),
            retired_instance_ids=tuple(
                MethodInstanceId(item)
                for item in identifiers(
                    data.get("retired_instance_ids", ()), f"{name}.retired_instance_ids"
                )
            ),
            order_constraints=sequence_of(
                data.get("order_constraints", ()),
                f"{name}.order_constraints",
                lambda item, where: OrderConstraint.from_json(item, where),
            ),
            data_requirements=sequence_of(
                data.get("data_requirements", ()),
                f"{name}.data_requirements",
                lambda item, where: DataRequirement.from_json(item, where),
            ),
            obligation_coverage=sequence_of(
                data.get("obligation_coverage", ()),
                f"{name}.obligation_coverage",
                lambda item, where: ObligationCoverage.from_json(item, where),
            ),
            referenced_occurrences=tuple(
                OccurrenceId(item) for item in data.get("referenced_occurrences", ())
            ),
            compiled_from_proposal_id=data.get("compiled_from_proposal_id"),
        )


class FeedbackOutcome(StrEnum):
    """``execution-feedback-v1``: what the attempt produced, as the worker saw it."""

    CANDIDATE = "candidate"
    BLOCKED = "blocked"
    FAILURE = "failure"
    NO_PROGRESS = "no_progress"
    PROPOSED_SUBTASKS = "proposed_subtasks"


class DiagnosisCategory(StrEnum):
    """§9.1: not every problem is "decompose it again"."""

    NONE = "none"
    MISSING_INFORMATION = "missing_information"
    MISSING_PREREQUISITE = "missing_prerequisite"
    CONTENT_DEFECT = "content_defect"
    TOO_COMPLEX = "too_complex"
    METHOD_INVALID = "method_invalid"
    INFRASTRUCTURE = "infrastructure"
    AUTHORIZATION = "authorization"
    OPERATION_UNKNOWN = "operation_unknown"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True, slots=True)
class FeedbackObservation:
    """A worker statement always travels with the evidence it rests on."""

    statement: str
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "statement", text(self.statement, "observation.statement"))
        if not self.evidence_refs:
            raise ContractError("observation.evidence_refs must not be empty")

    def to_json(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "observation") -> FeedbackObservation:
        data = fields_of(value, name, required=("statement", "evidence_refs"))
        return cls(
            statement=data["statement"],
            evidence_refs=sequence_of(
                data["evidence_refs"],
                f"{name}.evidence_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class FeedbackDiagnosis:
    category: DiagnosisCategory
    explanation: str
    uncertainty: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "category", enum_of(DiagnosisCategory, self.category, "diagnosis.category")
        )
        object.__setattr__(self, "explanation", text(self.explanation, "diagnosis.explanation"))
        object.__setattr__(self, "uncertainty", text(self.uncertainty, "diagnosis.uncertainty"))

    def to_json(self) -> dict[str, Any]:
        return {
            "category": str(self.category),
            "explanation": self.explanation,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "diagnosis") -> FeedbackDiagnosis:
        data = fields_of(value, name, required=("category", "explanation", "uncertainty"))
        return cls(
            category=data["category"],
            explanation=data["explanation"],
            uncertainty=data["uncertainty"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionFeedbackV1:
    """``execution-feedback-v1`` (§9.1): what came back from one attempt.

    P1.1 provides the boundary codec only — classifying the feedback and deciding
    what to repair is P3's ``analyze_impact`` / decision table.  Carrying the
    ``uncertainty`` field beside the diagnosis is deliberate: "we did not find it"
    must stay distinguishable from "it is not there" (ADR-07).
    """

    feedback_id: str
    mission_id: MissionRef
    task_id: TaskRef
    attempt_id: str
    contract_revision: ContractRevision
    input_manifest_hash: str
    outcome: FeedbackOutcome
    observations: tuple[FeedbackObservation, ...]
    diagnosis: FeedbackDiagnosis
    proposed_method_refs: tuple[VersionedRef, ...] = ()
    artifact_refs: tuple[EvidenceRef, ...] = ()
    unresolved_operation_ids: tuple[str, ...] = ()
    cost_receipt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "feedback_id", identifier(self.feedback_id, "feedback.feedback_id")
        )
        object.__setattr__(self, "mission_id", mission_ref(self.mission_id, "feedback.mission_id"))
        object.__setattr__(self, "task_id", task_ref(self.task_id, "feedback.task_id"))
        object.__setattr__(self, "attempt_id", identifier(self.attempt_id, "feedback.attempt_id"))
        object.__setattr__(
            self,
            "contract_revision",
            contract_revision(self.contract_revision, "feedback.contract_revision"),
        )
        object.__setattr__(
            self,
            "input_manifest_hash",
            hash_hex(self.input_manifest_hash, "feedback.input_manifest_hash"),
        )
        object.__setattr__(
            self, "outcome", enum_of(FeedbackOutcome, self.outcome, "feedback.outcome")
        )
        if not isinstance(self.diagnosis, FeedbackDiagnosis):
            raise ContractError("feedback.diagnosis must be a FeedbackDiagnosis")
        object.__setattr__(
            self,
            "unresolved_operation_ids",
            identifiers(self.unresolved_operation_ids, "feedback.unresolved_operation_ids"),
        )
        object.__setattr__(
            self,
            "cost_receipt_ids",
            identifiers(self.cost_receipt_ids, "feedback.cost_receipt_ids"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_FEEDBACK_SCHEMA_VERSION,
            "feedback_id": self.feedback_id,
            "mission_id": str(self.mission_id),
            "task_id": str(self.task_id),
            "attempt_id": self.attempt_id,
            "contract_revision": int(self.contract_revision),
            "input_manifest_hash": self.input_manifest_hash,
            "outcome": str(self.outcome),
            "observations": [item.to_json() for item in self.observations],
            "diagnosis": self.diagnosis.to_json(),
            "proposed_method_refs": [ref.to_json() for ref in self.proposed_method_refs],
            "artifact_refs": [ref.to_json() for ref in self.artifact_refs],
            "unresolved_operation_ids": list(self.unresolved_operation_ids),
            "cost_receipt_ids": list(self.cost_receipt_ids),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "execution_feedback") -> ExecutionFeedbackV1:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "feedback_id",
                "mission_id",
                "task_id",
                "attempt_id",
                "contract_revision",
                "input_manifest_hash",
                "outcome",
                "observations",
                "diagnosis",
                "proposed_method_refs",
                "artifact_refs",
                "unresolved_operation_ids",
                "cost_receipt_ids",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=EXECUTION_FEEDBACK_SCHEMA_VERSION,
        )
        # Worker-reported feedback is model output too: it may cite a tool receipt,
        # but it may not be the thing that says the receipt came from a tool.
        reject_model_claimed_provenance(data, name)
        return cls(
            feedback_id=data["feedback_id"],
            mission_id=MissionRef(data["mission_id"]),
            task_id=TaskRef(data["task_id"]),
            attempt_id=data["attempt_id"],
            contract_revision=ContractRevision(data["contract_revision"]),
            input_manifest_hash=data["input_manifest_hash"],
            outcome=data["outcome"],
            observations=sequence_of(
                data["observations"],
                f"{name}.observations",
                lambda item, where: FeedbackObservation.from_json(item, where),
            ),
            diagnosis=FeedbackDiagnosis.from_json(data["diagnosis"], f"{name}.diagnosis"),
            proposed_method_refs=sequence_of(
                data["proposed_method_refs"],
                f"{name}.proposed_method_refs",
                lambda item, where: VersionedRef.from_json(item, where),
            ),
            artifact_refs=sequence_of(
                data["artifact_refs"],
                f"{name}.artifact_refs",
                lambda item, where: EvidenceRef.from_json(item, where),
            ),
            unresolved_operation_ids=tuple(data["unresolved_operation_ids"]),
            cost_receipt_ids=tuple(data["cost_receipt_ids"]),
        )


# --------------------------------------------------------------------------------------
# Typed relation edges (§6.5 / TG §4)
# --------------------------------------------------------------------------------------


class EndpointKind(StrEnum):
    """What a typed edge endpoint names.  The kind is carried, never inferred."""

    OCCURRENCE = "occurrence"
    TASK = "task"
    METHOD_INSTANCE = "method_instance"
    OBLIGATION = "obligation"
    EVIDENCE = "evidence"
    SUPERVISOR = "supervisor"


@dataclass(frozen=True, slots=True)
class NetworkEndpoint:
    kind: EndpointKind
    id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", enum_of(EndpointKind, self.kind, "endpoint.kind"))
        object.__setattr__(self, "id", identifier(self.id, "endpoint.id"))

    def to_json(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "id": self.id}

    @classmethod
    def from_json(cls, value: object, name: str = "endpoint") -> NetworkEndpoint:
        data = fields_of(value, name, required=("kind", "id"))
        return cls(kind=data["kind"], id=data["id"])


_EVIDENCE_TARGETS = frozenset(
    {
        EndpointKind.TASK,
        EndpointKind.OCCURRENCE,
        EndpointKind.METHOD_INSTANCE,
        EndpointKind.OBLIGATION,
    }
)

#: §6.5: which endpoints each relation may join.  Written out per relation rather
#: than left to a ``kind: str`` plus a free-form payload, because "these two things
#: are related" is exactly the ambiguity the four-relation split exists to remove.
TYPED_EDGE_ENDPOINTS: Mapping[
    RelationKind, tuple[frozenset[EndpointKind], frozenset[EndpointKind]]
] = {
    RelationKind.SUPPORT: (frozenset({EndpointKind.EVIDENCE}), _EVIDENCE_TARGETS),
    RelationKind.ASSUMPTION: (frozenset({EndpointKind.EVIDENCE}), _EVIDENCE_TARGETS),
    RelationKind.SUPERVISION: (
        frozenset({EndpointKind.SUPERVISOR}),
        frozenset({EndpointKind.TASK, EndpointKind.OCCURRENCE, EndpointKind.OBLIGATION}),
    ),
    RelationKind.FUNDING: (
        frozenset({EndpointKind.OBLIGATION}),
        frozenset({EndpointKind.OBLIGATION}),
    ),
    RelationKind.SUPERSEDES: (
        frozenset(
            {
                EndpointKind.TASK,
                EndpointKind.OCCURRENCE,
                EndpointKind.METHOD_INSTANCE,
                EndpointKind.OBLIGATION,
            }
        ),
        frozenset(
            {
                EndpointKind.TASK,
                EndpointKind.OCCURRENCE,
                EndpointKind.METHOD_INSTANCE,
                EndpointKind.OBLIGATION,
            }
        ),
    ),
}

#: The relations a :class:`TypedEdge` may carry.  ``refinement`` / ``satisfies``,
#: ``ORDER`` and ``DATA`` each have their own contract type (``ChildBinding``,
#: ``OrderConstraint``, ``DataRequirement``) and are refused here, so no caller can
#: express an execution dependency as a generic edge and bypass those checks.
TYPED_EDGE_RELATIONS = frozenset(TYPED_EDGE_ENDPOINTS)


@dataclass(frozen=True, slots=True)
class TypedEdge:
    """One relation with exactly one meaning (§6.5 / TG §4).

    ``SUPPORT`` and ``ASSUMPTION`` say what a conclusion rests on — never that the
    target may execute.  ``supervision`` is a coordination scope, not inherited
    authority.  ``funding`` keeps the single-owner budget tree beside the DAG of
    execution dependencies.  ``supersedes`` replaces a subject without rewriting
    the history of the one it replaced.
    """

    relation: RelationKind
    source: NetworkEndpoint
    target: NetworkEndpoint
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relation", enum_of(RelationKind, self.relation, "typed_edge.relation")
        )
        for name in ("source", "target"):
            if not isinstance(getattr(self, name), NetworkEndpoint):
                raise ContractError(f"typed_edge.{name} must be a NetworkEndpoint")
        if self.relation not in TYPED_EDGE_ENDPOINTS:
            raise ContractError(
                f"{self.relation!s} has its own contract type and is not a generic edge; "
                "use ChildBinding, OrderConstraint or DataRequirement"
            )
        allowed_source, allowed_target = TYPED_EDGE_ENDPOINTS[self.relation]
        if self.source.kind not in allowed_source:
            raise ContractError(
                f"{self.relation!s} source must be one of "
                f"{sorted(str(item) for item in allowed_source)}, not {self.source.kind!s}"
            )
        if self.target.kind not in allowed_target:
            raise ContractError(
                f"{self.relation!s} target must be one of "
                f"{sorted(str(item) for item in allowed_target)}, not {self.target.kind!s}"
            )
        if self.relation is RelationKind.SUPERSEDES and self.source.kind is not self.target.kind:
            raise ContractError("supersedes joins two subjects of the same kind")
        if self.source == self.target:
            raise ContractError("a typed edge must join two different endpoints")
        if not isinstance(self.label, str):
            raise ContractError("typed_edge.label must be a string")
        if self.label:
            text(self.label, "typed_edge.label", limit=512)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "relation": str(self.relation),
            "source": self.source.to_json(),
            "target": self.target.to_json(),
        }
        if self.label:
            payload["label"] = self.label
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "typed_edge") -> TypedEdge:
        data = fields_of(
            value, name, required=("relation", "source", "target"), optional=("label",)
        )
        return cls(
            relation=data["relation"],
            source=NetworkEndpoint.from_json(data["source"], f"{name}.source"),
            target=NetworkEndpoint.from_json(data["target"], f"{name}.target"),
            label=data.get("label", ""),
        )


# --------------------------------------------------------------------------------------
# Authority: the semantic binding decides obligation_id and form
# --------------------------------------------------------------------------------------


def assert_occurrences_match_bindings(
    occurrences: tuple[OccurrenceSpec, ...],
    bindings: Mapping[TaskRef, TaskSemanticBindingV1],
) -> None:
    """TG §3.2: ``TaskSemanticBindingV1`` is the authority for ``obligation_id`` and ``form``.

    An occurrence is a *position* in a method; it repeats the duty and the form so a
    delta can be read on its own, and repeating them is precisely how the two can
    drift.  A delta whose occurrence disagrees with the binding is refused rather
    than resolved in either direction: silently trusting the occurrence would let a
    re-parented node carry the wrong duty's retry budget, and silently trusting the
    binding would hide that the compiler built something else.
    """

    for occurrence in occurrences:
        binding = bindings.get(occurrence.task_id)
        if binding is None:
            raise ContractError(
                f"occurrence {occurrence.occurrence_id!s} names task "
                f"{occurrence.task_id!s}, which has no semantic binding"
            )
        if occurrence.obligation_id != binding.obligation_id:
            raise ContractError(
                f"occurrence {occurrence.occurrence_id!s} claims obligation "
                f"{occurrence.obligation_id!s} but its task binding says "
                f"{binding.obligation_id!s}; the binding is the authority"
            )
        if occurrence.form is not binding.form:
            raise ContractError(
                f"occurrence {occurrence.occurrence_id!s} claims form "
                f"{occurrence.form!s} but its task binding says {binding.form!s}; "
                "the binding is the authority"
            )


def assert_method_instances_match_occurrences(
    drafts: tuple[MethodInstanceDraft, ...],
    occurrences: tuple[OccurrenceSpec, ...],
) -> None:
    """TG §12: a refinement is pinned to ``(goal_id, goal_occurrence_id)``, not to the task.

    A shared compound goal can sit at several occurrences, each adopted by a
    different consumer's slot.  Checking only ``goal_id`` would let a refinement of
    one consumer's occurrence be read as a refinement of another's — which is how a
    shared sub-goal quietly acquires two owners.

    Drafts whose occurrence this delta does not carry are left to the full-network
    check (P3.1): a delta legitimately refines nodes that already exist.
    """

    by_occurrence = {spec.occurrence_id: spec for spec in occurrences}
    for draft in drafts:
        occurrence = by_occurrence.get(draft.effective_goal_occurrence_id)
        if occurrence is None:
            continue
        if occurrence.task_id != draft.goal_id:
            raise ContractError(
                f"method instance {draft.instance_id!s} refines occurrence "
                f"{occurrence.occurrence_id!s}, which belongs to task "
                f"{occurrence.task_id!s}, not to its goal {draft.goal_id!s}"
            )
        if occurrence.form is not TaskForm.COMPOUND:
            raise ContractError(
                f"method instance {draft.instance_id!s} refines occurrence "
                f"{occurrence.occurrence_id!s}, which is primitive; only a compound "
                "task is refined by a method (§6.2)"
            )


def require_commit_ready(candidate: object) -> ProposedPlanDelta:
    """§18.3 naming rule: a Commit takes a compiled delta, never a raw proposal."""

    if isinstance(candidate, PlanProposal):
        raise ContractError(
            "a PlanProposal is an unchecked model proposal; compile it into a "
            "ProposedPlanDelta before committing (§18.3)"
        )
    if not isinstance(candidate, ProposedPlanDelta):
        raise ContractError("commit input must be a ProposedPlanDelta")
    return candidate


__all__ = (
    "EXECUTION_FEEDBACK_SCHEMA_VERSION",
    "TYPED_EDGE_ENDPOINTS",
    "TYPED_EDGE_RELATIONS",
    "MAX_CONDITION_NODES",
    "METHOD_CONTRACT_SCHEMA_VERSION",
    "MODEL_SUBMITTABLE_STATUS",
    "PLAN_REVISION_PROPOSAL_SCHEMA_VERSION",
    "AbsenceRead",
    "AcceptanceRef",
    "AllCondition",
    "AnyCondition",
    "ApplicabilityCheckPolicy",
    "ArrayValue",
    "ArtifactRef",
    "AttemptRef",
    "Binding",
    "BindSharedGoalOperation",
    "BoundInput",
    "ChildBinding",
    "Condition",
    "ConstantCondition",
    "ConstantValue",
    "ContractRevision",
    "CriterionLink",
    "DataBindingId",
    "DataRequirement",
    "DiagnosisCategory",
    "DispatchGeneration",
    "EndpointKind",
    "ExecutionFeedbackV1",
    "FeedbackDiagnosis",
    "FeedbackObservation",
    "FeedbackOutcome",
    "GoalSignature",
    "GraphStructureBudget",
    "InputBindingRevision",
    "MethodComposition",
    "MethodContract",
    "MethodInstanceDraft",
    "MethodInstanceId",
    "MethodOccurrenceBinding",
    "MethodOrdering",
    "MethodRef",
    "MethodRegistration",
    "MethodRegistryStatus",
    "MethodStep",
    "MissionRef",
    "NetworkEndpoint",
    "NotCondition",
    "ObjectValue",
    "ObligationCoverage",
    "ObligationId",
    "ObligationRelation",
    "OccurrenceId",
    "OccurrenceSpec",
    "OrderConstraint",
    "OutputValue",
    "ParameterValue",
    "PlanOperation",
    "PlanProposal",
    "PlanRevision",
    "PolicyRef",
    "PortCardinality",
    "PortOrdering",
    "PortSpec",
    "PreconditionRef",
    "PredicateCondition",
    "ProposeSuccessorOperation",
    "ProposedPlanDelta",
    "StructureBudget",
    "ReadItem",
    "ReadItemKind",
    "RecordVersion",
    "RefineOperation",
    "RegistryAuthor",
    "RelationKind",
    "ReleaseCondition",
    "Requiredness",
    "ResourceRef",
    "ResultRef",
    "RetireMethodOperation",
    "ReusePolicy",
    "RunningWorkPolicy",
    "SchemaRef",
    "ScopeEpochRead",
    "SemanticReadSet",
    "SideEffectKind",
    "SourceRevisionPolicy",
    "SupportSetRead",
    "TaskForm",
    "TaskRef",
    "TaskSemanticBindingV1",
    "TypedEdge",
    "ValidityRevision",
    "ValueExpr",
    "admit_method",
    "arguments_to_json",
    "assert_method_instances_match_occurrences",
    "assert_occurrences_match_bindings",
    "condition_digest",
    "condition_node_count",
    "contract_revision",
    "dispatch_generation",
    "input_binding_revision",
    "is_empty_expression",
    "method_instance_id",
    "mission_ref",
    "obligation_id",
    "occurrence_id",
    "parse_condition",
    "parse_conditions",
    "parse_plan_operation",
    "parse_value",
    "plan_revision",
    "record_version",
    "require_commit_ready",
    "resource_conflicts",
    "task_ref",
    "task_ref_from_typed",
    "undeclared_set_ports",
    "validity_revision",
)
