# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""DataRequirement -> BoundInput -> InputManifest, resolved as a pure function (P2.2b).

Plan §24.1 decision 3 and annex TG §4.3 / §10.1-10.3 replace "an Attempt starts
from every accepted artifact of every ancestor" with a declared contract:

``DataRequirement``
    what the planner promised: *this* producer occurrence's *this* output port
    feeds *that* consumer's *that* input port, under a named schema, assurance
    and freshness policy.
``ResolvedInputBinding``
    that promise resolved, before dispatch, to one exact identity — result id,
    acceptance id + support revision, artifact id + content hash.  The annex is
    explicit that ``latest`` may never be left for the Worker to interpret.
``InputManifest``
    the immutable set of those bindings, with a canonical hash that
    ``Acceptance.input_manifest_hash`` can quote.

What this module deliberately does **not** do, so the two modes stay apart until
P2.3b wires them together:

* it never reads a file, a workspace or a content-addressed store — the plan it
  produces says *where* bytes should go, and something else puts them there;
* it never touches :mod:`.versioning`; the legacy all-ancestors merge keeps its
  bytes and its ``ArtifactConflict`` semantics (plan §18.2, §24.1 decision 4);
* it never propagates ORDER.  An ORDER-only predecessor has no DataRequirement,
  so it cannot appear in a manifest however many artifacts it accepted — which
  is the whole point of TG §10.1;
* it never *infers* schema compatibility.  Exact identity, or a registered
  declaration, or a refusal.  No containment proof, no ``Any``.

Every refusal is a :class:`ResolutionProblem` with its own kind, because the
caller has to act differently on each: an ambiguous single-valued port needs a
human choice or a synthesis Task, an unordered set port needs a declaration, an
unusable witness needs a re-check.  A single boolean would hide all three.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts.evidence_state import ValidityWitness, WitnessDecision, WitnessPurpose
from ..contracts.htn import (
    BoundInput,
    DataRequirement,
    OccurrenceId,
    PortCardinality,
    PortOrdering,
    PortSpec,
    SourceRevisionPolicy,
    TaskRef,
    TaskSemanticBindingV1,
)
from ..contracts.models import ContractError, sha256_hex
from ..contracts.semantic_base import TypedRefKind, VersionedRef
from .paths import normalise_workspace_path

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


class DisclosureState(StrEnum):
    """TG §4.3: revocation, deletion and purpose limits are checked *now*.

    ``PINNED`` pins a revision, never a permission — a pinned input whose source
    was withdrawn is not readable because it used to be.
    """

    DISCLOSABLE = "disclosable"
    REVOKED = "revoked"
    DELETED = "deleted"
    PURPOSE_RESTRICTED = "purpose_restricted"


class ResolutionProblemKind(StrEnum):
    """One value per class of refusal.  Never collapsed into a boolean."""

    AMBIGUOUS_SINGLE_PORT = "ambiguous_single_port"
    SET_PORT_UNORDERED = "set_port_unordered"
    SET_PORT_ORDER_INCOMPLETE = "set_port_order_incomplete"
    UNDECLARED_PORT = "undeclared_port"
    FOREIGN_REQUIREMENT = "foreign_requirement"
    UNBOUND_REQUIRED_PORT = "unbound_required_port"
    SCHEMA_MISMATCH = "schema_mismatch"
    NOT_DISCLOSABLE = "not_disclosable"
    WITNESS_MISSING = "witness_missing"
    WITNESS_NOT_USABLE = "witness_not_usable"
    WITNESS_PURPOSE_MISMATCH = "witness_purpose_mismatch"
    WITNESS_CONSUMER_MISMATCH = "witness_consumer_mismatch"
    WITNESS_SUPPORT_MISMATCH = "witness_support_mismatch"
    WITNESS_EPOCH_UNKNOWN = "witness_epoch_unknown"
    PENDING_PRODUCER = "pending_producer"
    PROVISIONAL_NOT_AUTHORIZED = "provisional_not_authorized"
    REVISION_NOT_AVAILABLE = "revision_not_available"
    TARGET_PATH_CONFLICT = "target_path_conflict"
    TARGET_PATH_INVALID = "target_path_invalid"


@dataclass(frozen=True, slots=True)
class ResolutionProblem:
    """One refusal.

    ``input_ports`` is the full set of ports a problem touches; ``input_port`` is
    the convenience for the common single-port case and is folded into
    ``input_ports`` automatically.  A target-path conflict spans several ports, so
    it fills ``input_ports`` and leaves the singular field empty rather than
    naming one port and hiding the rest.
    """

    kind: ResolutionProblemKind
    detail: str
    input_port: str | None = None
    requirement_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    input_ports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.input_ports and self.input_port is not None:
            object.__setattr__(self, "input_ports", (self.input_port,))
        else:
            object.__setattr__(self, "input_ports", tuple(self.input_ports))

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "detail": self.detail,
            "input_port": self.input_port,
            "input_ports": list(self.input_ports),
            "requirement_ids": list(self.requirement_ids),
            "candidates": list(self.candidates),
        }


class ManifestNotFrozen(RuntimeError):
    """The manifest still holds a symbolic binding, so it has no dispatch identity.

    Implementation annex §4.3: freezing a dispatch intent means every input is an
    exact identity.  A manifest that still says "whatever occ-b eventually
    accepts" has no hash an Acceptance could quote and nothing a Worker could be
    handed, so both are refused rather than approximated.
    """


# --------------------------------------------------------------------------------------
# Resource identity (TG §10.2: at least (namespace/workspace/object-id, normalised path))
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Where a byte string lives.  The relative path alone is **not** an identity.

    ``attempt-A/report.md`` and ``attempt-B/report.md`` are two resources: two
    isolated namespaces that happen to agree on a relative name.  Collapsing them
    because the tail matches is exactly the independent-exploration bug TG §10.2
    names.
    """

    namespace: str
    path: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise ContractError("resource.namespace must be a non-blank string")
        if not isinstance(self.path, str) or not self.path.strip():
            raise ContractError("resource.path must be a non-blank string")
        object.__setattr__(self, "path", normalise_workspace_path(self.path))
        if not self.path:
            raise ContractError("resource.path normalises to nothing")

    @property
    def is_escaping(self) -> bool:
        """``..`` survives normalisation on purpose, so the caller can refuse it."""

        return ".." in self.path.split("/")

    def collation_key(self, *, case_insensitive: bool) -> tuple[str, str]:
        """The key two identities collide on under a given platform rule."""

        if case_insensitive:
            return (self.namespace.casefold(), self.path.casefold())
        return (self.namespace, self.path)

    def to_json(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "path": self.path}


# --------------------------------------------------------------------------------------
# What the resolver is given
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceptedOutput:
    """One accepted contribution at one producer occurrence's output port."""

    producer_occurrence: OccurrenceId
    producer_task_ref: TaskRef
    output_port: str
    producer_result_id: str
    acceptance_id: str
    support_revision: int
    artifact_id: str
    content_hash: str
    schema_ref: VersionedRef
    source_revision: str
    source_identity: ResourceIdentity
    producer_ordinal: int = 0
    order_keys: Mapping[str, str] = field(default_factory=dict)
    disclosure: DisclosureState = DisclosureState.DISCLOSABLE
    disclosure_scope: str = "mission"
    provisional: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.schema_ref, VersionedRef):
            raise ContractError("accepted_output.schema_ref must be a VersionedRef")
        if not isinstance(self.source_identity, ResourceIdentity):
            raise ContractError("accepted_output.source_identity must be a ResourceIdentity")
        object.__setattr__(self, "order_keys", dict(self.order_keys))


@dataclass(frozen=True, slots=True)
class AcceptedOutputsIndex:
    """The accepted outputs the resolver may choose from, plus who has finished.

    ``completed_producers`` is separate from the outputs because "this producer
    accepted nothing at that port" and "this producer has not finished" are
    different answers: the first leaves a required port unbound, the second
    leaves a symbolic binding that a later resolution can complete.
    """

    outputs: tuple[AcceptedOutput, ...] = ()
    completed_producers: frozenset[OccurrenceId] = frozenset()
    authorized_revisions: Mapping[tuple[OccurrenceId, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "completed_producers", frozenset(self.completed_producers))
        object.__setattr__(self, "authorized_revisions", dict(self.authorized_revisions))

    def at(self, occurrence: OccurrenceId, output_port: str) -> tuple[AcceptedOutput, ...]:
        """Every accepted output at that port, in a deterministic order."""

        found = [
            item
            for item in self.outputs
            if item.producer_occurrence == occurrence and item.output_port == output_port
        ]
        return tuple(sorted(found, key=lambda item: (item.source_revision, item.artifact_id)))

    def is_complete(self, occurrence: OccurrenceId) -> bool:
        return occurrence in self.completed_producers

    def authorized_revision(self, occurrence: OccurrenceId, output_port: str) -> str | None:
        return self.authorized_revisions.get((occurrence, output_port))


@dataclass(frozen=True, slots=True)
class SchemaCompatibilityRule:
    """A *registered* statement that one produced schema may feed one required one.

    It is directed and it names its authority: the annex forbids claiming the
    system can decide arbitrary JSON Schema containment, so the only yes that
    exists is one a person or a registry wrote down.
    """

    produced: VersionedRef
    required: VersionedRef
    declaration_ref: str
    converter_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaCompatibilityRegistry:
    rules: tuple[SchemaCompatibilityRule, ...] = ()

    def match(
        self, produced: VersionedRef, required: VersionedRef
    ) -> SchemaCompatibilityRule | None:
        for rule in self.rules:
            if rule.produced == produced and rule.required == required:
                return rule
        return None


@dataclass(frozen=True, slots=True)
class ExplicitPortOrder:
    """The declared order of an ``EXPLICIT`` set port.

    ``PortSpec`` carries ``ordering`` and ``order_key`` but no explicit list, so
    the list rides beside the policy until the contract grows a field for it (see
    the P2.2b journal's contract-change request).
    """

    input_port: str
    ordered_requirement_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    """Everything the resolution depends on that is not in the network itself."""

    schema_registry: SchemaCompatibilityRegistry = field(
        default_factory=SchemaCompatibilityRegistry
    )
    explicit_orders: tuple[ExplicitPortOrder, ...] = ()
    #: requirement_id -> the revision this consumer is already bound to.
    pinned_revisions: Mapping[str, str] = field(default_factory=dict)
    #: input ports on which speculative execution has been authorised.
    provisional_ports: frozenset[str] = frozenset()
    require_witness: bool = True
    #: The purposes a witness may carry to license *this* use.  §11.5: permission
    #: to use one conclusion for one purpose is not permission for another, so a
    #: PLAN-purpose witness does not license starting the work.
    witness_purposes: frozenset[WitnessPurpose] = frozenset({WitnessPurpose.START})
    now_ms: int = 0
    #: scope_id -> the epoch that is current now (I19: a cached witness expires).
    scope_epochs: Mapping[str, int] = field(default_factory=dict)
    #: I19 is a *comparison*; not knowing the current epoch is not a pass.  A
    #: caller that genuinely has no epoch service must say so out loud.
    allow_unknown_scope: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pinned_revisions", dict(self.pinned_revisions))
        object.__setattr__(self, "scope_epochs", dict(self.scope_epochs))
        object.__setattr__(self, "provisional_ports", frozenset(self.provisional_ports))
        object.__setattr__(self, "witness_purposes", frozenset(self.witness_purposes))

    def explicit_order(self, input_port: str) -> tuple[str, ...] | None:
        for declared in self.explicit_orders:
            if declared.input_port == input_port:
                return tuple(declared.ordered_requirement_ids)
        return None


# --------------------------------------------------------------------------------------
# What the resolver produces
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedInputBinding:
    """Implementation annex §4.3: one input, frozen to one exact identity.

    The identity half **is** the contract's own :class:`~..contracts.htn.BoundInput`,
    embedded rather than copied field by field: requirement id, result id,
    acceptance id, artifact id, content hash, schema and source revision have
    exactly one home, so a wire record and a resolved binding cannot drift into
    disagreeing about which artifact was bound.  Everything else here is
    *routing and policy* around that identity — who produced it at which port,
    which consumer port it feeds, where it came from, and under which read,
    freshness and disclosure terms.
    """

    binding_id: str
    bound: BoundInput
    producer_task_ref: TaskRef
    producer_occurrence: OccurrenceId
    output_port: str
    support_revision: int
    consumer_task_ref: TaskRef
    input_port: str
    port_ordinal: int
    source_identity: ResourceIdentity
    produced_schema_ref: VersionedRef
    read_policy: str
    freshness_policy: str
    disclosure_scope: str
    source_revision_policy: SourceRevisionPolicy
    converter_ref: str | None = None
    requires_reacceptance: bool = False
    provisional: bool = False
    witness_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bound, BoundInput):
            raise ContractError("binding.bound must be a contracts.htn.BoundInput")

    def as_bound_input(self) -> BoundInput:
        """The contract record this binding resolves to, unchanged."""

        return self.bound

    # The identity is read through the embedded BoundInput so there is one source
    # of truth, not two that a later edit could let diverge.
    @property
    def requirement_id(self) -> str:
        return self.bound.requirement_id

    @property
    def producer_result_id(self) -> str:
        return self.bound.producer_result_id

    @property
    def acceptance_id(self) -> str:
        return self.bound.acceptance_id

    @property
    def artifact_id(self) -> str:
        return self.bound.artifact_id

    @property
    def content_hash(self) -> str:
        return self.bound.content_hash

    @property
    def schema_ref(self) -> VersionedRef:
        """The *required* schema the input was bound against."""

        return self.bound.schema_ref

    @property
    def source_revision(self) -> str:
        return self.bound.source_revision

    def to_json(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "bound_input": self.bound.to_json(),
            "producer_task_ref": str(self.producer_task_ref),
            "producer_occurrence": str(self.producer_occurrence),
            "output_port": self.output_port,
            "support_revision": self.support_revision,
            "consumer_task_ref": str(self.consumer_task_ref),
            "input_port": self.input_port,
            "port_ordinal": self.port_ordinal,
            "source_identity": self.source_identity.to_json(),
            "produced_schema_ref": self.produced_schema_ref.to_json(),
            "read_policy": self.read_policy,
            "freshness_policy": self.freshness_policy,
            "disclosure_scope": self.disclosure_scope,
            "source_revision_policy": str(self.source_revision_policy),
            "converter_ref": self.converter_ref,
            "requires_reacceptance": self.requires_reacceptance,
            "provisional": self.provisional,
            "witness_id": self.witness_id,
        }


@dataclass(frozen=True, slots=True)
class SymbolicBinding:
    """A promise kept open because its producer has not finished yet."""

    requirement_id: str
    producer_occurrence: OccurrenceId
    output_port: str
    consumer_task_ref: TaskRef
    input_port: str

    def to_json(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "producer_occurrence": str(self.producer_occurrence),
            "output_port": self.output_port,
            "consumer_task_ref": str(self.consumer_task_ref),
            "input_port": self.input_port,
        }


@dataclass(frozen=True, slots=True)
class InputManifest:
    """The immutable inputs of one dispatch.

    Bindings are stored in canonical order — by input port, then by the port's
    declared ordinal — so the hash depends on what was resolved and on the
    *declared* set-port order, never on the order the requirements happened to
    arrive in.
    """

    consumer_task_ref: TaskRef
    bindings: tuple[ResolvedInputBinding, ...] = ()
    pending: tuple[SymbolicBinding, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(
                self.bindings,
                key=lambda item: (item.input_port, item.port_ordinal, item.binding_id),
            )
        )
        if len({item.binding_id for item in ordered}) != len(ordered):
            raise ContractError("manifest must not repeat a binding_id")
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(
            self,
            "pending",
            tuple(sorted(self.pending, key=lambda item: (item.input_port, item.requirement_id))),
        )

    @property
    def is_frozen(self) -> bool:
        return not self.pending

    @property
    def has_provisional(self) -> bool:
        return any(item.provisional for item in self.bindings)

    @property
    def provisional_ports(self) -> frozenset[str]:
        return frozenset(item.input_port for item in self.bindings if item.provisional)

    def for_port(self, input_port: str) -> tuple[ResolvedInputBinding, ...]:
        return tuple(item for item in self.bindings if item.input_port == input_port)

    def required_ports_satisfied(self, consumer: TaskSemanticBindingV1) -> bool:
        """TG §4.3: a provisional binding may run, but never *claims* the DATA is met."""

        for spec in consumer.input_ports:
            if not spec.required:
                continue
            firm = [item for item in self.for_port(spec.port_key) if not item.provisional]
            if not firm:
                return False
        return True

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "consumer_task_ref": str(self.consumer_task_ref),
            "bindings": [item.to_json() for item in self.bindings],
        }

    def manifest_hash(self) -> str:
        if not self.is_frozen:
            raise ManifestNotFrozen(
                f"{len(self.pending)} input(s) are still symbolic; "
                "a dispatch identity needs every input resolved (implementation annex §4.3)"
            )
        return sha256_hex(self._hash_payload())

    def to_json(self) -> dict[str, Any]:
        return {
            "consumer_task_ref": str(self.consumer_task_ref),
            "bindings": [item.to_json() for item in self.bindings],
            "pending": [item.to_json() for item in self.pending],
            "manifest_hash": self.manifest_hash() if self.is_frozen else None,
        }


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """``manifest`` or ``problems`` — and, for a pending producer, both."""

    manifest: InputManifest | None
    problems: tuple[ResolutionProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def kinds(self) -> frozenset[ResolutionProblemKind]:
        return frozenset(problem.kind for problem in self.problems)

    def of_kind(self, kind: ResolutionProblemKind) -> tuple[ResolutionProblem, ...]:
        return tuple(problem for problem in self.problems if problem.kind is kind)


# --------------------------------------------------------------------------------------
# resolve_declared_inputs
# --------------------------------------------------------------------------------------


def _binding_id(requirement: DataRequirement, candidate: AcceptedOutput, consumer: str) -> str:
    return (
        "binding-"
        + sha256_hex(
            {
                "requirement_id": requirement.requirement_id,
                "consumer_task_ref": consumer,
                "input_port": requirement.input_port,
                "artifact_id": candidate.artifact_id,
                "content_hash": candidate.content_hash,
                "source_revision": candidate.source_revision,
            }
        )[:16]
    )


def _select_by_revision(
    requirement: DataRequirement,
    candidates: Sequence[AcceptedOutput],
    accepted: AcceptedOutputsIndex,
    policy: ResolutionPolicy,
) -> tuple[tuple[AcceptedOutput, ...], str | None, ResolutionProblem | None]:
    """Narrow the candidates to the revision the source policy names.

    ``PINNED`` answers with the revision this consumer was already bound to, and a
    newer accepted revision does not overrule it.  ``FOLLOW_AUTHORIZED_REVISION``
    answers with whatever is authorised now, and says so by returning a target
    that differs from the pin — which is what raises ``requires_reacceptance``.
    """

    pinned = policy.pinned_revisions.get(requirement.requirement_id)
    authorized = accepted.authorized_revision(
        requirement.producer_occurrence, requirement.output_port
    )
    if requirement.source_revision_policy is SourceRevisionPolicy.PINNED:
        target = pinned
    else:
        target = authorized if authorized is not None else pinned
    if target is None:
        # Nothing pins this input yet: every accepted revision is still a candidate,
        # and an ambiguity here is reported rather than resolved by recency.
        return tuple(candidates), None, None
    matching = tuple(item for item in candidates if item.source_revision == target)
    if not matching:
        return (
            (),
            target,
            ResolutionProblem(
                kind=ResolutionProblemKind.REVISION_NOT_AVAILABLE,
                detail=(
                    f"revision {target!r} of {requirement.producer_occurrence}."
                    f"{requirement.output_port} is not among the accepted outputs"
                ),
                input_port=requirement.input_port,
                requirement_ids=(requirement.requirement_id,),
            ),
        )
    return matching, target, None


def _check_schema(
    requirement: DataRequirement,
    port_spec: PortSpec,
    candidate: AcceptedOutput,
    policy: ResolutionPolicy,
) -> tuple[str | None, ResolutionProblem | None]:
    required = requirement.schema_ref
    if required != port_spec.schema_ref:
        return None, ResolutionProblem(
            kind=ResolutionProblemKind.SCHEMA_MISMATCH,
            detail=(
                f"requirement {requirement.requirement_id!r} declares schema "
                f"{required.id!r} v{required.version} but port {port_spec.port_key!r} "
                f"declares {port_spec.schema_ref.id!r} v{port_spec.schema_ref.version}"
            ),
            input_port=requirement.input_port,
            requirement_ids=(requirement.requirement_id,),
        )
    if candidate.schema_ref == required:
        return None, None
    rule = policy.schema_registry.match(candidate.schema_ref, required)
    if rule is not None:
        return rule.converter_ref, None
    return None, ResolutionProblem(
        kind=ResolutionProblemKind.SCHEMA_MISMATCH,
        detail=(
            f"{candidate.artifact_id} carries schema {candidate.schema_ref.id!r} "
            f"v{candidate.schema_ref.version} and no registered declaration maps it onto "
            f"{required.id!r} v{required.version}; containment is never inferred"
        ),
        input_port=requirement.input_port,
        requirement_ids=(requirement.requirement_id,),
        candidates=(candidate.artifact_id,),
    )


def _check_witness(
    requirement: DataRequirement,
    candidate: AcceptedOutput,
    consumer_task_ref: TaskRef,
    witnesses: Mapping[str, ValidityWitness],
    policy: ResolutionPolicy,
) -> tuple[str | None, ResolutionProblem | None]:
    """Is *this* witness permission for *this* consumer to use *this* support, now?

    §11.5 / AER §8.1: a witness is permission to use one conclusion, once, for one
    purpose — so four things have to agree before it licenses a binding, and each
    disagreement is its own refusal:

    ``purpose``
        a PLAN-purpose witness does not authorise starting work.
    ``consumer_ref``
        a witness issued to another task is that task's permission, not this one's.
    ``support_revision``
        a witness taken over revision 1 of the support says nothing about
        revision 99; reusing it would be quoting an old reading of new evidence.
    ``decision`` + freshness
        USABLE, at the current scope epoch, before the deadline (invariant I19).
    """

    def refuse(kind: ResolutionProblemKind, detail: str) -> tuple[None, ResolutionProblem]:
        return None, ResolutionProblem(
            kind=kind,
            detail=detail,
            input_port=requirement.input_port,
            requirement_ids=(requirement.requirement_id,),
            candidates=(candidate.artifact_id,),
        )

    witness = witnesses.get(candidate.acceptance_id)
    if witness is None:
        if not policy.require_witness:
            return None, None
        return refuse(
            ResolutionProblemKind.WITNESS_MISSING,
            f"no validity witness covers acceptance {candidate.acceptance_id}",
        )
    if witness.purpose not in policy.witness_purposes:
        return refuse(
            ResolutionProblemKind.WITNESS_PURPOSE_MISMATCH,
            (
                f"witness {witness.witness_id} was issued for {witness.purpose!s}; binding an "
                f"input needs one of {sorted(str(p) for p in policy.witness_purposes)}"
            ),
        )
    if witness.consumer_ref.kind is not TypedRefKind.TASK or witness.consumer_ref.id != str(
        consumer_task_ref
    ):
        return refuse(
            ResolutionProblemKind.WITNESS_CONSUMER_MISMATCH,
            (
                f"witness {witness.witness_id} was issued to "
                f"{witness.consumer_ref.kind!s} {witness.consumer_ref.id!r}, "
                f"not to task {str(consumer_task_ref)!r}"
            ),
        )
    if witness.support_revision != candidate.support_revision:
        return refuse(
            ResolutionProblemKind.WITNESS_SUPPORT_MISMATCH,
            (
                f"witness {witness.witness_id} was taken over support revision "
                f"{witness.support_revision} but {candidate.artifact_id} carries "
                f"{candidate.support_revision}"
            ),
        )
    if witness.decision is not WitnessDecision.USABLE:
        return refuse(
            ResolutionProblemKind.WITNESS_NOT_USABLE,
            f"witness {witness.witness_id} decided {witness.decision!s}",
        )
    if witness.scope_id not in policy.scope_epochs:
        if not policy.allow_unknown_scope:
            return refuse(
                ResolutionProblemKind.WITNESS_EPOCH_UNKNOWN,
                (
                    f"the current epoch of scope {witness.scope_id!r} is unknown, so witness "
                    f"{witness.witness_id} cannot be compared against it (I19); set "
                    "allow_unknown_scope to accept that risk explicitly"
                ),
            )
        current_epoch = witness.scope_epoch
    else:
        current_epoch = policy.scope_epochs[witness.scope_id]
    if not witness.is_fresh_for(now_ms=policy.now_ms, current_scope_epoch=current_epoch):
        return refuse(
            ResolutionProblemKind.WITNESS_NOT_USABLE,
            (
                f"witness {witness.witness_id} is stale: it was taken at scope epoch "
                f"{witness.scope_epoch} (now {current_epoch}) and expires at "
                f"{witness.not_after_ms}"
            ),
        )
    return witness.witness_id, None


def _order_entries(
    port_spec: PortSpec,
    entries: Sequence[tuple[DataRequirement, AcceptedOutput, str | None, str | None]],
    policy: ResolutionPolicy,
) -> tuple[tuple[int, ...], ResolutionProblem | None]:
    """Give each entry of a set port its ordinal, or say why it cannot be ordered."""

    requirement_ids = tuple(entry[0].requirement_id for entry in entries)
    if port_spec.ordering is None:
        return (), ResolutionProblem(
            kind=ResolutionProblemKind.SET_PORT_UNORDERED,
            detail=(
                f"set port {port_spec.port_key!r} has {len(entries)} binding(s) and no declared "
                "ordering; a set port may not fall back to whichever producer finished most "
                "recently"
            ),
            input_port=port_spec.port_key,
            requirement_ids=requirement_ids,
        )
    if port_spec.ordering is PortOrdering.BY_PRODUCER_ORDINAL:
        keys: list[Any] = [entry[1].producer_ordinal for entry in entries]
        label = "producer ordinal"
    elif port_spec.ordering is PortOrdering.BY_KEY:
        key_name = port_spec.order_key or ""
        raw = [entry[1].order_keys.get(key_name) for entry in entries]
        if any(value is None for value in raw):
            return (), ResolutionProblem(
                kind=ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE,
                detail=(
                    f"set port {port_spec.port_key!r} orders by key {key_name!r} but "
                    f"{sum(1 for value in raw if value is None)} candidate(s) do not carry it"
                ),
                input_port=port_spec.port_key,
                requirement_ids=requirement_ids,
            )
        keys = list(raw)
        label = f"order key {key_name!r}"
    else:
        declared = policy.explicit_order(port_spec.port_key)
        if declared is None:
            return (), ResolutionProblem(
                kind=ResolutionProblemKind.SET_PORT_UNORDERED,
                detail=(
                    f"set port {port_spec.port_key!r} declares an EXPLICIT order but no order "
                    "was supplied"
                ),
                input_port=port_spec.port_key,
                requirement_ids=requirement_ids,
            )
        if sorted(declared) != sorted(requirement_ids) or len(set(declared)) != len(declared):
            return (), ResolutionProblem(
                kind=ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE,
                detail=(
                    f"the EXPLICIT order of {port_spec.port_key!r} is {list(declared)} but the "
                    f"bindings are {sorted(requirement_ids)}; every binding needs exactly one "
                    "position"
                ),
                input_port=port_spec.port_key,
                requirement_ids=requirement_ids,
            )
        keys = [declared.index(entry[0].requirement_id) for entry in entries]
        label = "explicit order"
    if len(set(keys)) != len(keys):
        return (), ResolutionProblem(
            kind=ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE,
            detail=(
                f"set port {port_spec.port_key!r} orders by {label} and two bindings share one "
                "position, so the order is not determined"
            ),
            input_port=port_spec.port_key,
            requirement_ids=requirement_ids,
        )
    ranking = {key: rank for rank, key in enumerate(sorted(keys))}
    return tuple(ranking[key] for key in keys), None


def resolve_declared_inputs(
    consumer: TaskSemanticBindingV1,
    requirements: Sequence[DataRequirement],
    accepted: AcceptedOutputsIndex,
    *,
    witnesses: Mapping[str, ValidityWitness],
    policy: ResolutionPolicy,
    consumer_occurrence: OccurrenceId,
) -> ResolutionResult:
    """Resolve the declared DATA contract of one consumer into an InputManifest.

    ``consumer_occurrence`` is required, not optional.  ``consumer`` is identified
    by TaskRef while a :class:`~..contracts.htn.DataRequirement` names an
    occurrence, and without the occurrence there is nothing to compare a
    requirement's ``consumer_occurrence`` against — a requirement aimed at a
    different consumer would be absorbed silently as one of this task's inputs.

    Only the declared requirements are consulted.  An ORDER-only predecessor has
    none, so it contributes nothing however many artifacts it accepted — the
    all-ancestors sweep of :func:`.versioning.collect_upstream_inputs` is not
    reached from here and is not changed by this module (TG §10.1, §10.3).
    """

    ports = {spec.port_key: spec for spec in consumer.input_ports}
    problems: list[ResolutionProblem] = []
    pending: list[SymbolicBinding] = []
    # input_port -> (requirement, candidate, converter_ref, witness_id)
    per_port: dict[str, list[tuple[DataRequirement, AcceptedOutput, str | None, str | None]]]
    per_port = {}

    for requirement in sorted(requirements, key=lambda item: item.requirement_id):
        if requirement.consumer_occurrence != consumer_occurrence:
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.FOREIGN_REQUIREMENT,
                    detail=(
                        f"requirement {requirement.requirement_id!r} feeds "
                        f"{requirement.consumer_occurrence}, not {consumer_occurrence}"
                    ),
                    input_port=requirement.input_port,
                    requirement_ids=(requirement.requirement_id,),
                )
            )
            continue
        port_spec = ports.get(requirement.input_port)
        if port_spec is None:
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.UNDECLARED_PORT,
                    detail=(
                        f"requirement {requirement.requirement_id!r} targets input port "
                        f"{requirement.input_port!r}, which {consumer.task_id} does not declare"
                    ),
                    input_port=requirement.input_port,
                    requirement_ids=(requirement.requirement_id,),
                )
            )
            continue
        if not accepted.is_complete(requirement.producer_occurrence):
            pending.append(
                SymbolicBinding(
                    requirement_id=requirement.requirement_id,
                    producer_occurrence=requirement.producer_occurrence,
                    output_port=requirement.output_port,
                    consumer_task_ref=consumer.task_id,
                    input_port=requirement.input_port,
                )
            )
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.PENDING_PRODUCER,
                    detail=(
                        f"{requirement.producer_occurrence} has not finished; requirement "
                        f"{requirement.requirement_id!r} stays symbolic and the manifest cannot "
                        "be frozen"
                    ),
                    input_port=requirement.input_port,
                    requirement_ids=(requirement.requirement_id,),
                )
            )
            continue
        candidates = accepted.at(requirement.producer_occurrence, requirement.output_port)
        narrowed, _target, revision_problem = _select_by_revision(
            requirement, candidates, accepted, policy
        )
        if revision_problem is not None:
            problems.append(revision_problem)
            continue
        for candidate in narrowed:
            if candidate.disclosure is not DisclosureState.DISCLOSABLE:
                problems.append(
                    ResolutionProblem(
                        kind=ResolutionProblemKind.NOT_DISCLOSABLE,
                        detail=(
                            f"{candidate.artifact_id} is {candidate.disclosure!s}; a pinned "
                            "revision does not carry a permission forward"
                        ),
                        input_port=requirement.input_port,
                        requirement_ids=(requirement.requirement_id,),
                        candidates=(candidate.artifact_id,),
                    )
                )
                continue
            converter_ref, schema_problem = _check_schema(requirement, port_spec, candidate, policy)
            if schema_problem is not None:
                problems.append(schema_problem)
                continue
            if candidate.provisional and requirement.input_port not in policy.provisional_ports:
                problems.append(
                    ResolutionProblem(
                        kind=ResolutionProblemKind.PROVISIONAL_NOT_AUTHORIZED,
                        detail=(
                            f"{candidate.artifact_id} is provisional and port "
                            f"{requirement.input_port!r} is not in the authorised speculation scope"
                        ),
                        input_port=requirement.input_port,
                        requirement_ids=(requirement.requirement_id,),
                        candidates=(candidate.artifact_id,),
                    )
                )
                continue
            witness_id, witness_problem = _check_witness(
                requirement, candidate, consumer.task_id, witnesses, policy
            )
            if witness_problem is not None:
                problems.append(witness_problem)
                continue
            per_port.setdefault(requirement.input_port, []).append(
                (requirement, candidate, converter_ref, witness_id)
            )

    bindings: list[ResolvedInputBinding] = []
    for port_key in sorted(per_port):
        port_spec = ports[port_key]
        entries = per_port[port_key]
        if port_spec.cardinality is PortCardinality.SINGLE and len(entries) > 1:
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT,
                    detail=(
                        f"single-valued port {port_key!r} has {len(entries)} live candidates; "
                        "choose one explicitly or introduce a synthesis task"
                    ),
                    input_port=port_key,
                    requirement_ids=tuple(sorted({entry[0].requirement_id for entry in entries})),
                    candidates=tuple(sorted(entry[1].artifact_id for entry in entries)),
                )
            )
            continue
        if port_spec.cardinality is PortCardinality.SET:
            ordinals, order_problem = _order_entries(port_spec, entries, policy)
            if order_problem is not None:
                problems.append(order_problem)
                continue
        else:
            ordinals = (0,) * len(entries)
        for ordinal, (requirement, candidate, converter_ref, witness_id) in zip(
            ordinals, entries, strict=True
        ):
            follows = (
                requirement.source_revision_policy
                is SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION
            )
            pin = policy.pinned_revisions.get(requirement.requirement_id)
            bindings.append(
                ResolvedInputBinding(
                    binding_id=_binding_id(requirement, candidate, str(consumer.task_id)),
                    bound=BoundInput(
                        requirement_id=requirement.requirement_id,
                        producer_result_id=candidate.producer_result_id,
                        acceptance_id=candidate.acceptance_id,
                        artifact_id=candidate.artifact_id,
                        content_hash=candidate.content_hash,
                        schema_ref=requirement.schema_ref,
                        source_revision=candidate.source_revision,
                    ),
                    producer_task_ref=candidate.producer_task_ref,
                    producer_occurrence=candidate.producer_occurrence,
                    output_port=candidate.output_port,
                    support_revision=candidate.support_revision,
                    consumer_task_ref=consumer.task_id,
                    input_port=requirement.input_port,
                    port_ordinal=ordinal,
                    source_identity=candidate.source_identity,
                    produced_schema_ref=candidate.schema_ref,
                    read_policy=requirement.assurance_policy_ref,
                    freshness_policy=requirement.freshness_policy_ref,
                    disclosure_scope=candidate.disclosure_scope,
                    source_revision_policy=requirement.source_revision_policy,
                    converter_ref=converter_ref,
                    # A FOLLOW input that has never been pinned is *also* an input
                    # change this consumer has not been accepted against: the first
                    # binding is the first version, not a neutral starting point.
                    requires_reacceptance=(
                        follows and (pin is None or pin != candidate.source_revision)
                    ),
                    provisional=candidate.provisional,
                    witness_id=witness_id,
                )
            )

    bound_ports = {item.input_port for item in bindings}
    pending_ports = {item.input_port for item in pending}
    failed_ports = {problem.input_port for problem in problems}
    for spec in consumer.input_ports:
        if not spec.required or spec.port_key in bound_ports or spec.port_key in pending_ports:
            continue
        if spec.port_key in failed_ports:
            continue  # already explained by a more specific refusal
        problems.append(
            ResolutionProblem(
                kind=ResolutionProblemKind.UNBOUND_REQUIRED_PORT,
                detail=(
                    f"required input port {spec.port_key!r} of {consumer.task_id} has no "
                    "accepted binding"
                ),
                input_port=spec.port_key,
            )
        )

    ordered_problems = tuple(
        sorted(problems, key=lambda item: (str(item.kind), item.input_port or "", item.detail))
    )
    blocking = [
        problem
        for problem in ordered_problems
        if problem.kind is not ResolutionProblemKind.PENDING_PRODUCER
    ]
    if blocking:
        return ResolutionResult(manifest=None, problems=ordered_problems)
    manifest = InputManifest(
        consumer_task_ref=consumer.task_id,
        bindings=tuple(bindings),
        pending=tuple(pending),
    )
    return ResolutionResult(manifest=manifest, problems=ordered_problems)


# --------------------------------------------------------------------------------------
# materialise_plan
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetRules:
    """How a binding's source identity becomes a place in the consumer's namespace.

    ``preserve_source_namespace`` is the switch TG §10.2 asks for: keep the two
    attempts apart instead of letting one relative name collapse them.
    ``case_insensitive`` models a platform whose file system folds case, so a
    collision that only a mac or a Windows host would see is reported on every
    host rather than discovered in production.
    """

    namespace: str
    port_prefixes: Mapping[str, str] = field(default_factory=dict)
    preserve_source_namespace: bool = False
    case_insensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise ContractError("target_rules.namespace must be a non-blank string")
        object.__setattr__(self, "port_prefixes", dict(self.port_prefixes))

    def target_for(self, binding: ResolvedInputBinding) -> ResourceIdentity:
        parts: list[str] = []
        prefix = self.port_prefixes.get(binding.input_port)
        if prefix:
            parts.append(prefix)
        if self.preserve_source_namespace:
            parts.append(binding.source_identity.namespace)
        parts.append(binding.source_identity.path)
        return ResourceIdentity(namespace=self.namespace, path="/".join(parts))


@dataclass(frozen=True, slots=True)
class MaterialisationEntry:
    """One place, one content hash, and every binding that asked for it."""

    target: ResourceIdentity
    content_hash: str
    artifact_ids: tuple[str, ...]
    binding_ids: tuple[str, ...]
    input_ports: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target.to_json(),
            "content_hash": self.content_hash,
            "artifact_ids": list(self.artifact_ids),
            "binding_ids": list(self.binding_ids),
            "input_ports": list(self.input_ports),
        }


@dataclass(frozen=True, slots=True)
class MaterialisationPlan:
    """Where each input *should* go.  Nothing is written by producing one."""

    consumer_task_ref: TaskRef
    entries: tuple[MaterialisationEntry, ...] = ()
    problems: tuple[ResolutionProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def plan_hash(self) -> str:
        return sha256_hex(
            {
                "consumer_task_ref": str(self.consumer_task_ref),
                "entries": [entry.to_json() for entry in self.entries],
            }
        )


def materialise_plan(manifest: InputManifest, target_rules: TargetRules) -> MaterialisationPlan:
    """Map an immutable manifest onto target places, refusing to merge a collision.

    Two bindings whose *content hashes differ* aimed at one target place is a
    ``TARGET_PATH_CONFLICT``: TG §10.2 requires an explicit selection, conversion
    or synthesis that produces a new artifact and is verified again.  The same
    hash at one place is not a conflict — it is one file two bindings agree on.
    """

    if not manifest.is_frozen:
        raise ManifestNotFrozen(
            "a materialisation plan needs every input resolved to an exact identity"
        )
    problems: list[ResolutionProblem] = []
    grouped: dict[tuple[str, str], list[tuple[ResourceIdentity, ResolvedInputBinding]]] = {}
    for binding in manifest.bindings:
        target = target_rules.target_for(binding)
        if target.is_escaping:
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.TARGET_PATH_INVALID,
                    detail=(
                        f"binding {binding.binding_id} would be placed at {target.path!r}, "
                        "which leaves the consumer namespace"
                    ),
                    input_port=binding.input_port,
                    requirement_ids=(binding.requirement_id,),
                    candidates=(binding.artifact_id,),
                )
            )
            continue
        key = target.collation_key(case_insensitive=target_rules.case_insensitive)
        grouped.setdefault(key, []).append((target, binding))

    entries: list[MaterialisationEntry] = []
    for key in sorted(grouped):
        members = grouped[key]
        hashes = {binding.content_hash for _target, binding in members}
        if len(hashes) > 1:
            problems.append(
                ResolutionProblem(
                    kind=ResolutionProblemKind.TARGET_PATH_CONFLICT,
                    detail=(
                        f"{len(hashes)} different contents would occupy {key[1]!r} in "
                        f"{target_rules.namespace!r}; choose, convert or synthesise one new "
                        "artifact and verify it again"
                    ),
                    input_ports=tuple(sorted({binding.input_port for _target, binding in members})),
                    requirement_ids=tuple(
                        sorted({binding.requirement_id for _target, binding in members})
                    ),
                    candidates=tuple(sorted({binding.artifact_id for _t, binding in members})),
                )
            )
            continue
        # F5 / TG §10.2: under a case-folding platform rule the *canonical* spelling
        # is the folded one, not whichever binding happened to be visited first.
        # Picking arbitrarily would make the plan depend on iteration order.
        target = (
            ResourceIdentity(namespace=members[0][0].namespace, path=members[0][0].path.casefold())
            if target_rules.case_insensitive
            else members[0][0]
        )
        entries.append(
            MaterialisationEntry(
                target=target,
                content_hash=members[0][1].content_hash,
                artifact_ids=tuple(sorted({binding.artifact_id for _t, binding in members})),
                binding_ids=tuple(sorted(binding.binding_id for _t, binding in members)),
                input_ports=tuple(sorted({binding.input_port for _t, binding in members})),
            )
        )
    return MaterialisationPlan(
        consumer_task_ref=manifest.consumer_task_ref,
        entries=tuple(entries),
        problems=tuple(sorted(problems, key=lambda item: (str(item.kind), item.detail))),
    )


# --------------------------------------------------------------------------------------
# explain
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Explanation:
    """A refusal a person can act on: what happened, and what would fix it."""

    kind: ResolutionProblemKind
    summary: str
    remedy: str
    input_port: str | None = None
    requirement_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    input_ports: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "summary": self.summary,
            "remedy": self.remedy,
            "input_port": self.input_port,
            "input_ports": list(self.input_ports),
            "requirement_ids": list(self.requirement_ids),
            "candidates": list(self.candidates),
        }


#: The remedy is deliberately a *decision to make*, never a default to apply: no
#: entry here tells the caller to prefer a producer by position or by recency.
_REMEDIES: Mapping[ResolutionProblemKind, str] = {
    ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT: (
        "Name one candidate in the plan, or add a synthesis task that combines them and is "
        "accepted on its own."
    ),
    ResolutionProblemKind.SET_PORT_UNORDERED: (
        "Declare the port's ordering (by producer ordinal, by key, or an explicit sequence)."
    ),
    ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE: (
        "Give every binding of the port exactly one position under the declared ordering."
    ),
    ResolutionProblemKind.UNDECLARED_PORT: (
        "Declare the input port on the consumer's semantic binding, or retarget the requirement."
    ),
    ResolutionProblemKind.FOREIGN_REQUIREMENT: (
        "Resolve this requirement against the consumer it actually names."
    ),
    ResolutionProblemKind.UNBOUND_REQUIRED_PORT: (
        "Add a data requirement for the port, mark the port optional, or plan a producer for it."
    ),
    ResolutionProblemKind.SCHEMA_MISMATCH: (
        "Register an explicit compatibility declaration with a converter, insert a conversion "
        "task, or align the schemas."
    ),
    ResolutionProblemKind.NOT_DISCLOSABLE: (
        "Obtain a current permission for the source, or plan the work around a source that may "
        "be read."
    ),
    ResolutionProblemKind.WITNESS_MISSING: (
        "Produce a validity witness for the acceptance before the input is bound."
    ),
    ResolutionProblemKind.WITNESS_NOT_USABLE: (
        "Re-evaluate the witness against the current scope epoch and deadline."
    ),
    ResolutionProblemKind.WITNESS_PURPOSE_MISMATCH: (
        "Obtain a witness issued for this use, or widen the accepted purposes deliberately."
    ),
    ResolutionProblemKind.WITNESS_CONSUMER_MISMATCH: (
        "Obtain a witness issued to this consumer; another task's permission is not this one's."
    ),
    ResolutionProblemKind.WITNESS_SUPPORT_MISMATCH: (
        "Re-take the witness over the support revision the artifact actually carries."
    ),
    ResolutionProblemKind.WITNESS_EPOCH_UNKNOWN: (
        "Supply the current epoch of the witness's scope, or set allow_unknown_scope to accept "
        "an uncompared witness on purpose."
    ),
    ResolutionProblemKind.PENDING_PRODUCER: (
        "Wait for the producer to be accepted, then resolve again; the symbolic binding is kept."
    ),
    ResolutionProblemKind.PROVISIONAL_NOT_AUTHORIZED: (
        "Authorise speculation on this port explicitly, or wait for a firm accepted output."
    ),
    ResolutionProblemKind.REVISION_NOT_AVAILABLE: (
        "Re-pin the input to a revision that is still accepted, or restore the named revision."
    ),
    ResolutionProblemKind.TARGET_PATH_CONFLICT: (
        "Choose, convert or synthesise one artifact for the place and verify it again, or keep "
        "the sources in separate namespaces."
    ),
    ResolutionProblemKind.TARGET_PATH_INVALID: (
        "Give the binding a place inside the consumer namespace."
    ),
}

_SUMMARIES: Mapping[ResolutionProblemKind, str] = {
    ResolutionProblemKind.AMBIGUOUS_SINGLE_PORT: "a single-valued port has more than one candidate",
    ResolutionProblemKind.SET_PORT_UNORDERED: "a set-valued port has no declared ordering",
    ResolutionProblemKind.SET_PORT_ORDER_INCOMPLETE: (
        "the declared ordering does not rank every binding"
    ),
    ResolutionProblemKind.UNDECLARED_PORT: (
        "a requirement targets a port the consumer does not declare"
    ),
    ResolutionProblemKind.FOREIGN_REQUIREMENT: "a requirement belongs to another consumer",
    ResolutionProblemKind.UNBOUND_REQUIRED_PORT: "a required input port has no binding",
    ResolutionProblemKind.SCHEMA_MISMATCH: "the produced schema is not the required schema",
    ResolutionProblemKind.NOT_DISCLOSABLE: "the source may not be read right now",
    ResolutionProblemKind.WITNESS_MISSING: "no validity witness covers the acceptance",
    ResolutionProblemKind.WITNESS_NOT_USABLE: "the validity witness does not permit this use",
    ResolutionProblemKind.WITNESS_PURPOSE_MISMATCH: (
        "the witness was issued for a different purpose"
    ),
    ResolutionProblemKind.WITNESS_CONSUMER_MISMATCH: (
        "the witness was issued to a different consumer"
    ),
    ResolutionProblemKind.WITNESS_SUPPORT_MISMATCH: (
        "the witness covers a different support revision"
    ),
    ResolutionProblemKind.WITNESS_EPOCH_UNKNOWN: (
        "the current epoch of the witness's scope is unknown"
    ),
    ResolutionProblemKind.PENDING_PRODUCER: "the producer has not finished",
    ResolutionProblemKind.PROVISIONAL_NOT_AUTHORIZED: "speculation is not authorised on this port",
    ResolutionProblemKind.REVISION_NOT_AVAILABLE: "the named source revision is not accepted",
    ResolutionProblemKind.TARGET_PATH_CONFLICT: "two different contents claim one place",
    ResolutionProblemKind.TARGET_PATH_INVALID: "the computed place is outside the namespace",
}


def explain(problems: Sequence[ResolutionProblem]) -> tuple[Explanation, ...]:
    """One structured reason per problem, in the order they were reported."""

    return tuple(
        Explanation(
            kind=problem.kind,
            summary=f"{_SUMMARIES[problem.kind]}: {problem.detail}",
            remedy=_REMEDIES[problem.kind],
            input_port=problem.input_port,
            input_ports=problem.input_ports,
            requirement_ids=problem.requirement_ids,
            candidates=problem.candidates,
        )
        for problem in problems
    )


__all__ = (
    "AcceptedOutput",
    "AcceptedOutputsIndex",
    "DisclosureState",
    "Explanation",
    "ExplicitPortOrder",
    "InputManifest",
    "ManifestNotFrozen",
    "MaterialisationEntry",
    "MaterialisationPlan",
    "ResolutionPolicy",
    "ResolutionProblem",
    "ResolutionProblemKind",
    "ResolutionResult",
    "ResolvedInputBinding",
    "ResourceIdentity",
    "SchemaCompatibilityRegistry",
    "SchemaCompatibilityRule",
    "SymbolicBinding",
    "TargetRules",
    "explain",
    "materialise_plan",
    "resolve_declared_inputs",
)
