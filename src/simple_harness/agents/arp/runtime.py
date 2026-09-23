# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``build_arp_runtime``: the ARP factory over the existing ``build_agent_runtime``.

The factory (BW10) upgrades the execution library to v11, refuses AllowAll
authorisation and uncertified token counters, freezes the activated profile and
its policy in the same library, verifies the managed root marker, swaps the
Context port / provider wire for the native composer (RP-B) and attaches the
native creation service so every root / batch / delegate creation goes through it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.agents.runtime import AgentRuntime, agent_id_for, build_agent_runtime, start_input_for
from simple_harness.execution.sqlite.database import Database

from . import PROTOCOL, store
from .authorization import is_allow_all
from .context.composer import ArpContextPort, ArpProviderWire
from .context.recall import ContextRecallCoordinator
from .creation import NativeCreationService
from .errors import ArpError
from .indexing import SessionIndexCoordinator
from .meter import MeterBinding, NativeMeterAdapter
from .migration import migrate_execution_to_v11
from .pins import Pin
from .ports import ArpPorts, RootIdentity, read_root
from .search import SessionSearchService
from .strict import digest


@dataclass(slots=True)
class ArpRuntime:
    """ARP state attached to an ``AgentRuntime`` (``runtime.arp``)."""

    ports: ArpPorts
    root: RootIdentity
    profile: store.ProfileRow
    policy: store.PolicyObjectRow
    creation: NativeCreationService
    meter: NativeMeterAdapter
    index: SessionIndexCoordinator
    recall: ContextRecallCoordinator
    context: ArpContextPort
    _services: dict[str, SessionSearchService] = field(default_factory=dict)

    @property
    def protocol(self) -> str:
        return PROTOCOL

    def search_for(self, session: store.SessionRow):  # type: ignore[no-untyped-def]
        """(SessionSearchService, FileGuard, GenerationRow) for one live Session."""

        state = self.index.state_for(session)
        service = self._services.get(session.session_id)
        if service is None:
            service = SessionSearchService(
                state.partition,
                clock_ms=self.ports.clock_ms,
                charge=self.index.count,
                cursor_ttl_ms=int(self.policy.body["query_cursor_ttl_ms"]),
            )
            self._services[session.session_id] = service
        return service, state.guard, state.generation

    def tick(self) -> dict[str, int]:
        """One fair coordination pass: due INDEX jobs, then non-terminal recalls."""

        jobs = self.index.process_due()
        resumed = blocked = deferred = 0
        connection = self.index.uow.database.connection
        for row in store.list_pending_recalls(connection):
            session = store.read_session(connection, row.session_id)
            if session is None:
                continue
            access = self.context._access(session, row.turn_id)
            clock = Pin("receipt", f"tick:{row.recall_key}", 0, digest({"tick": self.ports.clock_ms()}))
            try:
                self.recall.resume(row.recall_key, access, now_ms=self.ports.clock_ms(), clock_receipt_ref=clock)
            except ArpError as error:
                if error.code == "FILE_BUSY":
                    deferred += 1  # another holder of the Session file; retried next tick
                    continue
                if error.code != "RECALL_PREPARE_BLOCKED":
                    raise  # anything else is a coordinator defect, not a recorded outcome
                blocked += 1  # the row itself carries the named BLOCKED/STALE code
            resumed += 1
        return {"jobs": len(jobs), "recalls": resumed, "blocked": blocked, "deferred": deferred}

    def close(self) -> None:
        self.index.close()
        self._services.clear()


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
    if not isinstance(arp.meter, MeterBinding):
        raise ArpError("GENERIC_TOKEN_BOUND_UNCERTIFIED", "ARP runtimes need a certified MeterBinding")
    if arp.meter.tokenizer is not ports.tokenizer:
        raise ArpError("TOKEN_SERIALIZER_CHANGED", "the meter binding must wrap the runtime's tokenizer")
    if ports.default_max_output_tokens > int(arp.meter.model_limits["max_output_tokens"]) or ports.max_output_tokens_ceiling > int(arp.meter.model_limits["max_output_tokens"]):
        raise ArpError("INVALID_OUTPUT_RESERVE", "output caps exceed the deployment's max_output_tokens")
    if arp.embedding is not None and arp.embedding_resource_ref is None:
        raise ArpError("EMBEDDING_RESOURCE_MISSING", "an embedding port needs its approved deployment pin")
    root = read_root(arp.root_dir)
    path = Path(ports.database_path)
    Database.open(path).close()  # fresh v10 when missing; validates an existing library
    migrate_execution_to_v11(path)
    holder: dict[str, Any] = {}

    def context_factory(uow, **kwargs):  # type: ignore[no-untyped-def]
        # Native composer: the legacy recall adapter is not used (F comes from the
        # ContextRecallCoordinator), the append semantics are inherited unchanged.
        kwargs["recall"] = None
        kwargs["recall_token_share"] = 0.0
        holder["context"] = ArpContextPort(uow, **kwargs)
        return holder["context"]

    def wire_factory(inner, database, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["request_guard"] = None  # the ARP budget and meter govern the final count
        return ArpProviderWire(inner, database, context=lambda: holder["context"], **kwargs)

    runtime = build_agent_runtime(ports, owner_scope=owner_scope, context_factory=context_factory, wire_factory=wire_factory)
    try:
        profile, policy = _freeze_profile(runtime.uow.database, arp)
        creation = NativeCreationService(
            runtime, arp, root, profile, policy, agent_id_for=agent_id_for, start_input_for=start_input_for
        )
        tokenizer = ports.tokenizer
        index = SessionIndexCoordinator(
            uow=runtime.uow,
            root=root,
            policy=policy.body,
            tokenizer_fingerprint=str(tokenizer.fingerprint),
            count=tokenizer.count_text,
            clock_ms=arp.clock_ms,
            clock=ports.clock,
            owner_id=ports.owner_id,
            embedding=arp.embedding,
            embedding_resource_ref=arp.embedding_resource_ref,
            fault=arp.fault,
        )
        context: ArpContextPort = holder["context"]
        state = ArpRuntime(
            arp, root, profile, policy, creation, NativeMeterAdapter(arp.meter), index,
            ContextRecallCoordinator(
                uow=runtime.uow,
                search_for=lambda session: state.search_for(session),
                embedding=arp.embedding,
                clock_ms=arp.clock_ms,
                clock=ports.clock,
                fault=arp.fault,
                embedding_deployment_ref=arp.embedding_resource_ref,
                capture=lambda session, highwater: holder["context"]._capture(session, highwater),
            ),
            context,
        )
        context.bind(state)
    except BaseException:
        runtime.uow.database.close()
        raise
    runtime.arp = state  # type: ignore[attr-defined]
    original_shutdown = runtime.shutdown

    async def shutdown() -> None:
        try:
            await original_shutdown()
        finally:
            state.close()

    runtime.shutdown = shutdown  # type: ignore[method-assign]
    return runtime


def arp_of(runtime: Any) -> ArpRuntime | None:
    return getattr(runtime, "arp", None)


__all__ = ("ArpRuntime", "arp_of", "build_arp_runtime")
