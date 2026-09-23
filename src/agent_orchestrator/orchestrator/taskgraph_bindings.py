# SPDX-License-Identifier: Apache-2.0
"""Current original semantic inventory, including nonmaterialized compound goals."""
from __future__ import annotations

from ..contracts.htn import TaskSemanticBindingV1
from ..storage.htn_store import HtnStore


def current_bindings(semantics: HtnStore, mission_id: str) -> tuple[TaskSemanticBindingV1, ...]:
    # The original reader orders revisions ascending. Semantic compounds need
    # no physical Task row; joining through Tasks silently drops the seed root.
    latest = {}
    for binding in semantics.list_task_semantics(mission_id):
        latest[str(binding.task_id)] = binding
    return tuple(latest[key] for key in sorted(latest))
