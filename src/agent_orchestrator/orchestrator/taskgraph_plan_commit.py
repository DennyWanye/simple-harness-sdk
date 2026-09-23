# SPDX-License-Identifier: Apache-2.0
"""TaskGraph participant in the original PlanCommit transaction.

The installed H1 adapters recheck sources and persist the real APPLIED check.
This participant never fabricates an admission result and never commits Store.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from simple_harness.contracts import canonical_json

from ..contracts.models import Event
from ..graph.convergence import ConvergenceImpact, compute_convergence_impact
from ..graph.execution_contracts import PreviewBindingV1
from ..graph.network_codec import NetworkDocumentV1
from ..graph.notification_contracts import FollowupCauseRef, FollowupKind, FollowupV1
from ..graph.revision_records import PlanAdmissionCertificate, SourceRef
from ..graph.revision_events import revision_event_payload
from ..planning.htn.grounding import derive_id
from ..storage.htn_store import PlanCommitReceipt
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.taskgraph_followups import TaskGraphFollowupStore
from ..storage.taskgraph_store import TaskGraphStore
from .taskgraph_candidate import document_for_commit
from .taskgraph_policy import read_installed_graph_policy
from ..contracts.htn import GraphStructureBudget
from ..graph.taskgraph_validation import validate_taskgraph_structure

if TYPE_CHECKING:
    from .plan_commits import CommitPlanCommand, PlanPrincipal
    from ..storage.taskgraph_convergence import TaskGraphConvergenceStore


class TaskGraphPlanCommitParticipant:
    def __init__(self, store: Store, *, history: TaskGraphStore, document: NetworkDocumentV1,
                 preview: PreviewBindingV1,
                 recheck_sources: Callable[[Store, "CommitPlanCommand", "PlanPrincipal", PreviewBindingV1], None],
                 convergence: tuple["TaskGraphConvergenceStore", str, int] | None = None) -> None:
        if history.store is not store:
            raise ValueError("TaskGraph history participant must share the original Commit Store")
        self.store = store
        self.history = history
        self.document = document
        self.preview = preview
        self.recheck_sources = recheck_sources
        self._write_applied_check: Callable[[Store, "CommitPlanCommand", PlanCommitReceipt, PreviewBindingV1], str] | None = None
        self.convergence = convergence

    def bind_h1_commit(self, writer: Callable[[Store, "CommitPlanCommand", PlanCommitReceipt, PreviewBindingV1], str]) -> None:
        """Bound by the original H1 entry for this transaction, never a deployment port."""
        if self._write_applied_check is not None:
            raise StoreError("TASKGRAPH_H1_COMMIT_ALREADY_BOUND")
        self._write_applied_check = writer

    def prepare(self, command: "CommitPlanCommand", principal: "PlanPrincipal") -> ConvergenceImpact | None:
        if self._write_applied_check is None:
            raise StoreError("TASKGRAPH_H1_COMMIT_ENTRY_REQUIRED")
        if not self.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_PREPARE_REQUIRES_COMMIT_TRANSACTION")
        active = self.store.connection.execute("SELECT revision FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'",
                                               (command.mission_id,)).fetchall()
        if len(active) > 1 or (active and active[0][0] != self.preview.base_revision):
            raise StoreConflict("TASKGRAPH_COMMIT_BASE_STALE")
        previous = self.history.read_revision(command.mission_id, self.preview.base_revision).record if active else None
        actual = document_for_commit(self.store, command, self.document.requirements_ref,
                                     before=None if previous is None else previous.document)
        if (actual != self.document or command.mission_id != self.preview.mission_id
                or self.preview.base_revision != int(command.delta.base_plan_revision)
                or self.document.revision != self.preview.base_revision + 1
                or hashlib.sha256(canonical_json(actual.to_json()).encode()).hexdigest() != self.preview.candidate_hash
                or hashlib.sha256(canonical_json(command.delta.to_json()).encode()).hexdigest() != self.preview.delta_hash
                or hashlib.sha256(canonical_json(command.read_set.to_json()).encode()).hexdigest() != self.preview.read_set_hash):
            raise StoreConflict("TASKGRAPH_PREVIEW_COMMAND_MISMATCH")
        policy = read_installed_graph_policy(self.store, command.mission_id)
        graph_budget = GraphStructureBudget.from_json(policy.to_json()["graph_structure_budget"])
        if command.structure_budget != graph_budget:
            raise StoreConflict("TASKGRAPH_COMMIT_POLICY_BUDGET_MISMATCH")
        report = validate_taskgraph_structure(actual, graph_budget)
        if not report.ok:
            raise StoreError("TASKGRAPH_COMMIT_STRUCTURE_INVALID")
        if self.preview.pending_compound_ids != report.pending_compounds:
            raise StoreConflict("TASKGRAPH_PREVIEW_PENDING_COMPOUNDS_MISMATCH")
        self.recheck_sources(self.store, command, principal, self.preview)
        if not active:
            if self.preview.base_revision != 0 or self.convergence is not None:
                raise StoreConflict("TASKGRAPH_SEED_BASE_MISMATCH")
            return None
        if len(active) != 1 or active[0][0] != self.preview.base_revision:
            raise StoreConflict("TASKGRAPH_COMMIT_BASE_STALE")
        if previous is None:
            raise StoreError("TASKGRAPH_COMMIT_BASE_UNAVAILABLE")
        if previous.manifest_hash != self.preview.network_hash:
            raise StoreConflict("TASKGRAPH_PREVIEW_NETWORK_MISMATCH")
        impact = compute_convergence_impact(previous.document, self.document)
        if self.convergence is not None:
            jobs, job_id, expected_version = self.convergence
            if jobs.store is not self.store:
                raise StoreError("TASKGRAPH_CONVERGENCE_STORE_MISMATCH")
            job = jobs.get_job(command.mission_id, job_id)
            if (self.preview.required_convergence_ids != (job_id,)
                    or job.row_version != expected_version or job.state != "READY"
                    or job.command_id != command.command_id or job.decision_id != self.preview.decision_id
                    or job.candidate_hash != impact.candidate_hash or job.impact_hash != impact.content_hash
                    or job.targets != impact.targets):
                raise StoreConflict("TASKGRAPH_CONVERGENCE_NOT_READY_FOR_CANDIDATE")
            jobs.authority.require_quiescence(self.store, job)
        elif impact.targets or self.preview.required_convergence_ids:
            raise StoreConflict("TASKGRAPH_CONVERGENCE_REQUIRED")
        return impact

    def record_applied(self, command: "CommitPlanCommand", receipt: PlanCommitReceipt) -> None:
        if not self.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_RECORD_REQUIRES_COMMIT_TRANSACTION")
        if (receipt.command_id != command.command_id or receipt.mission_id != command.mission_id
                or receipt.new_plan_revision != self.document.revision
                or receipt.intent_hash != command.intent_hash()):
            raise StoreError("TASKGRAPH_COMMIT_RECEIPT_MISMATCH")
        # The original PlanCommit receipt exists now. Only the actual H1 producer
        # may write the APPLIED phase; missing producer/check aborts the entire Commit.
        writer = self._write_applied_check
        if writer is None:
            raise StoreError("TASKGRAPH_H1_COMMIT_ENTRY_REQUIRED")
        check_id = writer(self.store, command, receipt, self.preview)
        row = self.store.connection.execute("SELECT * FROM planning_admission_checks WHERE check_id=?", (check_id,)).fetchone()
        if row is None or row["phase"] != "APPLIED":
            raise StoreError("TASKGRAPH_APPLIED_CHECK_UNAVAILABLE")
        revision = self.document.revision
        certificate = PlanAdmissionCertificate(preview=self.preview,
            commit_receipt_ref=SourceRef(channel="commit_receipt", identity=command.command_id, revision=revision,
                digest=hashlib.sha256(canonical_json(receipt.to_json()).encode()).hexdigest()),
            admission_check_ref=SourceRef(channel="planning_admission_check", identity=check_id, revision=revision,
                digest=hashlib.sha256(canonical_json(dict(row)).encode()).hexdigest()))
        identity = derive_id("tg-revision-recorded", command.command_id)
        source_kind = "SEED_COMMIT" if receipt.base_plan_revision == 0 else "COMMIT"
        parent = None if source_kind == "SEED_COMMIT" else self.history.read_revision(
            command.mission_id, receipt.base_plan_revision).record.document
        event = self.store.append_event(Event(id=identity, type="TaskGraphRevisionRecorded", trace_id=command.command_id,
            mission_id=command.mission_id, task_id=None, attempt_id=None, actor_type="system", actor_id="taskgraph-exec-v2",
            payload=revision_event_payload(self.document, source_kind=source_kind,
                sdk_snapshot_hash=str(receipt.output_identity["snapshot_hash"]), command_id=command.command_id,
                decision_id=self.preview.decision_id, parent=parent),
            idempotency_key=identity, created_at=self.store.now))
        self.history.insert_revision_record(document=self.document,
            source_kind=source_kind,
            sdk_snapshot_hash=str(receipt.output_identity["snapshot_hash"]), certificate=certificate,
            admission_check_id=check_id, event_id=event.id, command_id=command.command_id, created_at=self.store.now)
        TaskGraphFollowupStore(self.store).append_followup(FollowupV1(mission_id=command.mission_id,
            source_event_id=event.id, kind=FollowupKind.REEVALUATE, subject_key=command.mission_id,
            source_revision=revision, cause_ref=FollowupCauseRef(kind="plan_revision", id=command.mission_id,
                revision=revision, content_hash=self.preview.candidate_hash)), now_ms=int(self.store.now * 1000))
        if self.convergence is not None:
            jobs, job_id, version = self.convergence
            jobs.mark_applied(command.mission_id, job_id, expected_version=version,
                              committed_revision=revision, now_ms=int(self.store.now * 1000))
