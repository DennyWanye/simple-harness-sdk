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

from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from simple_harness.agents.background_health import BackgroundHealthBook
from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.agents.runtime import AgentRuntime, agent_id_for, build_agent_runtime, start_input_for
from simple_harness.execution.sqlite.database import Database

from . import PROTOCOL, store
from .authorization import is_allow_all
from .bootstrap import BootstrapReport, bootstrap_builtin_tools, refresh_builtin_health, runtime_caller
from .catalogue import CapabilityResolver, CatalogueScope, CatalogueService, ToolExposureService
from .context.composer import ArpContextPort, ArpProviderWire
from .context.recall import ContextRecallCoordinator
from .creation import NativeCreationService
from .errors import ArpError
from .indexing import SessionIndexCoordinator
from .retriever import SessionRetriever
from .lifecycle import SkillLifecycleService
from .session_lifecycle import SessionLifecycleService
from .skill_tools import ArpModelTools
from .skill_use import SkillUseService
from .meter import MeterBinding, NativeMeterAdapter
from .migration import migrate_execution_to_v11
from .pins import Pin
from .ports import ArpPorts, RootIdentity, read_root
from .search import SessionSearchService
from .skills import SkillImporter
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
    retriever: SessionRetriever | None = None
    catalogue: CatalogueService | None = None
    exposure: ToolExposureService | None = None
    resolver: CapabilityResolver | None = None
    bootstrap: BootstrapReport | None = None
    catalogue_caller: Any | None = None
    skills: SkillImporter | None = None
    lifecycle: SkillLifecycleService | None = None
    skill_use: SkillUseService | None = None
    sessions: SessionLifecycleService | None = None
    health: BackgroundHealthBook = field(default_factory=BackgroundHealthBook)
    _services: dict[str, SessionSearchService] = field(default_factory=dict)

    @property
    def protocol(self) -> str:
        return PROTOCOL

    def policy_for(self, session: store.SessionRow) -> tuple[store.PolicyAdoptionRow, store.PolicyObjectRow]:
        """The Session's currently adopted policy (settings update = next request only); the
        frozen profile policy is only its first revision."""

        connection = self.index.uow.database.connection
        adoption = store.latest_adoption(connection, session.session_id)
        if adoption is None:
            raise ArpError("POLICY_CONFLICT", "session has no adopted context policy")
        row = store.read_policy_object(connection, adoption.policy_ref.id, adoption.policy_ref.revision)
        if row is None or row.content_hash != adoption.policy_ref.content_hash:
            raise ArpError("POLICY_CONFLICT", "adopted policy object is missing or differs")
        return adoption, row

    def search_for(self, session: store.SessionRow):  # type: ignore[no-untyped-def]
        """(SessionSearchService, FileGuard, GenerationRow) for one live Session."""

        state = self.index.state_for(session)
        _, policy = self.policy_for(session)
        service = self._services.get(session.session_id)
        if service is None:
            service = SessionSearchService(
                state.partition,
                clock_ms=self.ports.clock_ms,
                charge=self.index.count,
                cursor_ttl_ms=int(policy.body["query_cursor_ttl_ms"]),
            )
            self._services[session.session_id] = service
        service._ttl = int(policy.body["query_cursor_ttl_ms"])  # the adopted policy's TTL governs new cursors
        return service, state.guard, state.generation

    def release_session(self, session_id: str) -> None:
        """Drop one Session's in-memory partition and search service (before destroy/rebuild)."""

        self.index.release(session_id)
        self._services.pop(session_id, None)

    def tick(self) -> dict[str, int]:
        """One fair coordination pass: due INDEX/PURGE jobs, DRAINING proofs, then non-terminal recalls."""

        return self._tick_after(self.index.process_due())

    async def tick_async(self) -> dict[str, int]:
        """The background pump's pass: embeddings are computed off the event loop."""

        return self._tick_after(await self.index.process_due_async())

    def _tick_after(self, jobs: tuple[Any, ...]) -> dict[str, int]:
        # 2026-09-25 主流程优化条目 6: every stage of the pass is isolated per item.  A
        # coordinator defect on one Session is recorded in ``health`` (and logged) and
        # the pass moves on, instead of aborting the whole tick — every tick — silently.
        health = self.health
        draining = 0
        if self.sessions is not None:
            try:
                draining = self.sessions.drive_draining(
                    on_error=lambda session_id, error: health.fail("draining", error, item=session_id))
                if draining:
                    health.ok("draining")
            except Exception as error:  # noqa: BLE001 - listing itself failed
                health.fail("draining", error)
        probed = False
        if self.catalogue is not None and self.bootstrap is not None and self.catalogue_caller is not None:
            try:
                probed = refresh_builtin_health(self.catalogue, self.bootstrap.deployment_ref, caller=self.catalogue_caller)
                health.ok("tool_probe")
            except Exception as error:  # noqa: BLE001
                health.fail("tool_probe", error)
        resumed = blocked = deferred = failed = 0
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
                    # a coordinator defect, not a recorded outcome: visible, and isolated
                    failed += 1
                    health.fail("recall", error, item=row.recall_key)
                    continue
                blocked += 1  # the row itself carries the named BLOCKED/STALE code
            except Exception as error:  # noqa: BLE001
                failed += 1
                health.fail("recall", error, item=row.recall_key)
                continue
            resumed += 1
        if resumed or blocked:
            health.ok("recall")
        return {"jobs": len(jobs), "draining": draining, "recalls": resumed, "blocked": blocked, "deferred": deferred,
                "failed": failed, "health_probed": int(probed)}

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


def _standalone_fence(agent_id: str, turn_id: str):  # type: ignore[no-untyped-def]
    """A deployment without its own lifecycle fence: nothing to hold across the handoff."""
    del agent_id, turn_id
    return nullcontext()


def _admitted_ports(ports: AgentRuntimePorts) -> AgentRuntimePorts:
    """The composer runs on ``prepare_request``; the kernel prepares a request *before* the
    physical handoff only when an admission port exists (without one the legacy wire
    prepares inside ``invoke``, after the invocation is already ``handed_off``).  A frozen
    context request must precede the handoff — the prior-output reserve reads the run's
    recorded invocations and would otherwise meet the request's own, unresolved one — so a
    deployment that brings no admission gets the SDK's local one with a no-op fence.  The
    concurrency cap moves from the wire semaphore to that admission unchanged."""
    if ports.provider_admission is not None or ports.local_provider_admission is not None or ports.provider_handoff_fence is not None:
        return ports
    return replace(ports, provider_handoff_fence=_standalone_fence)


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
    ports = _admitted_ports(ports)
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

    def session_tools_factory():  # type: ignore[no-untyped-def]
        holder["tools"] = ArpModelTools()
        return holder["tools"]

    def exposure_reader(base):  # type: ignore[no-untyped-def]
        return _DynamicExposure(base, holder)

    runtime = build_agent_runtime(
        ports, owner_scope=owner_scope, context_factory=context_factory, wire_factory=wire_factory, session_tools_factory=session_tools_factory,
        exposure_reader=exposure_reader,
    )
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
        state.retriever = SessionRetriever(state, capture=lambda session, highwater: holder["context"]._capture(session, highwater))
        holder["tools"].attach(state.retriever)
        # Unified catalogue for this namespace: mount, builtin bootstrap, exposure/resolver.
        scope = CatalogueScope(realm_id=root.root_id, owner_id=owner_scope, project_id=None, catalogue_owner_root_id=root.root_id)
        state.catalogue = CatalogueService(runtime.uow, scope, arp.clock_ms, ports.clock)
        owner_contract = Pin("policy", f"{profile.profile_id}:owner-mode:{arp.profile.owner_mode}", profile.revision, profile.body_hash)
        caller = runtime_caller(arp.profile.refs.activation_receipt_ref, owner_contract)
        state.catalogue.mount(mount_receipt_ref=caller.command_receipt_ref)
        from simple_harness import __version__ as sdk_version

        state.bootstrap = bootstrap_builtin_tools(
            state.catalogue, specs=tuple(runtime._assembled.registry.specs), caller=caller, approval_ref=policy.approval_ref,
            effect_classes=arp.extra.get("tool_effect_classes"), sdk_version=str(sdk_version),
        )
        state.catalogue_caller = caller
        state.exposure = ToolExposureService(state.catalogue)
        state.resolver = CapabilityResolver(state.catalogue)
        state.skills = SkillImporter(
            state.catalogue, root.directory / "bundles", state.bootstrap.instructions_capability_ref, state.bootstrap.instructions_schema_ref,
            state.bootstrap.verification_policy_ref, arp.clock_ms,
        )
        state.lifecycle = SkillLifecycleService(state.catalogue, state.skills, arp.acceptance, arp.clock_ms)
        # BW09: an acceptance reader that needs the catalogue's dispatch links (the real
        # Assurance adapter) binds to the lifecycle service here; test doubles ignore it.
        bind = getattr(arp.acceptance, "bind_lifecycle", None)
        if callable(bind):
            bind(state.lifecycle)
        from .context.skill_blocks import skill_blocks_for

        def skill_capacity() -> int:
            limits = state.meter.binding.model_limits
            body = policy.body
            candidates = [int(body["max_context_tokens"]), int(limits["input_limit_tokens"])]
            if limits.get("combined_limit_tokens") is not None:
                candidates.append(int(limits["combined_limit_tokens"]) - int(limits["max_output_tokens"]))
            return max(0, (min(candidates) - int(body["safety_reserve_tokens"]) - int(body["tool_headroom_tokens"])) // 2)

        state.skill_use = SkillUseService(
            state.catalogue, state.skills, runtime.uow, arp.clock_ms, ports.clock, policy.approval_ref, str(arp.profile.owner_mode), script_runner=arp.script_runner,
            count=index.count, skill_capacity=skill_capacity, loaded_blocks=lambda session: skill_blocks_for(state, session, count=index.count),
        )
        state.sessions = SessionLifecycleService(
            runtime=runtime, root=root, index=index, recall=state.recall, access_for=lambda session, turn_id: holder["context"]._access(session, turn_id),
            capture=lambda session, highwater: holder["context"]._capture(session, highwater), release=state.release_session,
            retention_policy_ref=arp.profile.refs.retention_policy_ref, authority_ref=policy.approval_ref, clock_ms=arp.clock_ms, clock=ports.clock,
            owner_id=ports.owner_id, fault=arp.fault,
        )
        index.handlers["PURGE"] = state.sessions.process_purge_job
        holder["exposure"] = state.exposure
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


class _DynamicExposure:
    """Per-call exposure: the Run's configured tools ∩ the catalogue's currently usable tools."""

    dynamic = True

    def __init__(self, base: Any, holder: dict[str, Any]) -> None:
        self._base = base
        self._holder = holder

    def __call__(self, run_id: str) -> tuple[str, ...] | None:
        names = self._base(run_id)
        exposure = self._holder.get("exposure")
        if names is None or exposure is None:
            return names
        exposed, _ = exposure.exposed(tuple(names), now_ms=exposure.catalogue.clock_ms())
        return tuple(e.model_name for e in exposed)


def arp_of(runtime: Any) -> ArpRuntime | None:
    return getattr(runtime, "arp", None)


__all__ = ("ArpRuntime", "arp_of", "build_arp_runtime")
