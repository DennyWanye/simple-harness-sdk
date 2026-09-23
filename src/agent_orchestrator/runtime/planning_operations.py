# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read-only operation identity/action admission seam (H1H-ADM-1.0, Blocker O).

The mapper joins only frozen operation identity, occurrence binding, explicit
identity bridge, and the exact action version named by that bridge.  It never
joins by task, target, connector text, or "latest" action and never writes
execution state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol, cast

from simple_harness.contracts import canonical_json

from ..storage.planning_admission_store import PlanningAdmissionStore
from .connectors import Receipt


class SourceUnavailable(RuntimeError):
    """A required producer or exact identity join could not be read safely."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        self.reason = str(reason)
        self.detail = str(detail)
        super().__init__(f"{self.reason}: {self.detail}" if self.detail else self.reason)


class OperationEffect(StrEnum):
    NOT_HANDED_OFF = "NOT_HANDED_OFF"
    IN_FLIGHT = "IN_FLIGHT"
    APPLIED = "APPLIED"
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class OperationBindingRow:
    mission_id: str
    operation_id: str
    operation_occurrence_id: str
    request_hash: str
    envelope_hash: str
    principal_id: str = ""
    scope_id: str = ""
    obligation_id: str = ""


@dataclass(frozen=True, slots=True)
class OperationActionLinkRow:
    mission_id: str
    operation_id: str
    operation_occurrence_id: str
    request_hash: str
    envelope_hash: str
    action_key: str
    action_id: str
    action_version: int
    params_hash: str
    idempotency_key: str
    principal_id: str = ""
    scope_id: str = ""
    obligation_id: str = ""


@dataclass(frozen=True, slots=True)
class ActionLedgerRow:
    mission_id: str
    action_key: str
    action_id: str
    version: int
    params_hash: str
    idempotency_key: str | None
    state: str
    handoffs: int
    matching_success_receipt: bool = False
    authoritative_not_applied: bool = False
    all_handoffs_covered: bool = False
    no_late_apply_proven: bool = False
    history: tuple[Mapping[str, Any], ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _VerifiedScopedNegative:
    action_key: str


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    mission_id: str
    read_digest: str
    effects: tuple[tuple[str, OperationEffect], ...]
    bindings: tuple[OperationBindingRow, ...] = ()
    links: tuple[OperationActionLinkRow, ...] = ()
    actions: tuple[ActionLedgerRow, ...] = ()
    complete_read: bool = True

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(
            operation_id
            for operation_id, effect in self.effects
            if effect in {OperationEffect.IN_FLIGHT, OperationEffect.UNRESOLVED}
        )

    @property
    def complete_empty(self) -> bool:
        # A REFUSED/zero-handoff row is retained in the digest but is explicitly
        # non-materialized, so it does not make an otherwise empty operation set
        # look executable.
        return not self.bindings and not self.links and not self.effects

    @property
    def operation_effects(self) -> Mapping[str, OperationEffect]:
        return dict(self.effects)


# Names used by the H1-H reference and by callers that prefer the shorter rows.
BindingRow = OperationBindingRow
LinkRow = OperationActionLinkRow
ActionRow = ActionLedgerRow


@dataclass(frozen=True, slots=True)
class BoundPlanningOperationOrigin:
    """System-frozen identity used when a candidate becomes a real action."""

    operation_id: str
    operation_occurrence_id: str
    request_hash: str
    mission_id: str
    envelope_hash: str
    principal_id: str
    scope_id: str
    obligation_id: str
    producer_task_id: str
    producer_htn_occurrence_id: str
    producer_contract_revision: int
    producer_plan_revision: int
    provenance_receipt_id: str


@dataclass(frozen=True, slots=True)
class RuntimeWorkSnapshot:
    mission_id: str = ""
    retiring_instance_ids: tuple[str, ...] = ()
    live_attempt_refs: tuple[Mapping[str, Any], ...] = ()
    live_dispatch_intent_refs: tuple[Mapping[str, Any], ...] = ()
    handed_off_actions: tuple[str, ...] = ()
    snapshot_digest: str = ""

    @property
    def requires_convergence(self) -> bool:
        return bool(
            self.live_attempt_refs or self.live_dispatch_intent_refs or self.handed_off_actions
        )


class OperationReader(Protocol):
    def read_operation_sources(self, mission_id: str) -> Mapping[str, Any]: ...


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _row(value: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        document = dict(to_json())
        if "operation_id" in document and "operation_occurrence_id" in document:
            document.setdefault("mission_id", str(value.mission_id))
            document.setdefault("request_hash", str(value.request_hash))
            document.setdefault("obligation_id", str(value.obligation_id))
            document.setdefault("scope_id", str(value.scope_id))
            document.setdefault("envelope_hash", _digest(document))
        return document
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable("operation_source_row_invalid") from exc


def _receipt_matches(action: Mapping[str, Any]) -> bool:
    receipt = action.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    try:
        parsed = Receipt.from_json(receipt)
    except (KeyError, TypeError, ValueError):
        return False
    return parsed.applied and all(
        getattr(parsed, name) == action.get(name)
        for name in ("idempotency_key", "params_hash", "target", "connector", "operation")
    )


def _classify(action: ActionLedgerRow) -> OperationEffect:
    known = {
        "PROPOSED",
        "AWAITING_APPROVAL",
        "APPROVED",
        "HANDED_OFF",
        "UNKNOWN",
        "SUCCEEDED",
        "FAILED",
        "REJECTED",
        "REVOKED",
        "EXPIRED",
        "SUPERSEDED",
        "CANCELLED",
        "REFUSED",
    }
    if action.state not in known:
        raise SourceUnavailable("unrecognized_action_state", detail=action.state)
    if action.state == "SUCCEEDED":
        if action.handoffs >= 1 and action.matching_success_receipt:
            return OperationEffect.APPLIED
        raise SourceUnavailable("success_without_matching_receipt", detail=action.action_key)
    if action.state == "HANDED_OFF":
        return OperationEffect.IN_FLIGHT
    if action.state == "UNKNOWN":
        if (
            action.authoritative_not_applied
            and action.all_handoffs_covered
            and action.no_late_apply_proven
        ):
            return OperationEffect.CONFIRMED_NOT_APPLIED
        return OperationEffect.UNRESOLVED
    if action.state in {"PROPOSED", "AWAITING_APPROVAL", "APPROVED"} and action.handoffs == 0:
        return OperationEffect.NOT_HANDED_OFF
    if action.state in {
        "FAILED",
        "REJECTED",
        "REVOKED",
        "EXPIRED",
        "SUPERSEDED",
        "CANCELLED",
        "REFUSED",
    }:
        if action.handoffs == 0:
            return OperationEffect.CONFIRMED_NOT_APPLIED
        if (
            action.authoritative_not_applied
            and action.all_handoffs_covered
            and action.no_late_apply_proven
        ):
            return OperationEffect.CONFIRMED_NOT_APPLIED
    return OperationEffect.UNRESOLVED


def _as_binding(value: Mapping[str, Any]) -> OperationBindingRow:
    return OperationBindingRow(
        mission_id=str(value["mission_id"]),
        operation_id=str(value["operation_id"]),
        operation_occurrence_id=str(
            value.get("operation_occurrence_id", value.get("occurrence_id"))
        ),
        request_hash=str(value["request_hash"]),
        envelope_hash=str(value["envelope_hash"]),
        principal_id=str(value.get("principal_id", "")),
        scope_id=str(value.get("scope_id", "")),
        obligation_id=str(value.get("obligation_id", "")),
    )


def _as_link(value: Mapping[str, Any]) -> OperationActionLinkRow:
    return OperationActionLinkRow(
        mission_id=str(value["mission_id"]),
        operation_id=str(value["operation_id"]),
        operation_occurrence_id=str(
            value.get("operation_occurrence_id", value.get("occurrence_id"))
        ),
        request_hash=str(value["request_hash"]),
        envelope_hash=str(value["envelope_hash"]),
        action_key=str(value["action_key"]),
        action_id=str(value["action_id"]),
        action_version=int(value.get("action_version", value.get("version"))),
        params_hash=str(value["params_hash"]),
        idempotency_key=str(value["idempotency_key"]),
        principal_id=str(value.get("principal_id", "")),
        scope_id=str(value.get("scope_id", "")),
        obligation_id=str(value.get("obligation_id", "")),
    )


def _as_action(value: Mapping[str, Any]) -> ActionLedgerRow:
    proof = value.get("_verified_scoped_negative")
    verified = (
        isinstance(proof, _VerifiedScopedNegative) and proof.action_key == value["action_key"]
    )
    raw = {k: v for k, v in value.items() if k != "_verified_scoped_negative"}
    return ActionLedgerRow(
        mission_id=str(value["mission_id"]),
        action_key=str(value["action_key"]),
        action_id=str(value["action_id"]),
        version=int(value["version"]),
        params_hash=str(value["params_hash"]),
        idempotency_key=None
        if value.get("idempotency_key") is None
        else str(value["idempotency_key"]),
        state=str(value["state"]),
        handoffs=int(value.get("handoffs") or 0),
        matching_success_receipt=_receipt_matches(value),
        authoritative_not_applied=verified,
        all_handoffs_covered=verified,
        no_late_apply_proven=verified,
        history=tuple(value.get("history") or ()),
        raw=raw,
    )


def _source_value(
    reader: OperationReader | Mapping[str, Any], mission_id: str
) -> Mapping[str, Any]:
    if isinstance(reader, Mapping):
        return reader
    method = getattr(reader, "read_operation_sources", None)
    if not callable(method):
        bindings_reader = getattr(reader, "list_operation_bindings", None)
        links_reader = getattr(reader, "list_operation_action_links", None)
        actions_reader = getattr(reader, "list_actions", None)
        if not all(callable(item) for item in (bindings_reader, links_reader, actions_reader)):
            raise SourceUnavailable("operation_reader_unavailable")
        bindings_reader_fn = cast(Callable[[str], Any], bindings_reader)
        links_reader_fn = cast(Callable[[str], Any], links_reader)
        actions_reader_fn = cast(Callable[[str], Any], actions_reader)
        try:
            return {
                "complete_read": True,
                "bindings": tuple(bindings_reader_fn(mission_id)),
                "links": tuple(links_reader_fn(mission_id)),
                "actions": tuple(actions_reader_fn(mission_id)),
            }
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable("operation_read_failed", detail=type(exc).__name__) from exc
    try:
        value = method(mission_id)
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable("operation_read_failed", detail=type(exc).__name__) from exc
    if not isinstance(value, Mapping):
        raise SourceUnavailable("operation_read_incomplete")
    return value


def build_operation_snapshot(
    mission_id: str, *, reader: OperationReader | Mapping[str, Any], now_ms: int = 0
) -> OperationSnapshot:
    """Build a complete, conservative operation snapshot from one read source."""

    del now_ms  # time never turns an expired lease into a negative proof
    source = _source_value(reader, mission_id)
    if source.get("complete_read") is not True:
        raise SourceUnavailable("operation_read_incomplete")
    try:
        bindings = tuple(_as_binding(_row(item)) for item in source.get("bindings", ()))
        links = tuple(_as_link(_row(item)) for item in source.get("links", ()))
        actions = tuple(_as_action(_row(item)) for item in source.get("actions", ()))
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailable("operation_source_row_invalid") from exc
    for group in (bindings, links, actions):
        if any(row.mission_id != mission_id for row in group):
            raise SourceUnavailable("cross_mission_rows")
    if len({row.operation_id for row in bindings}) != len(bindings):
        raise SourceUnavailable("mapping_not_unique")
    if len({row.operation_id for row in links}) != len(links) or len(
        {row.action_key for row in links}
    ) != len(links):
        raise SourceUnavailable("mapping_not_unique")
    action_map = {row.action_key: row for row in actions}
    # A refusal with no idempotency identity is the one explicit non-materialized
    # record.  It remains in the digest but has no operation effect to gate.
    executable_actions = tuple(
        row
        for row in actions
        if not (row.state == "REFUSED" and row.handoffs == 0 and row.idempotency_key is None)
    )
    if set(row.operation_id for row in bindings) != {row.operation_id for row in links}:
        raise SourceUnavailable("operation_mapping_incomplete")
    if {row.action_key for row in links} != {row.action_key for row in executable_actions}:
        raise SourceUnavailable("operation_mapping_incomplete")
    binding_map = {row.operation_id: row for row in bindings}
    link_map = {row.operation_id: row for row in links}
    effects: list[tuple[str, OperationEffect]] = []
    for operation_id, link in sorted(link_map.items()):
        bound = binding_map[operation_id]
        action = action_map.get(link.action_key)
        if action is None:
            raise SourceUnavailable("operation_mapping_incomplete")
        if (link.operation_occurrence_id, link.request_hash, link.envelope_hash) != (
            bound.operation_occurrence_id,
            bound.request_hash,
            bound.envelope_hash,
        ):
            raise SourceUnavailable("envelope_link_mismatch")
        if link.principal_id and bound.principal_id and link.principal_id != bound.principal_id:
            raise SourceUnavailable("envelope_link_mismatch")
        if link.scope_id and bound.scope_id and link.scope_id != bound.scope_id:
            raise SourceUnavailable("envelope_link_mismatch")
        if link.obligation_id and bound.obligation_id and link.obligation_id != bound.obligation_id:
            raise SourceUnavailable("envelope_link_mismatch")
        if (action.action_id, action.version, action.params_hash, action.idempotency_key) != (
            link.action_id,
            link.action_version,
            link.params_hash,
            link.idempotency_key,
        ):
            raise SourceUnavailable("action_link_mismatch")
        effects.append((operation_id, _classify(action)))
    raw = {
        "mission": mission_id,
        "bindings": [asdict(row) for row in bindings],
        "links": [asdict(row) for row in links],
        "actions": [
            {**asdict(row), "history": list(row.history), "raw": dict(row.raw)} for row in actions
        ],
    }
    return OperationSnapshot(
        mission_id=mission_id,
        read_digest=_digest(raw),
        effects=tuple(effects),
        bindings=bindings,
        links=links,
        actions=actions,
        complete_read=True,
    )


def operation_gate(snapshot: OperationSnapshot) -> None:
    if snapshot.unresolved:
        raise SourceUnavailable("operation_unresolved", detail=",".join(snapshot.unresolved))


class StoreOperationReader:
    """Consistent reader over the existing Store and HtnStore tables."""

    def __init__(
        self,
        store: Any,
        *,
        max_rows: int = 10_000,
        ignore_intent_ids: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.max_rows = max_rows
        self.ignore_intent_ids = frozenset(str(item) for item in ignore_intent_ids)

    def read_operation_sources(self, mission_id: str) -> Mapping[str, Any]:
        with self.store.read_view():
            try:
                adapter = PlanningAdmissionStore(self.store)
                bindings = adapter.list_operation_bindings(mission_id)
                links = adapter.list_operation_action_links(mission_id)
                actions = adapter.list_operation_actions(mission_id)
                from .operation_reconciliation import stored_negative_proof

                actions = [
                    dict(
                        action,
                        _verified_scoped_negative=(
                            _VerifiedScopedNegative(action["action_key"])
                            if stored_negative_proof(self.store, action)
                            else None
                        ),
                    )
                    for action in actions
                ]
            except Exception as exc:
                raise SourceUnavailable("operation_read_failed", detail=type(exc).__name__) from exc
            if len(bindings) + len(links) + len(actions) > self.max_rows:
                raise SourceUnavailable("operation_read_incomplete", detail="row_limit")
            return {
                "complete_read": True,
                "bindings": bindings,
                "links": links,
                "actions": actions,
            }

    def read_running_work(
        self, mission_id: str, retiring_instance_ids: Sequence[str]
    ) -> Mapping[str, Any]:
        """Read live attempts/intents and handed-off actions from one snapshot.

        ``retiring_instance_ids`` is preserved in the returned evidence; it is not
        used to discard work.  A cancelled Task with an active Attempt therefore
        remains a convergence requirement.
        """

        from .operation_reconciliation import stored_negative_proof

        with self.store.read_view() as connection:
            try:
                attempts = connection.execute(
                    "SELECT attempt_id,task_id,status,lease_owner,lease_expires_at,version,json "
                    "FROM attempts WHERE mission_id=? AND status IN "
                    "('PENDING','CLAIMED','RUNNING','SUBMITTED','VERIFYING') "
                    "ORDER BY attempt_id",
                    (mission_id,),
                ).fetchall()
                intents = connection.execute(
                    "SELECT intent_id,subject_id,state,version,lease_owner,lease_expires_at,"
                    "config_json AS json "
                    "FROM dispatch_intents WHERE mission_id=? AND state IN "
                    "('PENDING','CLAIMED','AGENT_CREATED','SUBMITTED') ORDER BY intent_id",
                    (mission_id,),
                ).fetchall()
                handed = connection.execute(
                    "SELECT action_key,state,json FROM actions "
                    "WHERE mission_id=? AND state IN ('HANDED_OFF','UNKNOWN') ORDER BY action_key",
                    (mission_id,),
                ).fetchall()
            except Exception as exc:
                raise SourceUnavailable(
                    "running_work_read_failed", detail=type(exc).__name__
                ) from exc
            return {
                "complete_read": True,
                "retiring_instance_ids": tuple(str(item) for item in retiring_instance_ids),
                "live_attempt_refs": [
                    {
                        "attempt_id": row[0],
                        "task_id": row[1],
                        "status": row[2],
                        "lease_owner": row[3],
                        "lease_expires_at": row[4],
                        "version": row[5],
                        "json": json.loads(row[6]),
                    }
                    for row in attempts
                ],
                "live_dispatch_intent_refs": [
                    {
                        "intent_id": row[0],
                        "subject_id": row[1],
                        "state": row[2],
                        "version": row[3],
                        "lease_owner": row[4],
                        "lease_expires_at": row[5],
                        "json": json.loads(row[6]),
                    }
                    for row in intents
                    if str(row[0]) not in self.ignore_intent_ids
                ],
                "handed_off_actions": [
                    str(row[0])
                    for row in handed
                    if row[1] == "HANDED_OFF"
                    or not stored_negative_proof(self.store, json.loads(row[2]))
                ],
            }


def read_running_work(
    mission_id: str, retiring_instance_ids: Sequence[str], *, reader: Any
) -> RuntimeWorkSnapshot:
    method = getattr(reader, "read_running_work", None)
    if not callable(method):
        raise SourceUnavailable("running_work_reader_unavailable")
    try:
        value = method(mission_id, tuple(retiring_instance_ids))
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable("running_work_read_failed", detail=type(exc).__name__) from exc
    if not isinstance(value, Mapping) or value.get("complete_read") is not True:
        raise SourceUnavailable("running_work_read_incomplete")
    attempts = tuple(dict(item) for item in value.get("live_attempt_refs", ()))
    intents = tuple(dict(item) for item in value.get("live_dispatch_intent_refs", ()))
    handed = tuple(str(item) for item in value.get("handed_off_actions", ()))
    raw = {
        "mission_id": mission_id,
        "retiring_instance_ids": list(retiring_instance_ids),
        "live_attempt_refs": list(attempts),
        "live_dispatch_intent_refs": list(intents),
        "handed_off_actions": list(handed),
    }
    return RuntimeWorkSnapshot(
        mission_id=mission_id,
        retiring_instance_ids=tuple(str(item) for item in retiring_instance_ids),
        live_attempt_refs=attempts,
        live_dispatch_intent_refs=intents,
        handed_off_actions=handed,
        snapshot_digest=_digest(raw),
    )


__all__ = (
    "ActionLedgerRow",
    "ActionRow",
    "BindingRow",
    "BoundPlanningOperationOrigin",
    "LinkRow",
    "OperationActionLinkRow",
    "OperationBindingRow",
    "OperationEffect",
    "OperationReader",
    "OperationSnapshot",
    "RuntimeWorkSnapshot",
    "SourceUnavailable",
    "StoreOperationReader",
    "build_operation_snapshot",
    "operation_gate",
    "read_running_work",
)
