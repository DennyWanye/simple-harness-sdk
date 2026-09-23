# SPDX-License-Identifier: Apache-2.0
"""Bind original Task terminal events to the exact semantic control generation.

This records a Task conclusion, never physical settlement or permission. ORDER
still requires the separate complete Attempt/SDK/Operation settlement reader.
"""
from __future__ import annotations

from typing import Any

from ..contracts.models import Event, Task, sha256_hex
from ..contracts.htn import TaskSemanticBindingV1
from ..graph.eligibility import OccurrenceOutcome
from ..planning.htn.grounding import derive_id
from ..runtime.planning_operations import SourceUnavailable
from ..storage.htn_store import HtnStore
from ..storage.store import Store, StoreError

TERMINAL_EVENTS = {"TaskCompleted": "COMPLETED", "TaskFailed": "FAILED",
                   "TaskCancelled": "CANCELLED", "TaskSuperseded": "CANCELLED"}


def _control(binding: TaskSemanticBindingV1) -> dict[str, Any]:
    return {"task_id": str(binding.task_id), "contract_revision": int(binding.contract_revision),
            "contract_hash": binding.contract_hash, "dispatch_generation": int(binding.dispatch_generation),
            "input_binding_revision": int(binding.input_binding_revision), "obligation_id": str(binding.obligation_id)}


def record_terminal_event(store: Store, source: Event) -> None:
    if not store.connection.in_transaction:
        raise StoreError("TASKGRAPH_TERMINAL_TRANSACTION_REQUIRED")
    task = store.get_task(str(source.task_id))
    if task is None or task.mission_id != source.mission_id or str(task.status) != TERMINAL_EVENTS[source.type]:
        raise StoreError("TASKGRAPH_TERMINAL_TASK_MISMATCH")
    binding = HtnStore(store).task_semantics_of(task.mission_id, task.id)
    if binding is None:
        raise StoreError("TASKGRAPH_TERMINAL_BINDING_MISSING")
    identity = derive_id("tg-task-terminal", source.id)
    store.append_event(Event(id=identity, type="TaskGraphTaskTerminalRecorded", trace_id=source.trace_id,
        mission_id=task.mission_id, task_id=task.id, attempt_id=source.attempt_id,
        actor_type="system", actor_id="taskgraph-exec-v2", idempotency_key=identity, created_at=store.now,
        payload={"version": 1, "source_event_id": source.id, "source_event_hash": sha256_hex(source.to_json()),
                 "task_version": task.version, "task_hash": sha256_hex(task.to_json()),
                 "status": str(task.status), "control": _control(binding)}))


def read_terminal_outcome(store: Store, task: Task, binding: TaskSemanticBindingV1) -> OccurrenceOutcome:
    """No event for this exact generation means unknown, not inherited terminal."""
    if str(task.status) not in set(TERMINAL_EVENTS.values()):
        return OccurrenceOutcome.RUNNING
    rows = store.connection.execute("SELECT seq,event_id FROM events WHERE mission_id=? AND task_id=? "
        "AND type='TaskGraphTaskTerminalRecorded' ORDER BY seq DESC", (task.mission_id, task.id)).fetchall()
    for row in rows:
        events = store.list_events(task.mission_id, after_seq=int(row["seq"])-1, limit=1)
        if len(events) != 1 or events[0].id != row["event_id"]:
            raise SourceUnavailable("taskgraph_terminal_event_missing")
        proof = dict(events[0].payload)
        if set(proof) != {"version", "source_event_id", "source_event_hash", "task_version", "task_hash", "status", "control"}:
            raise SourceUnavailable("taskgraph_terminal_event_invalid")
        if type(proof["version"]) is not int or proof["version"] != 1:
            raise SourceUnavailable("taskgraph_terminal_event_version_invalid")
        if proof["control"] != _control(binding):
            continue
        source = store.connection.execute("SELECT seq FROM events WHERE event_id=? AND mission_id=? AND task_id=?",
            (proof["source_event_id"], task.mission_id, task.id)).fetchone()
        if source is None or events[0].seq is None or int(source[0]) >= events[0].seq:
            raise SourceUnavailable("taskgraph_terminal_source_missing")
        original = store.list_events(task.mission_id, after_seq=int(source[0])-1, limit=1)
        if (len(original) != 1 or original[0].id != proof["source_event_id"]
                or sha256_hex(original[0].to_json()) != proof["source_event_hash"]
                or TERMINAL_EVENTS.get(original[0].type) != proof["status"]):
            raise SourceUnavailable("taskgraph_terminal_source_mismatch")
        if (type(proof["task_version"]) is not int or proof["task_version"] != task.version
                or proof["task_hash"] != sha256_hex(task.to_json()) or proof["status"] != str(task.status)):
            return OccurrenceOutcome.UNKNOWN
        return {"FAILED": OccurrenceOutcome.FAILED, "CANCELLED": OccurrenceOutcome.CANCELLED,
                "COMPLETED": OccurrenceOutcome.SETTLED_OTHER}[str(task.status)]
    return OccurrenceOutcome.UNKNOWN
