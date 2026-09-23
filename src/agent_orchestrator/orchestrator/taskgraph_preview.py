# SPDX-License-Identifier: Apache-2.0
"""System-owned TaskGraph preview binding and original Commit participant factory.

The final H1 deployment supplies one complete plan-context reader. Candidate
encoding, binding, recovery identity and participant construction belong here;
a deployment cannot replace those steps with an arbitrary successful factory.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..contracts.htn import TaskSemanticBindingV1
from ..contracts.models import Event, sha256_hex
from ..contracts.semantic_base import TypedRef
from ..graph.convergence import compute_convergence_impact
from ..runtime.taskgraph_operation_sources import OperationProducer
from ..graph.execution_contracts import GraphSourceRead, PreviewBindingV1
from ..graph.network_codec import NetworkDocumentV1
from ..graph.taskgraph_validation import validate_taskgraph_structure
from ..graph.taskgraph_sharing import SharingSources, validate_sharing
from ..planning.htn.grounding import derive_id
from ..planning.plan_preview import CandidatePreview
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.htn_store import HtnStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.taskgraph_store import TaskGraphStore
from ..storage.taskgraph_convergence import TaskGraphConvergenceStore
from .plan_commits import CommitPlanCommand, PlanPrincipal
from .taskgraph_candidate import document_from_bindings
from .taskgraph_plan_commit import TaskGraphPlanCommitParticipant


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanMutationSources:
    mission_id: str
    request_id: str
    decision_id: str
    decision_hash: str
    source_snapshot_hash: str
    before: NetworkDocumentV1
    requirements_ref: TypedRef
    current_bindings: tuple[TaskSemanticBindingV1, ...]
    existing_task_ids: frozenset[str]
    source_reads: tuple[GraphSourceRead, ...]
    sharing: SharingSources
    operation_producers: tuple[OperationProducer, ...]

    def __post_init__(self) -> None:
        if self.before.mission_id != self.mission_id:
            raise ValueError("plan source Mission differs from its immutable history")
        if not all(isinstance(item, TaskSemanticBindingV1) for item in self.current_bindings):
            raise ValueError("plan source requires original typed bindings")
        # Reuse the closed twelve-channel codec for shape validation only. This
        # does not construct a candidate or authorize a source as complete.
        channels = {item.channel for item in self.source_reads if isinstance(item, GraphSourceRead)}
        if (len(self.source_reads) != 12 or len(channels) != 12
                or channels != {"request", "authorization", "network", "methods", "evidence", "capabilities",
                                "operations", "running_work", "acceptances", "demand", "budget", "inputs"}):
            raise ValueError("plan source reader must return all twelve complete channels")


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenTaskGraphCandidate:
    document: NetworkDocumentV1
    preview: PreviewBindingV1

    def __post_init__(self) -> None:
        if (self.document.mission_id != self.preview.mission_id
                or self.document.revision != self.preview.base_revision + 1
                or sha256_hex(self.document.to_json()) != self.preview.candidate_hash):
            raise ValueError("frozen TaskGraph candidate identity mismatch")

    def to_json(self) -> dict[str, Any]:
        return {"document": self.document.to_json(), "preview": self.preview.to_json()}


PlanSourceReader = Callable[[Store, str, str, PlanPrincipal], PlanMutationSources]


class TaskGraphPreviewService:
    def __init__(self, store: Store, *, commit: Any, history: TaskGraphStore,
                 read_sources: PlanSourceReader, convergence: TaskGraphConvergenceStore) -> None:
        if (commit.store is not store or history.store is not store
                or convergence.store is not store or not callable(read_sources)):
            raise ValueError("TaskGraph preview requires fixed same-Store readers")
        self.commit = commit
        self.store, self.history, self.read_sources, self.convergence = store, history, read_sources, convergence

    def capture(self, request_id: str, decision_id: str, principal: PlanPrincipal) -> PlanMutationSources:
        with self.store.read_view():
            sources = self.read_sources(self.store, request_id, decision_id, principal)
            if not isinstance(sources, PlanMutationSources) or sources.request_id != request_id or sources.decision_id != decision_id:
                raise StoreError("TASKGRAPH_PLAN_SOURCE_IDENTITY_MISMATCH")
            decisions = PlanningDecisionStore(self.store)
            request = decisions.get_planning_request(request_id)
            decision = decisions.get_planning_decision(decision_id)
            if (request is None or request.mission_id != sources.mission_id
                    or request.base_plan_revision != sources.before.revision
                    or decision is None or decision["request_id"] != request_id
                    or decision["canonical_hash"] != sources.decision_hash):
                raise StoreConflict("TASKGRAPH_PLAN_DECISION_SOURCE_CHANGED")
            semantics = HtnStore(self.store)
            requirement = semantics.latest_requirements_revision(sources.mission_id)
            if (requirement is None or sources.requirements_ref.id != str(requirement.revision_id)
                    or sources.requirements_ref.revision != int(requirement.revision)
                    or sources.requirements_ref.content_hash != sha256_hex(requirement.to_json())):
                raise StoreConflict("TASKGRAPH_PLAN_REQUIREMENTS_SOURCE_CHANGED")
            tasks = self.store.list_tasks(sources.mission_id)
            if frozenset(task.id for task in tasks) != sources.existing_task_ids:
                raise StoreConflict("TASKGRAPH_PLAN_TASK_SET_CHANGED")
            from .taskgraph_bindings import current_bindings
            actual_bindings = current_bindings(semantics, sources.mission_id)
            if {str(item.task_id): item for item in actual_bindings} != {
                    str(item.task_id): item for item in sources.current_bindings}:
                raise StoreConflict("TASKGRAPH_PLAN_BINDING_SET_CHANGED")
            if sources.before.revision == 0:
                if (self.store.connection.execute("SELECT 1 FROM plan_revisions WHERE mission_id=?", (sources.mission_id,)).fetchone()
                        or self.store.connection.execute("SELECT 1 FROM taskgraph_revision_records WHERE mission_id=?", (sources.mission_id,)).fetchone()):
                    raise StoreConflict("TASKGRAPH_PLAN_SEED_CHANGED")
            else:
                original = self.history.read_revision(sources.mission_id, sources.before.revision).record
                if original.document != sources.before:
                    raise StoreConflict("TASKGRAPH_PLAN_HISTORY_CHANGED")
            return sources

    def freeze(self, command: CommitPlanCommand, preview: CandidatePreview,
               sources: PlanMutationSources) -> FrozenTaskGraphCandidate:
        """Pure after capture: no Store reads, writes, cancellation or reservation."""
        if (command.mission_id != sources.mission_id or preview.request_id != sources.request_id
                or preview.decision_hash != sources.decision_hash
                or preview.source_snapshot_hash != sources.source_snapshot_hash
                or int(command.delta.base_plan_revision) != sources.before.revision
                or command.delta != preview.compilation.delta or command.network != preview.compilation.network
                or not preview.delta_report.structural_check_complete or preview.plan_shape != "CHECKED_VALID"):
            raise StoreConflict("TASKGRAPH_PREVIEW_SOURCE_MISMATCH")
        document = document_from_bindings(command, sources.requirements_ref, before=(sources.before if sources.before.revision else None),
                                           current_bindings=sources.current_bindings, existing_task_ids=sources.existing_task_ids)
        report = validate_taskgraph_structure(document, command.structure_budget)
        if not report.ok or report.pending_compounds != preview.delta_report.pending_compounds:
            raise StoreError("TASKGRAPH_PREVIEW_STRUCTURE_INVALID")
        validate_sharing(sources.before, document, sources.sharing)
        impact = compute_convergence_impact(sources.before, document)
        required = (() if not impact.targets else
                    (derive_id("tg-converge", command.mission_id, sources.decision_id),))
        binding = PreviewBindingV1(decision_id=sources.decision_id, request_id=sources.request_id,
            mission_id=sources.mission_id, base_revision=sources.before.revision, decision_hash=sources.decision_hash,
            network_hash=sha256_hex(sources.before.to_json()), candidate_hash=sha256_hex(document.to_json()),
            delta_hash=sha256_hex(command.delta.to_json()), read_set_hash=sha256_hex(command.read_set.to_json()),
            validator_id=preview.validator_id, validator_version="taskgraph-structure-v1",
            source_reads=sources.source_reads, pending_compound_ids=report.pending_compounds,
            required_convergence_ids=required)
        return FrozenTaskGraphCandidate(document=document, preview=binding)

    def recheck(self, store: Store, command: CommitPlanCommand, principal: PlanPrincipal,
                preview: PreviewBindingV1) -> None:
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_PLAN_RECHECK_TRANSACTION_REQUIRED")
        current = self.capture(preview.request_id, preview.decision_id, principal)
        if (current.mission_id != preview.mission_id or current.decision_hash != preview.decision_hash
                or current.before.revision != preview.base_revision
                or sha256_hex(current.before.to_json()) != preview.network_hash
                or tuple(sorted(current.source_reads, key=lambda item: item.channel)) != preview.source_reads):
            raise StoreConflict("TASKGRAPH_PLAN_SOURCE_CHANGED")
        candidate = document_from_bindings(command, current.requirements_ref,
            before=current.before if current.before.revision else None,
            current_bindings=current.current_bindings, existing_task_ids=current.existing_task_ids)
        if sha256_hex(candidate.to_json()) != preview.candidate_hash:
            raise StoreConflict("TASKGRAPH_PLAN_CANDIDATE_CHANGED")
        validate_sharing(current.before, candidate, current.sharing)

    def resume_preview(self, previous: object, frozen: FrozenTaskGraphCandidate,
                       command: CommitPlanCommand, principal: PlanPrincipal) -> None:
        """Permit proved convergence progress, never silently replace a preview.

        Business identities and semantic sources must remain byte-identical. The
        runtime, Operation, acceptance and accounting channels are read afresh by
        the original preview and again in Commit. Their lawful progress is recorded
        as a new immutable event; the original COMPILED detail remains unchanged.
        """
        original = PreviewBindingV1.from_json(previous)
        current = frozen.preview
        old_body, new_body = original.to_json(), current.to_json()
        old_body.pop("source_reads")
        new_body.pop("source_reads")
        if old_body != new_body or len(current.required_convergence_ids) != 1:
            raise StoreConflict("TASKGRAPH_RESUME_CANDIDATE_CHANGED")
        old_reads = {read.channel: read for read in original.source_reads}
        mutable = {"operations", "running_work", "acceptances", "budget"}
        if any(read != old_reads[read.channel] for read in current.source_reads if read.channel not in mutable):
            raise StoreConflict("TASKGRAPH_RESUME_SEMANTIC_SOURCE_CHANGED")
        with self.store.transaction():
            self.recheck(self.store, command, principal, current)
            job = self.convergence.get_job(command.mission_id, current.required_convergence_ids[0])
            if (job.state != "READY" or job.command_id != command.command_id
                    or job.decision_id != current.decision_id or job.request_id != current.request_id
                    or job.candidate_hash != current.candidate_hash):
                raise StoreConflict("TASKGRAPH_RESUME_CONVERGENCE_NOT_READY")
            self.convergence.authority.require_quiescence(self.store, job)
            identity = derive_id("tg-preview-revalidated", job.job_id, str(job.row_version), original.canonical_hash(), current.canonical_hash())
            payload = {"job_id": job.job_id, "job_version": job.row_version,
                "command_id": command.command_id, "intent_hash": command.intent_hash(),
                "original_preview": original.to_json(), "current_preview": current.to_json()}
            self._event_once(identity, "TaskGraphPreviewRevalidated", command.mission_id, payload)

    def _event_once(self, identity: str, event_type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        import json
        row = self.store.connection.execute("SELECT seq,mission_id,type,payload_json FROM events WHERE event_id=?", (identity,)).fetchone()
        if row is not None:
            if row["mission_id"] != mission_id or row["type"] != event_type or json.loads(row["payload_json"]) != payload:
                raise StoreConflict("TASKGRAPH_PREVIEW_EVENT_CONFLICT")
            events = self.store.list_events(mission_id, after_seq=int(row["seq"])-1, limit=1)
            if len(events) != 1 or events[0].id != identity:
                raise StoreError("TASKGRAPH_PREVIEW_EVENT_MISSING")
            return events[0]
        return self.store.append_event(Event(id=identity, type=event_type, trace_id=identity,
            mission_id=mission_id, task_id=None, attempt_id=None, actor_type="system", actor_id="taskgraph-exec-v2",
            payload=payload, idempotency_key=identity, created_at=self.store.now))

    def ensure_convergence(self, command: CommitPlanCommand, principal: PlanPrincipal,
                           frozen: FrozenTaskGraphCandidate) -> Any:
        if not frozen.preview.required_convergence_ids:
            return None
        with self.store.transaction():
            self.recheck(self.store, command, principal, frozen.preview)
            # Before any fence or cancellation, reuse H4's original semantic,
            # control-generation, accepted-work and owner checks. Preview has
            # only encoded its compiler output; that cannot authorize revocation.
            self.commit._check_binding_rewrites(HtnStore(self.store), command)
            before = self.history.read_revision(command.mission_id, frozen.preview.base_revision).record.document
            impact = compute_convergence_impact(before, frozen.document)
            job_id = derive_id("tg-converge", command.mission_id, frozen.preview.decision_id)
            if frozen.preview.required_convergence_ids != (job_id,):
                raise StoreConflict("TASKGRAPH_CONVERGENCE_PREVIEW_IDENTITY_CHANGED")
            existing = self.store.connection.execute("SELECT 1 FROM taskgraph_convergence_jobs WHERE mission_id=? AND job_id=?",
                (command.mission_id, job_id)).fetchone()
            if existing is not None:
                job = self.convergence.get_job(command.mission_id, job_id)
                if (job.command_id != command.command_id or job.candidate_hash != frozen.preview.candidate_hash
                        or job.impact_hash != impact.content_hash or job.targets != impact.targets
                        or job.request_id != frozen.preview.request_id or job.decision_id != frozen.preview.decision_id):
                    raise StoreConflict("TASKGRAPH_CONVERGENCE_COMMAND_CHANGED")
                if job.state == "ABANDONED":
                    raise StoreConflict("TASKGRAPH_CONVERGENCE_ABANDONED")
                return job
            decision = PlanningDecisionStore(self.store).get_planning_decision(frozen.preview.decision_id)
            if (decision is None or decision["status"] != "COMPILED"
                    or decision.get("detail", {}).get("taskgraph_preview") != frozen.preview.to_json()):
                raise StoreConflict("TASKGRAPH_CONVERGENCE_COMPILED_PREVIEW_REQUIRED")
            identity = derive_id("tg-convergence-requested", job_id)
            event = Event(id=identity, type="TaskGraphConvergenceRequested", trace_id=command.command_id,
                mission_id=command.mission_id, task_id=None, attempt_id=None, actor_type="system", actor_id="taskgraph-exec-v2",
                payload={"job_id": job_id, "candidate_hash": impact.candidate_hash, "impact_hash": impact.content_hash,
                    "command_id": command.command_id, "intent_hash": command.intent_hash(),
                    "preview_hash": frozen.preview.canonical_hash()},
                idempotency_key=identity, created_at=self.store.now)
            return self.convergence.begin_convergence(caller=principal, preview=frozen.preview,
                before=before, candidate=frozen.document, command_id=command.command_id,
                event=event, now_ms=int(self.store.now * 1000))

    def participant(self, command: CommitPlanCommand, principal: PlanPrincipal, admission: Any) -> TaskGraphPlanCommitParticipant:
        frozen = admission.taskgraph_candidate
        if not isinstance(frozen, FrozenTaskGraphCandidate):
            raise StoreError("TASKGRAPH_FROZEN_PREVIEW_REQUIRED")
        preview = frozen.preview
        convergence = None
        if preview.required_convergence_ids:
            if len(preview.required_convergence_ids) != 1:
                raise StoreError("TASKGRAPH_CONVERGENCE_IDENTITY_INVALID")
            job = self.convergence.get_job(command.mission_id, preview.required_convergence_ids[0])
            convergence = (self.convergence, job.job_id, job.row_version)
        return TaskGraphPlanCommitParticipant(self.store, history=self.history, document=frozen.document,
            preview=preview, recheck_sources=self.recheck, convergence=convergence)
