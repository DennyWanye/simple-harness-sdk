# SPDX-License-Identifier: Apache-2.0
"""A TaskGraph terminal event and its record are one unit (real run 2026-09-27).

A cancellation path emitted ``TaskCancelled`` outside a transaction; the TaskGraph
terminal record requires one, so every loop round failed with
TASKGRAPH_TERMINAL_TRANSACTION_REQUIRED.
"""

from types import SimpleNamespace

from agent_orchestrator.orchestrator import taskgraph_dispatch, taskgraph_terminal
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.storage import htn_store
from agent_orchestrator.storage.store import Store


def test_a_terminal_event_emitted_outside_a_transaction_is_recorded_in_one(tmp_path, monkeypatch):
    store = Store.open(tmp_path / "o.db")
    service = CommitService.__new__(CommitService)
    service._store = store
    service._require_task = lambda task_id: SimpleNamespace(version=3)
    monkeypatch.setattr(taskgraph_dispatch, "taskgraph_enabled", lambda store, mission_id: True)
    monkeypatch.setattr(htn_store.HtnStore, "task_semantics_of", lambda self, m, t: object())
    seen = []

    def record(store_, event):
        seen.append((event.type, store_.connection.in_transaction))

    monkeypatch.setattr(taskgraph_terminal, "record_terminal_event", record)
    appended = []
    monkeypatch.setattr(store, "append_event", lambda event: appended.append(event) or event)
    assert not store.connection.in_transaction
    event = CommitService._emit(service, "TaskCancelled", "m1", key="t1", task_id="t1")
    assert seen == [("TaskCancelled", True)]
    assert event.idempotency_key.endswith(":taskgraph:3")
    assert not store.connection.in_transaction
