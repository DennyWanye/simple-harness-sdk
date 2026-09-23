# SPDX-License-Identifier: Apache-2.0
"""Purpose-specific TaskGraph reads over the original Store snapshot."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from simple_harness.contracts import canonical_json

from ..governance.permissions import Principal
from ..graph.execution_contracts import CompleteRead, GraphReadToken
from ..graph.network_codec import NetworkDocumentV1, decode, encode
from ..graph.revision_pins import RevisionPins, build_revision_pins
from ..contracts.semantic_base import TypedRef, TypedRefKind
from ..contracts.models import sha256_hex
from ..storage.htn_store import HtnStore
from ..graph.revision_records import RevisionRecord
from ..graph.task_network import TaskNetworkSnapshot
from ..runtime.planning_operations import SourceUnavailable
from ..storage.store import Store
from ..storage.taskgraph_store import RefVerifier, TaskGraphStore
from .taskgraph_policy import InstalledGraphPolicy, KERNEL_VERSION
from .taskgraph_epochs import current_scope_epochs


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralReadContext:
    caller: Principal
    policy: CompleteRead[InstalledGraphPolicy]
    record: RevisionRecord
    network: TaskNetworkSnapshot
    token: GraphReadToken

    @property
    def document(self) -> NetworkDocumentV1:
        return self.record.document

    @property
    def pins(self) -> RevisionPins:
        return self.record.pins


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedStructuralReadContext:
    """Actual original root seed; explicitly has no revision record/certificate."""
    caller: Principal
    policy: CompleteRead[InstalledGraphPolicy]
    network: TaskNetworkSnapshot
    token: GraphReadToken
    document: NetworkDocumentV1
    pins: RevisionPins


class TaskGraphSources:
    def __init__(self, store: Store, *, tenant_id: str, principal: Principal,
                 external_ref_verifier: RefVerifier | None = None,
                 history: TaskGraphStore | None = None) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.principal = principal
        if history is not None and (history.store is not store or external_ref_verifier is not None):
            raise ValueError("bind the original same-Store history or supply its verifier, not both")
        self.history = history if history is not None else TaskGraphStore(store, external_ref_verifier=external_ref_verifier)

    def read_structure(self, mission_id: str, *, revision: int | None = None) -> StructuralReadContext:
        """Historical reads never depend on a currently active planning grant."""
        with self.store.read_view() as db:
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.tenant_id != self.tenant_id:
                raise SourceUnavailable("mission_not_found")
            binding = db.execute("SELECT * FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)).fetchone()
            if binding is None or binding["kernel_version"] != KERNEL_VERSION:
                raise SourceUnavailable("taskgraph_policy_unavailable")
            policy = InstalledGraphPolicy(canonical_document=binding["policy_json"])
            receipt = self.store.get_receipt(binding["enabling_command_id"])
            if (policy.content_hash != binding["policy_hash"] or receipt is None
                    or receipt.get("kind") != "TaskGraphContractEnabled"
                    or receipt.get("mission_id") != mission_id
                    or receipt.get("policy_hash") != policy.content_hash
                    or receipt.get("command_id") != binding["enabling_command_id"]
                    or hashlib.sha256(canonical_json(dict(receipt)).encode()).hexdigest() != binding["enabling_receipt_hash"]):
                raise SourceUnavailable("taskgraph_policy_receipt_corrupt")
            if revision is None:
                active = db.execute("SELECT revision FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'",
                                    (mission_id,)).fetchall()
                if len(active) != 1:
                    raise SourceUnavailable("taskgraph_active_revision_unavailable")
                revision = active[0][0]
            record = self.history.read_revision(mission_id, revision).record
            sequence = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0]
            epochs = tuple(current_scope_epochs(self.store, mission_id).items())
            token = GraphReadToken(mission_id=mission_id, plan_revision=revision, manifest_hash=record.manifest_hash,
                                   through_seq=sequence, validity_epochs=epochs)
            return StructuralReadContext(caller=self.principal,
                policy=CompleteRead(value=policy, source_id=binding["enabling_command_id"],
                                    source_digest=policy.content_hash, through_seq=sequence),
                record=record, network=decode(record.document.to_json()).snapshot, token=token)

    def read_seed_structure(self, mission_id: str, network: TaskNetworkSnapshot) -> SeedStructuralReadContext:
        """Called only with the original hierarchy's seed reader in this Store view."""
        from .taskgraph_policy import read_installed_graph_policy
        with self.store.read_view() as db:
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.tenant_id != self.tenant_id:
                raise SourceUnavailable("mission_not_found")
            if (str(network.mission_id) != mission_id or int(network.plan_revision) != 0
                    or len(network.occurrences) != 1 or len(network.root_occurrence_ids) != 1
                    or str(network.occurrences[0].form) != "compound"
                    or network.method_instances or network.adopted_instance_ids
                    or network.order_constraints or network.data_requirements
                    or db.execute("SELECT 1 FROM plan_revisions WHERE mission_id=?", (mission_id,)).fetchone()
                    or db.execute("SELECT 1 FROM taskgraph_revision_records WHERE mission_id=?", (mission_id,)).fetchone()):
                raise SourceUnavailable("taskgraph_seed_source_invalid")
            semantics = HtnStore(self.store)
            requirement = semantics.latest_requirements_revision(mission_id)
            if requirement is None:
                raise SourceUnavailable("taskgraph_seed_requirements_missing")
            for binding in network.task_bindings:
                if semantics.task_semantics_of(mission_id, str(binding.task_id)) != binding:
                    raise SourceUnavailable("taskgraph_seed_binding_changed")
            policy = read_installed_graph_policy(self.store, mission_id)
            command = db.execute("SELECT enabling_command_id FROM taskgraph_policy_bindings WHERE mission_id=?",
                                 (mission_id,)).fetchone()[0]
            document = encode(network, TypedRef(kind=TypedRefKind.REQUIREMENTS,
                id=str(requirement.revision_id), revision=int(requirement.revision),
                content_hash=sha256_hex(requirement.to_json())))
            sequence = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0]
            epochs = tuple(current_scope_epochs(self.store, mission_id).items())
            return SeedStructuralReadContext(caller=self.principal,
                policy=CompleteRead(value=policy, source_id=command, source_digest=policy.content_hash, through_seq=sequence),
                network=network, token=GraphReadToken(mission_id=mission_id, plan_revision=0,
                    manifest_hash=sha256_hex(document.to_json()), through_seq=sequence, validity_epochs=epochs),
                document=document, pins=build_revision_pins(document))
