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

    Read from the :class:`~..contracts.htn.DataRequirement` edges rather than from the
    producer's own port list, because a port with no consumer feeds nothing and a
    consumer's compatibility check is made against the *edge's* schema.  A port
    declared twice with two schemas is refused rather than resolved to the last one:
    two answers about what flows down one edge is not something a later read settles.
    """

    return _ports_of(network.data_requirements, producer)


def declared_ports_in_revision(
    requirements: Sequence[Any], producer: OccurrenceId
) -> Mapping[str, VersionedRef]:
    """:func:`declared_output_ports` for a caller holding the *rows*, not a network.

    ``accept_review`` runs inside the write transaction and must not rebuild the
    whole typed network to check one port — that is the slow read
    ``plan_commits`` keeps out of a transaction on purpose.  It holds the plan
    revision's ``data_requirements`` rows instead, and the rule applied to them has
    to be the same rule, so both readers call :func:`_ports_of` and neither owns a
    second copy of "which ports this occurrence declares".
    """

    return _ports_of(requirements, producer)


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
    "accepted_output_from_json",
    "accepted_output_json",
    "check_against_ports",
    "check_declared",
    "declared_output_ports",
    "declared_ports_in_revision",
)
