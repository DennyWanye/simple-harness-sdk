# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Durable dispatch/result anchors for the operation-completion protocol.

The worker's resolved input set and its local output-port claims are needed again
when a verified result is accepted after a restart or through Selection.  They
therefore cannot live only in the event handler.  This module freezes and reads
those anchors; it does not decide acceptance or materialise an output itself.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.versioning import UpstreamInput, manifest_upstream_inputs
from ..contracts.htn import TaskRef
from ..contracts.models import sha256_hex
from ..contracts.operation_completion import PlanRevisionPinV1
from ..contracts.semantic_base import content_hash_of
from ..runtime.output_blocks import PortClaim
from ..storage.htn_store import HtnStore
from ..storage.store import Store
from .accepted_outputs import check_against_ports, output_ports_in_revision
from .hierarchical_dispatch import HierarchicalDispatch
from .leaf_acceptance import accepted_outputs_for
from .operation_completion import OperationCompletionError, OperationCompletionReader

_PROTOCOL = "planning-decision-v1"
_RESULT_SUBMITTED = "ResultSubmitted"


def _refuse(code: str, message: str) -> OperationCompletionError:
    return OperationCompletionError(code, message)


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenCompletionInputs:
    """The exact frozen input manifest for one new-protocol Attempt.

    ``manifest_hash`` is the immutable HtnStore document address.  The embedded
    ``InputManifest`` has its own protocol hash, retained separately because the
    two hashes cover different canonical documents.
    """

    schema_version: int
    occurrence_id: str
    scope_id: str
    scope_hash: str
    plan_revision: int
    plan_snapshot_hash: str
    task_id: str
    task_revision: int
    task_hash: str
    manifest_hash: str
    embedded_manifest_hash: str
    attempt_id: str
    request_id: str
    manifest: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "occurrence_id": self.occurrence_id,
            "scope_id": self.scope_id,
            "scope_hash": self.scope_hash,
            "plan_revision": self.plan_revision,
            "plan_snapshot_hash": self.plan_snapshot_hash,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "task_hash": self.task_hash,
            "manifest_hash": self.manifest_hash,
            "embedded_manifest_hash": self.embedded_manifest_hash,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "manifest": dict(self.manifest),
        }

    @classmethod
    def from_json(cls, value: object) -> FrozenCompletionInputs:
        if not isinstance(value, Mapping):
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "completion inputs are not an object")
        required = {
            "schema_version",
            "occurrence_id",
            "scope_id",
            "scope_hash",
            "plan_revision",
            "plan_snapshot_hash",
            "task_id",
            "task_revision",
            "task_hash",
            "manifest_hash",
            "embedded_manifest_hash",
            "attempt_id",
            "request_id",
            "manifest",
        }
        if set(value) != required:
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "completion inputs shape differs")
        integer_names = ("schema_version", "plan_revision", "task_revision")
        for name in integer_names:
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int):
                raise _refuse(
                    "OP_COMPLETION_INPUTS_UNAVAILABLE", f"completion inputs {name} differs"
                )
        string_names = (
            "scope_id",
            "occurrence_id",
            "scope_hash",
            "plan_snapshot_hash",
            "task_id",
            "task_hash",
            "manifest_hash",
            "embedded_manifest_hash",
            "attempt_id",
            "request_id",
        )
        if any(not isinstance(value[name], str) or not value[name] for name in string_names):
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "completion inputs identity differs")
        if value["schema_version"] != 1 or not isinstance(value["manifest"], Mapping):
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "completion inputs codec differs")
        return cls(
            schema_version=value["schema_version"],
            occurrence_id=value["occurrence_id"],
            scope_id=value["scope_id"],
            scope_hash=value["scope_hash"],
            plan_revision=value["plan_revision"],
            plan_snapshot_hash=value["plan_snapshot_hash"],
            task_id=value["task_id"],
            task_revision=value["task_revision"],
            task_hash=value["task_hash"],
            manifest_hash=value["manifest_hash"],
            embedded_manifest_hash=value["embedded_manifest_hash"],
            attempt_id=value["attempt_id"],
            request_id=value["request_id"],
            manifest=dict(value["manifest"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenCompletionResultInputs:
    """Result-specific durable anchors used by both normal and Selection acceptance."""

    frozen: FrozenCompletionInputs
    scope: Any
    port_claims: tuple[PortClaim, ...]


def _new_protocol(store: Store, mission_id: str) -> bool:
    row = store.connection.execute(
        "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id=?", (mission_id,)
    ).fetchone()
    return row is not None and row[0] == _PROTOCOL


def _exact_inputs(values: Sequence[Mapping[str, Any]]) -> Counter[tuple[str, str, str, str]]:
    rows: Counter[tuple[str, str, str, str]] = Counter()
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {
            "task_id",
            "path",
            "content_hash",
            "artifact_id",
        }:
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "dispatch inputs differ")
        if any(not isinstance(value[name], str) for name in value):
            raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "dispatch input type differs")
        rows[(value["task_id"], value["path"], value["content_hash"], value["artifact_id"])] += 1
    return rows


def _upstream_counter(inputs: Sequence[UpstreamInput]) -> Counter[tuple[str, str, str, str]]:
    return Counter(
        (item.task_id, item.path, item.content_hash, item.artifact_id) for item in inputs
    )


def freeze_attempt_completion_inputs(
    store: Store,
    commit: Any,
    task: Any,
    inputs: Sequence[Mapping[str, Any]],
    *,
    attempt_id: str,
    request_id: str,
    selection_inputs: Sequence[UpstreamInput] = (),
) -> FrozenCompletionInputs | None:
    """Freeze the exact resolved manifest used to build a new-protocol Attempt.

    Call this from the ``create_attempt`` writer transaction, after the Attempt and
    dispatch intent identities exist.  Legacy Missions return ``None`` unchanged.
    """

    mission_id = str(getattr(task, "mission_id", ""))
    task_id = str(getattr(task, "id", ""))
    if not mission_id or not task_id:
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "Task identity is unavailable")
    if not _new_protocol(store, mission_id):
        return None
    if (
        not isinstance(attempt_id, str)
        or not attempt_id
        or not isinstance(request_id, str)
        or not request_id
    ):
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "Attempt binding identity is unavailable")

    dispatch = HierarchicalDispatch.for_commit(commit, mission_id)
    network = dispatch.network(mission_id)
    active = HtnStore(store).active_plan_revision(mission_id)
    if active is None:
        raise _refuse("OP_COMPLETION_SCOPE_UNRESOLVED", "active plan is unavailable")
    members = [item for item in network.occurrences if str(item.task_id) == task_id]
    if len(members) != 1:
        raise _refuse("OP_COMPLETION_SCOPE_UNRESOLVED", "Task occurrence is missing or ambiguous")
    plan_ref = PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash)
    scope = OperationCompletionReader(store).read_scope(
        mission_id, plan_ref, str(members[0].occurrence_id)
    )
    resolved = dispatch.resolved_inputs(mission_id, network, members[0])
    manifest = resolved.manifest
    if manifest is None or not manifest.is_frozen:
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "dispatch manifest is not frozen")
    target_rules = dispatch.target_rules_for(task_id)
    expected = manifest_upstream_inputs(manifest, target_rules, network=network)
    expected = dispatch.overlay_attempt_inputs(mission_id, expected)
    # Candidate material is independently authorized by the original Selection
    # decision. It remains outside the accepted DATA manifest and carries its
    # candidate namespace; never promote it into a DATA binding.
    if _exact_inputs(inputs) != _upstream_counter([*expected, *selection_inputs]):
        raise _refuse(
            "OP_COMPLETION_INPUTS_UNAVAILABLE", "dispatch inputs differ from frozen manifest"
        )

    document = manifest.to_json()
    embedded_hash = document.get("manifest_hash")
    if not isinstance(embedded_hash, str) or embedded_hash != manifest.manifest_hash():
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "embedded manifest hash differs")
    digest = HtnStore(store).insert_input_manifest(
        mission_id,
        task_id,
        document,
        attempt_id=attempt_id,
        request_id=request_id,
    )
    return FrozenCompletionInputs(
        schema_version=1,
        occurrence_id=str(members[0].occurrence_id),
        scope_id=str(scope.scope_id),
        scope_hash=scope.content_hash(),
        plan_revision=plan_ref.revision,
        plan_snapshot_hash=plan_ref.snapshot_hash,
        task_id=scope.task_ref.id,
        task_revision=scope.task_ref.revision,
        task_hash=scope.task_ref.content_hash,
        manifest_hash=digest,
        embedded_manifest_hash=embedded_hash,
        attempt_id=attempt_id,
        request_id=request_id,
        manifest=document,
    )


def _claims_from_event(value: object) -> tuple[PortClaim, ...]:
    if not isinstance(value, list):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result port claims are unavailable")
    claims: list[PortClaim] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"port_key", "path"}:
            raise _refuse(
                "OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result port claim shape differs"
            )
        port, path = item["port_key"], item["path"]
        if (
            not isinstance(port, str)
            or not port
            or not isinstance(path, str)
            or not path
            or port in seen
        ):
            raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result port claim differs")
        seen.add(port)
        claims.append(PortClaim(port_key=port, path=path))
    return tuple(claims)


def _verify_frozen_document(store: Store, frozen: FrozenCompletionInputs) -> None:
    document = HtnStore(store).get_input_manifest(frozen.manifest_hash)
    if document != dict(frozen.manifest) or content_hash_of(document) != frozen.manifest_hash:
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "stored input manifest differs")
    if (
        document.get("manifest_hash") != frozen.embedded_manifest_hash
        or document.get("pending") != []
        or not isinstance(document.get("consumer_task_ref"), str)
        or not isinstance(document.get("bindings"), list)
    ):
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "stored input manifest is not frozen")
    payload = {"consumer_task_ref": document["consumer_task_ref"], "bindings": document["bindings"]}
    if sha256_hex(payload) != frozen.embedded_manifest_hash:
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "stored embedded manifest hash differs")


def validate_result_port_claims(
    store: Store,
    *,
    commit: Any,
    mission: Any,
    task: Any,
    result: Any,
    artifacts: Sequence[Any],
    port_claims: Sequence[PortClaim],
) -> tuple[dict[str, str], ...]:
    """Validate model-local port/path claims against the stored result and live plan."""

    mission_id = str(getattr(mission, "id", ""))
    task_id = str(getattr(task, "id", ""))
    envelope = getattr(result, "envelope", None)
    if (
        not mission_id
        or not task_id
        or envelope is None
        or getattr(envelope, "mission_id", None) != mission_id
        or getattr(envelope, "task_id", None) != task_id
        or not _new_protocol(store, mission_id)
    ):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result identity differs")
    if not all(isinstance(item, PortClaim) for item in port_claims):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "port claim type differs")
    if len({item.port_key for item in port_claims}) != len(port_claims):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result port claim is repeated")
    result_ids = tuple(getattr(result, "artifacts", ()))
    if Counter(str(getattr(item, "id", "")) for item in artifacts) != Counter(result_ids):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result artifacts differ")
    if any(
        getattr(item, "mission_id", None) != mission_id
        or getattr(item, "task_id", None) != task_id
        or getattr(item, "attempt_id", None) != envelope.attempt_id
        for item in artifacts
    ):
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result artifact ownership differs")
    if len({str(getattr(item, "path", "")) for item in artifacts}) != len(artifacts):
        raise _refuse(
            "OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "result artifact paths are ambiguous"
        )

    htn = HtnStore(store)
    active = htn.active_plan_revision(mission_id)
    if active is None:
        raise _refuse("OP_COMPLETION_SCOPE_UNRESOLVED", "active plan is unavailable")
    members = [
        item
        for item in htn.list_plan_memberships(mission_id, active.revision)
        if str(item.task_id) == task_id
    ]
    if len(members) != 1:
        raise _refuse("OP_COMPLETION_SCOPE_UNRESOLVED", "Task occurrence is missing or ambiguous")
    ports = output_ports_in_revision(
        htn, mission_id, active.revision, members[0].occurrence_id, task_id
    )
    outputs = accepted_outputs_for(
        ports,
        occurrence=members[0].occurrence_id,
        task_id=TaskRef(task_id),
        result_id=str(envelope.id),
        acceptance_id="completion-input-validation",
        support_revision=0,
        artifacts=artifacts,
        namespace="workspace",
        claims=port_claims,
    )
    if len(outputs) != len(port_claims):
        raise _refuse(
            "OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "port claim cannot bind a result artifact"
        )
    check_against_ports(ports, members[0].occurrence_id, outputs)
    if set(ports) - {item.output_port for item in outputs}:
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "required output port is unclaimed")
    return tuple({"port_key": item.port_key, "path": item.path} for item in port_claims)


def load_completion_result_inputs(store: Store, result: Any) -> FrozenCompletionResultInputs | None:
    """Re-read frozen dispatch inputs and durable ResultSubmitted port claims.

    ``None`` is only the legacy path.  A new-protocol Result without these anchors
    is refused so Selection and cold recovery cannot silently fall back to a current
    plan or to handler-local state.
    """

    envelope = getattr(result, "envelope", None)
    if envelope is None:
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "result envelope is unavailable")
    mission_id, task_id, attempt_id, result_id = (
        str(envelope.mission_id),
        str(envelope.task_id),
        str(envelope.attempt_id),
        str(envelope.id),
    )
    if not _new_protocol(store, mission_id):
        return None
    attempt = store.get_attempt(attempt_id)
    intent = store.get_intent_for_subject(attempt_id)
    if (
        attempt is None
        or intent is None
        or attempt.mission_id != mission_id
        or attempt.task_id != task_id
        or intent.mission_id != mission_id
        or intent.kind != "attempt"
        or intent.subject_id != attempt_id
        or intent.config.get("attempt_id") != attempt_id
    ):
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "original Attempt intent differs")
    frozen = FrozenCompletionInputs.from_json(intent.config.get("completion_inputs"))
    if (
        frozen.task_id != task_id
        or frozen.attempt_id != attempt_id
        or frozen.request_id != intent.intent_id
    ):
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "frozen Attempt binding differs")
    _verify_frozen_document(store, frozen)
    bindings = HtnStore(store).list_manifest_bindings(frozen.manifest_hash)
    if not any(
        item["mission_id"] == mission_id and item["task_id"] == task_id for item in bindings
    ):
        raise _refuse("OP_COMPLETION_INPUTS_UNAVAILABLE", "input manifest is not bound to Attempt")

    plan_ref = PlanRevisionPinV1(
        revision=frozen.plan_revision, snapshot_hash=frozen.plan_snapshot_hash
    )
    scope = OperationCompletionReader(store).read_scope(mission_id, plan_ref, frozen.occurrence_id)
    if (
        str(scope.scope_id) != frozen.scope_id
        or scope.content_hash() != frozen.scope_hash
        or scope.task_ref.id != frozen.task_id
        or scope.task_ref.revision != frozen.task_revision
        or scope.task_ref.content_hash != frozen.task_hash
    ):
        raise _refuse("OP_EFFECT_SCOPE_STALE", "frozen completion scope differs")

    events = [
        event
        for event in store.iter_events(mission_id)
        if event.type == _RESULT_SUBMITTED
        and event.idempotency_key == f"{_RESULT_SUBMITTED}:{result_id}"
        and event.task_id == task_id
        and event.attempt_id == attempt_id
    ]
    if len(events) != 1 or events[0].payload.get("result_id") != result_id:
        raise _refuse("OP_COMPLETION_PORT_CLAIMS_UNAVAILABLE", "ResultSubmitted source differs")
    return FrozenCompletionResultInputs(
        frozen=frozen,
        scope=scope,
        port_claims=_claims_from_event(events[0].payload.get("completion_port_claims")),
    )


__all__ = (
    "FrozenCompletionInputs",
    "FrozenCompletionResultInputs",
    "freeze_attempt_completion_inputs",
    "load_completion_result_inputs",
    "validate_result_port_claims",
)
