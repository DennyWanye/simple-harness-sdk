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

from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.meter import NO_PRIOR, PriorBasis
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import TrustedCaller
from simple_harness.agents.arp.strict import digest
from simple_harness.contracts import RunId


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
    # Called once with the assembled ``AgentRuntime`` (late bindings such as the prior
    # reserve reader below, which needs the pool's own execution library).
    after_build: Callable[[Any], None] | None = None


class RunPriorReserve:
    """``MeterBinding.prior_reserve`` for a pool: ``P`` is the sum of every prior output
    token the pool's own execution library recorded for the Agent run.

    The reader mirrors the orchestrator's admission accounting: a never-handed-off
    (``claimed``) invocation has no usage; any other invocation whose usage is not yet
    resolved makes the reserve unavailable by name — a reserve is never guessed.  It is
    bound to the runtime after assembly (``NativePlaneAssembly.after_build``).
    """

    def __init__(self) -> None:
        self._uow: Any = None

    def bind(self, runtime: Any) -> None:
        self._uow = runtime.uow

    def __call__(self, run_id: str) -> PriorBasis:
        if self._uow is None:
            raise ArpError("PRIOR_RESERVE_UNAVAILABLE", "prior reserve reader is not bound to a runtime")
        rows: list[tuple[str, int]] = []
        for previous in self._uow.list_provider_invocations(RunId(run_id)):
            record = self._uow.read_effective_provider_invocation(previous.invocation_id)
            if record is None or str(record.state) == "claimed":
                continue
            usage = record.usage_json
            values = usage.get("usage") if isinstance(usage, dict) else None
            output = values.get("output_tokens") if isinstance(values, dict) else None
            if str(record.state) not in ("succeeded", "failed") or type(output) is not int or output < 0:
                raise ArpError(
                    "PRIOR_RESERVE_UNAVAILABLE",
                    "prior provider usage is unresolved",
                    detail={"invocation_id": record.invocation_id, "state": str(record.state)},
                )
            rows.append((record.invocation_id, output))
        tokens = sum(output for _, output in rows)
        if tokens == 0:
            return NO_PRIOR
        basis = {"run_id": run_id, "invocations": [{"invocation_id": i, "output_tokens": o} for i, o in rows]}
        return PriorBasis(tokens, Pin("receipt", f"prior-output:{run_id}", 0, digest(basis)))


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


__all__ = ("NativePlaneAssembly", "RunPriorReserve", "intent_caller")
