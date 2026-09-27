# SPDX-License-Identifier: Apache-2.0
"""Authenticated one-time TaskGraph activation; not a model-callable command."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from simple_harness.contracts import canonical_json

from ..contracts.htn import GraphStructureBudget
from ..contracts.models import Event
from ..contracts.planning_decisions import PLANNING_DECISION_V1
from ..contracts.state_machines import TERMINAL_MISSION
from ..governance.permissions import Principal
from ..graph.notification_contracts import _text
from ..graph.revision_records import SourceRef
from ..planning.htn.grounding import derive_id
from ..storage.store import Store, StoreConflict, StoreError
from .plan_commits import HIERARCHICAL_SEMANTICS, semantics_of
from .planning_protocol_binding import planning_protocol_for_mission

KERNEL_VERSION = "taskgraph-exec-v2"


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledGraphPolicy:
    """A frozen canonical snapshot of installed versioned deployment policy."""
    canonical_document: str

    def __post_init__(self) -> None:
        raw = json.loads(self.canonical_document)
        if not isinstance(raw, dict) or canonical_json(raw) != self.canonical_document:
            raise StoreError("TASKGRAPH_POLICY_NOT_CANONICAL")
        if set(raw) != {"kernel_version", "graph_structure_budget", "planning_delegation_ref",
                        "candidate_policy_ref", "schema_policy_ref", "target_policy_ref", "deployment_policy_ref"}:
            raise StoreError("TASKGRAPH_POLICY_FIELDS_INVALID")
        if raw["kernel_version"] != KERNEL_VERSION:
            raise StoreError("TASKGRAPH_POLICY_VERSION_UNSUPPORTED")
        GraphStructureBudget.from_json(raw["graph_structure_budget"])
        from ..graph.revision_records import SourceRef
        for key, value in raw.items():
            if key.endswith("_ref"):
                SourceRef.from_json(value)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_document.encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return dict(json.loads(self.canonical_document))


def read_installed_graph_policy(store: Store, mission_id: str) -> InstalledGraphPolicy:
    """Read a policy only with its original enabling command receipt."""
    with store.read_view() as db:
        row = db.execute("SELECT * FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)).fetchone()
        if row is None or row["kernel_version"] != KERNEL_VERSION:
            raise StoreError("TASKGRAPH_POLICY_UNAVAILABLE")
        policy = InstalledGraphPolicy(canonical_document=row["policy_json"])
        receipt = store.get_receipt(row["enabling_command_id"])
        if (policy.content_hash != row["policy_hash"] or receipt is None
                or receipt.get("kind") != "TaskGraphContractEnabled"
                or receipt.get("mission_id") != mission_id or receipt.get("kernel_version") != KERNEL_VERSION
                or receipt.get("policy_hash") != policy.content_hash
                or receipt.get("command_id") != row["enabling_command_id"]
                or hashlib.sha256(canonical_json(dict(receipt)).encode()).hexdigest() != row["enabling_receipt_hash"]):
            raise StoreError("TASKGRAPH_POLICY_RECEIPT_CORRUPT")
        acceptance = SourceRef.from_json(receipt.get("deployment_acceptance_ref"))
        if acceptance.channel != "h1h_deployment_acceptance":
            raise StoreError("TASKGRAPH_POLICY_DEPLOYMENT_RECEIPT_INVALID")
        return policy


class TaskGraphPolicyAuthority(Protocol):
    def require_enable(self, store: Store, mission_id: str, caller: Principal) -> SourceRef:
        """Validate current delegation and return actual deployed H1-H acceptance.

        No environment variable, local component PASS, or fabricated receipt may
        satisfy this check. A missing actual authority is a refusal.
        """
        ...

    def installed_policy(self, store: Store, mission_id: str) -> InstalledGraphPolicy:
        """Read actual frozen versioned deployment/candidate/schema/target policy."""
        ...

class TaskGraphPolicyService:
    def __init__(self, store: Store, *, tenant_id: str, principal: Principal,
                 authority: TaskGraphPolicyAuthority, capture_baseline: Callable[..., None],
                 validate_read: Callable[[Principal, str], None]) -> None:
        _text(tenant_id, "tenant_id")
        self.store = store
        self.tenant_id = tenant_id
        self.principal = principal
        self.authority = authority
        self.capture_baseline = capture_baseline
        self.validate_read = validate_read

    def matches_binding(self, commit: Any, tenant_id: str, principal: Any) -> bool:
        return commit.store is self.store and tenant_id == self.tenant_id and principal == self.principal

    def enable_taskgraph_contract(self, mission_id: str, command_id: str) -> Mapping[str, Any]:
        _text(command_id, "command_id")
        with self.store.transaction() as db:
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.tenant_id != self.tenant_id:
                raise StoreError("MISSION_NOT_FOUND")
            self.validate_read(self.principal, mission_id)
            intent: dict[str, Any] = {"kind": "EnableTaskGraphContract", "mission_id": mission_id,
                      "kernel_version": KERNEL_VERSION, "principal_id": self.principal.principal_id}
            intent_hash = hashlib.sha256(canonical_json(intent).encode()).hexdigest()
            previous = self.store.get_receipt(command_id)
            if previous is not None:
                if previous.get("intent_hash") != intent_hash:
                    raise StoreConflict("COMMAND_PAYLOAD_CONFLICT")
                # Reading an existing result does not re-enable or re-authorize a write.
                return previous
            deployment_acceptance = self.authority.require_enable(self.store, mission_id, self.principal)
            if (not isinstance(deployment_acceptance, SourceRef)
                    or deployment_acceptance.channel != "h1h_deployment_acceptance"):
                raise StoreError("TASKGRAPH_DEPLOYMENT_ACCEPTANCE_UNAVAILABLE")
            protocol = planning_protocol_for_mission(self.store, mission_id)
            if (semantics_of(mission) != HIERARCHICAL_SEMANTICS or mission.status in TERMINAL_MISSION
                    or protocol is None or protocol.get("protocol_version") != PLANNING_DECISION_V1):
                raise StoreError("TASKGRAPH_MISSION_NOT_ELIGIBLE")
            policy = self.authority.installed_policy(self.store, mission_id)
            old = db.execute("SELECT policy_hash FROM taskgraph_policy_bindings WHERE mission_id=?",
                             (mission_id,)).fetchone()
            if old is not None:
                raise StoreConflict("TASKGRAPH_POLICY_ALREADY_BOUND")
            receipt: dict[str, Any] = {"schema_version": 1, "kind": "TaskGraphContractEnabled",
                                      "command_id": command_id, "mission_id": mission_id,
                                      "intent_hash": intent_hash, "policy_hash": policy.content_hash,
                                      "deployment_acceptance_ref": deployment_acceptance.to_json(),
                                      "kernel_version": KERNEL_VERSION}
            receipt_hash = hashlib.sha256(canonical_json(receipt).encode()).hexdigest()
            db.execute("INSERT INTO taskgraph_policy_bindings VALUES (?,?,?,?,?,?,?)",
                       (mission_id, KERNEL_VERSION, policy.content_hash, policy.canonical_document,
                        command_id, receipt_hash, self.store.now))
            self.store.insert_receipt(commit_id=command_id, kind="EnableTaskGraphContract",
                                      subject_id=mission_id, base_version=mission.version,
                                      proposal_hash=intent_hash, receipt=receipt)
            event_key = derive_id("tg-enable-event", command_id)
            # NEXT-TG-1.0 §6.4: enabling is a trusted deployment action taken on the
            # principal's behalf (after the principal's own planning grant), not a
            # person's click — recorded as system, delegated by that principal.
            self.store.append_event(Event(id=event_key, type="TaskGraphContractEnabled", trace_id=command_id,
                                          mission_id=mission_id, task_id=None, attempt_id=None,
                                          actor_type="system", actor_id=self.principal.principal_id,
                                          payload={**receipt, "enabled_by": "HOST_DELEGATED"},
                                          idempotency_key=event_key, created_at=self.store.now))
            active = db.execute("SELECT revision FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'",
                                (mission_id,)).fetchall()
            if len(active) > 1:
                raise StoreError("TASKGRAPH_MULTIPLE_ACTIVE_REVISIONS")
            if active:
                self.capture_baseline(self.store, mission_id=mission_id, revision=active[0][0],
                                      command_id=command_id, caller=self.principal, policy=policy)
                baseline = db.execute("SELECT source_kind,admission_check_id FROM taskgraph_revision_records "
                                      "WHERE mission_id=? AND revision=?", (mission_id, active[0][0])).fetchone()
                if baseline is None or tuple(baseline) != ("CAPTURED_BASELINE", None):
                    raise StoreError("TASKGRAPH_BASELINE_NOT_CAPTURED")
            return receipt
