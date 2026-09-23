# SPDX-License-Identifier: Apache-2.0
"""Durable, bounded wakes for convergence waiting on original runtime facts.

These are scheduling events, never evidence of quiescence or authorization. The
normal CONVERGE consumer must reread the original authorities on every delivery.
"""
from __future__ import annotations

from ..contracts.models import Event, sha256_hex
from ..graph.notification_contracts import FollowupCauseRef, FollowupKind, FollowupV1, _integer
from ..planning.htn.grounding import derive_id
from ..storage.store import StoreError
from ..storage.taskgraph_followups import TaskGraphFollowupStore


class TaskGraphConvergenceWakeups:
    def __init__(self, notifications: TaskGraphFollowupStore, *, interval_ms: int = 5_000) -> None:
        self.notifications = notifications
        self.store = notifications.store
        self.interval_ms = _integer(interval_ms, "interval_ms", minimum=1_000)

    def schedule(self, mission_id: str, *, now_ms: int, limit: int = 16) -> int:
        """Commit source event, notification and next due time in one transaction.

        Restart needs no in-memory registration. Outstanding leases/retries are
        recovered by the existing pump. BLOCKED deliveries require explicit repair;
        a periodic wake must not silently give a failed consumer five more retries.
        """
        _integer(now_ms, "now_ms")
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("wake limit must be between 1 and 256")
        scheduled = 0
        with self.store.transaction() as db:
            jobs = db.execute(
                "SELECT j.* FROM taskgraph_convergence_jobs j "
                "WHERE j.mission_id=? AND j.state IN ('FENCED','WAITING','READY') "
                "AND NOT EXISTS (SELECT 1 FROM taskgraph_followups f WHERE f.mission_id=j.mission_id "
                "AND f.kind='CONVERGE' AND f.subject_key=j.job_id AND f.delivery_state<>'ACKED') "
                "ORDER BY j.updated_at,j.job_id", (mission_id,)).fetchall()
            for job in jobs:
                key = derive_id("tg-convergence-wake", mission_id, job["job_id"])
                saved = self.store.get_scheduler_state(key)
                ordinal = 1
                if saved is not None:
                    if (set(saved) != {"mission_id", "job_id", "ordinal", "next_due_ms"}
                            or saved["mission_id"] != mission_id or saved["job_id"] != job["job_id"]):
                        raise StoreError("TASKGRAPH_CONVERGENCE_WAKE_CORRUPT")
                    ordinal = _integer(saved["ordinal"], "ordinal", minimum=1) + 1
                    if now_ms < _integer(saved["next_due_ms"], "next_due_ms"):
                        continue
                identity = derive_id("tg-convergence-wake-event", key, str(ordinal))
                event = self.store.append_event(Event(
                    id=identity, type="TaskGraphConvergenceWakeRequested", trace_id=identity,
                    mission_id=mission_id, task_id=None, attempt_id=None,
                    actor_type="system", actor_id="taskgraph-exec-v2",
                    payload={"job_id": job["job_id"], "job_version": job["row_version"],
                             "ordinal": ordinal}, idempotency_key=identity, created_at=self.store.now))
                if event.seq is None:
                    raise StoreError("TASKGRAPH_CONVERGENCE_WAKE_NOT_PERSISTED")
                self.notifications.append_followup(FollowupV1(
                    mission_id=mission_id, source_event_id=event.id, kind=FollowupKind.CONVERGE,
                    subject_key=job["job_id"], source_revision=job["source_revision"],
                    cause_ref=FollowupCauseRef(kind="event", id=event.id, revision=event.seq,
                                              content_hash=sha256_hex(event.to_json()))), now_ms=now_ms)
                self.store.put_scheduler_state(key, {"mission_id": mission_id, "job_id": job["job_id"],
                    "ordinal": ordinal, "next_due_ms": _integer(now_ms + self.interval_ms, "next_due_ms")})
                scheduled += 1
                if scheduled == limit:
                    break
        return scheduled
