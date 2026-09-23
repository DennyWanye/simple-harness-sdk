# SPDX-License-Identifier: Apache-2.0
"""Canonical TaskGraph event identity, derived from immutable documents."""
from __future__ import annotations

from typing import Any

from ..contracts.models import ContractError, sha256_hex
from .convergence import compute_plan_effects
from .execution_contracts import PlanEffectSet
from .network_codec import NetworkDocumentV1, decode


def revision_event_payload(document: NetworkDocumentV1, *, source_kind: str,
                           sdk_snapshot_hash: str, command_id: str, decision_id: str | None,
                           parent: NetworkDocumentV1 | None) -> dict[str, Any]:
    if source_kind == "COMMIT":
        if parent is None or decision_id is None:
            raise ContractError("TASKGRAPH_COMMIT_EVENT_SOURCE_MISSING")
        effects = compute_plan_effects(parent, document)
    elif source_kind in {"SEED_COMMIT", "CAPTURED_BASELINE"}:
        if parent is not None or ((source_kind == "CAPTURED_BASELINE") != (decision_id is None)):
            raise ContractError("TASKGRAPH_ROOT_EVENT_SOURCE_MISMATCH")
        occurrences = tuple(str(item.occurrence_id) for item in decode(document.to_json()).snapshot.occurrences)
        effects = PlanEffectSet(retained=occurrences if source_kind == "CAPTURED_BASELINE" else (),
            revalidate=(), retiring=(), newly_materialized=occurrences if source_kind == "SEED_COMMIT" else (),
            shared_retained=(), coverage="CONSERVATIVE")
    else:
        raise ContractError("TASKGRAPH_EVENT_SOURCE_KIND_UNKNOWN")
    return {"schema_version": 1, "mission_id": document.mission_id, "revision": document.revision,
            "parent_hash": None if parent is None else sha256_hex(parent.to_json()),
            "manifest_hash": sha256_hex(document.to_json()), "sdk_snapshot_hash": sdk_snapshot_hash,
            "command_id": command_id, "decision_id": decision_id, "source_kind": source_kind,
            "effect_set_hash": sha256_hex(effects.to_json())}
