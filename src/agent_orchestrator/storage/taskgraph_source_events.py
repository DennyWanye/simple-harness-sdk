# SPDX-License-Identifier: Apache-2.0
"""Durable wakeups for original sources that have no business event of their own."""
from __future__ import annotations

from ..contracts.models import Event
from ..graph.notification_contracts import FollowupCauseRef
from ..planning.htn.grounding import derive_id
from .store import Store, StoreConflict, StoreError


def taskgraph_enabled(store: Store, mission_id: str) -> bool:
    """Select the explicitly bound protocol; this is not an authority check."""
    return store.has_table("taskgraph_policy_bindings") and store.connection.execute(
        "SELECT 1 FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)
    ).fetchone() is not None


def record_source_change(store: Store, mission_id: str, *, kind: str, source_id: str,
                         revision: int, content_hash: str) -> None:
    """Append alongside the actual source write, only for an enabled Mission.

    This reference is a re-read signal, never a grant or a validity verdict.
    No raw source payload (in particular no grant body) goes into the event.
    """
    if not store.connection.in_transaction:
        raise StoreError("TASKGRAPH_SOURCE_EVENT_TRANSACTION_REQUIRED")
    if not taskgraph_enabled(store, mission_id):
        return
    source = FollowupCauseRef(kind=kind, id=source_id, revision=revision,
                              content_hash=content_hash).to_json()
    identity = derive_id("tg-source-change", mission_id, kind, source_id, str(revision))
    event = store.append_event(Event(
        id=identity, type="TaskGraphSourceChanged", trace_id=identity, mission_id=mission_id,
        task_id=None, attempt_id=None, actor_type="system", actor_id="taskgraph-source-v1",
        payload={"source_ref": source}, idempotency_key=identity, created_at=store.now))
    if (event.mission_id != mission_id or event.type != "TaskGraphSourceChanged"
            or dict(event.payload) != {"source_ref": source}):
        raise StoreConflict("TASKGRAPH_SOURCE_EVENT_CONFLICT")
