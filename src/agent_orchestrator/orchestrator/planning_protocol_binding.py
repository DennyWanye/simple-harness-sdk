# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The durable H1 planning-protocol binding for a Mission."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.planning_decisions import (
    LEGACY_PLANNING_PROTOCOL,
    PLANNING_DECISION_V1,
)
from ..runtime.role_templates import (
    PLANNER_HIERARCHICAL_V8_VERSION,
    PLANNING_DECISION_PACKAGE_VERSION,
)
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import Store

PLANNING_PROTOCOL_BINDING = {
    "protocol_version": PLANNING_DECISION_V1,
    "package_version": PLANNING_DECISION_PACKAGE_VERSION,
    "prompt_version": PLANNER_HIERARCHICAL_V8_VERSION,
}


def planning_protocol_binding_hash(protocol_version: str) -> str:
    """Return the digest of the exact three-field binding document."""

    document = {**PLANNING_PROTOCOL_BINDING, "protocol_version": protocol_version}
    return sha256(canonical_json(document).encode("utf-8")).hexdigest()


def bind_planning_protocol(store: Store, mission_id: str, protocol_version: str) -> None:
    """Write the binding through the existing storage writer and transaction."""

    PlanningDecisionStore(store).bind_mission_protocol(
        mission_id,
        protocol_version=protocol_version,
        package_version=PLANNING_PROTOCOL_BINDING["package_version"],
        prompt_version=PLANNING_PROTOCOL_BINDING["prompt_version"],
        binding_hash=planning_protocol_binding_hash(protocol_version),
    )


def planning_protocol_for_mission(
    conn_or_store: Store | sqlite3.Connection, mission_id: str
) -> dict[str, Any] | None:
    """Read a Mission's persisted binding; an absent row means legacy."""

    connection = conn_or_store.connection if isinstance(conn_or_store, Store) else conn_or_store
    row = connection.execute(
        "SELECT mission_id, protocol_version, package_version, prompt_version, "
        "binding_hash, created_at FROM mission_planning_protocols WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return {
        "mission_id": row[0],
        "protocol_version": row[1],
        "package_version": row[2],
        "prompt_version": row[3],
        "binding_hash": row[4],
        "created_at": row[5],
    }


def planning_protocol_replay_conflict(
    store: Store, mission_id: str, protocol_version: str
) -> str | None:
    """Return a replay conflict, keeping protocol invariants out of the hot file."""

    stored = planning_protocol_for_mission(store, mission_id)
    if protocol_version == PLANNING_DECISION_V1:
        expected = {
            "protocol_version": PLANNING_DECISION_V1,
            "package_version": PLANNING_PROTOCOL_BINDING["package_version"],
            "prompt_version": PLANNING_PROTOCOL_BINDING["prompt_version"],
        }
        if stored is None or any(stored.get(key) != value for key, value in expected.items()):
            return f"mission {mission_id} has no matching durable planning protocol binding"
    elif protocol_version == LEGACY_PLANNING_PROTOCOL and stored is not None:
        return f"mission {mission_id} cannot switch planning protocol"
    return None


__all__ = (
    "PLANNING_PROTOCOL_BINDING",
    "bind_planning_protocol",
    "planning_protocol_binding_hash",
    "planning_protocol_for_mission",
    "planning_protocol_replay_conflict",
)
