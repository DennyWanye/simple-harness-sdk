# SPDX-License-Identifier: Apache-2.0
"""Fixed TaskGraph consumers attached to the original orchestrator cycle."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..contracts.htn import OccurrenceId
from ..contracts.models import Event, sha256_hex
from ..graph.notification_contracts import FollowupCauseRef, FollowupKind, FollowupV1
from ..planning.htn.grounding import derive_id
from ..storage.store import StoreError
from ..contracts.models import ContractError
from ..runtime.planning_operations import SourceUnavailable
from ..storage.taskgraph_followups import DurableFollowupReceipt, ReceiptKind, TaskGraphFollowupStore
from ..storage.taskgraph_store import TaskGraphStore
from .taskgraph_convergence import TaskGraphConvergence
from .taskgraph_followups import TaskGraphEventConsumer, TaskGraphFollowupPump
from .taskgraph_wakeups import TaskGraphConvergenceWakeups

# Only real event types from the original Commit services. Their payload is not
# interpreted as a command or a tool name. Consumer receipts are deliberately absent.
_RECHECK_EVENTS = frozenset({
    "AttemptCreated", "AttemptClaimed", "AttemptStarted", "AttemptLost", "AttemptTimedOut",
    "AttemptCancelled", "AttemptSuperseded", "AcceptanceCommitted", "GoalResolutionCommitted",
    "ActionSucceeded", "ActionFailed", "ActionOutcomeUnknown", "ActionReconciled",
    "ActionCancelled", "ActionSuperseded", "ObligationDemandAdmitted", "ObligationDemandWithdrawn",
    "CompoundPhaseChanged", "TaskGraphTaskTerminalRecorded",
    "InputSubmitted", "IntentSettled", "ServiceIntentRehandedOff", "BudgetReleased", "BudgetReserved",
    "ActionProposed", "ActionRefused", "ActionHandoffRefused", "ActionHandedOff",
    "KnowledgeCommitted", "KnowledgeSuperseded",
    "TaskPaused", "TaskResumed", "SearchPolicyBound",
    "ApprovalRequested", "ApprovalGranted", "ApprovalRejected", "ApprovalRevoked",
    "ApprovalExpired", "ApprovalCancelled", "ApprovalSuperseded",
    "TaskGraphSourceChanged",
    "MissionCancelled", "MissionFailed", "MissionCompleted",
})


class TaskGraphNotifications:
    def __init__(self, orchestrator: Any, *, history: TaskGraphStore,
                 convergence: TaskGraphConvergence,
                 validate_current: Callable[[str], None]) -> None:
        self.orchestrator = orchestrator
        self.store = orchestrator.store
        if history.store is not self.store or convergence.jobs.store is not self.store:
            raise ValueError("TaskGraph notification consumers must share the original Store")
        self.history = history
        self.convergence = convergence
        self.validate_current = validate_current
        self.notifications = TaskGraphFollowupStore(self.store)
        self.wakeups = TaskGraphConvergenceWakeups(self.notifications)
        self.events = TaskGraphEventConsumer(self.notifications, consumer_id="taskgraph-exec-v2",
                                             projector=self.project)
        self.pump = TaskGraphFollowupPump(self.notifications, owner=orchestrator._owner,
            reevaluate=self.reevaluate, converge=self.converge,
            request_composition=self.request_composition,
            require_execution_root=orchestrator._require_assurance_execution_root,
            clock_ms=lambda: int(self.store.now * 1000))

    def project(self, event: Event) -> tuple[FollowupV1, ...]:
        if event.type not in _RECHECK_EVENTS:
            return ()
        if event.seq is None:
            raise StoreError("TASKGRAPH_SOURCE_EVENT_NOT_PERSISTED")
        if event.type == "TaskGraphSourceChanged":
            if set(event.payload) != {"source_ref"}:
                raise StoreError("TASKGRAPH_SOURCE_CHANGE_INVALID")
            source = FollowupCauseRef.from_json(event.payload["source_ref"])
            if source.kind not in {"planning_grant", "validity_witness", "observation", "validity_epoch"}:
                raise StoreError("TASKGRAPH_SOURCE_CHANGE_UNSUPPORTED")
        # Associate the event with the graph that really existed at its sequence,
        # not whichever ACTIVE graph the delayed consumer happens to see.
        row = self.store.connection.execute(
            "SELECT r.revision FROM taskgraph_revision_records r JOIN events e ON e.event_id=r.event_id "
            "WHERE r.mission_id=? AND e.seq<=? ORDER BY r.revision DESC LIMIT 1",
            (event.mission_id, event.seq)).fetchone()
        if row is None:
            # Events before the explicit seed/captured baseline are outside coverage.
            return ()
        revision = int(row[0])
        self.history.read_revision(event.mission_id, revision)
        cause = FollowupCauseRef(kind="event", id=event.id, revision=event.seq,
                                 content_hash=sha256_hex(event.to_json()))
        def message(kind: FollowupKind, subject: str) -> FollowupV1:
            return FollowupV1(mission_id=event.mission_id, source_event_id=event.id,
                              kind=kind, subject_key=subject, source_revision=revision, cause_ref=cause)
        terminal_event = event.type in {"MissionCancelled", "MissionFailed", "MissionCompleted"}
        out = [] if terminal_event else [message(FollowupKind.REEVALUATE, event.mission_id)]
        if event.type == "CompoundPhaseChanged" and event.payload.get("phase") == "composition_review":
            occurrence = event.payload.get("occurrence_id")
            if not isinstance(occurrence, str) or not occurrence:
                raise StoreError("TASKGRAPH_COMPOSITION_SOURCE_INVALID")
            out.append(message(FollowupKind.REQUEST_COMPOSITION, occurrence))
        # An original runtime change wakes every affected, still fenced job. A null
        # task identity requires the complete Mission set, not a guessed empty set.
        jobs = self.store.connection.execute(
            "SELECT DISTINCT j.job_id FROM taskgraph_convergence_jobs j "
            "JOIN taskgraph_convergence_targets t ON t.job_id=j.job_id "
            "WHERE j.mission_id=? AND j.state IN ('FENCED','WAITING','READY') "
            "AND (? IS NULL OR t.task_id=?) ORDER BY j.job_id",
            (event.mission_id, event.task_id, event.task_id)).fetchall()
        out.extend(message(FollowupKind.CONVERGE, str(job[0])) for job in jobs)
        return tuple(out)

    def _existing(self, message: FollowupV1, key: str) -> DurableFollowupReceipt | None:
        row = self.store.connection.execute(
            "SELECT event_id,payload_json FROM events WHERE mission_id=? AND idempotency_key=?",
            (message.mission_id, key)).fetchone()
        if row is None:
            return None
        import json
        if json.loads(row["payload_json"]).get("notification_hash") != sha256_hex(message.to_json()):
            raise StoreError("TASKGRAPH_CONSUMER_REPLAY_CONFLICT")
        return self.notifications.consumer_receipt(message.mission_id, key, ReceiptKind.EVENT, row[0])

    def _record(self, message: FollowupV1, key: str, result: dict[str, Any]) -> DurableFollowupReceipt:
        with self.store.transaction():
            existing = self._existing(message, key)
            if existing is not None:
                return existing
            event = self.store.append_event(Event(
                id=derive_id("tg-followup-result", key), type="TaskGraphFollowupConsumed",
                trace_id=key, mission_id=message.mission_id, task_id=None, attempt_id=None,
                actor_type="system", actor_id="taskgraph-exec-v2",
                payload={"notification_hash": sha256_hex(message.to_json()),
                         "kind": str(message.kind), "subject_key": message.subject_key, "result": result},
                idempotency_key=key, created_at=self.store.now))
            return self.notifications.consumer_receipt(message.mission_id, key, ReceiptKind.EVENT, event.id)

    def _verify_source(self, message: FollowupV1) -> None:
        row = self.store.connection.execute(
            "SELECT seq FROM events WHERE mission_id=? AND event_id=?",
            (message.mission_id, message.source_event_id)).fetchone()
        if row is None:
            raise StoreError("TASKGRAPH_FOLLOWUP_SOURCE_UNAVAILABLE")
        ref = message.cause_ref
        if ref.kind == "event":
            events = self.store.list_events(message.mission_id, after_seq=row[0] - 1, limit=1)
            if (not events or events[0].id != ref.id or ref.id != message.source_event_id
                    or events[0].seq != ref.revision or sha256_hex(events[0].to_json()) != ref.content_hash):
                raise StoreError("TASKGRAPH_FOLLOWUP_SOURCE_CHANGED")
        elif ref.kind == "plan_revision":
            record = self.history.read_revision(message.mission_id, message.source_revision).record
            if (ref.id != message.mission_id or ref.revision != message.source_revision
                    or ref.content_hash != record.manifest_hash or message.source_event_id != record.event_id):
                raise StoreError("TASKGRAPH_FOLLOWUP_SOURCE_CHANGED")
        elif ref.kind == "convergence_job":
            job = self.convergence.jobs.get_job(message.mission_id, ref.id)
            if (message.subject_key != job.job_id or ref.revision != 1
                    or ref.content_hash != job.impact_hash or message.source_revision != job.source_revision):
                raise StoreError("TASKGRAPH_FOLLOWUP_SOURCE_CHANGED")
        else:
            raise StoreError("TASKGRAPH_FOLLOWUP_SOURCE_UNSUPPORTED")

    async def reevaluate(self, message: FollowupV1, key: str) -> DurableFollowupReceipt:
        # Evaluation and its receipt are one original Store transaction. Actual
        # dispatch remains in the existing cycle after this notification phase.
        with self.store.transaction():
            existing = self._existing(message, key)
            if existing is not None:
                return existing
            self._verify_source(message)
            self.validate_current(message.mission_id)
            result = self.orchestrator.taskgraph_recheck_mission(message.mission_id)
            self.validate_current(message.mission_id)
            return self._record(message, key, result)

    async def converge(self, message: FollowupV1, key: str) -> DurableFollowupReceipt:
        with self.store.read_view():
            existing = self._existing(message, key)
            if existing is not None:
                return existing
            self._verify_source(message)
        job = await self.convergence.advance_convergence(
            message.mission_id, message.subject_key, now_ms=int(self.store.now * 1000))
        if job.state == "READY":
            from .taskgraph_resume import resume_converged_plan
            await resume_converged_plan(self.orchestrator, job)
            job = self.convergence.jobs.get_job(job.mission_id, job.job_id)
        return self._record(message, key, {"job_id": job.job_id, "row_version": job.row_version,
                                           "state": job.state})

    async def request_composition(self, message: FollowupV1, key: str) -> DurableFollowupReceipt:
        with self.store.read_view():
            existing = self._existing(message, key)
            if existing is not None:
                return existing
            self._verify_source(message)
            self.validate_current(message.mission_id)
        # Original producers use the package/occurrence identity for idempotency,
        # including a crash after intent creation but before the notification ACK.
        result = await self.orchestrator.taskgraph_request_composition(
            message.mission_id, OccurrenceId(message.subject_key))
        return self._record(message, key, result)

    async def tick(self) -> bool:
        self.orchestrator._require_assurance_execution_root()
        rows = self.store.connection.execute(
            "SELECT mission_id FROM taskgraph_policy_bindings ORDER BY mission_id").fetchall()
        consumed = 0
        for row in rows:
            mission_id = str(row[0])
            fault_key = derive_id("tg-source-fault", mission_id)
            try:
                consumed += self.events.consume(mission_id, now_ms=int(self.store.now * 1000))
                consumed += self.wakeups.schedule(mission_id, now_ms=int(self.store.now * 1000))
            except (StoreError, ContractError, SourceUnavailable):
                # Isolate an unreadable Mission. Do not advance its cursor, invent
                # an empty graph, expose a raw exception or block other Missions.
                with self.store.transaction():
                    self.store.put_scheduler_state(fault_key, {"mission_id": mission_id,
                        "blocked": True, "code": "TASKGRAPH_EVENT_SOURCE_UNAVAILABLE"})
                    cursor_key = derive_id("taskgraph-exec-v2-event-cursor", self.events.consumer_id, mission_id)
                    try:
                        cursor = self.store.get_scheduler_state(cursor_key)
                    except (ValueError, TypeError):
                        cursor = {}
                    sequence = 0 if cursor is None else cursor.get("through_seq")
                    if type(sequence) is not int or not 0 <= sequence <= 2**53 - 1:
                        # Unknown is not a zero checkpoint. Preserve the damaged
                        # cursor for repair and record a diagnostic without using
                        # it as an index or advancing any source event.
                        sequence = None
                    identity = derive_id("tg-source-fault-event", mission_id, str(sequence))
                    self.store.append_event(Event(id=identity, type="TaskGraphSourceUnavailable",
                        trace_id=identity, mission_id=mission_id, task_id=None, attempt_id=None,
                        actor_type="system", actor_id="taskgraph-exec-v2",
                        payload={"code": "TASKGRAPH_EVENT_SOURCE_UNAVAILABLE", "through_seq": sequence},
                        idempotency_key=identity, created_at=self.store.now))
            else:
                if self.store.get_scheduler_state(fault_key) is not None:
                    with self.store.transaction():
                        self.store.put_scheduler_state(fault_key, {"mission_id": mission_id,
                            "blocked": False, "code": None})
        delivered = await self.pump.pump_taskgraph_followups(limit=16)
        return bool(consumed or delivered)

    def has_pending(self, mission_id: str | None = None) -> bool:
        # A WAITING job can have acknowledged its last observation and be between
        # durable wake deadlines. The original run loop must remain alive to call
        # tick at that deadline; no new Host automation is involved. A BLOCKED
        # convergence delivery, however, awaits explicit repair and gets no fresh
        # automatic retry budget merely because its job remains fenced.
        return self.store.connection.execute(
            "SELECT 1 FROM taskgraph_followups "
            "WHERE delivery_state IN ('PENDING','LEASED') AND (? IS NULL OR mission_id=?) "
            "UNION ALL SELECT 1 FROM taskgraph_convergence_jobs j "
            "WHERE j.state IN ('FENCED','WAITING','READY') AND (? IS NULL OR j.mission_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM taskgraph_followups f WHERE f.mission_id=j.mission_id "
            "AND f.kind='CONVERGE' AND f.subject_key=j.job_id AND f.delivery_state='BLOCKED') LIMIT 1",
            (mission_id, mission_id, mission_id, mission_id)).fetchone() is not None

    def awaiting_sources(self, mission_id: str) -> bool:
        """A durable blocked notification or active fence is not an idle failure."""
        fault = self.store.get_scheduler_state(derive_id("tg-source-fault", mission_id))
        if fault is not None and fault["blocked"] is True:
            return True
        return self.store.connection.execute(
            "SELECT 1 FROM taskgraph_followups WHERE mission_id=? AND delivery_state<>'ACKED' "
            "UNION ALL SELECT 1 FROM taskgraph_convergence_jobs WHERE mission_id=? "
            "AND state IN ('FENCED','WAITING','READY') LIMIT 1",
            (mission_id, mission_id)).fetchone() is not None
