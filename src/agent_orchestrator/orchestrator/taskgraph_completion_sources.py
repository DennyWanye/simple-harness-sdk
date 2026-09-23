# SPDX-License-Identifier: Apache-2.0
"""Pin HTN completion inputs alongside the other original planning sources."""
from __future__ import annotations

from typing import Any

from ..graph.task_network import TaskNetworkSnapshot
from ..storage.htn_store import HtnStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.store import Store
from .completion_status import current_effect_proofs, read_occurrence_completion
from .operation_completion import OperationCompletionError
from .scoped_content_review import uses_completion_protocol


def read_completion_sources(store: Store, mission_id: str,
                            network: TaskNetworkSnapshot) -> dict[str, Any]:
    """Read immutable side bindings through their original identity validators.

    These facts participate in currentness checks; they confer no acceptance or
    authority. Explicit pre-plan/unmapped states remain incomplete.
    """
    with store.read_view() as db:
        if not uses_completion_protocol(store, mission_id):
            return {"protocol": "LEGACY"}
        completion = OperationCompletionStore(store)
        semantics = HtnStore(store)
        specs = tuple(completion.get_spec_exact(mission_id, row[0], row[1]) for row in db.execute(
            "SELECT requirements_revision,requirements_hash FROM operation_completion_specs "
            "WHERE mission_id=? ORDER BY requirements_revision", (mission_id,)))
        scopes = tuple(completion.get_scope_exact(mission_id, row[0], row[1]) for row in db.execute(
            "SELECT plan_revision,occurrence_id FROM operation_completion_scopes "
            "WHERE mission_id=? ORDER BY plan_revision,occurrence_id", (mission_id,)))
        outcomes = tuple(completion.get_outcome_binding_exact(mission_id, row[0]) for row in db.execute(
            "SELECT binding_id FROM operation_outcome_review_bindings "
            "WHERE mission_id=? ORDER BY binding_id", (mission_id,)))
        contributions = tuple(completion.get_acceptance_scope_exact(mission_id, row[0]) for row in db.execute(
            "SELECT acceptance_id FROM operation_acceptance_scopes "
            "WHERE mission_id=? ORDER BY acceptance_id", (mission_id,)))
        states: dict[str, Any] = {}
        active = semantics.active_plan_revision(mission_id)
        for occurrence in network.occurrences:
            identity = str(occurrence.occurrence_id)
            if active is None:
                states[identity] = {"state": "AWAITING_INITIAL_PLAN"}
                continue
            try:
                states[identity] = read_occurrence_completion(store, mission_id, identity)
            except OperationCompletionError as error:
                if error.code not in {"OP_COMPLETION_SCOPE_UNRESOLVED", "OP_REQUIREMENT_MAPPING_MISSING"}:
                    raise
                states[identity] = {"state": error.code}
        return {"protocol": "OPERATION_COMPLETION", "specs": specs, "scopes": scopes,
                "outcomes": outcomes, "contributions": contributions, "states": states,
                "dirty": semantics.list_dirty(mission_id),
                "effect_proofs": current_effect_proofs(store, mission_id) if active is not None else ()}
