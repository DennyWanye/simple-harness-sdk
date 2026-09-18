# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Task types, schemas and the method registry with its admission protocol (§7.3).

Three catalogues and one registry, all pure data:

:class:`SchemaCatalog`
    declared field names and types behind a ``parameter_schema_ref`` /
    ``output_schema_ref``.  A ``VersionedRef`` alone says *which* schema; the
    catalogue says what is in it, which is what makes a type check possible at
    all.
:class:`TaskTypeCatalog`
    one entry per declared task type — compound goal or primitive operator — with
    its ports, its capability requirements and its declared effects.  §6.4: an
    atomic Operator must really be registered, so "this step names an operator
    nobody registered" is an admission refusal, not a run-time surprise.
:class:`MethodRegistry`
    immutable method definitions keyed by ``(method_id, version, content_hash)``,
    their :class:`~agent_orchestrator.contracts.htn.MethodRegistration` rows and
    the six-step admission protocol of §7.3.

Nothing here knows a domain.  A domain is a set of rows in these catalogues, so
adding one is data, never a branch: there is no ``if domain == ...`` in this
package and :mod:`.seed_methods` registers ``code`` and ``appworld`` through the
same public functions a third domain would use.

What P2.1 deliberately does *not* do: promote past ``TRIAL_ADMITTED``.  §7.3
assigns the offline ``EVALUATED → ADMITTED`` evaluation to P8, so
:meth:`MethodRegistry.promote` answers ``PROMOTION_NOT_AVAILABLE`` for every
target beyond the trial, and step 4 (independent planning review) and step 5
(formal modelling) are recorded as *deferred* rather than quietly passed — a
receipt that claimed them would be a claim about checks nobody ran.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...contracts.evidence_state import TruthValue
from ...contracts.htn import (
    AllCondition,
    AnyCondition,
    ArrayValue,
    ConstantCondition,
    ConstantValue,
    GoalSignature,
    MethodContract,
    MethodRef,
    MethodRegistration,
    MethodRegistryStatus,
    MethodStep,
    MissionRef,
    NotCondition,
    ObjectValue,
    OutputValue,
    ParameterValue,
    PortSpec,
    PredicateCondition,
    RegistryAuthor,
    ResourceRef,
    ReusePolicy,
    SideEffectKind,
    TaskForm,
    admit_method,
    mission_ref,
    parse_conditions,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import (
    MAX_TEXT,
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
    enum_of,
    fields_of,
    flag,
    identifier,
    identifiers,
    index,
    reject_executable,
    sequence_of,
    text,
)
from ...knowledge.predicates import (
    ArgumentCheck,
    ArgumentType,
    PredicateRegistry,
)
from .applicability import CapabilitySnapshot

#: §6.6: the effect kinds a shared or reused goal may carry.  Anything that writes
#: is one occurrence of a real action, so two slots may not quietly become one.
READ_ONLY_EFFECTS = frozenset({SideEffectKind.NONE, SideEffectKind.EXTERNAL_READ})

#: The statuses a method may be *retrieved* at.  ``TRIAL_ADMITTED`` is additionally
#: scoped to one mission (§7.3 v1.2), which :meth:`MethodRegistry.candidates_for`
#: enforces; ``SUSPENDED`` is deliberately absent — a suspended method keeps its
#: history and its existing instances but is never offered to a new search.
GLOBALLY_RETRIEVABLE_STATUS = frozenset({MethodRegistryStatus.ADMITTED})

_TYPE_PREDICATES: Mapping[ArgumentType, Any] = {
    ArgumentType.STRING: lambda value: isinstance(value, str),
    ArgumentType.INTEGER: lambda value: isinstance(value, int) and not isinstance(value, bool),
    ArgumentType.NUMBER: lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    ArgumentType.BOOLEAN: lambda value: isinstance(value, bool),
    ArgumentType.REF: lambda value: isinstance(value, str) and bool(value.strip()),
}


def value_matches(kind: ArgumentType, value: object) -> bool:
    """Is ``value`` an instance of the declared argument type?"""

    return bool(_TYPE_PREDICATES[enum_of(ArgumentType, kind, "argument_type")](value))


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaField:
    """One declared field of a parameter or output schema."""

    name: str
    type: ArgumentType
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "schema_field.name"))
        object.__setattr__(self, "type", enum_of(ArgumentType, self.type, "schema_field.type"))
        object.__setattr__(self, "required", flag(self.required, "schema_field.required"))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": str(self.type), "required": self.required}

    @classmethod
    def from_json(cls, value: object, name: str = "schema_field") -> SchemaField:
        data = fields_of(value, name, required=("name", "type"), optional=("required",))
        return cls(name=data["name"], type=data["type"], required=data.get("required", True))


@dataclass(frozen=True, slots=True)
class ObjectSchema:
    """The declared shape behind one ``VersionedRef``.

    Deliberately shallow: a flat record of typed fields is what a method parameter
    binding and a port payload need to be checked against, and it is the largest
    claim this layer can actually make good on.  §18.3 is explicit that nothing
    here proves arbitrary JSON-Schema containment.
    """

    schema_ref: VersionedRef
    fields: tuple[SchemaField, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.schema_ref, VersionedRef):
            raise ContractError("schema.schema_ref must be a VersionedRef")
        names = [item.name for item in self.fields]
        if len(set(names)) != len(names):
            raise ContractError("schema.fields must not repeat a name")

    @property
    def key(self) -> tuple[str, int]:
        return (self.schema_ref.id, self.schema_ref.version)

    def field_named(self, name: str) -> SchemaField | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def check(self, values: Mapping[str, Any]) -> ArgumentCheck:
        """Type-check one grounded record against this schema."""

        declared = {item.name: item for item in self.fields}
        missing = tuple(
            sorted(name for name, item in declared.items() if item.required and name not in values)
        )
        unknown = tuple(sorted(name for name in values if name not in declared))
        wrong = tuple(
            sorted(
                name
                for name, value in values.items()
                if name in declared and not value_matches(declared[name].type, value)
            )
        )
        return ArgumentCheck(missing=missing, unknown=unknown, wrong_type=wrong)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_ref": self.schema_ref.to_json(),
            "fields": [item.to_json() for item in self.fields],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "object_schema") -> ObjectSchema:
        data = fields_of(value, name, required=("schema_ref",), optional=("fields",))
        return cls(
            schema_ref=VersionedRef.from_json(data["schema_ref"], f"{name}.schema_ref"),
            fields=sequence_of(
                data.get("fields", ()),
                f"{name}.fields",
                lambda item, where: SchemaField.from_json(item, where),
            ),
        )


class SchemaCatalog:
    """Declared schemas, addressed by ``(id, version)`` and pinned by content hash."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], ObjectSchema] = {}

    def register(self, schema: ObjectSchema) -> None:
        if not isinstance(schema, ObjectSchema):
            raise ContractError("register expects an ObjectSchema")
        reject_executable(schema.to_json(), "object_schema")
        existing = self._schemas.get(schema.key)
        if existing is not None:
            if existing.schema_ref.content_hash != schema.schema_ref.content_hash:
                raise ContractError(
                    f"schema {schema.key[0]!r} v{schema.key[1]} is already registered with "
                    "different content; publish a new version instead"
                )
            return
        self._schemas[schema.key] = schema

    def resolve(self, ref: VersionedRef) -> ObjectSchema | None:
        schema = self._schemas.get((ref.id, ref.version))
        if schema is None or schema.schema_ref.content_hash != ref.content_hash:
            return None
        return schema

    def require(self, ref: VersionedRef) -> ObjectSchema:
        schema = self.resolve(ref)
        if schema is None:
            raise ContractError(f"schema {ref.id!r} v{ref.version} is not registered")
        return schema

    def schemas(self) -> tuple[ObjectSchema, ...]:
        return tuple(self._schemas.values())


# --------------------------------------------------------------------------------------
# Task types (compound goals and primitive operators)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskTypeSpec:
    """One declared task type: what it is, what it takes, what it does.

    ``form`` decides which half of §6.2 this is.  A ``PRIMITIVE`` type carries an
    ``operator_ref`` because it is dispatched; a ``COMPOUND`` type carries none
    because it is refined.  ``reuse_policy`` is what the *type* permits, which is
    the data-side expression of TG §12's "only when the method explicitly allows
    reuse": a type that has not said so is never auto-shared, whatever two goals
    look like.
    """

    task_type_ref: VersionedRef
    form: TaskForm
    goal_signature: GoalSignature
    input_ports: tuple[PortSpec, ...] = ()
    output_ports: tuple[PortSpec, ...] = ()
    parameter_schema_ref: VersionedRef | None = None
    output_schema_ref: VersionedRef | None = None
    operator_ref: VersionedRef | None = None
    required_capabilities: tuple[str, ...] = ()
    side_effect_kind: SideEffectKind = SideEffectKind.NONE
    reversible: bool = True
    resource_reads: tuple[ResourceRef, ...] = ()
    resource_writes: tuple[ResourceRef, ...] = ()
    reuse_policy: ReusePolicy = ReusePolicy.NEW_WORK
    observes: tuple[VersionedRef, ...] = ()
    #: Structured conditions this type requires before it may be dispatched (§6.6).
    #: They are data, exactly like a method's ``applicable_when``, and are what makes
    #: "this leaf has an unresolved precondition" a checkable statement rather than a
    #: property of whichever method happened to open the slot.
    preconditions: tuple[Any, ...] = ()
    effect_identity: str | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_type_ref, VersionedRef):
            raise ContractError("task_type.task_type_ref must be a VersionedRef")
        object.__setattr__(self, "form", enum_of(TaskForm, self.form, "task_type.form"))
        if not isinstance(self.goal_signature, GoalSignature):
            raise ContractError("task_type.goal_signature must be a GoalSignature")
        for label, ports in (
            ("input_ports", self.input_ports),
            ("output_ports", self.output_ports),
        ):
            keys = [port.port_key for port in ports]
            if len(set(keys)) != len(keys):
                raise ContractError(f"task_type.{label} must not repeat a port_key")
        object.__setattr__(
            self,
            "required_capabilities",
            identifiers(self.required_capabilities, "task_type.required_capabilities"),
        )
        object.__setattr__(
            self,
            "side_effect_kind",
            enum_of(SideEffectKind, self.side_effect_kind, "task_type.side_effect_kind"),
        )
        object.__setattr__(self, "reversible", flag(self.reversible, "task_type.reversible"))
        object.__setattr__(
            self, "reuse_policy", enum_of(ReusePolicy, self.reuse_policy, "task_type.reuse_policy")
        )
        object.__setattr__(
            self,
            "effect_identity",
            None
            if self.effect_identity is None
            else identifier(self.effect_identity, "task_type.effect_identity"),
        )
        object.__setattr__(
            self,
            "domain",
            None if self.domain is None else identifier(self.domain, "task_type.domain"),
        )
        if self.form is TaskForm.PRIMITIVE and self.operator_ref is None:
            raise ContractError(
                "a primitive task type is dispatched and therefore needs an operator_ref "
                "(§6.4: an atomic Operator must be really registered)"
            )
        if self.form is TaskForm.COMPOUND and self.operator_ref is not None:
            raise ContractError(
                "a compound task type is refined by a method, not dispatched; "
                "it has no operator_ref (§6.2)"
            )
        if self.form is TaskForm.COMPOUND and (
            self.resource_reads
            or self.resource_writes
            or self.side_effect_kind is not SideEffectKind.NONE
        ):
            raise ContractError(
                "a compound task type declares no resources or effects; those belong to "
                "the primitive operators it is refined into (§6.2)"
            )
        if self.reuse_policy is not ReusePolicy.NEW_WORK and (
            self.side_effect_kind not in READ_ONLY_EFFECTS and self.effect_identity is None
        ):
            raise ContractError(
                "a side-effecting task type may only declare reuse together with an "
                "explicit effect_identity; two sends are two sends (TG §12)"
            )

    @property
    def key(self) -> tuple[str, int]:
        return (self.task_type_ref.id, self.task_type_ref.version)

    @property
    def read_only(self) -> bool:
        return self.side_effect_kind in READ_ONLY_EFFECTS

    @property
    def low_risk(self) -> bool:
        """§7.2 / ADaPT: may this be *tried* before it is planned?

        Only when nothing irreversible happens outside the orchestrator.  A local
        write that the type declares reversible qualifies; an external state or
        event write never does, however small it looks.
        """

        if self.read_only:
            return True
        return self.side_effect_kind is SideEffectKind.LOCAL_WRITE and self.reversible

    def output_port(self, port_key: str) -> PortSpec | None:
        for port in self.output_ports:
            if port.port_key == port_key:
                return port
        return None

    def input_port(self, port_key: str) -> PortSpec | None:
        for port in self.input_ports:
            if port.port_key == port_key:
                return port
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_type_ref": self.task_type_ref.to_json(),
            "form": str(self.form),
            "goal_signature": self.goal_signature.to_json(),
            "input_ports": [port.to_json() for port in self.input_ports],
            "output_ports": [port.to_json() for port in self.output_ports],
            "parameter_schema_ref": (
                None if self.parameter_schema_ref is None else self.parameter_schema_ref.to_json()
            ),
            "output_schema_ref": (
                None if self.output_schema_ref is None else self.output_schema_ref.to_json()
            ),
            "operator_ref": None if self.operator_ref is None else self.operator_ref.to_json(),
            "required_capabilities": list(self.required_capabilities),
            "side_effect_kind": str(self.side_effect_kind),
            "reversible": self.reversible,
            "resource_reads": [ref.to_json() for ref in self.resource_reads],
            "resource_writes": [ref.to_json() for ref in self.resource_writes],
            "reuse_policy": str(self.reuse_policy),
            "observes": [ref.to_json() for ref in self.observes],
            "preconditions": [item.to_json() for item in self.preconditions],
            "effect_identity": self.effect_identity,
            "domain": self.domain,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "task_type") -> TaskTypeSpec:
        data = fields_of(
            value,
            name,
            required=("task_type_ref", "form", "goal_signature"),
            optional=(
                "input_ports",
                "output_ports",
                "parameter_schema_ref",
                "output_schema_ref",
                "operator_ref",
                "required_capabilities",
                "side_effect_kind",
                "reversible",
                "resource_reads",
                "resource_writes",
                "reuse_policy",
                "observes",
                "preconditions",
                "effect_identity",
                "domain",
            ),
        )

        def optional_ref(key: str) -> VersionedRef | None:
            raw = data.get(key)
            return None if raw is None else VersionedRef.from_json(raw, f"{name}.{key}")

        return cls(
            task_type_ref=VersionedRef.from_json(data["task_type_ref"], f"{name}.task_type_ref"),
            form=data["form"],
            goal_signature=GoalSignature.from_json(
                data["goal_signature"], f"{name}.goal_signature"
            ),
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
            parameter_schema_ref=optional_ref("parameter_schema_ref"),
            output_schema_ref=optional_ref("output_schema_ref"),
            operator_ref=optional_ref("operator_ref"),
            required_capabilities=tuple(data.get("required_capabilities", ())),
            side_effect_kind=data.get("side_effect_kind", SideEffectKind.NONE),
            reversible=data.get("reversible", True),
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
            reuse_policy=data.get("reuse_policy", ReusePolicy.NEW_WORK),
            observes=sequence_of(
                data.get("observes", ()),
                f"{name}.observes",
                lambda item, where: VersionedRef.from_json(item, where),
            ),
            preconditions=parse_conditions(data.get("preconditions", ()), f"{name}.preconditions"),
            effect_identity=data.get("effect_identity"),
            domain=data.get("domain"),
        )


class TaskTypeCatalog:
    """Every declared task type of a deployment.  Domains differ only by content."""

    def __init__(self) -> None:
        self._types: dict[tuple[str, int], TaskTypeSpec] = {}

    def register(self, spec: TaskTypeSpec) -> None:
        if not isinstance(spec, TaskTypeSpec):
            raise ContractError("register expects a TaskTypeSpec")
        reject_executable(spec.to_json(), "task_type")
        existing = self._types.get(spec.key)
        if existing is not None:
            if existing.task_type_ref.content_hash != spec.task_type_ref.content_hash:
                raise ContractError(
                    f"task type {spec.key[0]!r} v{spec.key[1]} is already registered with "
                    "different content; publish a new version instead"
                )
            return
        self._types[spec.key] = spec

    def resolve(self, ref: VersionedRef) -> TaskTypeSpec | None:
        spec = self._types.get((ref.id, ref.version))
        if spec is None or spec.task_type_ref.content_hash != ref.content_hash:
            return None
        return spec

    def require(self, ref: VersionedRef) -> TaskTypeSpec:
        spec = self.resolve(ref)
        if spec is None:
            raise ContractError(
                f"task type {ref.id!r} v{ref.version} is not registered at that content hash"
            )
        return spec

    def task_types(self) -> tuple[TaskTypeSpec, ...]:
        return tuple(self._types.values())

    def observers_for(self, predicate_ref: VersionedRef) -> tuple[TaskTypeSpec, ...]:
        """Read-only primitive types that can observe one predicate (§7.3 seed set).

        The registry declares *that* a type observes a predicate and nothing about
        how; a type that writes is never offered here, because an evidence-gathering
        occurrence exists precisely to answer a question without changing the answer.
        """

        return tuple(
            sorted(
                (
                    spec
                    for spec in self._types.values()
                    if spec.form is TaskForm.PRIMITIVE
                    and spec.read_only
                    and any(
                        item.id == predicate_ref.id and item.version == predicate_ref.version
                        for item in spec.observes
                    )
                ),
                key=lambda spec: spec.key,
            )
        )


# --------------------------------------------------------------------------------------
# Admission protocol (§7.3)
# --------------------------------------------------------------------------------------


class AdmissionStepId(StrEnum):
    """§7.3's six steps, named so a receipt can say which one decided."""

    STRUCTURE_AND_TYPES = "structure_and_types"
    REGISTRY_TYPE_CHECK = "registry_type_check"
    STRUCTURAL_CHECKS = "structural_checks"
    INDEPENDENT_PLAN_REVIEW = "independent_plan_review"
    FORMAL_MODELING = "formal_modeling"
    TRIAL_ADMISSION = "trial_admission"


class StepOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    #: The step belongs to a later work package; P2.1 records that rather than
    #: reporting a pass for a check nobody ran.
    DEFERRED = "DEFERRED"
    #: An earlier step already failed, so this one was never attempted.
    NOT_REACHED = "NOT_REACHED"


class AdmissionVerdict(StrEnum):
    TRIAL_ADMITTED = "TRIAL_ADMITTED"
    REJECTED = "REJECTED"
    PROMOTION_NOT_AVAILABLE = "PROMOTION_NOT_AVAILABLE"


class RejectionCode(StrEnum):
    """Why a submission was refused.  One code per class of defect."""

    MODEL_CLAIMED_STATUS = "MODEL_CLAIMED_STATUS"
    MALFORMED_DEFINITION = "MALFORMED_DEFINITION"
    UNKNOWN_TASK_TYPE = "UNKNOWN_TASK_TYPE"
    UNKNOWN_OPERATOR = "UNKNOWN_OPERATOR"
    FORM_MISMATCH = "FORM_MISMATCH"
    UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"
    UNKNOWN_PREDICATE = "UNKNOWN_PREDICATE"
    PREDICATE_TYPE_ERROR = "PREDICATE_TYPE_ERROR"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    UNBOUNDED_RECURSION = "UNBOUNDED_RECURSION"
    ORDERING_CYCLE = "ORDERING_CYCLE"
    PORT_UNAVAILABLE = "PORT_UNAVAILABLE"
    ROOT_COVERAGE_GAP = "ROOT_COVERAGE_GAP"
    SIZE_BOUND = "SIZE_BOUND"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"


#: P2.3q / P2-4.  Structured reason on a ``SIZE_BOUND`` problem that is a
#: synthesised method's step count, not a ports-per-step overflow.  The
#: correctable-reask path matches this token, never a detail substring.
SYNTHESIS_WIDTH_REASON = "synthesis_width"

#: P2.3t.  Structured reason on a ``ROOT_COVERAGE_GAP``: a criterion whose
#: evidence_requirement needs added/modified tests to pass, but no write-type
#: step declares a ``tests`` output port that a verify step binds.
SYNTHESIS_TESTS_PORT_REASON = "tests_port_required"
TESTS_OUTPUT_PORT = "tests"

_ADDED_TESTS_MARKERS = (
    "c-contract-tests-pass",
    "tests covering",
    "must be added or turned from red to green",
    "added or turned from red to green",
    "新增测试",
    "加入测试",
    "contract test",
)


def evidence_requires_added_tests(*parts: str) -> bool:
    """Whether the criterion text asks for tests to be added (not just re-run)."""

    blob = "\n".join(str(item) for item in parts).lower()
    return any(marker in blob for marker in _ADDED_TESTS_MARKERS)


@dataclass(frozen=True, slots=True)
class AdmissionProblem:
    code: RejectionCode
    detail: str
    step: AdmissionStepId
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", enum_of(RejectionCode, self.code, "problem.code"))
        object.__setattr__(self, "detail", text(self.detail, "problem.detail", limit=MAX_TEXT))
        object.__setattr__(self, "step", enum_of(AdmissionStepId, self.step, "problem.step"))
        object.__setattr__(self, "reason", str(self.reason or ""))


@dataclass(frozen=True, slots=True)
class AdmissionStepRecord:
    step: AdmissionStepId
    outcome: StepOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """§7.3: what the registry service decided, and on the basis of which steps.

    ``transitions`` is the status chain actually written, so a caller can see
    ``DRAFT → STRUCTURALLY_VALID → TRIAL_ADMITTED`` rather than infer it from the
    final value.  ``missing_capabilities`` / ``missing_operators`` are separate
    fields because "this deployment has no such tool" and "this method names a
    capability nobody declared" are different repairs.
    """

    method_ref: MethodRef
    verdict: AdmissionVerdict
    author: RegistryAuthor
    policy_ref: str
    policy_version: int
    mission_id: MissionRef | None
    steps: tuple[AdmissionStepRecord, ...]
    transitions: tuple[MethodRegistryStatus, ...]
    problems: tuple[AdmissionProblem, ...] = ()
    missing_operators: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    missing_predicates: tuple[str, ...] = ()
    recursive: bool = False
    registration: MethodRegistration | None = None

    @property
    def admitted(self) -> bool:
        return self.verdict is AdmissionVerdict.TRIAL_ADMITTED

    @property
    def status(self) -> MethodRegistryStatus:
        if self.transitions:
            return self.transitions[-1]
        return MethodRegistryStatus.REJECTED

    def codes(self) -> frozenset[RejectionCode]:
        return frozenset(problem.code for problem in self.problems)

    def receipt_ref(self) -> TypedRef:
        """A content-addressed reference the registration can point at."""

        payload = {
            "method_ref": self.method_ref.to_json(),
            "verdict": str(self.verdict),
            "policy_ref": self.policy_ref,
            "policy_version": self.policy_version,
            "mission_id": None if self.mission_id is None else str(self.mission_id),
            "problems": [
                {"code": str(item.code), "detail": item.detail, "step": str(item.step)}
                for item in self.problems
            ],
        }
        digest = content_hash_of(payload)
        return TypedRef(
            kind=TypedRefKind.REVIEW,
            id=f"method-admission-{digest[:32]}",
            revision=1,
            content_hash=digest,
        )


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """The versioned deployment facts one admission is decided against (ADR-08).

    Everything the protocol consults is here, so a receipt is reproducible: the
    same definition against the same policy version yields the same verdict.
    """

    policy_ref: str
    policy_version: int
    mission_id: MissionRef
    predicates: PredicateRegistry
    task_types: TaskTypeCatalog
    schemas: SchemaCatalog
    capabilities: CapabilitySnapshot
    max_steps: int = 64
    max_ports_per_step: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_ref", identifier(self.policy_ref, "policy.policy_ref"))
        object.__setattr__(
            self, "policy_version", index(self.policy_version, "policy.policy_version", minimum=1)
        )
        object.__setattr__(self, "mission_id", mission_ref(self.mission_id, "policy.mission_id"))
        if not isinstance(self.predicates, PredicateRegistry):
            raise ContractError("policy.predicates must be a PredicateRegistry")
        if not isinstance(self.task_types, TaskTypeCatalog):
            raise ContractError("policy.task_types must be a TaskTypeCatalog")
        if not isinstance(self.schemas, SchemaCatalog):
            raise ContractError("policy.schemas must be a SchemaCatalog")
        if not isinstance(self.capabilities, CapabilitySnapshot):
            raise ContractError("policy.capabilities must be a CapabilitySnapshot")
        object.__setattr__(self, "max_steps", index(self.max_steps, "policy.max_steps", minimum=1))
        object.__setattr__(
            self,
            "max_ports_per_step",
            index(self.max_ports_per_step, "policy.max_ports_per_step", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class MethodProposal:
    """A submission to the registry — model-authored or human-authored.

    The declared status travels with the submission precisely so it can be
    *refused*: §6.3 and §7.3 put the registry status outside the definition, and a
    model-authored payload that spells ``ADMITTED`` is exactly the bypass those
    sections exist to close.
    """

    method: MethodContract
    author: RegistryAuthor
    declared_status: MethodRegistryStatus = MethodRegistryStatus.DRAFT
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.method, MethodContract):
            raise ContractError("proposal.method must be a MethodContract")
        object.__setattr__(self, "author", enum_of(RegistryAuthor, self.author, "proposal.author"))
        object.__setattr__(
            self,
            "declared_status",
            enum_of(MethodRegistryStatus, self.declared_status, "proposal.declared_status"),
        )
        object.__setattr__(
            self,
            "rationale",
            text(self.rationale, "proposal.rationale", limit=MAX_TEXT) if self.rationale else "",
        )

    @classmethod
    def from_json(cls, value: object, name: str = "method_proposal") -> MethodProposal:
        """Decode a scripted ``method_proposal`` block (§18.5 C8).

        The block is model output, so the definition goes through
        :meth:`MethodContract.from_json` — which refuses callables, SQL fragments
        and unknown condition shapes — and the status is read as a *claim*, never
        applied.
        """

        data = fields_of(
            value,
            name,
            required=("method",),
            optional=("author", "registry_status", "rationale"),
        )
        return cls(
            method=MethodContract.from_json(data["method"], f"{name}.method"),
            author=data.get("author", RegistryAuthor.MODEL),
            declared_status=data.get("registry_status", MethodRegistryStatus.DRAFT),
            rationale=data.get("rationale", ""),
        )


@dataclass(frozen=True, slots=True)
class MethodCandidate:
    """One retrievable method offered for a goal type."""

    method: MethodContract
    method_ref: MethodRef
    registration: MethodRegistration
    trial_uses: int = 0

    @property
    def trial_scoped(self) -> bool:
        return self.registration.status is MethodRegistryStatus.TRIAL_ADMITTED


class SuggestionReason(StrEnum):
    """Why a method is *suggested* rather than offered.  Never an admission."""

    OTHER_VERSION_OF_SAME_GOAL_TYPE = "other_version_of_same_goal_type"
    SIMILAR_GOAL_STATEMENT = "similar_goal_statement"
    NOT_RETRIEVABLE_HERE = "not_retrievable_here"


@dataclass(frozen=True, slots=True)
class MethodSuggestion:
    """§8.3 / TG §12: lexical closeness produces a *suggestion*, nothing more."""

    method_ref: MethodRef
    reason: SuggestionReason
    detail: str
    similarity: float = 0.0
    #: Stated on every suggestion so no caller can read one as a decision.
    advisory_only: bool = True


def _statement_tokens(statement: str) -> frozenset[str]:
    return frozenset(
        token
        for token in "".join(
            character if character.isalnum() else " " for character in statement.lower()
        ).split()
        if token
    )


def statement_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the two statements' word sets.

    Deliberately crude and deliberately advisory: §8.3 says lexical or vector
    similarity may only produce a merge *candidate*, so the number exists to rank
    suggestions for a human or a planner, never to authorise a binding.
    """

    left_tokens = _statement_tokens(left)
    right_tokens = _statement_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


class MethodRegistry:
    """Immutable method definitions, their registrations and §7.3's protocol.

    Definitions are keyed by ``(method_id, version, content_hash)``: re-submitting
    the same bytes is idempotent, and changed bytes at the same version are refused
    rather than silently replacing a definition other instances already point at.
    """

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int, str], MethodContract] = {}
        self._registrations: dict[tuple[str, int, str], MethodRegistration] = {}
        self._receipts: dict[tuple[str, int, str], AdmissionReceipt] = {}
        self._trial_uses: dict[tuple[tuple[str, int, str], str], int] = {}
        #: goal type ``(id, version, content_hash)`` → the methods written for it, in
        #: insertion order.  Retrieval is on the hot path of every refinement, and a
        #: scan of every definition per frontier item is the wrong shape for it.
        self._by_goal_type: dict[tuple[str, int, str], list[tuple[str, int, str]]] = {}
        #: goal type id → every version of it that some method targets, for suggestions.
        self._by_goal_type_id: dict[str, list[tuple[str, int, str]]] = {}

    # -- storage ------------------------------------------------------------------

    @staticmethod
    def _key(ref: MethodRef) -> tuple[str, int, str]:
        return (ref.method_id, ref.version, ref.content_hash)

    def definition(self, ref: MethodRef) -> MethodContract | None:
        return self._definitions.get(self._key(ref))

    def registration(self, ref: MethodRef) -> MethodRegistration | None:
        return self._registrations.get(self._key(ref))

    def receipt(self, ref: MethodRef) -> AdmissionReceipt | None:
        return self._receipts.get(self._key(ref))

    def method_refs(self) -> tuple[MethodRef, ...]:
        return tuple(
            MethodRef(method_id=item[0], version=item[1], content_hash=item[2])
            for item in sorted(self._definitions)
        )

    # -- admission ----------------------------------------------------------------

    def admit(
        self,
        proposal: MethodProposal,
        *,
        author: RegistryAuthor,
        policy: AdmissionPolicy,
    ) -> AdmissionReceipt:
        """Run §7.3's six steps and write the resulting registration.

        The chain is walked in order and stops at the first failing step: a method
        whose operator does not exist has not earned a structural check, and a
        receipt that listed one would be describing work nobody did.
        """

        if not isinstance(proposal, MethodProposal):
            raise ContractError("admit expects a MethodProposal")
        if not isinstance(policy, AdmissionPolicy):
            raise ContractError("admit expects an AdmissionPolicy")
        author = enum_of(RegistryAuthor, author, "author")
        method = proposal.method
        ref = method.method_ref()
        steps: list[AdmissionStepRecord] = []
        problems: list[AdmissionProblem] = []
        transitions: list[MethodRegistryStatus] = [MethodRegistryStatus.DRAFT]

        # §6.3 / §7.3: the status is written by the registry service.  A model may
        # submit DRAFT and nothing else, and the claim is refused before any check
        # is run — otherwise a rejected claim could still leave a valid receipt.
        if author is RegistryAuthor.MODEL and proposal.declared_status not in (
            MethodRegistryStatus.DRAFT,
        ):
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.MODEL_CLAIMED_STATUS,
                    detail=(
                        f"a model-authored submission claimed registry_status "
                        f"{proposal.declared_status!s}; only DRAFT may be submitted (§7.3)"
                    ),
                    step=AdmissionStepId.STRUCTURE_AND_TYPES,
                )
            )
            return self._refuse(ref, author, policy, steps, problems, transitions)
        if proposal.author is not author:
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.MODEL_CLAIMED_STATUS,
                    detail=(
                        f"the submission declares author {proposal.author!s} but was "
                        f"presented as {author!s}; authorship is not self-asserted"
                    ),
                    step=AdmissionStepId.STRUCTURE_AND_TYPES,
                )
            )
            return self._refuse(ref, author, policy, steps, problems, transitions)

        existing = self._definitions.get(self._key(ref))
        if existing is not None and existing.to_json() != method.to_json():
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.ALREADY_REGISTERED,
                    detail=(
                        f"method {ref.method_id!r} v{ref.version} is registered with other "
                        "content at this hash"
                    ),
                    step=AdmissionStepId.STRUCTURE_AND_TYPES,
                )
            )
            return self._refuse(ref, author, policy, steps, problems, transitions)

        # Step 1 — structure, types, references, scope.
        step_one, step_one_missing_operators = _check_structure_and_types(method, policy)
        problems.extend(step_one)
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.STRUCTURE_AND_TYPES,
                outcome=StepOutcome.FAILED if step_one else StepOutcome.PASSED,
                detail="JSON / type / reference / scope check (§7.3 step 1)",
            )
        )
        if step_one:
            _fill_not_reached(steps, after=AdmissionStepId.STRUCTURE_AND_TYPES)
            return self._refuse(
                ref,
                author,
                policy,
                steps,
                problems,
                transitions,
                missing_operators=step_one_missing_operators,
            )

        # Step 2 — type check against the predicate registry and the capability table.
        step_two, missing_predicates, missing_capabilities, missing_operators = _check_registries(
            method, policy
        )
        problems.extend(step_two)
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.REGISTRY_TYPE_CHECK,
                outcome=StepOutcome.FAILED if step_two else StepOutcome.PASSED,
                detail="predicate / operator / capability type check (§7.3 step 2)",
            )
        )
        if step_two:
            _fill_not_reached(steps, after=AdmissionStepId.REGISTRY_TYPE_CHECK)
            return self._refuse(
                ref,
                author,
                policy,
                steps,
                problems,
                transitions,
                missing_operators=missing_operators,
                missing_capabilities=missing_capabilities,
                missing_predicates=missing_predicates,
            )

        # Step 3 — bounded expansion, acyclic partial order, ports, root coverage.
        step_three, recursive = _check_structure(method, policy)
        problems.extend(step_three)
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.STRUCTURAL_CHECKS,
                outcome=StepOutcome.FAILED if step_three else StepOutcome.PASSED,
                detail="bounded expansion / acyclic order / ports / root coverage (§7.3 step 3)",
            )
        )
        if step_three:
            _fill_not_reached(steps, after=AdmissionStepId.STRUCTURAL_CHECKS)
            return self._refuse(
                ref, author, policy, steps, problems, transitions, recursive=recursive
            )

        transitions.append(MethodRegistryStatus.STRUCTURALLY_VALID)

        # Steps 4 and 5 belong to later packages and are recorded as deferred, not
        # as passes: §7.3 step 4 is an independent planning review (a real model
        # call, which P2.1 does not make) and step 5 is PANDA (P2.2, and explicitly
        # "unsupported when unavailable", never a silent pass).
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.INDEPENDENT_PLAN_REVIEW,
                outcome=StepOutcome.DEFERRED,
                detail="independent planning review is a real review, delivered with P2.3",
            )
        )
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.FORMAL_MODELING,
                outcome=StepOutcome.DEFERRED,
                detail="HDDL export / PANDA verification is P2.2; unavailable is UNSUPPORTED",
            )
        )

        # Step 6 — trial admission, scoped to this mission (§7.3 v1.2).
        transitions.append(MethodRegistryStatus.TRIAL_ADMITTED)
        steps.append(
            AdmissionStepRecord(
                step=AdmissionStepId.TRIAL_ADMISSION,
                outcome=StepOutcome.PASSED,
                detail=f"trial admitted for mission {policy.mission_id!s} only (§7.3 step 6)",
            )
        )
        receipt = AdmissionReceipt(
            method_ref=ref,
            verdict=AdmissionVerdict.TRIAL_ADMITTED,
            author=author,
            policy_ref=policy.policy_ref,
            policy_version=policy.policy_version,
            mission_id=policy.mission_id,
            steps=tuple(steps),
            transitions=tuple(transitions),
            problems=(),
            recursive=recursive,
        )
        # The registration's ``author`` is who *wrote the row*, and past DRAFT that
        # is always the registry service: ``MethodRegistration`` refuses a
        # model-authored row beyond DRAFT precisely so a promoted status can never
        # be attributed to the submitter.  Who proposed the method is on the
        # receipt, which is what the registration points at.
        registration = admit_method(
            ref,
            MethodRegistryStatus.TRIAL_ADMITTED,
            author=RegistryAuthor.SYSTEM,
            admission_receipt_ref=receipt.receipt_ref(),
            trial_scope_mission=str(policy.mission_id),
        )
        receipt = AdmissionReceipt(
            method_ref=receipt.method_ref,
            verdict=receipt.verdict,
            author=receipt.author,
            policy_ref=receipt.policy_ref,
            policy_version=receipt.policy_version,
            mission_id=receipt.mission_id,
            steps=receipt.steps,
            transitions=receipt.transitions,
            problems=receipt.problems,
            recursive=receipt.recursive,
            registration=registration,
        )
        key = self._key(ref)
        self._definitions[key] = method
        self._registrations[key] = registration
        self._receipts[key] = receipt
        self._index(method, key)
        return receipt

    def _refuse(
        self,
        ref: MethodRef,
        author: RegistryAuthor,
        policy: AdmissionPolicy,
        steps: Sequence[AdmissionStepRecord],
        problems: Sequence[AdmissionProblem],
        transitions: Sequence[MethodRegistryStatus],
        *,
        missing_operators: Sequence[str] = (),
        missing_capabilities: Sequence[str] = (),
        missing_predicates: Sequence[str] = (),
        recursive: bool = False,
    ) -> AdmissionReceipt:
        """Write a REJECTED registration and return the receipt that explains it.

        The rejected definition is stored: §7.3 wants the refusal to be a fact one
        can point at, and a method that is merely dropped can be re-submitted
        unchanged forever without anyone noticing.
        """

        receipt = AdmissionReceipt(
            method_ref=ref,
            verdict=AdmissionVerdict.REJECTED,
            author=author,
            policy_ref=policy.policy_ref,
            policy_version=policy.policy_version,
            mission_id=policy.mission_id,
            steps=tuple(steps),
            transitions=(*tuple(transitions), MethodRegistryStatus.REJECTED),
            problems=tuple(problems),
            missing_operators=tuple(dict.fromkeys(missing_operators)),
            missing_capabilities=tuple(dict.fromkeys(missing_capabilities)),
            missing_predicates=tuple(dict.fromkeys(missing_predicates)),
            recursive=recursive,
        )
        registration = admit_method(
            ref,
            MethodRegistryStatus.REJECTED,
            author=RegistryAuthor.SYSTEM,
            admission_receipt_ref=receipt.receipt_ref(),
        )
        receipt = AdmissionReceipt(
            method_ref=receipt.method_ref,
            verdict=receipt.verdict,
            author=receipt.author,
            policy_ref=receipt.policy_ref,
            policy_version=receipt.policy_version,
            mission_id=receipt.mission_id,
            steps=receipt.steps,
            transitions=receipt.transitions,
            problems=receipt.problems,
            missing_operators=receipt.missing_operators,
            missing_capabilities=receipt.missing_capabilities,
            missing_predicates=receipt.missing_predicates,
            recursive=receipt.recursive,
            registration=registration,
        )
        key = self._key(ref)
        self._registrations[key] = registration
        self._receipts[key] = receipt
        return receipt

    # -- lifecycle ----------------------------------------------------------------

    def promote(
        self, ref: MethodRef, target: MethodRegistryStatus, *, policy: AdmissionPolicy
    ) -> AdmissionReceipt:
        """§7.3: P2 implements the lifecycle only as far as ``TRIAL_ADMITTED``.

        Everything beyond it — ``EVALUATED`` and ``ADMITTED`` — needs the offline
        multi-instance evaluation that P8 delivers, so this answers
        ``PROMOTION_NOT_AVAILABLE`` instead of writing a status the evidence does
        not support.  Succeeding once is not a promotion.
        """

        target = enum_of(MethodRegistryStatus, target, "target")
        registration = self.registration(ref)
        detail = (
            f"promotion to {target!s} needs the offline evaluation delivered with P8; "
            "one successful trial does not promote a method (§7.3)"
        )
        return AdmissionReceipt(
            method_ref=ref,
            verdict=AdmissionVerdict.PROMOTION_NOT_AVAILABLE,
            author=RegistryAuthor.SYSTEM,
            policy_ref=policy.policy_ref,
            policy_version=policy.policy_version,
            mission_id=policy.mission_id,
            steps=(
                AdmissionStepRecord(
                    step=AdmissionStepId.TRIAL_ADMISSION,
                    outcome=StepOutcome.DEFERRED,
                    detail=detail,
                ),
            ),
            transitions=(() if registration is None else (registration.status,)),
            registration=registration,
        )

    def suspend(self, ref: MethodRef, *, reason: str) -> MethodRegistration:
        """§7.3 v1.2: a counter-example suspends a method without erasing history.

        Existing instances keep their definition — that is why the definition stays
        in ``_definitions`` — but :meth:`candidates_for` stops offering it.  Nothing
        in P2.1 triggers this automatically; deciding *when* a counter-example
        suspends a method is the P8 elimination edge.
        """

        registration = self._require_registration(ref)
        updated = admit_method(
            ref,
            MethodRegistryStatus.SUSPENDED,
            author=RegistryAuthor.SYSTEM,
            admission_receipt_ref=registration.admission_receipt_ref,
        )
        text(reason, "reason", limit=MAX_TEXT)
        self._registrations[self._key(ref)] = updated
        return updated

    def reinstate(
        self, ref: MethodRef, *, mission_id: MissionRef, reason: str
    ) -> MethodRegistration:
        """Return a SUSPENDED method to its trial scope after a review."""

        registration = self._require_registration(ref)
        if registration.status is not MethodRegistryStatus.SUSPENDED:
            raise ContractError(
                f"method {ref.method_id!r} is {registration.status!s}, not SUSPENDED"
            )
        text(reason, "reason", limit=MAX_TEXT)
        updated = admit_method(
            ref,
            MethodRegistryStatus.TRIAL_ADMITTED,
            author=RegistryAuthor.SYSTEM,
            admission_receipt_ref=registration.admission_receipt_ref,
            trial_scope_mission=str(mission_ref(mission_id, "mission_id")),
        )
        self._registrations[self._key(ref)] = updated
        return updated

    def retire(self, ref: MethodRef, *, reason: str) -> MethodRegistration:
        """Retire a method.  History stays readable; retrieval stops."""

        registration = self._require_registration(ref)
        text(reason, "reason", limit=MAX_TEXT)
        updated = admit_method(
            ref,
            MethodRegistryStatus.RETIRED,
            author=RegistryAuthor.SYSTEM,
            admission_receipt_ref=registration.admission_receipt_ref,
        )
        self._registrations[self._key(ref)] = updated
        return updated

    def _index(self, method: MethodContract, key: tuple[str, int, str]) -> None:
        goal = method.goal_type_ref
        bucket = self._by_goal_type.setdefault((goal.id, goal.version, goal.content_hash), [])
        if key not in bucket:
            bucket.append(key)
        by_id = self._by_goal_type_id.setdefault(goal.id, [])
        if key not in by_id:
            by_id.append(key)

    def _require_registration(self, ref: MethodRef) -> MethodRegistration:
        registration = self.registration(ref)
        if registration is None:
            raise ContractError(f"method {ref.method_id!r} v{ref.version} is not registered")
        return registration

    # -- trial accounting ---------------------------------------------------------

    def note_trial_use(self, ref: MethodRef, *, mission_id: MissionRef) -> int:
        """§7.3 v1.2: trial counts follow the mission, not the plan revision."""

        key = (self._key(ref), str(mission_ref(mission_id, "mission_id")))
        self._trial_uses[key] = self._trial_uses.get(key, 0) + 1
        return self._trial_uses[key]

    def trial_uses(self, ref: MethodRef, *, mission_id: MissionRef) -> int:
        return self._trial_uses.get((self._key(ref), str(mission_ref(mission_id, "mission_id"))), 0)

    # -- retrieval ----------------------------------------------------------------

    def retrievable(self, ref: MethodRef, *, mission_id: MissionRef) -> bool:
        registration = self.registration(ref)
        if registration is None:
            return False
        if registration.status in GLOBALLY_RETRIEVABLE_STATUS:
            return True
        # Normalised here rather than trusted from the caller: the trial scope is a
        # mission identity, and comparing a raw string against a validated one is how
        # a blank or control-character mission id would quietly match nothing.
        mission = mission_ref(mission_id, "mission_id")
        return (
            registration.status is MethodRegistryStatus.TRIAL_ADMITTED
            and registration.trial_scope_mission == str(mission)
        )

    def candidates_for(
        self, goal_type_ref: VersionedRef, *, mission_id: MissionRef
    ) -> tuple[MethodCandidate, ...]:
        """Methods offered for exactly this goal type, in a deterministic order.

        Matching is on the full ``(id, version, content_hash)`` of the goal type:
        a method written against another revision of the same goal is a
        *suggestion* (:meth:`suggest_for`), not a candidate, because the parameter
        and coverage contract it was checked against is a different one.
        """

        mission = mission_ref(mission_id, "mission_id")
        out: list[MethodCandidate] = []
        offered = self._by_goal_type.get(
            (goal_type_ref.id, goal_type_ref.version, goal_type_ref.content_hash), []
        )
        for key in sorted(offered):
            method = self._definitions[key]
            ref = MethodRef(method_id=key[0], version=key[1], content_hash=key[2])
            if not self.retrievable(ref, mission_id=mission):
                continue
            registration = self._registrations[key]
            out.append(
                MethodCandidate(
                    method=method,
                    method_ref=ref,
                    registration=registration,
                    trial_uses=self.trial_uses(ref, mission_id=mission),
                )
            )
        return tuple(out)

    def suggest_for(
        self,
        goal_type_ref: VersionedRef,
        *,
        mission_id: MissionRef,
        statement: str = "",
        minimum_similarity: float = 0.5,
    ) -> tuple[MethodSuggestion, ...]:
        """Advisory near-matches.  §8.3: similarity produces candidates, not bindings.

        Every entry carries ``advisory_only=True`` and no caller in this package
        ever turns one into a :class:`MethodCandidate`; a human or a planner has to
        publish a method against the real goal type first.
        """

        mission = mission_ref(mission_id, "mission_id")
        out: list[MethodSuggestion] = []
        for key in sorted(self._definitions):
            method = self._definitions[key]
            ref = MethodRef(method_id=key[0], version=key[1], content_hash=key[2])
            if method.goal_type_ref == goal_type_ref:
                if not self.retrievable(ref, mission_id=mission):
                    out.append(
                        MethodSuggestion(
                            method_ref=ref,
                            reason=SuggestionReason.NOT_RETRIEVABLE_HERE,
                            detail=(
                                f"registered as {self._registrations[key].status!s}; "
                                "not offered to this mission's search"
                            ),
                        )
                    )
                continue
            if method.goal_type_ref.id == goal_type_ref.id:
                out.append(
                    MethodSuggestion(
                        method_ref=ref,
                        reason=SuggestionReason.OTHER_VERSION_OF_SAME_GOAL_TYPE,
                        detail=(
                            f"written against {method.goal_type_ref.id!r} "
                            f"v{method.goal_type_ref.version}, not v{goal_type_ref.version}"
                        ),
                        similarity=1.0,
                    )
                )
                continue
            if statement:
                similarity = statement_similarity(statement, method.goal_type_ref.id)
                if similarity >= minimum_similarity:
                    out.append(
                        MethodSuggestion(
                            method_ref=ref,
                            reason=SuggestionReason.SIMILAR_GOAL_STATEMENT,
                            detail="lexical overlap only; never an automatic binding (§8.3)",
                            similarity=similarity,
                        )
                    )
        return tuple(out)


# --------------------------------------------------------------------------------------
# The individual admission checks
# --------------------------------------------------------------------------------------


def _fill_not_reached(steps: list[AdmissionStepRecord], *, after: AdmissionStepId) -> None:
    order = list(AdmissionStepId)
    position = order.index(after)
    for step in order[position + 1 :]:
        steps.append(
            AdmissionStepRecord(
                step=step,
                outcome=StepOutcome.NOT_REACHED,
                detail="an earlier step failed; this one was not attempted",
            )
        )


def iter_conditions(condition: Any) -> tuple[Any, ...]:
    """Every node of one condition tree, parents before children."""

    if isinstance(condition, (AllCondition, AnyCondition)):
        nested: list[Any] = [condition]
        for item in condition.items:
            nested.extend(iter_conditions(item))
        return tuple(nested)
    if isinstance(condition, NotCondition):
        return (condition, *iter_conditions(condition.item))
    return (condition,)


def iter_predicates(conditions: Sequence[Any]) -> tuple[PredicateCondition, ...]:
    out: list[PredicateCondition] = []
    for condition in conditions:
        for node in iter_conditions(condition):
            if isinstance(node, PredicateCondition):
                out.append(node)
    return tuple(out)


def iter_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, ObjectValue):
        nested: list[Any] = [value]
        for item in value.fields.values():
            nested.extend(iter_values(item))
        return tuple(nested)
    if isinstance(value, ArrayValue):
        nested = [value]
        for item in value.items:
            nested.extend(iter_values(item))
        return tuple(nested)
    return (value,)


def method_parameter_names(method: MethodContract, policy: AdmissionPolicy) -> frozenset[str]:
    schema = policy.schemas.resolve(method.parameter_schema_ref)
    if schema is None:
        return frozenset()
    return frozenset(item.name for item in schema.fields)


def _check_structure_and_types(
    method: MethodContract, policy: AdmissionPolicy
) -> tuple[list[AdmissionProblem], list[str]]:
    """§7.3 step 1: JSON, types, references and scope.

    Returns the problems and, separately, the operators this deployment does not
    have — "the method is malformed" and "this machine cannot run it" are different
    repairs and the receipt keeps them apart.
    """

    problems: list[AdmissionProblem] = []
    missing_operators: list[str] = []
    step = AdmissionStepId.STRUCTURE_AND_TYPES

    def refuse(code: RejectionCode, detail: str, *, reason: str = "") -> None:
        problems.append(AdmissionProblem(code=code, detail=detail, step=step, reason=reason))

    try:
        # Belt and braces: the codec already refuses executables, but a contract
        # built in process has not been through it.
        reject_executable(method.to_json(), "method_contract")
    except ContractError as error:
        refuse(RejectionCode.MALFORMED_DEFINITION, str(error))
        return problems, missing_operators

    if not method.steps:
        refuse(
            RejectionCode.MALFORMED_DEFINITION,
            "a method with no steps refines nothing; use an operator instead",
        )
    if len(method.steps) > policy.max_steps:
        refuse(
            RejectionCode.SIZE_BOUND,
            f"the method declares {len(method.steps)} steps, above the policy's {policy.max_steps}",
            reason=SYNTHESIS_WIDTH_REASON,
        )

    goal_type = policy.task_types.resolve(method.goal_type_ref)
    if goal_type is None:
        refuse(
            RejectionCode.UNKNOWN_TASK_TYPE,
            f"goal type {method.goal_type_ref.id!r} v{method.goal_type_ref.version} is not "
            "registered at that content hash",
        )
    elif goal_type.form is not TaskForm.COMPOUND:
        refuse(
            RejectionCode.FORM_MISMATCH,
            f"goal type {method.goal_type_ref.id!r} is primitive; only a compound goal is "
            "refined by a method (§6.2)",
        )

    for label, ref in (
        ("parameter_schema_ref", method.parameter_schema_ref),
        ("output_schema_ref", method.output_schema_ref),
    ):
        if policy.schemas.resolve(ref) is None:
            refuse(
                RejectionCode.UNKNOWN_SCHEMA,
                f"method.{label} names schema {ref.id!r} v{ref.version}, which is not "
                "registered at that content hash",
            )

    declared = method_parameter_names(method, policy)
    locals_seen = {item.local_id for item in method.steps}
    for step_spec in method.steps:
        spec = policy.task_types.resolve(step_spec.task_type_ref)
        if spec is None:
            # For a primitive step the task type *is* the operator, so say so:
            # §6.4 requires an atomic Operator to be really registered, and
            # "unknown task type" would send the reader looking for a goal.
            if step_spec.form is TaskForm.PRIMITIVE:
                missing_operators.append(step_spec.task_type_ref.id)
                refuse(
                    RejectionCode.UNKNOWN_OPERATOR,
                    f"step {step_spec.local_id!r} needs operator "
                    f"{step_spec.task_type_ref.id!r} v{step_spec.task_type_ref.version}, "
                    "which no registered task type provides; this deployment cannot "
                    "execute the method (§7.3 step 2)",
                )
            else:
                refuse(
                    RejectionCode.UNKNOWN_TASK_TYPE,
                    f"step {step_spec.local_id!r} names task type "
                    f"{step_spec.task_type_ref.id!r} v{step_spec.task_type_ref.version}, "
                    "which is not registered",
                )
            continue
        if spec.form is not step_spec.form:
            refuse(
                RejectionCode.FORM_MISMATCH,
                f"step {step_spec.local_id!r} declares form {step_spec.form!s} but task type "
                f"{step_spec.task_type_ref.id!r} is {spec.form!s}",
            )
        if step_spec.form is TaskForm.PRIMITIVE and spec.operator_ref is None:
            refuse(
                RejectionCode.UNKNOWN_OPERATOR,
                f"step {step_spec.local_id!r} is primitive but task type "
                f"{spec.task_type_ref.id!r} registers no operator",
            )
        # §8.3 / TG §12: a method may declare that a slot's work is reusable, but it
        # may not declare that about work the *type* says has an unshareable effect.
        if (
            step_spec.reuse_policy is not None
            and step_spec.reuse_policy is not ReusePolicy.NEW_WORK
            and not spec.read_only
            and spec.effect_identity is None
        ):
            refuse(
                RejectionCode.MALFORMED_DEFINITION,
                f"step {step_spec.local_id!r} declares reuse policy "
                f"{step_spec.reuse_policy!s}, but task type {spec.task_type_ref.id!r} "
                "writes outside the orchestrator and declares no effect_identity; two "
                "such actions are two actions (§8.3)",
            )
        for name, argument in step_spec.arguments.items():
            for node in iter_values(argument):
                if isinstance(node, ParameterValue) and declared and node.name not in declared:
                    refuse(
                        RejectionCode.MALFORMED_DEFINITION,
                        f"step {step_spec.local_id!r} argument {name!r} reads parameter "
                        f"{node.name!r}, which the method's parameter schema does not declare",
                    )
                if isinstance(node, OutputValue):
                    if node.step not in locals_seen:
                        refuse(
                            RejectionCode.MALFORMED_DEFINITION,
                            f"step {step_spec.local_id!r} argument {name!r} reads the output "
                            f"of unknown step {node.step!r}",
                        )
                    elif node.step == step_spec.local_id:
                        refuse(
                            RejectionCode.MALFORMED_DEFINITION,
                            f"step {step_spec.local_id!r} argument {name!r} reads its own output",
                        )
    return problems, missing_operators


def _check_registries(
    method: MethodContract, policy: AdmissionPolicy
) -> tuple[list[AdmissionProblem], list[str], list[str], list[str]]:
    """§7.3 step 2: the predicate registry, the operator table and the capability table."""

    problems: list[AdmissionProblem] = []
    step = AdmissionStepId.REGISTRY_TYPE_CHECK
    missing_predicates: list[str] = []
    missing_capabilities: list[str] = []
    missing_operators: list[str] = []
    declared = method_parameter_names(method, policy)
    schema = policy.schemas.resolve(method.parameter_schema_ref)
    field_types = {} if schema is None else {item.name: item.type for item in schema.fields}

    all_conditions = (
        *method.applicable_when,
        *method.exploration_assumptions,
        *method.expected_effects,
    )
    for atom in iter_predicates(all_conditions):
        signature = policy.predicates.resolve(atom.predicate_ref)
        if signature is None:
            missing_predicates.append(f"{atom.predicate_ref.id}@{atom.predicate_ref.version}")
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.UNKNOWN_PREDICATE,
                    detail=(
                        f"condition names predicate {atom.predicate_ref.id!r} "
                        f"v{atom.predicate_ref.version}, which is not registered"
                    ),
                    step=step,
                )
            )
            continue
        parameters = {item.name: item for item in signature.parameters}
        for name, argument in atom.arguments.items():
            parameter = parameters.get(name)
            if parameter is None:
                problems.append(
                    AdmissionProblem(
                        code=RejectionCode.PREDICATE_TYPE_ERROR,
                        detail=(f"predicate {atom.predicate_ref.id!r} has no argument {name!r}"),
                        step=step,
                    )
                )
                continue
            if isinstance(argument, ConstantValue):
                if not value_matches(parameter.type, argument.value):
                    problems.append(
                        AdmissionProblem(
                            code=RejectionCode.PREDICATE_TYPE_ERROR,
                            detail=(
                                f"predicate {atom.predicate_ref.id!r} argument {name!r} "
                                f"expects {parameter.type!s}"
                            ),
                            step=step,
                        )
                    )
            elif isinstance(argument, ParameterValue):
                if declared and argument.name not in declared:
                    problems.append(
                        AdmissionProblem(
                            code=RejectionCode.PREDICATE_TYPE_ERROR,
                            detail=(
                                f"condition reads parameter {argument.name!r}, which the "
                                "method's parameter schema does not declare"
                            ),
                            step=step,
                        )
                    )
                elif (
                    argument.name in field_types
                    and field_types[argument.name] is not parameter.type
                ):
                    problems.append(
                        AdmissionProblem(
                            code=RejectionCode.PREDICATE_TYPE_ERROR,
                            detail=(
                                f"parameter {argument.name!r} is "
                                f"{field_types[argument.name]!s} but predicate "
                                f"{atom.predicate_ref.id!r} wants {parameter.type!s} for "
                                f"{name!r}"
                            ),
                            step=step,
                        )
                    )
            elif isinstance(argument, OutputValue):
                problems.append(
                    AdmissionProblem(
                        code=RejectionCode.PREDICATE_TYPE_ERROR,
                        detail=(
                            "a method condition may not read a step output; at selection "
                            "time it does not exist (§6.6)"
                        ),
                        step=step,
                    )
                )

    required = list(
        dict.fromkeys(
            [*method.required_capabilities]
            + [item for step_spec in method.steps for item in step_spec.required_capabilities]
        )
    )
    for capability in required:
        if policy.capabilities.lookup(capability) is None:
            missing_capabilities.append(capability)
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.UNKNOWN_CAPABILITY,
                    detail=(
                        f"capability {capability!r} is not declared by this deployment; "
                        "the method is not executable here (§7.3 step 2)"
                    ),
                    step=step,
                )
            )

    for step_spec in method.steps:
        if step_spec.form is not TaskForm.PRIMITIVE:
            continue
        spec = policy.task_types.resolve(step_spec.task_type_ref)
        if spec is None or spec.operator_ref is None:
            missing_operators.append(step_spec.task_type_ref.id)
            problems.append(
                AdmissionProblem(
                    code=RejectionCode.UNKNOWN_OPERATOR,
                    detail=(
                        f"step {step_spec.local_id!r} needs operator "
                        f"{step_spec.task_type_ref.id!r}, which no registered task type "
                        "provides"
                    ),
                    step=step,
                )
            )
            continue
        for capability in spec.required_capabilities:
            if policy.capabilities.lookup(capability) is None and (
                capability not in missing_capabilities
            ):
                missing_capabilities.append(capability)
                problems.append(
                    AdmissionProblem(
                        code=RejectionCode.UNKNOWN_CAPABILITY,
                        detail=(
                            f"operator {spec.task_type_ref.id!r} needs capability "
                            f"{capability!r}, which this deployment does not declare"
                        ),
                        step=step,
                    )
                )
    return problems, missing_predicates, missing_capabilities, missing_operators


def effective_reuse_policy(step: MethodStep, spec: TaskTypeSpec) -> ReusePolicy:
    """What de-duplication this slot permits (``method-contract-v1``, §8.3).

    The step's own declaration wins when it makes one; ``None`` means "take the
    default from the task type", which the contract deliberately leaves to the
    caller that knows the default rather than resolving it by omission.
    """

    if step.reuse_policy is not None:
        return step.reuse_policy
    return spec.reuse_policy


def method_is_recursive(method: MethodContract) -> bool:
    """Does the method contain a step of its own goal type?

    A recursive *definition* is legitimate (TG §3.3 / implementation design §3.3);
    what must be bounded is the expansion, which the obligation ledger's fuel and
    the repeat-expansion key handle at refinement time.
    """

    return any(step.task_type_ref == method.goal_type_ref for step in method.steps)


def implied_orderings(method: MethodContract) -> tuple[tuple[str, str], ...]:
    """Data flow inside a method also orders its steps.

    A step whose argument reads ``OutputValue(step=X, port=p)`` cannot start before
    ``X`` produced ``p``, so the compiler emits a DATA edge and the projection
    turns that into ``X.exit → this.entry``.  Returning the pairs here means the
    admission-time cycle check sees the same ordering the execution DAG will.
    """

    out: list[tuple[str, str]] = []
    for step in method.steps:
        for argument in step.arguments.values():
            for node in iter_values(argument):
                if isinstance(node, OutputValue) and node.step != step.local_id:
                    pair = (node.step, step.local_id)
                    if pair not in out:
                        out.append(pair)
    return tuple(out)


def _check_structure(
    method: MethodContract, policy: AdmissionPolicy
) -> tuple[list[AdmissionProblem], bool]:
    """§7.3 step 3: bounded expansion, acyclic order, usable ports, root coverage."""

    problems: list[AdmissionProblem] = []
    step_id = AdmissionStepId.STRUCTURAL_CHECKS
    recursive = method_is_recursive(method)

    def refuse(code: RejectionCode, detail: str, *, reason: str = "") -> None:
        problems.append(
            AdmissionProblem(code=code, detail=detail, step=step_id, reason=reason)
        )

    # Bounded expansion.  Recursion is allowed; recursion with nothing that can ever
    # stop it is not — a method that always re-expands its own goal and never says
    # under what condition would exhaust the obligation's fuel on every mission and
    # report BOUND_REACHED forever, which is a defect in the method, not a bound.
    if recursive:
        if not method.applicable_when:
            refuse(
                RejectionCode.UNBOUNDED_RECURSION,
                "a recursive method needs at least one applicability condition to act as a "
                "termination guard; without one every expansion re-expands the same goal",
            )
        if all(step.task_type_ref == method.goal_type_ref for step in method.steps):
            refuse(
                RejectionCode.UNBOUNDED_RECURSION,
                "every step of this recursive method re-expands its own goal type; no "
                "expansion makes progress",
            )

    # Acyclic partial order, over declared ORDER plus the ordering DATA implies.
    pairs = [(order.before, order.after) for order in method.ordering]
    pairs.extend(implied_orderings(method))
    cycle = _first_cycle(pairs, tuple(item.local_id for item in method.steps))
    if cycle:
        refuse(
            RejectionCode.ORDERING_CYCLE,
            "the method's partial order is cyclic: " + " -> ".join(cycle),
        )

    # Ports: an OutputValue must name a port the producing step's type declares, and
    # the consuming argument must be a port the consuming step's type declares.
    by_local = {item.local_id: item for item in method.steps}
    for step_spec in method.steps:
        consumer = policy.task_types.resolve(step_spec.task_type_ref)
        for name, argument in step_spec.arguments.items():
            for node in iter_values(argument):
                if not isinstance(node, OutputValue):
                    continue
                producer_step = by_local.get(node.step)
                if producer_step is None:
                    continue
                producer = policy.task_types.resolve(producer_step.task_type_ref)
                if producer is not None and producer.output_port(node.port) is None:
                    refuse(
                        RejectionCode.PORT_UNAVAILABLE,
                        f"step {step_spec.local_id!r} reads output port {node.port!r} of "
                        f"{node.step!r}, which task type "
                        f"{producer_step.task_type_ref.id!r} does not declare",
                    )
                if consumer is not None and consumer.input_port(name) is None:
                    refuse(
                        RejectionCode.PORT_UNAVAILABLE,
                        f"step {step_spec.local_id!r} binds input port {name!r}, which task "
                        f"type {step_spec.task_type_ref.id!r} does not declare",
                    )
        if consumer is not None and len(consumer.input_ports) > policy.max_ports_per_step:
            refuse(
                RejectionCode.SIZE_BOUND,
                f"task type {consumer.task_type_ref.id!r} declares "
                f"{len(consumer.input_ports)} input ports, above the policy's "
                f"{policy.max_ports_per_step}",
            )

    # Root coverage: every criterion the goal type publishes must be linked to a
    # child criterion.  §8.1: all leaves passing does not by itself satisfy the
    # parent, and an uncovered criterion is exactly how a decomposition loses the
    # half of the requirement nobody wrote a step for.
    goal_type = policy.task_types.resolve(method.goal_type_ref)
    if goal_type is not None:
        wanted = set(goal_type.goal_signature.coverage_criteria)
        covered = {link.parent_criterion_id for link in method.composition.criterion_links}
        missing = sorted(wanted - covered)
        if missing:
            refuse(
                RejectionCode.ROOT_COVERAGE_GAP,
                f"the composition covers none of criteria {', '.join(missing)} of goal type "
                f"{method.goal_type_ref.id!r}",
            )
        for link in method.composition.criterion_links:
            if link.child_step is not None and link.child_step not in by_local:
                refuse(
                    RejectionCode.MALFORMED_DEFINITION,
                    f"criterion link for {link.parent_criterion_id!r} names unknown step "
                    f"{link.child_step!r}",
                )
    _check_tests_port_coverage(method, policy, refuse)
    return problems, recursive


def _check_tests_port_coverage(
    method: MethodContract,
    policy: AdmissionPolicy,
    refuse: Any,
) -> None:
    """P2.3t: added-tests evidence cannot hang only on a read-only verify report.

    A write-type step must declare :data:`TESTS_OUTPUT_PORT` and a consumer
    (typically verify) must bind it.  Optional on every other method.
    """

    texts = [
        str(link.parent_criterion_id) for link in method.composition.criterion_links
    ]
    texts.extend(str(link.evidence_requirement) for link in method.composition.criterion_links)
    goal_type = policy.task_types.resolve(method.goal_type_ref)
    if goal_type is not None:
        texts.append(str(goal_type.goal_signature.statement))
        texts.extend(str(item) for item in goal_type.goal_signature.coverage_criteria)
    if not evidence_requires_added_tests(*texts):
        return
    write_declares = False
    consumed = False
    for step_spec in method.steps:
        producer = policy.task_types.resolve(step_spec.task_type_ref)
        if producer is not None and not producer.read_only:
            if producer.output_port(TESTS_OUTPUT_PORT) is not None:
                write_declares = True
        for argument in step_spec.arguments.values():
            for node in iter_values(argument):
                if isinstance(node, OutputValue) and node.port == TESTS_OUTPUT_PORT:
                    consumed = True
    if write_declares and consumed:
        return
    refuse(
        RejectionCode.ROOT_COVERAGE_GAP,
        "a criterion that requires added or modified tests to pass is not evidenced "
        "by a write-type step declaring output port 'tests' bound into a verify step; "
        "a read-only verify leaf cannot create those files",
        reason=SYNTHESIS_TESTS_PORT_REASON,
    )


def _first_cycle(pairs: Sequence[tuple[str, str]], nodes: Sequence[str]) -> tuple[str, ...]:
    """One concrete cycle over ``pairs``, or ``()`` when the order is acyclic.

    A small depth-limited walk rather than a second Kahn implementation: the graph
    here is one method's steps, and reporting the actual loop is what makes the
    refusal actionable.
    """

    successors: dict[str, list[str]] = {node: [] for node in nodes}
    for before, after in pairs:
        successors.setdefault(before, []).append(after)
        successors.setdefault(after, [])
    colour: dict[str, int] = {node: 0 for node in successors}
    path: list[str] = []

    def walk(node: str) -> tuple[str, ...]:
        colour[node] = 1
        path.append(node)
        for successor in sorted(successors[node]):
            if colour.get(successor, 0) == 1:
                start = path.index(successor)
                return (*path[start:], successor)
            if colour.get(successor, 0) == 0:
                found = walk(successor)
                if found:
                    return found
        path.pop()
        colour[node] = 2
        return ()

    for node in sorted(successors):
        if colour[node] == 0:
            found = walk(node)
            if found:
                return found
    return ()


def condition_truth_is_constant(condition: Any) -> TruthValue | None:
    """``TRUE``/``FALSE`` for a literal condition, ``None`` for everything else."""

    if isinstance(condition, ConstantCondition):
        return TruthValue.TRUE if condition.value else TruthValue.FALSE
    return None


__all__ = (
    "GLOBALLY_RETRIEVABLE_STATUS",
    "READ_ONLY_EFFECTS",
    "AdmissionPolicy",
    "AdmissionProblem",
    "AdmissionReceipt",
    "AdmissionStepId",
    "AdmissionStepRecord",
    "AdmissionVerdict",
    "MethodCandidate",
    "MethodProposal",
    "MethodRegistry",
    "MethodSuggestion",
    "ObjectSchema",
    "RejectionCode",
    "SYNTHESIS_TESTS_PORT_REASON",
    "SYNTHESIS_WIDTH_REASON",
    "TESTS_OUTPUT_PORT",
    "evidence_requires_added_tests",
    "SchemaCatalog",
    "SchemaField",
    "StepOutcome",
    "SuggestionReason",
    "TaskTypeCatalog",
    "TaskTypeSpec",
    "condition_truth_is_constant",
    "effective_reuse_policy",
    "implied_orderings",
    "iter_conditions",
    "iter_predicates",
    "iter_values",
    "method_is_recursive",
    "method_parameter_names",
    "statement_similarity",
    "value_matches",
)
