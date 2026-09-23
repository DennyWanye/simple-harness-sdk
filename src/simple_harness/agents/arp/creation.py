# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``NativeCreationService``: durable creation intent → kernel → one activation UOW (§3.1).

Order for every root / batch / child creation:

C0. ``arp_creation_intents`` row PREPARED (own short transaction).  Same key with a
    different command hash is ``CREATION_IDENTITY_CONFLICT``; a replay resumes.
C1. Session directory + ``SessionMarker`` file written *outside* any SQL transaction
    (lock order: file guard before the execution UOW).  An existing marker must be
    byte-identical.
C2. Original kernel Run start (``start_base_agent_run``); the kernel replays the same
    request id idempotently and never calls a model at creation.
C3. One execution transaction: binding row, ``arp_agent_protocols`` (ARP_V1_1_1),
    ``arp_agent_sessions`` CREATING, policy adoption 1, activation CREATING → ACTIVE,
    original events, creation intent BOUND.

A crash after any step resumes with the same identities; nothing is activated half
way (a session without its C3 row is simply not there).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from simple_harness.agents.config import AgentConfig, config_hash
from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.base_agent import AgentBindingRecord
from simple_harness.execution.sqlite.base_agent import turns as base_agent_turns
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime import RunStart

from . import PROTOCOL, store
from .codec import check
from .errors import ArpError
from .pins import Pin, original_receipt_pin
from .ports import ArpPorts, RootIdentity, TrustedCaller
from .profile import RuntimeProfile
from .strict import canonical, digest, parse_strict

if TYPE_CHECKING:  # pragma: no cover
    from simple_harness.agents.runtime import AgentRuntime

MARKER_FILE = "marker.json"


@dataclass(frozen=True, slots=True)
class CreationReceipt:
    binding: AgentBindingRecord
    session: store.SessionRow
    protocol: store.ProtocolRow
    intent: store.CreationIntentRow
    replayed: bool


def session_id_for(agent_id: str, root: RootIdentity, creation_key: str) -> str:
    return "session-" + digest(
        {"agent_id": agent_id, "root_incarnation": root.root_incarnation, "creation_key": creation_key}
    )[:32]


def session_marker(
    *,
    session_id: str,
    agent_id: str,
    root: RootIdentity,
    control_generation: int,
    creation_receipt_ref: Pin,
) -> dict[str, Any]:
    body = {
        "schema_version": 2,
        "session_id": session_id,
        "agent_id": agent_id,
        "root_id": root.root_id,
        "root_incarnation": root.root_incarnation,
        "control_generation": control_generation,
        "partition_id": f"{session_id}:partition:1",
        "creation_receipt_ref": creation_receipt_ref.to_json(),
    }
    return check("SessionMarker", body)


def write_marker(directory: Path, marker: Mapping[str, Any]) -> str:
    """Create the session directory and marker; an existing marker must match exactly."""

    raw = canonical(marker)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MARKER_FILE
    if path.exists():
        if path.read_bytes() != raw:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "existing session marker differs")
        return digest(marker)
    tmp = directory / (MARKER_FILE + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    return digest(marker)


def read_marker(directory: Path) -> Mapping[str, Any]:
    path = directory / MARKER_FILE
    if not path.is_file():
        raise ArpError("RUNTIME_CREATION_MARKER_MISSING", str(path))
    return check("SessionMarker", parse_strict(path.read_bytes(), max_bytes=16 * 1024))


class NativeCreationService:
    """Single creation entry for root, batch and delegated child Agents (BW01)."""

    def __init__(
        self,
        runtime: AgentRuntime,
        ports: ArpPorts,
        root: RootIdentity,
        profile: store.ProfileRow,
        policy: store.PolicyObjectRow,
        *,
        agent_id_for: Callable[[str, str], str],
        start_input_for: Callable[..., dict[str, Any]],
    ) -> None:
        self._runtime = runtime
        self._ports = ports
        self._root = root
        self._profile = profile
        self._policy = policy
        self._agent_id_for = agent_id_for
        self._start_input_for = start_input_for

    @property
    def profile(self) -> RuntimeProfile:
        return RuntimeProfile.from_json(self._profile.body)

    def _fault(self, point: str) -> None:
        if self._ports.fault is not None:
            self._ports.fault(point)

    # ---- C0 --------------------------------------------------------------------------

    def _command_hash(self, *, owner_scope: str, creation_key: str, config: AgentConfig, caller: TrustedCaller, role: str) -> str:
        return digest(
            {
                "kind": "create",
                "protocol": PROTOCOL,
                "owner_scope": owner_scope,
                "creation_key": creation_key,
                "role": role,
                "config_hash": config_hash(config),
                "profile_ref": self._profile.pin.to_json(),
                "caller": caller.to_json(),
            }
        )

    def prepare_intent(
        self, *, owner_scope: str, creation_key: str, agent_id: str, run_id: str, command_hash: str, caller: TrustedCaller
    ) -> store.CreationIntentRow:
        database = self._runtime.uow.database
        with database.transaction() as connection:
            intent = store.put_creation_intent_locked(
                connection,
                intent_id=f"intent-{digest({'owner_scope': owner_scope, 'creation_key': creation_key})[:32]}",
                owner_scope=owner_scope,
                creation_key=creation_key,
                command_hash=command_hash,
                proposed_agent_id=agent_id,
                proposed_run_id=run_id,
                profile_ref=self._profile.pin,
                original_receipt_ref=caller.command_receipt_ref,
            )
        if intent.state == "ABORTED":
            raise ArpError("CREATION_IDENTITY_CONFLICT", "creation intent was aborted")
        return intent

    # ---- public entry ------------------------------------------------------------------

    async def create(
        self,
        config: AgentConfig,
        *,
        creation_key: str,
        caller: TrustedCaller | None,
        owner_scope: str,
        role: str = "root",
        agent_id: str | None = None,
        kernel_start: Callable[[], Awaitable[None]] | None = None,
    ) -> CreationReceipt:
        """One creation for root / child / delegate alike (BW01).

        ``agent_id`` lets the original delegation ticket keep its child identity;
        ``kernel_start`` is the original kernel launch (a child launch under the parent's
        ticket, or the default root run start) — deferred until the durable intent exists
        and skipped when the Run already exists.
        """

        if not isinstance(config, AgentConfig):
            raise TypeError("config must use AgentConfig")
        if not isinstance(creation_key, str) or not creation_key.strip():
            raise ValueError("creation_key is required")
        if caller is None:
            raise ArpError("AUTHORITY_SOURCE_MISSING", "ARP creation requires an authenticated caller")
        profile = self.profile
        if profile.owner_mode != "STANDALONE_CHAT":
            # Mission-mode sources (TaskGraph / InputManifest / Assurance) are RP-B work;
            # without them creation is refused instead of silently standalone (§2).
            raise ArpError("SOURCE_UNAVAILABLE", "MISSION owner mode needs exact sources")
        mode = profile.creation_mode(embedding_available=self._ports.embedding_available)
        agent_id = agent_id or self._agent_id_for(owner_scope, creation_key)
        run_id = agent_id
        uow = self._runtime.uow
        existing = uow.read_agent_binding(agent_id)
        if existing is not None and existing.owner_scope != owner_scope:
            from simple_harness.agents.contracts import AgentNotFound

            raise AgentNotFound(agent_id)
        if existing is not None and existing.config_hash != config_hash(config):
            raise ValueError("creation_key reused with a different configuration")
        command_hash = self._command_hash(
            owner_scope=owner_scope, creation_key=creation_key, config=config, caller=caller, role=role
        )
        # C0
        intent = self.prepare_intent(
            owner_scope=owner_scope, creation_key=creation_key, agent_id=agent_id, run_id=run_id,
            command_hash=command_hash, caller=caller,
        )
        self._fault("create.after_intent")
        # C1
        session_id = session_id_for(agent_id, self._root, creation_key)
        relative_directory = f"sessions/{session_id}"
        creation_receipt_ref = original_receipt_pin(
            f"run:{run_id}:start", {"run_id": run_id, "request_id": f"{agent_id}:create", "creation_key": creation_key}
        )
        marker = session_marker(
            session_id=session_id, agent_id=agent_id, root=self._root, control_generation=1,
            creation_receipt_ref=creation_receipt_ref,
        )
        marker_hash = write_marker(self._root.resolve_relative(relative_directory), marker)
        self._fault("create.after_marker")
        # C2
        replayed = existing is not None
        if uow.read_run(run_id) is None and kernel_start is not None:
            await kernel_start()
        elif uow.read_run(run_id) is None:
            await self._runtime.kernel.start_base_agent_run(
                RunStart(
                    ExecutionSessionId(f"base-agent:{agent_id}"),
                    RunId(run_id),
                    RequestId(f"{agent_id}:create"),
                    f"{agent_id}:start",
                    self._start_input_for(
                        config,
                        agent_id=agent_id,
                        role=role,
                        owner_scope=owner_scope,
                        max_output_tokens=self._runtime.ports.default_max_output_tokens,
                    ),
                    1,
                )
            )
        self._fault("create.after_kernel")
        # C3
        now = self._runtime.ports.clock()
        now_ms = self._ports.clock_ms()
        with uow.database.transaction() as connection:
            try:
                binding = base_agent_turns.insert_binding(
                    connection,
                    agent_id=agent_id,
                    run_id=run_id,
                    owner_scope=owner_scope,
                    role=role,
                    creation_key=creation_key,
                    config_json=config.to_json(),
                    config_hash=config_hash(config),
                    now=now,
                    max_agents=self._runtime.ports.max_agents,
                )
            except UnitOfWorkConflict as error:
                raise ArpError("CREATION_IDENTITY_CONFLICT", str(error)) from error
            protocol = store.put_protocol_locked(
                connection,
                agent_id=agent_id,
                creation_receipt_ref=creation_receipt_ref,
                marker_hash=marker_hash,
                marked_at_ms=now_ms,
            )
            session = store.insert_session_locked(
                connection,
                session_id=session_id,
                agent_id=agent_id,
                profile_ref=self._profile.pin,
                creation_root_id=self._root.root_id,
                root_incarnation=self._root.root_incarnation,
                creation_key=creation_key,
                create_command_hash=command_hash,
                relative_directory=relative_directory,
                journal_seq_from=1,
                now_ms=now_ms,
            )
            if session.state == "CREATING":
                store.append_policy_adoption_locked(
                    connection,
                    session_id=session_id,
                    policy=self._policy,
                    command_id=f"{session_id}:adopt:1",
                    command_hash=command_hash,
                    authority_receipt_ref=Pin("receipt", self._profile.pin.id + ":activation", self._profile.revision, digest(self._profile.activation_receipt)),
                    source_receipt_ref=caller.command_receipt_ref,
                    now_ms=now_ms,
                )
                store.append_runtime_event_locked(
                    connection,
                    run_id=run_id,
                    event_type="AgentContextPolicyAdopted",
                    body={
                        "session_ref": Pin("session", session_id, 1, session.identity_hash).to_json(),
                        "adoption_revision": 1,
                        "policy_ref": self._policy.pin.to_json(),
                        "command_receipt_ref": caller.command_receipt_ref.to_json(),
                    },
                    source_receipt_ref=caller.command_receipt_ref,
                    dedupe_key=f"{session_id}:adoption:1",
                    now=now,
                )
                activated = store.transition_session_locked(connection, session, now_ms=now_ms, state="ACTIVE")
                store.append_runtime_event_locked(
                    connection,
                    run_id=run_id,
                    event_type="RuntimeSessionStateChanged",
                    body={
                        "schema_version": 2,
                        "session_ref": activated.pin.to_json(),
                        "row_version": activated.row_version,
                        "old_state": "CREATING",
                        "new_state": "ACTIVE",
                        "source_receipt_ref": creation_receipt_ref.to_json(),
                        "purge_progress_hash": None,
                    },
                    source_receipt_ref=creation_receipt_ref,
                    dedupe_key=f"{session_id}:state:{activated.row_version}",
                    now=now,
                )
                session = activated
            intent = store.finalize_creation_locked(connection, intent, state="BOUND")
        del mode  # LEXICAL_ONLY / HYBRID is recorded by the index partition owner (RP-B)
        return CreationReceipt(binding, session, protocol, intent, replayed)


__all__ = (
    "CreationReceipt",
    "MARKER_FILE",
    "NativeCreationService",
    "read_marker",
    "session_id_for",
    "session_marker",
    "write_marker",
)
