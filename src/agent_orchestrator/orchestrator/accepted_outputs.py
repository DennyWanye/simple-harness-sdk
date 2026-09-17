# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2: the codec for "which artifact did this Acceptance accept, at which port".

P2.3b left :meth:`HierarchicalDispatch._recorded_outputs` returning nothing and said
why: building an :class:`~..artifacts.input_bindings.AcceptedOutput` needs one fact
the schema did not hold, and guessing the port from an artifact path would be the
all-ancestors sweep §24.1 decision 4 removed, wearing a typed name.  Migration 17's
``acceptance_outputs`` holds the fact; this module is the two functions that put an
``AcceptedOutput`` into that row and take it back out.

Two things it deliberately does *not* do:

* **It does not derive the port.**  ``accepted_outputs_of`` checks that a stated port
  is one the plan actually declares for that producer, and refuses one it does not.
  A port nobody declared is not a typo to be corrected — it is a claim that this
  occurrence produces something the plan never said it produces, and the consumer
  that would bind to it is reading a contract that does not exist.
* **It does not invent a schema.**  The ``schema_ref`` comes from the
  :class:`~..contracts.htn.DataRequirement` that declares the edge, because that is
  the reference the consumer's compatibility check is made against.  Taking it from
  the producer's claim would let a producer relabel its own output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.input_bindings import AcceptedOutput, DisclosureState, ResourceIdentity
from ..contracts.htn import OccurrenceId, TaskRef
from ..contracts.models import ContractError
from ..contracts.semantic_base import VersionedRef
from ..graph.task_network import TaskNetworkSnapshot


def accepted_output_json(output: AcceptedOutput) -> dict[str, Any]:
    """One accepted output as the row stores it.

    ``AcceptedOutput`` has no codec of its own — it is an in-memory value the
    resolver consumes — so the shape is defined here, next to the only thing that
    persists it.  Every field is written: a reader rebuilding the index has to get
    back the value the writer had, and a field dropped here would silently become its
    default on the way out (``provisional=False`` above all, which would turn "this
    was accepted provisionally" into "this is firm").
    """

    return {
        "producer_occurrence": str(output.producer_occurrence),
        "producer_task_ref": str(output.producer_task_ref),
        "output_port": output.output_port,
        "producer_result_id": output.producer_result_id,
        "acceptance_id": output.acceptance_id,
        "support_revision": int(output.support_revision),
        "artifact_id": output.artifact_id,
        "content_hash": output.content_hash,
        "schema_ref": output.schema_ref.to_json(),
        "source_revision": output.source_revision,
        "source_identity": {
            "namespace": output.source_identity.namespace,
            "path": output.source_identity.path,
        },
        "producer_ordinal": int(output.producer_ordinal),
        "order_keys": dict(output.order_keys),
        "disclosure": str(output.disclosure),
        "disclosure_scope": output.disclosure_scope,
        "provisional": bool(output.provisional),
    }


def accepted_output_from_json(value: Mapping[str, Any]) -> AcceptedOutput:
    """Rebuild one accepted output from its stored row."""

    identity = value.get("source_identity") or {}
    return AcceptedOutput(
        producer_occurrence=OccurrenceId(str(value["producer_occurrence"])),
        producer_task_ref=TaskRef(str(value["producer_task_ref"])),
        output_port=str(value["output_port"]),
        producer_result_id=str(value["producer_result_id"]),
        acceptance_id=str(value["acceptance_id"]),
        support_revision=int(value["support_revision"]),
        artifact_id=str(value["artifact_id"]),
        content_hash=str(value["content_hash"]),
        schema_ref=VersionedRef.from_json(value["schema_ref"], "accepted_output.schema_ref"),
        source_revision=str(value["source_revision"]),
        source_identity=ResourceIdentity(
            namespace=str(identity.get("namespace", "")),
            path=str(identity.get("path", "")),
        ),
        producer_ordinal=int(value.get("producer_ordinal", 0)),
        order_keys=dict(value.get("order_keys") or {}),
        disclosure=DisclosureState(str(value.get("disclosure", DisclosureState.DISCLOSABLE))),
        disclosure_scope=str(value.get("disclosure_scope", "mission")),
        provisional=bool(value.get("provisional", False)),
    )


def declared_output_ports(
    network: TaskNetworkSnapshot, producer: OccurrenceId
) -> Mapping[str, VersionedRef]:
    """The ports this occurrence is declared to produce on, and each port's schema.

    Two things make a port declared, and P2.3d added the second one:

    * a :class:`~..contracts.htn.DataRequirement` edge **consumes** it — the schema
      then comes from the edge, because that is the reference the consumer's
      compatibility check is made against, and a port declared twice with two schemas
      is refused rather than resolved to the last one;
    * the adopted method's ``composition.criterion_links`` **point at** this
      occurrence — the finalizer step above all.  The Grok acceptance run (H arm,
      2026-09-17, defect D3) failed 10 of 40 episodes on exactly this gap: the seed
      method ``code.fix-by-patch`` hangs ``c-test-passes`` on the ``verify`` step's
      own criterion, ``verify`` declares a required ``report`` port and *nothing
      downstream consumes it*, so all three readers agreed it was "a port nobody
      wants" — the leaf was never told the port existed, never filled ``outputs``,
      the accept side had nothing to refuse, and the root reviewer then read
      ``evidence.kind=none`` and rejected the whole Mission.  A criterion link is a
      consumer: the root's success criterion is what reads that artifact.

    A criterion-linked port's schema comes from the producer's own
    :class:`~..contracts.htn.PortSpec`, because there is no edge to take it from; an
    edge that *does* consume the same port keeps its schema, so a producer still
    cannot relabel what flows down a live edge.
    """

    covered = criterion_linked_occurrences(network.obligation_coverage)
    binding = None
    if producer in covered:
        # Review P2-7: this used to pair ``task_bindings`` with ``occurrences`` by
        # position.  ``HierarchicalDispatch.network()`` does build the two side by side,
        # but a snapshot straight out of ``compile_proposal`` is "the old bindings then
        # the new ones", which is not the occurrence order — and an off-by-one there
        # would declare one step's ports on another.  The snapshot has a lookup for
        # exactly this, so ask it.
        try:
            binding = network.binding_for_occurrence(producer)
        except KeyError:
            binding = None
    return _merge_ports(
        _ports_of(network.data_requirements, producer),
        binding if producer in covered else None,
    )


def declared_ports_in_revision(
    requirements: Sequence[Any],
    producer: OccurrenceId,
    *,
    binding: Any = None,
    criterion_linked: bool = False,
) -> Mapping[str, VersionedRef]:
    """:func:`declared_output_ports` for a caller holding the *rows*, not a network.

    ``accept_review`` runs inside the write transaction and must not rebuild the
    whole typed network to check one port — that is the slow read
    ``plan_commits`` keeps out of a transaction on purpose.  It holds the plan
    revision's ``data_requirements`` rows instead, and the rule applied to them has
    to be the same rule, so both readers call :func:`_ports_of` and neither owns a
    second copy of "which ports this occurrence declares".

    ``criterion_linked`` says whether a ``criterion_link`` of the adopted method
    points at this occurrence; ``binding`` is that occurrence's semantic binding,
    read for its declared ``output_ports``.  Callers that hold a store should use
    :func:`output_ports_in_revision`, which answers both from the rows itself.
    """

    return _merge_ports(_ports_of(requirements, producer), binding if criterion_linked else None)


def output_ports_in_revision(
    semantics: Any,
    mission_id: str,
    revision: int,
    producer: OccurrenceId,
    task_id: str,
) -> Mapping[str, VersionedRef]:
    """The one answer to "which output ports does this occurrence owe", from rows.

    Every production reader of that question goes through here — the context package
    the leaf is handed (``HierarchicalDispatch.declared_output_ports_for``), the
    index the acceptance writes (``LeafAcceptanceAssembly._outputs``) and the check
    that refuses an unclaimed port (``ResolutionCommitsMixin.accept_review``) — so
    "told", "filed" and "enforced" cannot drift apart again, which is what defect D3
    was made of.
    """

    covered = criterion_linked_occurrences(coverage_in_revision(semantics, mission_id, revision))
    binding = semantics.task_semantics_of(mission_id, str(task_id)) if producer in covered else None
    return declared_ports_in_revision(
        semantics.list_data_requirements(mission_id, revision),
        producer,
        binding=binding,
        criterion_linked=producer in covered,
    )


def criterion_linked_occurrences(coverage: Sequence[Any]) -> frozenset[Any]:
    """The occurrences a plan's ``criterion_links`` point at.

    ``ObligationCoverage.covered_by`` *is* that set: it is built by
    :func:`~..planning.htn.compiler.coverage_from_slots` out of the adopted method's
    ``composition.criterion_links``, resolving a link with no ``child_step`` to the
    finalizer slot.  Reading it back here rather than walking the links again is what
    keeps one answer to "which step carries a parent criterion".
    """

    return frozenset(
        occurrence for claim in coverage for occurrence in getattr(claim, "covered_by", ())
    )


def coverage_in_revision(semantics: Any, mission_id: str, revision: int) -> tuple[Any, ...]:
    """:func:`stored_coverage` for a caller that holds only a store and a revision."""

    instances, adopted, bindings = _adopted_in_revision(semantics, mission_id, revision)
    return stored_coverage(semantics, instances, adopted, bindings)


def _adopted_in_revision(
    semantics: Any, mission_id: str, revision: int
) -> tuple[tuple[Any, ...], tuple[Any, ...], dict[Any, Any]]:
    """The method instances of one plan revision, which of them are adopted, and the
    semantic bindings of the tasks they refine — the three inputs every "what does the
    adopted plan say" reader starts from."""

    from ..contracts.htn import TaskRef

    bindings: dict[Any, Any] = {}
    for spec in semantics.list_plan_memberships(mission_id, revision):
        binding = semantics.task_semantics_of(mission_id, str(spec.task_id))
        if binding is not None:
            bindings[TaskRef(str(spec.task_id))] = binding
    instances = tuple(
        draft
        for draft in semantics.list_method_instances(mission_id)
        if TaskRef(str(draft.goal_id)) in bindings
    )
    adopted = tuple(
        draft.instance_id
        for draft in instances
        if semantics.method_instance_state(mission_id, str(draft.instance_id)) == "ADOPTED"
    )
    return instances, adopted, bindings


@dataclass(frozen=True, slots=True)
class CarriedCriterion:
    """One parent criterion a ``criterion_link`` hangs on one occurrence (§6.3).

    P2.3h.  ``ObligationCoverage`` answers "which occurrences carry *some* root
    criterion" and is enough for the port rule (D3).  It is not enough for the review
    side, which needs the pairing itself: the leaf's own criterion id
    (``leaf_criterion_id``, the link's ``child_criterion_id``), the parent criterion
    it covers, and the ``evidence_requirement`` the method wrote for that link —
    the sentence that tells a Worker what its report has to show and tells the root
    reviewer what to look for in that leaf's accepted output.  The real Grok C3 run
    rejected a correct Mission on exactly the absence of this: every leaf's review
    stamped every root criterion PASS, and the reviewer rightly refused to read a
    ``facts`` step's PASS on ``c-test-passes`` as evidence of anything.
    """

    parent_task_id: str
    parent_criterion_id: str
    occurrence_id: OccurrenceId
    task_id: str
    leaf_criterion_id: str
    evidence_requirement: str

    def to_json(self) -> dict[str, Any]:
        return {
            "root_task_id": self.parent_task_id,
            "root_criterion_id": self.parent_criterion_id,
            "occurrence_id": str(self.occurrence_id),
            "task_id": self.task_id,
            "leaf_criterion_id": self.leaf_criterion_id,
            "evidence_requirement": self.evidence_requirement,
        }


def carried_criteria_in_revision(
    semantics: Any, mission_id: str, revision: int
) -> tuple[CarriedCriterion, ...]:
    """Every ``criterion_link`` of the adopted plan, resolved to the occurrence and Task
    it lands on.

    The same walk as :func:`stored_coverage` — adopted instances, their contracts,
    slot → occurrence — kept beside it rather than folded in, because the two answer
    different questions and ``ObligationCoverage`` deliberately flattens the pairing
    this one exists to keep.  A link with no ``child_step`` lands on the finalizer, as
    :func:`~..planning.htn.compiler.coverage_from_slots` resolves it; a link whose step
    is not a slot contributes nothing (the compiler already refused that plan).  A
    link whose ``child_criterion_id`` is empty is carried under the parent's own id:
    the leaf then owes the parent criterion by name, which is what the method wrote.
    """

    from ..contracts.htn import TaskRef
    from ..storage.store import StoreError

    instances, adopted, bindings = _adopted_in_revision(semantics, mission_id, revision)
    task_of = {
        str(spec.occurrence_id): str(spec.task_id)
        for spec in semantics.list_plan_memberships(mission_id, revision)
    }
    chosen = {str(item) for item in adopted}
    carried: list[CarriedCriterion] = []
    for draft in instances:
        if str(draft.instance_id) not in chosen:
            continue
        parent = bindings.get(TaskRef(str(draft.goal_id)))
        if parent is None:
            continue
        try:
            stored = semantics.get_method(
                str(draft.method_ref.method_id), int(draft.method_ref.version)
            )
        except StoreError:
            continue
        by_slot = {str(child.slot_key): child.occurrence_id for child in draft.child_bindings}
        composition = stored.contract.composition
        finalizer = by_slot.get(composition.finalizer_step or "")
        for link in composition.criterion_links:
            bound = by_slot.get(link.child_step or "") if link.child_step else finalizer
            if bound is None or str(bound) not in task_of:
                continue
            carried.append(
                CarriedCriterion(
                    parent_task_id=str(parent.task_id),
                    parent_criterion_id=str(link.parent_criterion_id),
                    occurrence_id=bound,
                    task_id=task_of[str(bound)],
                    leaf_criterion_id=str(link.child_criterion_id or link.parent_criterion_id),
                    evidence_requirement=str(link.evidence_requirement),
                )
            )
    return tuple(carried)


def carried_criteria_for(
    semantics: Any, mission_id: str, revision: int, producer: OccurrenceId
) -> tuple[CarriedCriterion, ...]:
    """:func:`carried_criteria_in_revision`, for one occurrence."""

    return tuple(
        item
        for item in carried_criteria_in_revision(semantics, mission_id, revision)
        if str(item.occurrence_id) == str(producer)
    )


def stored_coverage(
    semantics: Any,
    instances: Sequence[Any],
    adopted: Sequence[Any],
    bindings: Mapping[Any, Any],
) -> tuple[Any, ...]:
    """The ``obligation_coverage`` claims of a plan read back from the store.

    The claims are a *function* of the adopted method instances — each one's contract
    says which parent criterion each slot covers, and the instance says which
    occurrence each slot bound — so they are recomputed rather than stored twice.
    :func:`~..planning.htn.compiler.coverage_from_slots` is that function, shared with
    the compiler so a re-read plan and a freshly compiled one cannot disagree about
    what covers what.

    An instance whose method the registry no longer holds contributes nothing rather
    than raising: the plan is still readable, and the coverage check will report the
    gap in the language it is about.
    """

    from ..contracts.htn import TaskRef
    from ..planning.htn.compiler import CompilationRefused, coverage_from_slots
    from ..storage.store import StoreError

    chosen = {str(item) for item in adopted}
    claims: list[Any] = []
    for draft in instances:
        if str(draft.instance_id) not in chosen:
            continue
        parent = bindings.get(TaskRef(str(draft.goal_id)))
        if parent is None:
            continue
        try:
            stored = semantics.get_method(
                str(draft.method_ref.method_id), int(draft.method_ref.version)
            )
        except StoreError:
            continue
        by_slot = {str(child.slot_key): child.occurrence_id for child in draft.child_bindings}
        try:
            claims.extend(
                coverage_from_slots(stored.contract, by_slot, obligation=parent.obligation_id)
            )
        except CompilationRefused:
            continue
    return tuple(claims)


def _merge_ports(consumed: Mapping[str, VersionedRef], binding: Any) -> Mapping[str, VersionedRef]:
    """``consumed`` plus the required ports a criterion-linked producer declares."""

    if binding is None:
        return consumed
    ports = dict(consumed)
    for port in getattr(binding, "output_ports", ()):
        if port.required:
            ports.setdefault(port.port_key, port.schema_ref)
    return ports


def _ports_of(requirements: Sequence[Any], producer: OccurrenceId) -> Mapping[str, VersionedRef]:
    ports: dict[str, VersionedRef] = {}
    for requirement in requirements:
        if requirement.producer_occurrence != producer:
            continue
        existing = ports.get(requirement.output_port)
        if existing is not None and existing.to_json() != requirement.schema_ref.to_json():
            raise ContractError(
                f"occurrence {producer!s} declares output port {requirement.output_port!r} "
                f"with two different schemas ({existing.id} and {requirement.schema_ref.id}); "
                "one port carries one contract"
            )
        ports[requirement.output_port] = requirement.schema_ref
    return ports


def check_declared(
    network: TaskNetworkSnapshot,
    producer: OccurrenceId,
    outputs: Sequence[AcceptedOutput],
) -> tuple[AcceptedOutput, ...]:
    """Refuse any stated output whose port or producer the plan does not declare.

    This is the gate that keeps ``_recorded_outputs`` from becoming the ancestor
    sweep again: an index entry exists only where the plan drew an edge, so a
    consumer can only ever bind to something a ``DataRequirement`` said it would get.
    """

    return check_against_ports(declared_output_ports(network, producer), producer, outputs)


def check_against_ports(
    ports: Mapping[str, VersionedRef],
    producer: OccurrenceId,
    outputs: Sequence[AcceptedOutput],
) -> tuple[AcceptedOutput, ...]:
    """:func:`check_declared`'s rule, applied to an already-read port table."""

    for output in outputs:
        if output.producer_occurrence != producer:
            raise ContractError(
                f"accepted output claims producer {output.producer_occurrence!s}, but the "
                f"acceptance being committed belongs to {producer!s}"
            )
        if output.output_port not in ports:
            raise ContractError(
                f"occurrence {producer!s} has no declared output port "
                f"{output.output_port!r} in this plan revision; the declared ports are "
                f"{sorted(ports)} (§24.1 decision 4: an index entry exists only where the "
                "plan drew an edge)"
            )
        declared = ports[output.output_port]
        if output.schema_ref.to_json() != declared.to_json():
            raise ContractError(
                f"accepted output at {producer!s}.{output.output_port} states schema "
                f"{output.schema_ref.id}@{output.schema_ref.version}, while the plan's data "
                f"requirement declares {declared.id}@{declared.version}"
            )
    return tuple(outputs)


__all__ = (
    "CarriedCriterion",
    "accepted_output_from_json",
    "accepted_output_json",
    "carried_criteria_for",
    "carried_criteria_in_revision",
    "check_against_ports",
    "check_declared",
    "coverage_in_revision",
    "criterion_linked_occurrences",
    "declared_output_ports",
    "declared_ports_in_revision",
    "output_ports_in_revision",
    "stored_coverage",
)
