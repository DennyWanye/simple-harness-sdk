# SPDX-License-Identifier: Apache-2.0
"""Settlement of original Action reservations without inventing Agent intents."""
from __future__ import annotations

from typing import Any

from ..governance.budgets import BudgetError
from ..runtime.planning_operations import (
    OperationEffect, SourceUnavailable, StoreOperationReader, build_operation_snapshot,
)
from ..runtime.taskgraph_operation_sources import read_operation_producers


def require_action_settlement(orchestrator: Any, subject_id: str, mission_id: str) -> None:
    store = orchestrator.store
    if not store.connection.in_transaction:
        raise SourceUnavailable("taskgraph_action_settlement_transaction_required")
    # Join the actual reservation column written by begin_handoff, never parse
    # an opaque subject or choose an Action by connector, Task or latest version.
    matches = [action for action in store.list_actions(mission_id)
               if action.get("reservation_subject") == subject_id]
    if len(matches) != 1:
        raise SourceUnavailable("taskgraph_action_settlement_source_missing")
    action = matches[0]
    snapshot = build_operation_snapshot(mission_id, reader=StoreOperationReader(store))
    read_operation_producers(store, snapshot)
    links = [link for link in snapshot.links if link.action_key == action["action_key"]]
    if len(links) != 1:
        raise SourceUnavailable("taskgraph_action_settlement_operation_ambiguous")
    if snapshot.operation_effects[links[0].operation_id] not in {
            OperationEffect.APPLIED, OperationEffect.CONFIRMED_NOT_APPLIED}:
        raise BudgetError("TASKGRAPH_ACTION_PHYSICAL_WORK_UNRESOLVED")
    if orchestrator.commit.ledger.has_unknown_usage(subject_id):
        raise BudgetError("TASKGRAPH_ACTION_ACCOUNTING_UNRESOLVED")


def settle_resolved_actions(orchestrator: Any) -> bool:
    """Revisit actual held Action accounts after the original proof is imported."""
    from .taskgraph_dispatch import taskgraph_enabled
    store = orchestrator.store
    progressed = False
    for mission in store.list_missions():
        if not taskgraph_enabled(store, mission.id):
            continue
        for action in store.list_actions(mission.id):
            subject = action.get("reservation_subject")
            if not subject:
                continue
            try:
                with store.transaction():
                    current = store.get_action(action["action_key"])
                    if current is None or current.get("reservation_subject") != subject:
                        raise SourceUnavailable("taskgraph_action_settlement_identity_changed")
                    reservation = orchestrator.commit.ledger.reservation(subject)
                    if reservation is None or reservation["state"] == "SETTLED":
                        continue
                    require_action_settlement(orchestrator, subject, mission.id)
                    orchestrator.commit._settle_subject(subject, mission.id, task_id=None,
                        tool_calls=int(current["handoffs"]))
                    progressed = True
            except (BudgetError, SourceUnavailable):
                # The original UNKNOWN/reservation records remain authoritative.
                # Scheduler recovery will re-read them after new real evidence.
                continue
    return progressed
