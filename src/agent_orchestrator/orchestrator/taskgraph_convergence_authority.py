# SPDX-License-Identifier: Apache-2.0
"""Fixed original-source checks and authenticated operator convergence authority."""
from __future__ import annotations

from typing import Any

from ..contracts.htn import GraphStructureBudget
from ..contracts.models import sha256_hex
from ..governance.planning_authorization import (
    planning_policy_for_mission, StorePlanningAuthorityReader, build_planning_authorization,
    PlanningAuthorizationSnapshot, check_planning_authorization,
)
from ..graph.execution_contracts import PreviewBindingV1
from ..graph.network_codec import NetworkDocumentV1, decode
from ..graph.taskgraph_sharing import validate_sharing
from ..graph.taskgraph_validation import validate_taskgraph_structure
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.taskgraph_convergence import ConvergenceJob
from ..storage.taskgraph_store import TaskGraphStore
from .plan_commits import PlanPrincipal
from .taskgraph_plan_sources import TaskGraphPlanSourceReader
from .taskgraph_policy import read_installed_graph_policy
from .taskgraph_runtime_observation import TaskGraphRuntimeObservationReader
from .taskgraph_reconciliation_proof import TaskGraphReconciliationProof
from .taskgraph_operator import TaskGraphOperatorGuard


class VerifiedConvergenceAuthority:
    def __init__(self, orchestrator: Any, *, history: TaskGraphStore,
                 sources: TaskGraphPlanSourceReader, operator: TaskGraphOperatorGuard) -> None:
        self.store, self.history, self.sources, self.operator = history.store, history, sources, operator
        if orchestrator.store is not self.store or sources.store is not self.store:
            raise ValueError("convergence authority requires the original Store")
        self.runtime = TaskGraphRuntimeObservationReader(orchestrator, history=history)
        self.reconciliation = TaskGraphReconciliationProof(orchestrator)

    def _store(self, store: Store) -> None:
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_CONVERGENCE_AUTHORITY_TRANSACTION_REQUIRED")

    def validate_begin(self, store: Store, caller: object, preview: PreviewBindingV1,
                       before: NetworkDocumentV1, candidate: NetworkDocumentV1) -> None:
        self._store(store)
        if not isinstance(caller, PlanPrincipal):
            raise StoreError("TASKGRAPH_CONVERGENCE_PLANNING_PRINCIPAL_REQUIRED")
        current = self.sources(store, preview.request_id, preview.decision_id, caller)
        if (current.before != before or current.decision_hash != preview.decision_hash
                or current.source_reads != preview.source_reads
                or sha256_hex(before.to_json()) != preview.network_hash
                or sha256_hex(candidate.to_json()) != preview.candidate_hash):
            raise StoreConflict("TASKGRAPH_CONVERGENCE_BEGIN_SOURCE_CHANGED")
        policy = read_installed_graph_policy(store, preview.mission_id)
        budget = GraphStructureBudget.from_json(policy.to_json()["graph_structure_budget"])
        report = validate_taskgraph_structure(candidate, budget)
        if not report.ok or report.pending_compounds != preview.pending_compound_ids:
            raise StoreError("TASKGRAPH_CONVERGENCE_BEGIN_STRUCTURE_INVALID")
        validate_sharing(before, candidate, current.sharing)
        decision = PlanningDecisionStore(store).get_planning_decision(preview.decision_id)
        if decision is None or decision["status"] != "COMPILED":
            raise StoreError("TASKGRAPH_CONVERGENCE_BEGIN_DECISION_NOT_COMPILED")
        authority = build_planning_authorization(preview.request_id,
            read=StorePlanningAuthorityReader(PlanningAdmissionStore(store), store),
            caller=caller, policy=planning_policy_for_mission(store, preview.mission_id), now_ms=int(store.now * 1000))
        if not isinstance(authority, PlanningAuthorizationSnapshot):
            raise StoreError("TASKGRAPH_CONVERGENCE_BEGIN_AUTHORITY_MISSING")
        import json
        from ..contracts.planning_decisions import PlanningDecisionEnvelopeV1
        from ..planning.decision_admission import _enablement_key
        decoded = PlanningDecisionEnvelopeV1.from_json(json.loads(decision["canonical_json"]))
        decision_key = _enablement_key(decoded)
        if check_planning_authorization(authority, decision_key=decision_key, now_ms=int(store.now * 1000)) is not None:
            raise StoreError("TASKGRAPH_CONVERGENCE_BEGIN_AUTHORITY_REFUSED")

    def _observe(self, store: Store, job: ConvergenceJob) -> Any:
        self._store(store)
        record = self.history.read_revision(job.mission_id, job.source_revision).record
        network = decode(record.document.to_json()).snapshot
        for target in job.targets:
            members = [item for item in network.occurrences if str(item.occurrence_id) == target.occurrence_id]
            if (len(members) != 1 or str(members[0].task_id) != target.task_id
                    or int(network.binding_for_occurrence(members[0].occurrence_id).dispatch_generation) != target.expected_generation):
                raise StoreError("TASKGRAPH_CONVERGENCE_TARGET_SOURCE_CHANGED")
        # The complete reliable importer remains mandatory in addition to local
        # accounting and SDK observations; this never upgrades a partial reader.
        self.sources.read_execution(job.mission_id)
        observed = self.runtime(store, job)
        if (observed.job_id != job.job_id or observed.job_version != job.row_version
                or observed.target_occurrences != tuple(sorted(item.occurrence_id for item in job.targets))):
            raise StoreError("TASKGRAPH_CONVERGENCE_OBSERVATION_IDENTITY_CHANGED")
        return observed

    def require_quiescence(self, store: Store, job: ConvergenceJob) -> None:
        observed = self._observe(store, job)
        if (not observed.quiescent or observed.actions
                or read_local_work(store, job.mission_id).blocking_subjects(frozenset(item.task_id for item in job.targets))):
            raise StoreConflict("TASKGRAPH_CONVERGENCE_NOT_QUIESCENT")

    def require_reconciliation_started(self, store: Store, job: ConvergenceJob) -> None:
        self._observe(store, job)
        self.reconciliation.require_started(job)

    def require_safe_abandonment(self, store: Store, job: ConvergenceJob,
                                 caller: object, command_id: str) -> None:
        from ..graph.notification_contracts import _text
        _text(command_id, "command_id")
        self.operator.require(store, job.mission_id, caller)
        self.require_quiescence(store, job)
        current = self.sources.read_execution(job.mission_id).local.structure
        original = self.history.read_revision(job.mission_id, job.source_revision).record
        if (current.document.revision != job.source_revision
                or current.document.to_json() != original.document.to_json()):
            raise StoreConflict("TASKGRAPH_ABANDONMENT_OLD_DEMAND_CHANGED")
        # Pins/demands were never removed by the fence. The explicit operator
        # command may restore their execution eligibility only after quiescence;
        # it cannot revive a stopped Turn, overwrite effects or grant new rights.
