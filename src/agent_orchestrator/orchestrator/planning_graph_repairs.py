# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Authoritative graph-repair facts; shared by package, preview and commit."""
from __future__ import annotations

from typing import Any

from ..contracts.htn import ObligationId
from ..contracts.semantic_base import content_hash_of
from ..graph.task_network import TaskNetworkSnapshot
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.store import Store


def graph_repair_sources(store: Store, network: TaskNetworkSnapshot) -> tuple[dict[str, Any], ...]:
    """Exact live demanded targets, with optional adopted completion proof."""
    from .completion_status import read_occurrence_completion
    from .operation_completion import OperationCompletionError

    htn, duties = HtnStore(store), ObligationStore(store)
    mission_id = str(network.mission_id)
    requirements = htn.latest_requirements_revision(mission_id)
    dirty = {item.subject_id for state in ("PENDING", "RECHECKING") for item in htn.list_dirty(mission_id, state=state)}
    rows: list[dict[str, Any]] = []
    for spec in network.occurrences:
        task = store.get_task(str(spec.task_id))
        if task is None or task.mission_id != mission_id or task.paused:
            continue
        account = duties.account(mission_id, ObligationId(str(spec.obligation_id)))
        if str(account.lifecycle) in {"CANCELLED", "SUPERSEDED"} or not account.has_admitted_demand:
            continue
        binding = network.binding_for_task(spec.task_id)
        row: dict[str, Any] = {
            "task_ref": {"kind": "task", "id": str(binding.task_id),
                         "semantic_revision": int(binding.contract_revision), "content_hash": binding.contract_hash},
            "occurrence_id": str(spec.occurrence_id), "resolution_ref": None,
            "share_active": not task.accepted_result_id and str(task.status) not in {"COMPLETED", "CANCELLED", "SUPERSEDED", "FAILED"},
        }
        # The legacy adopted pointer is per Obligation. Nested goals can share
        # that duty; current completion is instead pinned to the exact Task and
        # its still-adopted method, as verified by read_occurrence_completion.
        resolutions = tuple(item for item in htn.list_goal_resolutions(mission_id, obligation_id=str(spec.obligation_id))
            if item.goal_task_id == str(spec.task_id) and str(item.validity) == "CURRENT"
            and str(item.verdict) == "ACCEPT" and item.contract_revision == int(binding.contract_revision)
            and requirements is not None and item.requirements_version == int(requirements.revision))
        resolution = resolutions[0] if len(resolutions) == 1 else None
        if (resolution is not None and requirements is not None
                and resolution.goal_task_id == str(spec.task_id)
                and resolution.requirements_version == int(requirements.revision)
                and resolution.contract_revision == int(binding.contract_revision)
                and str(resolution.validity) == "CURRENT" and str(resolution.verdict) == "ACCEPT"
                and not {str(spec.task_id), str(spec.occurrence_id), str(resolution.resolution_id)} & dirty):
            stored = htn.get_review_record(str(resolution.review_receipt_id))
            official = htn.official_review_record(str(stored.record.package_id))
            try:
                completion = read_occurrence_completion(store, mission_id, str(spec.occurrence_id))
            except OperationCompletionError:
                completion = None
            if (stored.official and official == stored.record and str(stored.record.verdict) == "ACCEPT"
                    and completion is not None and completion.complete):
                row["share_active"] = False
                row["resolution_ref"] = {"kind": "resolution", "id": str(resolution.resolution_id),
                    "semantic_revision": 1, "content_hash": content_hash_of(resolution.to_json())}
        rows.append(row)
    return tuple(rows)
