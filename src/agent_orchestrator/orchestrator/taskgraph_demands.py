# SPDX-License-Identifier: Apache-2.0
"""Read independent demand provenance from the original obligation event ledger."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.models import sha256_hex
from ..contracts.obligations import ObligationAccountView
from ..runtime.planning_operations import SourceUnavailable
from ..storage.obligation_store import ObligationStore
from ..storage.store import Store
from .obligation_commits import REQUESTER_KINDS


@dataclass(frozen=True, slots=True, kw_only=True)
class IndependentDemand:
    obligation_id: str
    requester_kind: str
    event_id: str
    event_seq: int
    event_hash: str
    requester_json: str
    evidence_json: str
    principal_id: str

    def to_json(self) -> dict[str, Any]:
        return {"obligation_id": self.obligation_id, "requester_kind": self.requester_kind,
            "event_id": self.event_id, "event_seq": self.event_seq, "event_hash": self.event_hash,
            "requester": json.loads(self.requester_json), "evidence": json.loads(self.evidence_json),
            "principal_id": self.principal_id}


def read_independent_demands(store: Store, mission_id: str,
                             accounts: tuple[ObligationAccountView, ...]) -> tuple[IndependentDemand, ...]:
    """A demand bit is neither a consumer identity nor a new execution permission.

    Validate the complete original flip history against current accounts. Only an
    actual current mission_root/authorization admission is an independent demand;
    method slots are read separately from the immutable graph's demand refs.
    """
    with store.read_view() as db:
        ledger = ObligationStore(store)
        expected = {str(item) for item in ledger.obligation_ids(mission_id)}
        current = {str(account.obligation_id): account for account in accounts}
        if set(current) != expected or len(current) != len(accounts):
            raise SourceUnavailable("taskgraph_demand_account_set_incomplete")
        active: dict[str, tuple[Any, dict[str, Any]]] = {}
        latest: dict[str, bool] = {}
        for row in db.execute("SELECT seq,event_id FROM events WHERE mission_id=? "
                              "AND type IN ('ObligationDemandAdmitted','ObligationDemandWithdrawn') ORDER BY seq",
                              (mission_id,)):
            events = store.list_events(mission_id, after_seq=int(row["seq"]) - 1, limit=1)
            if len(events) != 1 or events[0].id != row["event_id"]:
                raise SourceUnavailable("taskgraph_demand_event_unavailable")
            event = events[0]
            payload = dict(event.payload)
            obligation_id = payload.get("obligation_id")
            requester, evidence = payload.get("requester"), payload.get("evidence")
            principal = payload.get("principal")
            if (not isinstance(obligation_id, str) or obligation_id not in current or not isinstance(requester, dict)
                    or requester.get("kind") not in REQUESTER_KINDS
                    or not isinstance(evidence, dict) or not evidence
                    or not isinstance(principal, str) or not principal.strip()):
                raise SourceUnavailable("taskgraph_demand_event_identity_invalid")
            admitted = event.type == "ObligationDemandAdmitted"
            if payload.get("has_admitted_demand") is not admitted:
                raise SourceUnavailable("taskgraph_demand_event_state_invalid")
            if latest.get(obligation_id, False) == admitted:
                raise SourceUnavailable("taskgraph_demand_history_not_alternating")
            latest[obligation_id] = admitted
            if admitted:
                active[obligation_id] = (event, payload)
            else:
                active.pop(obligation_id, None)
        out = []
        for identity, account in sorted(current.items()):
            if account != ledger.account(mission_id, account.obligation_id):
                raise SourceUnavailable("taskgraph_demand_account_changed")
            if account.has_admitted_demand != latest.get(identity, False):
                raise SourceUnavailable("taskgraph_demand_provenance_missing")
            if identity not in active:
                continue
            event, payload = active[identity]
            requester = payload["requester"]
            if requester["kind"] not in {"authorization", "mission_root"}:
                continue
            out.append(IndependentDemand(obligation_id=identity, requester_kind=requester["kind"],
                event_id=event.id, event_seq=event.seq, event_hash=sha256_hex(event.to_json()),
                requester_json=canonical_json(requester), evidence_json=canonical_json(payload["evidence"]),
                principal_id=payload["principal"]))
        return tuple(out)


def independently_required_occurrences(document: Any, demands: tuple[IndependentDemand, ...]) -> frozenset[str]:
    """Resolve an original request to its exact work, never all same-duty children.

    Mission-root identity comes from the explicit immutable roots. Independent
    authorization must name both occurrence and Task in its original requester;
    the obligation bit alone cannot identify a refines-parent descendant.
    """
    from ..graph.network_codec import decode

    network = decode(document.to_json()).snapshot
    members = {str(item.occurrence_id): item for item in network.occurrences}
    required = set()
    for demand in demands:
        if demand.requester_kind == "mission_root":
            roots = {str(identity) for identity in network.root_occurrence_ids
                     if str(network.binding_for_occurrence(identity).obligation_id) == demand.obligation_id}
            if not roots:
                raise SourceUnavailable("taskgraph_root_demand_identity_missing")
            required.update(roots)
            continue
        requester = json.loads(demand.requester_json)
        occurrence, task = requester.get("occurrence_id"), requester.get("task_id")
        member = members.get(occurrence) if isinstance(occurrence, str) else None
        if (member is None or not isinstance(task, str) or str(member.task_id) != task
                or str(member.obligation_id) != demand.obligation_id):
            raise SourceUnavailable("taskgraph_independent_demand_work_identity_missing")
        required.add(occurrence)
    return frozenset(required)
