# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``build_arp_runtime``: the ARP factory over the existing ``build_agent_runtime``.

The factory (BW10) upgrades the execution library to v11, refuses AllowAll
authorisation, freezes the activated profile and its policy in the same library,
verifies the managed root marker, and attaches the native creation service to the
``AgentRuntime`` so every root / batch / delegate creation goes through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.agents.runtime import AgentRuntime, agent_id_for, build_agent_runtime, start_input_for
from simple_harness.execution.sqlite.database import Database

from . import PROTOCOL, store
from .authorization import is_allow_all
from .creation import NativeCreationService
from .errors import ArpError
from .migration import migrate_execution_to_v11
from .pins import Pin
from .ports import ArpPorts, RootIdentity, read_root
from .strict import digest


@dataclass(slots=True)
class ArpRuntime:
    """ARP state attached to an ``AgentRuntime`` (``runtime.arp``)."""

    ports: ArpPorts
    root: RootIdentity
    profile: store.ProfileRow
    policy: store.PolicyObjectRow
    creation: NativeCreationService

    @property
    def protocol(self) -> str:
        return PROTOCOL


def _freeze_profile(database: Database, ports: ArpPorts) -> tuple[store.ProfileRow, store.PolicyObjectRow]:
    profile_json = ports.profile.to_json()
    activation = dict(ports.activation_receipt)
    with database.transaction() as connection:
        profile = store.put_profile_locked(connection, profile_json, activation)
        approval_ref = Pin(
            "authority", f"{profile.profile_id}:activation", profile.revision, digest(activation)
        )
        policy = store.put_policy_object_locked(
            connection,
            body=ports.profile.context_policy,
            revision=1,
            approval_ref=approval_ref,
            source_receipt_ref=ports.profile.refs.activation_receipt_ref,
        )
    return profile, policy


def build_arp_runtime(
    ports: AgentRuntimePorts, arp: ArpPorts, *, owner_scope: str = "default"
) -> AgentRuntime:
    """Assemble a BaseAgent runtime with the native runtime plane enabled."""

    if is_allow_all(ports.authorization):
        raise ArpError("AUTHORITY_SOURCE_MISSING", "ARP runtimes refuse AllowAll authorization")
    root = read_root(arp.root_dir)
    path = Path(ports.database_path)
    Database.open(path).close()  # fresh v10 when missing; validates an existing library
    migrate_execution_to_v11(path)
    runtime = build_agent_runtime(ports, owner_scope=owner_scope)
    try:
        profile, policy = _freeze_profile(runtime.uow.database, arp)
    except BaseException:
        runtime.uow.database.close()
        raise
    creation = NativeCreationService(
        runtime, arp, root, profile, policy, agent_id_for=agent_id_for, start_input_for=start_input_for
    )
    runtime.arp = ArpRuntime(arp, root, profile, policy, creation)  # type: ignore[attr-defined]
    return runtime


def arp_of(runtime: Any) -> ArpRuntime | None:
    return getattr(runtime, "arp", None)


__all__ = ("ArpRuntime", "arp_of", "build_arp_runtime")
