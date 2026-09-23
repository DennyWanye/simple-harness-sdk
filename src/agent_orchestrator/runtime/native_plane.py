# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Native runtime plane (ARP-EXEC-1.1.1) assembly inputs for one orchestrator pool.

A deployment that wants a pool to run on the native plane hands the assembly a
``NativePlaneAssembly`` on that pool's ``RuntimeProfile``.  The assembly then builds
the pool with ``build_arp_runtime`` instead of the legacy ``build_agent_runtime``:
a real authorization port (AllowAll is refused by the ARP factory), the deployment's
``ArpPorts`` derived from the pool's execution library path, and an authenticated
creation caller derived from every dispatch intent the orchestrator claims.  Nothing
here is model-provided; the Host's bootstrap supplies all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import TrustedCaller
from simple_harness.agents.arp.strict import digest


@dataclass(frozen=True)
class NativePlaneAssembly:
    """What a deployment supplies to run one pool on the native plane.

    ``arp_ports(execution_db)`` returns the ``ArpPorts`` for the pool whose execution
    library lives at ``execution_db`` (root directory, profile, meter, embedding,
    acceptance, artifacts, script runner...).  ``authorization`` is the pool's real
    ``AuthorizationPort``.  ``caller_for(intent)`` turns a claimed ``DispatchIntent``
    into the ``TrustedCaller`` the native creation chain records.
    """

    arp_ports: Callable[[Path], Any]
    authorization: Any
    caller_for: Callable[[Any], TrustedCaller]


def intent_caller(intent: Any, *, principal_id: str, owner_contract_ref: Pin) -> TrustedCaller:
    """The default ``caller_for``: the deployment's authenticated principal, the pool's
    owner contract, and the claimed intent itself as the command receipt (its id,
    mission, kind, creation key and frozen input hash)."""

    receipt = {
        "intent_id": str(intent.intent_id),
        "mission_id": str(intent.mission_id),
        "kind": str(intent.kind),
        "creation_key": str(intent.creation_key),
        "input_hash": str(intent.input_hash),
    }
    return TrustedCaller(
        principal_ref=Pin("principal", principal_id, 0, digest({"principal_id": principal_id})),
        owner_contract_ref=owner_contract_ref,
        command_receipt_ref=Pin("receipt", f"dispatch-intent:{intent.intent_id}", 0, digest(receipt)),
    )


__all__ = ("NativePlaneAssembly", "intent_caller")
