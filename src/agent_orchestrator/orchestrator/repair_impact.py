# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""H4 impact inputs from current Plan, reverse support, and operation ledgers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..graph.task_network import TaskNetworkSnapshot
from ..runtime.planning_operations import StoreOperationReader, build_operation_snapshot
from ..storage.htn_store import HtnStore
from ..storage.store import Store


def read_repair_impact_indexes(
    store: Store, network: TaskNetworkSnapshot, mission_id: str
) -> dict[str, Any]:
    htn = HtnStore(store)
    data: dict[str, set[str]] = defaultdict(set)
    support: dict[str, set[str]] = defaultdict(set)
    methods: dict[str, set[str]] = defaultdict(set)
    demands: dict[str, set[str]] = defaultdict(set)
    accepts: dict[str, set[str]] = defaultdict(set)
    all_items = {mission_id}
    for occurrence in network.occurrences:
        task, occ, duty = (
            str(occurrence.task_id),
            str(occurrence.occurrence_id),
            str(occurrence.obligation_id),
        )
        all_items.update((task, occ, duty))
        support[task].add(occ)
        support[occ].add(task)
        demands[duty].add(task)
        demands[mission_id].add(duty)
        for attempt in store.list_attempts(task):
            support[attempt.id].add(task)
        for row in store.connection.execute(
            "SELECT result_id FROM results WHERE mission_id=? AND task_id=?", (mission_id, task)
        ).fetchall():
            support[str(row[0])].add(task)
    for edge in network.data_requirements:
        data[str(edge.producer_occurrence)].add(str(edge.consumer_occurrence))
    for instance in network.method_instances:
        if not network.is_adopted(instance.instance_id):
            continue
        key = str(instance.instance_id)
        methods[str(instance.effective_goal_occurrence_id)].add(key)
        methods[key].update(str(child.occurrence_id) for child in instance.child_bindings)
        for child in instance.child_bindings:
            support[str(child.goal_occurrence_id or child.occurrence_id)].add(str(instance.effective_goal_occurrence_id))
    for member_id, subject_id in htn.support_dependency_edges(mission_id):
        support[member_id].add(subject_id)
    for acceptance in htn.list_acceptances(mission_id):
        key = str(acceptance.acceptance_id)
        accepts[key].add(str(acceptance.task_id))
        accepts[str(acceptance.review_record_id)].add(key)
        # A changed producer invalidates its acceptance and its DATA dependents.
        accepts[str(acceptance.task_id)].add(key)
    snapshot = build_operation_snapshot(
        mission_id, reader=StoreOperationReader(store), now_ms=int(store.now * 1000)
    )
    for binding in snapshot.bindings:
        demands[str(binding.obligation_id)].add(binding.operation_id)
        support[binding.operation_occurrence_id].add(binding.operation_id)
    for link in snapshot.links:
        support[link.action_key].add(link.operation_id)
    states = {
        operation_id: ("UNRESOLVED" if operation_id in snapshot.unresolved else str(effect))
        for operation_id, effect in snapshot.effects
    }
    stale = {
        item.subject_id
        for state in ("PENDING", "RECHECKING")
        for item in htn.list_dirty(mission_id, state=state)
    }
    all_items.update(states)
    return {
        "all_items": tuple(sorted(all_items)),
        "reverse_data": data,
        "reverse_support": support,
        "method_membership": methods,
        "demand_refs": demands,
        "acceptance_refs": accepts,
        "operation_states": states,
        "stale_items": stale,
    }
