# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The durable H1 planning-protocol binding for a Mission."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
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

#: The package/prompt pair every binding shares (the frozen v4 package, the v8 planner
#: prompt).  The protocol *name* is not part of this constant: it is the argument of
#: :func:`binding_document`, so there is exactly one place that assembles the three-field
#: document and no value here can be silently shadowed by an override.
PLANNING_PROTOCOL_BINDING = {
    "package_version": PLANNING_DECISION_PACKAGE_VERSION,
    "prompt_version": PLANNER_HIERARCHICAL_V8_VERSION,
}


#: The two wire names a Mission charter may name (§8.1).  Nothing else is accepted:
#: the switch is part of the frozen charter, so a typo must fail at the door rather
#: than silently leave a Mission on the legacy protocol.
PLANNING_PROTOCOLS = frozenset({LEGACY_PLANNING_PROTOCOL, PLANNING_DECISION_V1})


def checked_planning_protocol(protocol_version: object) -> str:
    """Return a known protocol name, or raise the contract error the door reports.

    ``MissionSpec.__post_init__`` runs this once, but a spec can also be built through
    ``dataclasses.replace`` (which skips ``__post_init__``) or by writing the dataclass
    fields directly.  ``create_mission`` runs it again so an unvalidated spec is refused
    before it reaches ``spec_hash`` or the library.
    """

    if not isinstance(protocol_version, str) or protocol_version not in PLANNING_PROTOCOLS:
        raise ContractError(f"unknown planning protocol version {protocol_version!r}")
    return protocol_version


def binding_document(protocol_version: str) -> dict[str, Any]:
    """Return the exact three-field binding document for a checked protocol name.

    The single assembly point: the digest, the storage write and the replay comparison all
    describe the same document, so a package or prompt bump cannot update one and miss the
    others.  The protocol name is always taken from the argument — never from a constant —
    or two different wires could digest to the same ``binding_hash`` while the row's
    ``protocol_version`` column says otherwise.
    """

    return {
        "protocol_version": checked_planning_protocol(protocol_version),
        **PLANNING_PROTOCOL_BINDING,
    }


def planning_protocol_binding_hash(protocol_version: str) -> str:
    """Return the digest of the exact three-field binding document."""

    document = binding_document(protocol_version)
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


def planning_protocol_for_mission(store: Store, mission_id: str) -> dict[str, Any] | None:
    """Read a Mission's persisted binding; an absent row means legacy.

    The read goes through the storage module's own reader, which is the documented gate
    for ``mission_planning_protocols``: this function stays a thin, side-effect-free
    adapter, so the table keeps exactly one reader and one writer (P2-3 of the
    independent review).  Recovery callers reconstruct a ``Store`` with ``Store.open``
    (or hold one already), so no raw-connection shim is offered.
    """

    return PlanningDecisionStore(store).get_mission_protocol(mission_id)


def planning_protocol_replay_conflict(
    store: Store, mission_id: str, protocol_version: str
) -> str | None:
    """Return a replay conflict, keeping protocol invariants out of the hot file.

    A Mission replays idempotently only when the wire it asks for is the wire that is
    durably bound: no row means legacy, and a row means that exact binding document is
    the Mission's identity (§8.2).  The comparison is made against the request's
    ``binding_hash`` so a Mission bound to a different package or prompt of the same
    protocol name is a conflict too, not just a Mission bound to another name.
    """

    checked = checked_planning_protocol(protocol_version)
    stored = planning_protocol_for_mission(store, mission_id)
    expected = binding_document(checked)
    if stored is None:  # absent means legacy: nobody may bind it after the fact
        if checked == LEGACY_PLANNING_PROTOCOL:
            return None
        return f"mission {mission_id} has no durable planning protocol binding"
    if any(stored.get(field) != value for field, value in expected.items()):
        return (
            f"mission {mission_id} is durably bound to protocol"
            f" {stored['protocol_version']!r}/package {stored['package_version']}, not"
            f" {checked!r}/package {expected['package_version']}"
        )
    return None


__all__ = (
    "PLANNING_PROTOCOL_BINDING",
    "PLANNING_PROTOCOLS",
    "binding_document",
    "bind_planning_protocol",
    "checked_planning_protocol",
    "planning_protocol_binding_hash",
    "planning_protocol_for_mission",
    "planning_protocol_replay_conflict",
)
