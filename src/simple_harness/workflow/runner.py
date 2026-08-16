# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable Workflow admission, activation, and Native delegation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import ExecutionLease, RunRecord, RunState
from simple_harness.runtime.orchestration import (
    RuntimeStartDispatchClaim,
    VerifiedWorkflowCatalogAuthority,
    VerifiedWorkflowGraphUnavailable,
    VerifiedWorkflowLaunchTicket,
    WorkflowCatalogProfileBinding,
    WorkflowProfileRegistration,
    WorkflowSpawnReadyActivation,
)
from simple_harness.runtime.profiles import ProfileDescriptor
from simple_harness.runtime.start_snapshot import RunStart, StartSnapshot
from simple_harness.tools.schema import validate_arguments

from .contracts import WorkflowContext, WorkflowHostServices, WorkflowRunStatus
from .definition import (
    CompiledWorkflow,
    WorkflowDefinition,
    WorkflowDefinitionRegistration,
    WorkflowManifest,
    compile_workflow,
    compile_workflow_registration,
    workflow_manifest_hash,
)
from .errors import WorkflowDependencyUnavailable
from .execution_ports import (
    CancelWorkflowRequest,
    DangerousEffectConfirmation,
    PrecreatedStartDispatch,
    ResumeAdmissionReceipt,
    ResumeAdmissionRequest,
    ResumeCommitBinding,
    ResumePhase,
    StartAdmissionRequest,
    StartMode,
    WorkflowActivation,
    WorkflowExecutionPorts,
    WorkflowRecoveryReceiptKind,
    WorkflowRecoveryWork,
    WorkflowTransaction,
)
from .recovery import RecoveryRecord, WorkflowRecoveryPort
from .trace import WorkflowTraceEvent, WorkflowTracePort

if TYPE_CHECKING:
    from .native import (
        NativeCheckpointStore,
        TerminalCommitProjectionPort,
        TerminalProjectionPort,
        WorkflowObserverPort,
        WorkflowProgressPort,
    )


class NativeExecutableProtocol(Protocol):
    manifest: WorkflowManifest

    async def ainvoke(
        self, state: object, context: WorkflowContext, **kwargs: object
    ) -> object: ...

    async def resume(
        self,
        responses: Mapping[str, JsonValue],
        context: WorkflowContext,
        **kwargs: object,
    ) -> object: ...


def manifest_hash(manifest: WorkflowManifest) -> str:
    """Compatibility alias for the public manifest fingerprint helper."""

    return workflow_manifest_hash(manifest)


def _clone_definition(definition: WorkflowDefinition) -> WorkflowDefinition:
    terminal = definition.terminal_projection_descriptor
    return replace(
        definition,
        nodes=tuple(
            replace(node, retry_policy=replace(node.retry_policy))
            for node in definition.nodes
        ),
        channels={name: replace(spec) for name, spec in definition.channels.items()},
        edges=tuple(replace(edge) for edge in definition.edges),
        conditional_edges=tuple(
            replace(edge, routes=dict(edge.routes))
            for edge in definition.conditional_edges
        ),
        loop_budgets=dict(definition.loop_budgets),
        loop_budget_bindings=dict(definition.loop_budget_bindings),
        prompt_manifest=copy.deepcopy(dict(definition.prompt_manifest)),
        tool_manifest=tuple(
            replace(
                tool,
                effect_policy=(
                    None
                    if tool.effect_policy is None
                    else replace(tool.effect_policy)
                ),
            )
            for tool in definition.tool_manifest
        ),
        policy_manifest=copy.deepcopy(dict(definition.policy_manifest)),
        terminal_projection_descriptor=(
            None if terminal is None else replace(terminal)
        ),
    )


def _sealed_compiled_workflow(workflow: CompiledWorkflow) -> CompiledWorkflow:
    try:
        definition_nodes = {node.node_id: node for node in workflow.definition.nodes}
        current_nodes = workflow._nodes
        if tuple(sorted(current_nodes)) != tuple(sorted(definition_nodes)) or any(
            current_nodes[node_id] != node
            for node_id, node in definition_nodes.items()
        ):
            raise ValueError("compiled node map differs from definition")
        sealed = compile_workflow(
            _clone_definition(workflow.definition),
            dependency_lock_hash=workflow.manifest.dependency_lock_hash,
        )
        if manifest_hash(sealed.manifest) != manifest_hash(workflow.manifest):
            raise ValueError("compiled manifest differs from definition")
        return sealed
    except Exception as exc:
        raise WorkflowDependencyUnavailable(
            "workflow executable integrity check failed"
        ) from exc


def _catalog_binding_json(
    binding: WorkflowCatalogProfileBinding,
) -> dict[str, JsonValue]:
    terminal = binding.terminal_projection_descriptor
    capability = thaw_json(cast(FrozenJsonValue, binding.capability_snapshot))
    if not isinstance(capability, dict):
        raise TypeError("catalog capability snapshot must remain an object")
    return {
        "profile_key": binding.profile_key,
        "description": binding.description,
        "use_when": binding.use_when,
        "avoid_when": binding.avoid_when,
        "input_schema_ref": binding.input_schema_ref,
        "profile_fingerprint": binding.profile_fingerprint,
        "workflow_name": binding.workflow_name,
        "workflow_version": binding.workflow_version,
        "implementation_fingerprint": binding.implementation_fingerprint,
        "checkpoint_namespace": binding.checkpoint_namespace,
        "manifest_hash": binding.manifest_hash,
        "state_schema_version": binding.state_schema_version,
        "start_input_schema": binding.start_input_schema.to_json(),
        "terminal_projection_descriptor": (
            None
            if terminal is None
            else thaw_json(cast(FrozenJsonValue, terminal))
        ),
        "terminal_request_factory_hash": cast(
            str | None, binding.terminal_request_factory_hash
        ),
        "capability_snapshot": capability,
    }


def _verified_ticket_binding_json(
    ticket: VerifiedWorkflowLaunchTicket,
) -> dict[str, JsonValue]:
    return {
        "profile_key": ticket.profile_key,
        "description": ticket.description,
        "use_when": ticket.use_when,
        "avoid_when": ticket.avoid_when,
        "input_schema_ref": ticket.input_schema_ref,
        "profile_fingerprint": ticket.profile_fingerprint,
        "workflow_name": ticket.workflow_name,
        "workflow_version": ticket.workflow_version,
        "implementation_fingerprint": ticket.implementation_fingerprint,
        "checkpoint_namespace": ticket.checkpoint_namespace,
        "manifest_hash": ticket.manifest_hash,
        "state_schema_version": ticket.state_schema_version,
        "start_input_schema": ticket.start_input_schema.to_json(),
        "terminal_projection_descriptor": (
            None
            if ticket.terminal_projection_descriptor is None
            else thaw_json(
                cast(FrozenJsonValue, ticket.terminal_projection_descriptor)
            )
        ),
        "terminal_request_factory_hash": cast(
            str | None, ticket.terminal_request_factory_hash
        ),
        "capability_snapshot": thaw_json(
            cast(FrozenJsonValue, ticket.capability_snapshot)
        ),
    }


@dataclass(frozen=True, slots=True)
class RegisteredWorkflow:
    workflow: CompiledWorkflow
    _sealed_manifest_hash: str
    _sealed_implementation_hash: str

    @classmethod
    def seal(cls, workflow: CompiledWorkflow) -> RegisteredWorkflow:
        sealed = _sealed_compiled_workflow(workflow)
        return cls(
            sealed,
            manifest_hash(sealed.manifest),
            sealed.manifest.implementation_bundle_hash,
        )

    def assert_integrity(self) -> None:
        try:
            rebuilt = _sealed_compiled_workflow(self.workflow)
            if (
                manifest_hash(self.workflow.manifest) != self._sealed_manifest_hash
                or manifest_hash(rebuilt.manifest) != self._sealed_manifest_hash
                or self.workflow.manifest.implementation_bundle_hash
                != self._sealed_implementation_hash
            ):
                raise ValueError("sealed workflow identity changed")
        except Exception as exc:
            raise WorkflowDependencyUnavailable(
                "workflow executable integrity check failed"
            ) from exc

    @property
    def manifest(self) -> WorkflowManifest:
        return self.workflow.manifest

    def materialize(
        self,
        *,
        store: NativeCheckpointStore,
        terminal_projection_port: TerminalProjectionPort,
        terminal_commit_projection_port: TerminalCommitProjectionPort,
        progress_port: WorkflowProgressPort | None,
        observer_port: WorkflowObserverPort | None,
    ) -> NativeExecutableProtocol:
        self.assert_integrity()
        return cast(
            NativeExecutableProtocol,
            self.workflow.bind(
                store=store,
                terminal_projection_port=terminal_projection_port,
                terminal_commit_projection_port=terminal_commit_projection_port,
                progress_port=progress_port,
                observer_port=observer_port,
            ),
        )


class WorkflowRegistry:
    def __init__(
        self,
        workflows: tuple[CompiledWorkflow, ...] = (),
        *,
        transaction_owner: object | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._revision = 0
        self._entries: dict[tuple[str, str], RegisteredWorkflow] = {}
        self._profile_registrations: dict[str, WorkflowDefinitionRegistration] = {}
        self._transaction_owner = transaction_owner
        for workflow in workflows:
            self.register(workflow)

    @property
    def transaction_owner(self) -> object | None:
        return self._transaction_owner

    def bind_transaction_owner(self, transaction_owner: object) -> None:
        if transaction_owner is None:
            raise ValueError("transaction_owner is required")
        with self._lock:
            if (
                self._transaction_owner is not None
                and self._transaction_owner is not transaction_owner
            ):
                raise ValueError("workflow registry transaction owner mismatch")
            self._transaction_owner = transaction_owner

    def register_definition(
        self, registration: WorkflowDefinitionRegistration
    ) -> RegisteredWorkflow:
        if not isinstance(registration, WorkflowDefinitionRegistration):
            raise TypeError(
                "registration must be a WorkflowDefinitionRegistration"
            )
        owner = self._transaction_owner
        if owner is None:
            raise ValueError("workflow registry transaction owner is not bound")
        key = registration.profile.descriptor.key
        with self._lock:
            existing_registration = self._profile_registrations.get(key)
            if existing_registration is not None:
                if existing_registration != registration:
                    raise ValueError(
                        "profile key already registered with different fingerprint"
                    )
                return self.require(
                    registration.definition.name, registration.definition.version
                )
            compiled = compile_workflow_registration(
                registration, transaction_owner=owner
            )
            entry = self.register(compiled)
            self._profile_registrations[key] = registration
            self._revision += 1
            return entry

    def profile_registrations(self) -> tuple[WorkflowDefinitionRegistration, ...]:
        with self._lock:
            return tuple(
                self._profile_registrations[key]
                for key in sorted(self._profile_registrations)
            )

    def profile_registration(
        self, profile_key: str
    ) -> WorkflowDefinitionRegistration | None:
        with self._lock:
            return self._profile_registrations.get(profile_key)

    def register(
        self, workflow: CompiledWorkflow, *, replace: bool = False
    ) -> RegisteredWorkflow:
        if not isinstance(workflow, CompiledWorkflow):
            raise TypeError("a registered workflow must be a CompiledWorkflow")
        manifest = workflow.manifest
        if str(manifest.durability) != "sync":
            raise ValueError("durable workflow manifests must use sync durability")
        key = (str(manifest.workflow_name), str(manifest.workflow_version))
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and not replace:
                if manifest_hash(existing.manifest) != manifest_hash(manifest):
                    raise ValueError(
                        "workflow version already registered with different manifest"
                    )
                return existing
            entry = RegisteredWorkflow.seal(workflow)
            self._entries[key] = entry
            self._revision += 1
            return entry

    def unregister(self, workflow_name: str, workflow_version: str) -> None:
        with self._lock:
            if self._entries.pop((workflow_name, workflow_version), None) is not None:
                self._revision += 1

    def get(
        self, workflow_name: str, workflow_version: str
    ) -> RegisteredWorkflow | None:
        with self._lock:
            return self._entries.get((workflow_name, workflow_version))

    def snapshot_required(
        self, identities: tuple[tuple[str, str], ...]
    ) -> tuple[int, tuple[RegisteredWorkflow, ...]]:
        """Capture one immutable registry view under the registry lock."""

        with self._lock:
            entries: list[RegisteredWorkflow] = []
            for workflow_name, workflow_version in identities:
                entry = self._entries.get((workflow_name, workflow_version))
                if entry is None:
                    raise WorkflowDependencyUnavailable(
                        "workflow graph version unavailable: "
                        f"{workflow_name}@{workflow_version}"
                    )
                entries.append(entry)
            return self._revision, tuple(entries)

    def require_snapshot_current(
        self, revision: int, entries: tuple[RegisteredWorkflow, ...] = ()
    ) -> None:
        with self._lock:
            if self._revision != revision:
                raise WorkflowDependencyUnavailable(
                    "workflow registry changed during catalog snapshot"
                )
            for entry in entries:
                entry.assert_integrity()

    def require(
        self,
        workflow_name: str,
        workflow_version: str,
        *,
        expected_manifest_hash: str | None = None,
        expected_implementation_hash: str | None = None,
    ) -> RegisteredWorkflow:
        with self._lock:
            _revision, entries = self.snapshot_required(
                ((workflow_name, workflow_version),)
            )
            entry = entries[0]
            entry.assert_integrity()
            if (
                expected_manifest_hash is not None
                and manifest_hash(entry.manifest) != expected_manifest_hash
            ):
                raise WorkflowDependencyUnavailable("workflow manifest hash mismatch")
            if (
                expected_implementation_hash is not None
                and entry.manifest.implementation_bundle_hash
                != expected_implementation_hash
            ):
                raise WorkflowDependencyUnavailable(
                    "workflow implementation hash mismatch"
                )
            return entry

    def versions(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(sorted(self._entries))

    def implementation_hashes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    item.manifest.implementation_bundle_hash
                    for item in self._entries.values()
                )
            )

    def snapshot_all(
        self,
    ) -> tuple[int, tuple[RegisteredWorkflow, ...], str]:
        """Return one sealed, deterministic registry content snapshot."""

        with self._lock:
            ordered = tuple(self._entries[key] for key in sorted(self._entries))
            content: list[JsonValue] = []
            for entry in ordered:
                entry.assert_integrity()
                content.append(
                    {
                        "workflow_name": entry.manifest.workflow_name,
                        "workflow_version": entry.manifest.workflow_version,
                        "manifest_hash": manifest_hash(entry.manifest),
                        "implementation_hash": (
                            entry.manifest.implementation_bundle_hash
                        ),
                    }
                )
            digest = hashlib.sha256(canonical_json(content).encode()).hexdigest()
            return self._revision, ordered, digest


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    run_id: str
    status: WorkflowRunStatus
    output: object | None = None
    error: dict[str, object] | None = None
    recovery_action: str | None = None


_STATUS = {
    RunState.CREATED: WorkflowRunStatus.CREATED,
    RunState.QUEUED: WorkflowRunStatus.RETRYABLE,
    RunState.RUNNING: WorkflowRunStatus.RUNNING,
    RunState.WAITING: WorkflowRunStatus.WAITING,
    RunState.CANCEL_REQUESTED: WorkflowRunStatus.CANCEL_REQUESTED,
    RunState.COMPLETED: WorkflowRunStatus.COMPLETED,
    RunState.FAILED: WorkflowRunStatus.FAILED,
    RunState.CANCELLED: WorkflowRunStatus.CANCELLED,
    RunState.ADMISSION_PENDING: WorkflowRunStatus.CREATED,
}


class WorkflowRunner:
    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        checkpoint: NativeCheckpointStore,
        recovery: WorkflowRecoveryPort,
        trace: WorkflowTracePort,
        execution_ports: WorkflowExecutionPorts,
        terminal_projection_port: TerminalProjectionPort,
        terminal_commit_projection_port: TerminalCommitProjectionPort,
        progress_port: WorkflowProgressPort | None = None,
        observer_port: WorkflowObserverPort | None = None,
        host_services: WorkflowHostServices = WorkflowHostServices(),
        owner: str | None = None,
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        clock=time.time,
        sleep=asyncio.sleep,
    ) -> None:
        owner_identity = execution_ports.unit_of_work.transaction_owner
        if (
            any(
                authority.transaction_owner is not owner_identity
                for authority in (checkpoint,)
            )
            or checkpoint.transaction_owner
            is not execution_ports.checkpoint.transaction_owner
        ):
            raise ValueError("workflow authorities have different transaction owners")
        if not isinstance(host_services, WorkflowHostServices):
            raise TypeError("host_services must be a WorkflowHostServices")
        if (
            registry.transaction_owner is not None
            and registry.transaction_owner is not owner_identity
        ):
            raise ValueError("workflow registry transaction owner mismatch")
        self.registry = registry
        self.checkpoint = checkpoint
        self.recovery = recovery
        self.trace = trace
        self.execution_ports = execution_ports
        self.native_store = checkpoint
        self.terminal_projection_port = terminal_projection_port
        self.terminal_commit_projection_port = terminal_commit_projection_port
        self.progress_port = progress_port
        self.observer_port = observer_port
        self.host_services = host_services
        self.owner = owner or f"workflow-runner-{uuid.uuid4().hex}"
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock
        self._sleep = sleep

    @staticmethod
    def _catalog_binding(
        entry: RegisteredWorkflow,
        registration: WorkflowProfileRegistration,
    ) -> WorkflowCatalogProfileBinding:
        entry.assert_integrity()
        descriptor = registration.descriptor
        manifest = entry.manifest
        terminal = manifest.terminal_projection_descriptor
        tools: list[dict[str, JsonValue]] = []
        for tool in entry.workflow.definition.tool_manifest:
            effect = tool.effect_policy
            tools.append(
                {
                    "name": tool.name,
                    "access": str(tool.access),
                    "spec_version": tool.spec_version,
                    "schema_hash": tool.schema_hash,
                    "effect_policy": (
                        None
                        if effect is None
                        else {
                            "policy_id": effect.policy_id,
                            "version": effect.version,
                            "kind": str(effect.kind),
                            "max_attempts": effect.max_attempts,
                            "reusable_across_branches": effect.reusable_across_branches,
                        }
                    ),
                    "outcome_parser_id": tool.outcome_parser_id,
                    "outcome_parser_version": tool.outcome_parser_version,
                    "outcome_parser_hash": tool.outcome_parser_hash,
                }
            )
        return WorkflowCatalogProfileBinding(
            profile_key=descriptor.key,
            description=descriptor.description,
            use_when=descriptor.use_when,
            avoid_when=descriptor.avoid_when,
            input_schema_ref=descriptor.input_schema_ref,
            profile_fingerprint=descriptor.fingerprint,
            workflow_name=registration.workflow_name,
            workflow_version=registration.workflow_version,
            implementation_fingerprint=manifest.implementation_bundle_hash,
            checkpoint_namespace=(
                f"workflow/{descriptor.key}/{registration.workflow_name}/"
                f"{registration.workflow_version}"
            ),
            manifest_hash=manifest_hash(manifest),
            state_schema_version=manifest.state_schema_version,
            start_input_schema=registration.start_input_schema,
            terminal_projection_descriptor=(
                None if terminal is None else terminal.to_dict()
            ),
            terminal_request_factory_hash=(
                None if terminal is None else terminal.request_factory_hash
            ),
            capability_snapshot={"tools": cast(JsonValue, tools)},
        )

    def prepare_catalog_authority(
        self,
        generation: int,
        registrations: tuple[WorkflowProfileRegistration, ...],
    ) -> VerifiedWorkflowCatalogAuthority:
        from simple_harness.runtime.orchestration import (
            WorkflowCatalogAuthority,
            _create_verified_workflow_catalog_authority,
            workflow_catalog_hash,
        )

        if isinstance(generation, bool) or generation < 1:
            raise ValueError("catalog generation must be positive")
        supplied = tuple(registrations)
        if not supplied or any(
            not isinstance(item, WorkflowProfileRegistration) for item in supplied
        ):
            raise TypeError("catalog registrations must be typed and non-empty")
        if any(item.descriptor.generation != generation for item in supplied):
            raise ValueError("registration generation differs from catalog")
        ordered = tuple(sorted(supplied, key=lambda item: item.descriptor.key))
        if len({item.descriptor.key for item in ordered}) != len(ordered):
            raise ValueError("catalog registrations contain a duplicate profile")
        registry_revision, entries = self.registry.snapshot_required(
            tuple(
                (item.workflow_name, item.workflow_version) for item in ordered
            )
        )
        bindings = []
        snapshot_profiles: list[dict[str, JsonValue]] = []
        for registration, entry in zip(ordered, entries, strict=True):
            binding = self._catalog_binding(entry, registration)
            bindings.append(binding)
            snapshot_profiles.append(_catalog_binding_json(binding))
        profile_tuple = tuple(bindings)
        authority = WorkflowCatalogAuthority(
            "model_spawnable",
            generation,
            generation,
            workflow_catalog_hash("model_spawnable", generation, profile_tuple),
            profile_tuple,
        )
        snapshot_payload: dict[str, JsonValue] = {
            "generation": generation,
            "profiles": cast(JsonValue, snapshot_profiles),
        }
        snapshot_hash = hashlib.sha256(
            canonical_json(snapshot_payload).encode()
        ).hexdigest()
        self.registry.require_snapshot_current(registry_revision, entries)
        return _create_verified_workflow_catalog_authority(authority, snapshot_hash)

    def _prove_graph_unavailable(
        self,
        ticket: VerifiedWorkflowLaunchTicket,
        activation: WorkflowSpawnReadyActivation,
    ) -> VerifiedWorkflowGraphUnavailable:
        from simple_harness.runtime.orchestration import (
            _create_verified_workflow_graph_unavailable,
        )

        if (
            type(ticket) is not VerifiedWorkflowLaunchTicket
            or not ticket._is_sdk_verified()
        ):
            raise TypeError("graph proof requires an SDK-verified ticket")
        if not isinstance(activation, WorkflowSpawnReadyActivation):
            raise TypeError("graph proof requires a ready activation")
        continuation = activation.continuation_claim
        if (
            activation.ready_receipt.ticket_receipt_id != ticket.ticket_receipt_id
            or continuation.ticket_receipt_id != ticket.ticket_receipt_id
            or continuation.parent_run_id != ticket.parent_run_id
        ):
            raise WorkflowDependencyUnavailable(
                "workflow graph proof activation differs from ticket"
            )
        revision, entries, registry_digest = self.registry.snapshot_all()
        observed = next(
            (
                entry
                for entry in entries
                if (
                    entry.manifest.workflow_name,
                    entry.manifest.workflow_version,
                )
                == (ticket.workflow_name, ticket.workflow_version)
            ),
            None,
        )
        if observed is None:
            observed_kind = "missing"
            observed_hash = None
        else:
            observed_hash = observed.manifest.implementation_bundle_hash
            if observed_hash == ticket.implementation_fingerprint:
                raise WorkflowDependencyUnavailable(
                    "workflow graph version is available"
                )
            observed_kind = "drift"
        self.registry.require_snapshot_current(revision, entries)
        return _create_verified_workflow_graph_unavailable(
            {
                "ticket_receipt_id": ticket.ticket_receipt_id,
                "profile_key": ticket.profile_key,
                "workflow_name": ticket.workflow_name,
                "workflow_version": ticket.workflow_version,
                "expected_implementation_hash": ticket.implementation_fingerprint,
                "registry_content_digest": registry_digest,
                "activation_receipt_id": activation.activation_receipt_id,
                "parent_run_id": continuation.parent_run_id,
                "owner_id": continuation.owner_id,
                "runtime_lease_epoch": continuation.runtime_lease_epoch,
                "run_fence_epoch": continuation.run_fence_epoch,
                "workflow_lease_epoch": continuation.workflow_lease_epoch,
                "continuation_claim_epoch": continuation.claim_epoch,
                "observed_kind": observed_kind,
                "observed_implementation_hash": observed_hash,
            }
        )

    def prepare_start_admission(
        self,
        ticket: VerifiedWorkflowLaunchTicket,
        start: RunStart,
    ) -> StartAdmissionRequest:
        from simple_harness.workflow.execution_ports import (
            StartAdmissionRequest,
            StartMode,
        )

        if type(ticket) is not VerifiedWorkflowLaunchTicket or not ticket._is_sdk_verified():
            raise TypeError("start admission requires an SDK-verified ticket")
        entry = self.registry.require(ticket.workflow_name, ticket.workflow_version)
        registration = WorkflowProfileRegistration(
            descriptor=ProfileDescriptor(
                key=ticket.profile_key,
                description=ticket.description,
                use_when=ticket.use_when,
                avoid_when=ticket.avoid_when,
                input_schema_ref=ticket.input_schema_ref,
                generation=ticket.catalog_generation,
                fingerprint=ticket.profile_fingerprint,
            ),
            workflow_name=ticket.workflow_name,
            workflow_version=ticket.workflow_version,
            start_input_schema=ticket.start_input_schema,
        )
        binding = self._catalog_binding(entry, registration)
        if _verified_ticket_binding_json(ticket) != _catalog_binding_json(binding):
            raise ValueError("verified ticket differs from the compiled Workflow binding")
        start_payload = copy.deepcopy(dict(start.input))
        validate_arguments(start_payload, ticket.start_input_schema.canonical_schema)
        if (
            start.execution_session_id.value != ticket.session_id
            or start.request_id.value != ticket.request_id
            or start.run_id.value != ticket.resolved_run_id
            or start.tool_catalog_generation != ticket.tool_catalog_generation
            or hashlib.sha256(canonical_json(start_payload).encode()).hexdigest()
            != ticket.start_input_hash
        ):
            raise ValueError("RunStart differs from the verified launch ticket")
        return StartAdmissionRequest(
            request_key=ticket.ticket_receipt_id,
            mode=StartMode.PRECREATED,
            session_id=ticket.session_id,
            request_id=ticket.request_id,
            turn_id=ticket.turn_id,
            profile_key=ticket.profile_key,
            driver_kind="workflow",
            tool_catalog_generation=ticket.tool_catalog_generation,
            workflow_name=ticket.workflow_name,
            workflow_version=ticket.workflow_version,
            requested_run_id=ticket.requested_run_id,
            requested_trace_id=ticket.requested_trace_id,
            requested_thread_id=ticket.requested_thread_id,
            resolved_run_id=ticket.resolved_run_id,
            resolved_trace_id=ticket.resolved_trace_id,
            resolved_thread_id=ticket.resolved_thread_id,
            checkpoint_namespace=binding.checkpoint_namespace,
            manifest_hash=binding.manifest_hash,
            implementation_hash=binding.implementation_fingerprint,
            state_schema_version=binding.state_schema_version,
            start_input_schema_ref=binding.start_input_schema.schema_ref,
            start_input_schema_hash=binding.start_input_schema.schema_hash,
            terminal_projection_descriptor=binding.terminal_projection_descriptor,
            terminal_request_factory_hash=binding.terminal_request_factory_hash,
            start_input=start_payload,
            capability_snapshot=cast(
                Mapping[str, JsonValue],
                thaw_json(cast(FrozenJsonValue, binding.capability_snapshot)),
            ),
        )

    async def start(
        self,
        *,
        session_id: str,
        request_id: str,
        turn_id: str,
        profile_key: str,
        tool_catalog_generation: int,
        workflow_name: str,
        workflow_version: str,
        start_input: Mapping[str, JsonValue],
        capability_snapshot: Mapping[str, JsonValue],
        request_key: str | None = None,
        capability_hash: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_ns: str = "native",
    ) -> str:
        profile_registration = self.registry.profile_registration(profile_key)
        if profile_registration is not None:
            profile = profile_registration.profile
            if (
                profile.workflow_name != workflow_name
                or profile.workflow_version != workflow_version
            ):
                raise ValueError(
                    "workflow identity differs from the registered profile"
                )
            validate_arguments(
                copy.deepcopy(dict(start_input)),
                profile.start_input_schema.canonical_schema,
            )
        entry = self.registry.require(workflow_name, workflow_version)
        capability = copy.deepcopy(dict(capability_snapshot))
        calculated = hashlib.sha256(canonical_json(capability).encode()).hexdigest()
        if capability_hash is not None and capability_hash != calculated:
            raise ValueError("capability hash does not match snapshot")
        descriptor = entry.manifest.terminal_projection_descriptor
        request = StartAdmissionRequest(
            request_key
            or f"{session_id}:{request_id}:{turn_id}:{profile_key}:{workflow_name}",
            StartMode.STANDALONE,
            session_id,
            request_id,
            turn_id,
            profile_key,
            "workflow",
            tool_catalog_generation,
            workflow_name,
            workflow_version,
            run_id,
            trace_id,
            thread_id,
            run_id,
            trace_id,
            thread_id,
            checkpoint_ns,
            manifest_hash(entry.manifest),
            entry.manifest.implementation_bundle_hash,
            entry.manifest.state_schema_version,
            None,
            None,
            None if descriptor is None else descriptor.to_dict(),
            None if descriptor is None else descriptor.request_factory_hash,
            start_input,
            capability,
        )

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            receipt = await self.execution_ports.lifecycle.admit_start_standalone(
                transaction, request, now=float(self._clock())
            )
            await self.trace.append(
                WorkflowTraceEvent(receipt.run_id, "workflow.admitted", {}),
                transaction=transaction,
            )
            return receipt

        receipt = await self.execution_ports.unit_of_work.run_atomic(
            operation, fault_label="workflow:start"
        )
        return receipt.run_id

    async def run(
        self,
        run_id: str,
        state: object | None = None,
        context: WorkflowContext | None = None,
    ) -> WorkflowRunResult:
        return await self._execute(
            run_id,
            state=state,
            responses=None,
            context=context or WorkflowContext(),
            precreated=None,
        )

    async def start_precreated(
        self,
        *,
        request: StartAdmissionRequest,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        dispatch_claim: RuntimeStartDispatchClaim,
    ) -> PrecreatedStartDispatch:
        if request.mode is not StartMode.PRECREATED:
            raise ValueError("precreated start requires a precreated request")

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            return await self.execution_ports.lifecycle.ensure_and_bind_precreated_start(
                transaction,
                request,
                execution_lease,
                run_fence,
                dispatch_claim,
                now=float(self._clock()),
                ttl_seconds=self.lease_ttl_seconds,
            )

        return await self.execution_ports.unit_of_work.run_atomic(
            operation, fault_label="workflow:start-precreated"
        )

    async def run_precreated(
        self,
        run_id: str,
        state: object | None = None,
        context: WorkflowContext | None = None,
        *,
        activation: WorkflowActivation,
    ) -> WorkflowRunResult:
        return await self._execute(
            run_id,
            state=state,
            responses=None,
            context=context or WorkflowContext(),
            precreated=None,
            precreated_activation=activation,
        )

    async def recover_precreated(
        self,
        run_id: str,
        *,
        recovery_work: WorkflowRecoveryWork,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        context: WorkflowContext | None = None,
    ) -> WorkflowRunResult:
        if recovery_work.run_id != run_id or recovery_work.mode is not StartMode.PRECREATED:
            raise ValueError("precreated recovery authority differs")
        if recovery_work.receipt_kind is WorkflowRecoveryReceiptKind.START:

            async def recover_start(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
                return await self.execution_ports.lifecycle.recover_precreated_start(
                    transaction,
                    recovery_work,
                    execution_lease,
                    run_fence,
                    now=float(self._clock()),
                    ttl_seconds=self.lease_ttl_seconds,
                )

            dispatch = await self.execution_ports.unit_of_work.run_atomic(
                recover_start, fault_label="workflow:recover-precreated-start"
            )
            if dispatch.activation is None:
                raise RuntimeError("recovered workflow start lacks activation")
            return await self._execute(
                run_id,
                state=None,
                responses=None,
                context=context or WorkflowContext(),
                precreated=None,
                precreated_activation=dispatch.activation,
            )
        receipt = recovery_work.receipt_snapshot
        if not isinstance(receipt, ResumeAdmissionReceipt):
            raise TypeError("workflow resume recovery receipt is invalid")

        async def recover_resume(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            return await self.execution_ports.lifecycle.claim_resume_precreated(
                transaction,
                receipt.request.receipt_id,
                receipt.version,
                execution_lease,
                run_fence,
                now=float(self._clock()),
                ttl_seconds=self.lease_ttl_seconds,
            )

        claimed = await self.execution_ports.unit_of_work.run_atomic(
            recover_resume, fault_label="workflow:recover-precreated-resume"
        )
        if claimed.activation is None:
            raise RuntimeError("recovered workflow resume lacks activation")
        responses = thaw_json(
            cast(FrozenJsonValue, claimed.request.responses)
        )
        if not isinstance(responses, dict) or not responses:
            raise RuntimeError("recovered workflow resume responses are invalid")
        binding = ResumeCommitBinding(
            claimed.request.receipt_id,
            claimed.version,
            claimed.request.expected_run_version,
            claimed.request_fingerprint,
        )
        return await self._execute(
            run_id,
            state=None,
            responses=cast(dict[str, JsonValue], responses),
            context=context or WorkflowContext(),
            precreated=None,
            precreated_activation=claimed.activation,
            recovered_resume_binding=binding,
        )

    async def resume(
        self,
        run_id: str,
        responses: Mapping[str, JsonValue],
        context: WorkflowContext | None = None,
    ) -> WorkflowRunResult:
        return await self._execute(
            run_id,
            state=None,
            responses=responses,
            context=context or WorkflowContext(),
            precreated=None,
        )

    async def resolve_and_resume(
        self,
        run_id: str,
        *,
        decision_id: str,
        nonce: str,
        expected_version: int,
        response: Mapping[str, JsonValue],
        context: WorkflowContext | None = None,
    ) -> WorkflowRunResult:
        """Atomically resolve one durable interrupt decision and admit resume.

        The decision CAS, Run transition, and durable resume admission share the
        execution UnitOfWork transaction.  Execution may happen after that
        commit; recovery can claim the admitted receipt if the process stops.
        """

        run = cast(RunRecord | None, self.execution_ports.unit_of_work.read_run(run_id))
        snapshot = self.execution_ports.unit_of_work.read_start_snapshot(run_id)
        decision = self.execution_ports.unit_of_work.read_decision(decision_id)
        if run is None or snapshot is None or decision is None:
            raise KeyError(f"workflow decision not found: {decision_id}")
        if run.driver_kind != "workflow" or decision.run_id != run_id:
            raise ValueError("workflow decision belongs to another Run")
        if decision.state.value != "open":
            raise ValueError("workflow decision is not open")
        if decision.version != expected_version:
            raise ValueError("workflow decision version changed")
        durable_request = thaw_json(cast(FrozenJsonValue, decision.request))
        if not isinstance(durable_request, Mapping) or str(
            durable_request.get("nonce") or decision_id
        ) != nonce:
            raise ValueError("workflow decision nonce changed")
        start_snapshot = StartSnapshot.from_json(snapshot)
        request = start_snapshot.workflow_admission
        if request is None:
            raise RuntimeError("workflow Run lacks its durable admission snapshot")
        native = await self.native_store.load_execution(
            run_id=run.run_id,
            thread_id=(
                request.resolved_thread_id
                or request.requested_thread_id
                or run.run_id
            ),
            checkpoint_ns=request.checkpoint_namespace,
        )
        interrupt = native.snapshot.interrupt
        if not isinstance(interrupt, Mapping):
            raise TypeError("workflow decision has no durable pending interrupt")
        interrupt_id = interrupt.get("interrupt_id")
        if interrupt_id != decision_id:
            raise ValueError("workflow decision differs from pending interrupt")
        request_hash = hashlib.sha256(
            canonical_json(cast(JsonValue, copy.deepcopy(dict(interrupt)))).encode()
        ).hexdigest()
        responses: dict[str, JsonValue] = {
            decision_id: copy.deepcopy(dict(response))
        }
        responses_hash = hashlib.sha256(
            canonical_json(cast(JsonValue, responses)).encode()
        ).hexdigest()
        resolved_run_version = run.version + 1
        receipt_id = hashlib.sha256(
            canonical_json(
                {
                    "run_id": run.run_id,
                    "run_version": resolved_run_version,
                    "checkpoint_head": native.snapshot.checkpoint_id,
                    "pending_interrupts": [[interrupt_id, request_hash]],
                    "responses_hash": responses_hash,
                }
            ).encode()
        ).hexdigest()
        resume_request = ResumeAdmissionRequest(
            receipt_id,
            run.run_id,
            resolved_run_version,
            native.snapshot.checkpoint_id,
            ((decision_id, request_hash),),
            responses,
            responses_hash,
            StartMode.STANDALONE,
        )

        async def resolve(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            return await self.execution_ports.lifecycle.resolve_decision_and_admit_resume(
                transaction,
                resume_request,
                decision_id=decision_id,
                nonce=nonce,
                expected_decision_version=expected_version,
                response=response,
                event_id=f"workflow-decision-resolved:{decision_id}:{expected_version}",
                now=float(self._clock()),
            )

        await self.execution_ports.unit_of_work.run_atomic(
            resolve, fault_label="workflow:resolve-and-resume"
        )
        return await self.resume(run_id, responses, context)

    async def resume_precreated(
        self,
        run_id: str,
        responses: Mapping[str, JsonValue],
        context: WorkflowContext | None = None,
        *,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
    ) -> WorkflowRunResult:
        return await self._execute(
            run_id,
            state=None,
            responses=responses,
            context=context or WorkflowContext(),
            precreated=(execution_lease, run_fence),
        )

    async def _activation(
        self, run: RunRecord, precreated: tuple[ExecutionLease, RunFenceLease] | None
    ) -> WorkflowActivation:
        now = float(self._clock())

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            if precreated is None:
                return await self.execution_ports.lifecycle.claim_activation(
                    transaction,
                    run.run_id,
                    run.version,
                    self.owner,
                    now=now,
                    ttl_seconds=self.lease_ttl_seconds,
                )
            return await self.execution_ports.lifecycle.bind_activation(
                transaction,
                run.run_id,
                run.version,
                precreated[0],
                precreated[1],
                now=now,
                ttl_seconds=self.lease_ttl_seconds,
            )

        return await self.execution_ports.unit_of_work.run_atomic(
            operation, fault_label="workflow:activate"
        )

    @staticmethod
    def _activation_payload(activation: WorkflowActivation) -> dict[str, JsonValue]:
        return {
            "run_id": activation.execution_lease.run_id,
            "owner_id": activation.execution_lease.owner_id,
            "runtime_namespace": activation.execution_lease.namespace,
            "runtime_epoch": activation.execution_lease.epoch,
            "expires_at": activation.execution_lease.expires_at,
            "run_fence_epoch": activation.run_fence.epoch,
            "workflow_namespace": activation.workflow_lease.namespace,
            "workflow_epoch": activation.workflow_lease.epoch,
        }

    async def _resume_activation(
        self,
        run: RunRecord,
        request: StartAdmissionRequest,
        responses: Mapping[str, JsonValue],
        precreated: tuple[ExecutionLease, RunFenceLease] | None,
    ) -> tuple[
        WorkflowActivation | None, ResumeCommitBinding | None, JsonValue | None
    ]:
        native = await self.native_store.load_execution(
            run_id=run.run_id,
            thread_id=(
                request.resolved_thread_id
                or request.requested_thread_id
                or run.run_id
            ),
            checkpoint_ns=request.checkpoint_namespace,
        )
        interrupt = native.snapshot.interrupt
        if not isinstance(interrupt, Mapping):
            raise TypeError("workflow resume requires a durable pending interrupt")
        interrupt_id = interrupt.get("interrupt_id")
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise RuntimeError("workflow pending interrupt identity is missing")
        request_hash = hashlib.sha256(
            canonical_json(cast(JsonValue, copy.deepcopy(dict(interrupt)))).encode()
        ).hexdigest()
        detached_responses = copy.deepcopy(dict(responses))
        responses_hash = hashlib.sha256(
            canonical_json(cast(JsonValue, detached_responses)).encode()
        ).hexdigest()
        receipt_id = hashlib.sha256(
            canonical_json(
                {
                    "run_id": run.run_id,
                    "run_version": run.version,
                    "checkpoint_head": native.snapshot.checkpoint_id,
                    "pending_interrupts": [[interrupt_id, request_hash]],
                    "responses_hash": responses_hash,
                }
            ).encode()
        ).hexdigest()
        resume_request = ResumeAdmissionRequest(
            receipt_id,
            run.run_id,
            run.version,
            native.snapshot.checkpoint_id,
            ((interrupt_id, request_hash),),
            detached_responses,
            responses_hash,
            StartMode.PRECREATED if precreated is not None else StartMode.STANDALONE,
        )

        async def admit(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            return await self.execution_ports.lifecycle.admit_resume(
                transaction, resume_request, now=float(self._clock())
            )

        admitted = await self.execution_ports.unit_of_work.run_atomic(
            admit, fault_label="workflow:resume-admit"
        )
        if admitted.phase is ResumePhase.SETTLED:
            return (
                None,
                None,
                cast(JsonValue | None, admitted.serialized_outcome),
            )

        async def claim(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            if precreated is None:
                return await self.execution_ports.lifecycle.claim_resume_standalone(
                    transaction,
                    admitted.request.receipt_id,
                    admitted.version,
                    self.owner,
                    now=float(self._clock()),
                    ttl_seconds=self.lease_ttl_seconds,
                )
            return await self.execution_ports.lifecycle.claim_resume_precreated(
                transaction,
                admitted.request.receipt_id,
                admitted.version,
                precreated[0],
                precreated[1],
                now=float(self._clock()),
                ttl_seconds=self.lease_ttl_seconds,
            )

        claimed = await self.execution_ports.unit_of_work.run_atomic(
            claim, fault_label="workflow:resume-claim"
        )
        if claimed.activation is None:
            raise RuntimeError("claimed workflow resume lacks durable activation")
        return (
            claimed.activation,
            ResumeCommitBinding(
                claimed.request.receipt_id,
                claimed.version,
                claimed.request.expected_run_version,
                claimed.request_fingerprint,
            ),
            None,
        )

    async def _execute(
        self,
        run_id: str,
        *,
        state: object | None,
        responses: Mapping[str, JsonValue] | None,
        context: WorkflowContext,
        precreated: tuple[ExecutionLease, RunFenceLease] | None,
        precreated_activation: WorkflowActivation | None = None,
        recovered_resume_binding: ResumeCommitBinding | None = None,
    ) -> WorkflowRunResult:
        run = cast(RunRecord | None, self.execution_ports.unit_of_work.read_run(run_id))
        snapshot = self.execution_ports.unit_of_work.read_start_snapshot(run_id)
        if run is None or snapshot is None:
            raise KeyError(f"workflow run not found: {run_id}")
        if run.driver_kind != "workflow":
            raise RuntimeError("Run is not owned by workflow driver")
        start_snapshot = StartSnapshot.from_json(snapshot)
        request = start_snapshot.workflow_admission
        if request is None:
            raise RuntimeError("workflow Run lacks its durable admission snapshot")
        context = self.host_services.bind_context(run.profile_key, context)
        workflow_name = request.workflow_name
        workflow_version = request.workflow_version
        entry = self.registry.require(
            workflow_name,
            workflow_version,
            expected_manifest_hash=request.manifest_hash,
            expected_implementation_hash=request.implementation_hash,
        )
        resume_binding: ResumeCommitBinding | None = None
        if recovered_resume_binding is not None:
            if responses is None or precreated_activation is None:
                raise RuntimeError("recovered workflow resume authority is incomplete")
            activation = precreated_activation
            resume_binding = recovered_resume_binding
        elif responses is None:
            activation = (
                precreated_activation
                if precreated_activation is not None
                else await self._activation(run, precreated)
            )
        else:
            resume_activation, resume_binding, settled = await self._resume_activation(
                run, request, responses, precreated
            )
            if settled is not None:
                return WorkflowRunResult(
                    run_id,
                    _STATUS[run.state],
                    cast(Mapping[str, JsonValue], settled).get("output")
                    if isinstance(settled, Mapping)
                    else None,
                )
            if resume_activation is None or resume_binding is None:
                raise RuntimeError("workflow resume authority is incomplete")
            activation = resume_activation
        executable = entry.materialize(
            store=self.native_store,
            terminal_projection_port=self.terminal_projection_port,
            terminal_commit_projection_port=self.terminal_commit_projection_port,
            progress_port=self.progress_port,
            observer_port=self.observer_port,
        )
        logical_timestamp = float(self._clock())
        if responses is not None:
            resumed_execution = await self.native_store.load_execution(
                run_id=run.run_id,
                thread_id=(
                    request.resolved_thread_id
                    or request.requested_thread_id
                    or run.run_id
                ),
                checkpoint_ns=request.checkpoint_namespace,
            )
            pinned_logical_timestamp = resumed_execution.snapshot.metadata.get(
                "logical_timestamp"
            )
            if (
                isinstance(pinned_logical_timestamp, bool)
                or not isinstance(pinned_logical_timestamp, (int, float))
            ):
                raise RuntimeError(
                    "workflow resume lacks a durable logical timestamp"
                )
            logical_timestamp = float(pinned_logical_timestamp)
        configurable: dict[str, JsonValue] = {
            "workflow_owner_id": activation.workflow_lease.owner_id,
            "workflow_lease_epoch": activation.workflow_lease.epoch,
            "runtime_lease_epoch": activation.execution_lease.epoch,
            "run_fence_epoch": activation.run_fence.epoch,
            "logical_timestamp": logical_timestamp,
            "workflow_activation": self._activation_payload(activation),
        }
        if resume_binding is not None:
            configurable["resume_binding"] = {
                "receipt_id": resume_binding.receipt_id,
                "expected_receipt_version": resume_binding.expected_receipt_version,
                "target_run_revision": resume_binding.target_run_revision,
                "request_fingerprint": resume_binding.request_fingerprint,
            }
        stop = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal activation
            while not stop.is_set():
                await self._sleep(self.heartbeat_interval_seconds)
                if stop.is_set():
                    return
                current_activation = activation

                async def renew(
                    tx: WorkflowTransaction,
                    *,
                    current: WorkflowActivation = current_activation,
                ):
                    return await self.execution_ports.lifecycle.renew_activation(
                        tx,
                        current,
                        now=float(self._clock()),
                        ttl_seconds=self.lease_ttl_seconds,
                    )

                try:
                    activation = await self.execution_ports.unit_of_work.run_atomic(
                        renew, fault_label="workflow:heartbeat"
                    )
                except BaseException as lease_error:
                    current_run = cast(
                        RunRecord | None,
                        self.execution_ports.unit_of_work.read_run(run_id),
                    )
                    if current_run is not None:

                        async def isolate(
                            tx: WorkflowTransaction,
                            *,
                            current: WorkflowActivation = current_activation,
                            version: int = current_run.version,
                        ) -> None:
                            await self.execution_ports.lifecycle.release_activation(
                                tx,
                                current,
                                version,
                                {"status": "lease_lost"},
                                now=float(self._clock()),
                            )

                        try:
                            await self.execution_ports.unit_of_work.run_atomic(
                                isolate, fault_label="workflow:heartbeat-isolate"
                            )
                        except BaseException as isolation_error:  # noqa: BLE001
                            lease_error.add_note(
                                "workflow activation isolation also failed: "
                                f"{type(isolation_error).__name__}"
                            )
                    raise

        async def invoke_native() -> object:
            thread_id = (
                request.resolved_thread_id
                or request.requested_thread_id
                or run_id
            )
            namespace = request.checkpoint_namespace
            initial = (
                state
                if state is not None
                else thaw_json(cast(FrozenJsonValue, request.start_input))
            )
            if responses is None:
                return await executable.ainvoke(
                    initial,
                    context,
                    thread_id=thread_id,
                    run_id=run_id,
                    checkpoint_ns=namespace,
                    configurable=configurable,
                )
            if not responses:
                raise ValueError("workflow resume requires at least one response")
            return await executable.resume(
                copy.deepcopy(dict(responses)),
                context,
                thread_id=thread_id,
                run_id=run_id,
                checkpoint_ns=namespace,
                configurable=configurable,
            )

        heartbeat_task = (
            None
            if precreated is not None or precreated_activation is not None
            else asyncio.create_task(heartbeat())
        )
        native_task = asyncio.create_task(invoke_native())
        try:
            if heartbeat_task is None:
                output = await native_task
            else:
                done, _pending = await asyncio.wait(
                    (native_task, heartbeat_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat_task in done:
                    native_task.cancel()
                    await asyncio.gather(native_task, return_exceptions=True)
                    exception = heartbeat_task.exception()
                    if exception is None:
                        raise RuntimeError("workflow heartbeat stopped unexpectedly")
                    raise exception
                output = native_task.result()
        finally:
            stop.set()
            if not native_task.done():
                native_task.cancel()
            pending = [native_task]
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                pending.append(heartbeat_task)
            await asyncio.gather(*pending, return_exceptions=True)
        current = cast(
            RunRecord | None, self.execution_ports.unit_of_work.read_run(run_id)
        )
        assert current is not None
        return WorkflowRunResult(run_id, _STATUS[current.state], output)

    async def request_cancel(
        self, run_id: str, reason: str = "user"
    ) -> WorkflowRunResult:
        return await self._request_cancel(run_id, reason=reason, precreated=None)

    async def request_cancel_precreated(
        self,
        run_id: str,
        *,
        reason: str,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
    ) -> WorkflowRunResult:
        return await self._request_cancel(
            run_id,
            reason=reason,
            precreated=(execution_lease, run_fence),
        )

    async def _request_cancel(
        self,
        run_id: str,
        *,
        reason: str,
        precreated: tuple[ExecutionLease, RunFenceLease] | None,
    ) -> WorkflowRunResult:
        run = cast(RunRecord | None, self.execution_ports.unit_of_work.read_run(run_id))
        if run is None:
            raise KeyError(run_id)
        if run.state is RunState.CANCELLED:
            durable = self.execution_ports.lifecycle.read_cancel_outcome(
                run_id=run_id, generation=0
            )
            if durable is None or not durable.terminal:
                raise RuntimeError(
                    "cancelled workflow Run lacks a terminal cancel receipt"
                )
            return WorkflowRunResult(
                run_id,
                WorkflowRunStatus.CANCELLED,
                {
                    "cancel_id": durable.cancel_id,
                    "generation": durable.generation,
                    "blocker_ids": list(durable.blocker_ids),
                },
            )
        request = None
        if run.state is RunState.CANCEL_REQUESTED:
            request = self.execution_ports.lifecycle.read_cancel_request(
                run_id=run_id, generation=0
            )
            if request is None:
                raise RuntimeError(
                    "cancel-requested workflow Run lacks its durable request"
                )
        if request is None:
            cancel_id = hashlib.sha256(
                canonical_json(
                    {
                        "protocol": "simple-harness-workflow-cancel-v1",
                        "run_id": run_id,
                        "reason": reason,
                        "generation": 0,
                    }
                ).encode()
            ).hexdigest()
            request = CancelWorkflowRequest(
                cancel_id,
                run_id,
                reason,
                0,
            )

        async def request_operation(tx: WorkflowTransaction):
            activation = None
            if precreated is not None:
                activation = await self.execution_ports.lifecycle.bind_activation(
                    tx,
                    run_id,
                    run.version,
                    precreated[0],
                    precreated[1],
                    now=float(self._clock()),
                    ttl_seconds=self.lease_ttl_seconds,
                )
            return await self.execution_ports.lifecycle.request_cancel(
                tx,
                request,
                run.version,
                activation,
                now=float(self._clock()),
            )

        outcome = await self.execution_ports.unit_of_work.run_atomic(
            request_operation, fault_label="workflow:cancel-request"
        )
        if outcome.terminal:
            return WorkflowRunResult(run_id, WorkflowRunStatus.CANCELLED)
        resolution_snapshot = (
            self.execution_ports.lifecycle.read_cancel_resolution_snapshot(
                outcome.cancel_id
            )
        )
        if outcome.blocker_ids and resolution_snapshot is None:
            return WorkflowRunResult(
                run_id,
                WorkflowRunStatus.CANCELLING,
                {
                    "cancel_id": outcome.cancel_id,
                    "generation": outcome.generation,
                    "blocker_ids": list(outcome.blocker_ids),
                },
            )

        checkpoint_id = hashlib.sha256(
            f"{outcome.cancel_id}|checkpoint".encode()
        ).hexdigest()
        event_id = hashlib.sha256(f"{outcome.cancel_id}|event".encode()).hexdigest()
        delivery_id = hashlib.sha256(
            f"{outcome.cancel_id}|delivery".encode()
        ).hexdigest()

        async def converge(tx: WorkflowTransaction):
            convergence = await self.execution_ports.lifecycle.claim_cancel_convergence(
                tx,
                outcome.cancel_id,
                outcome.generation,
                self.owner,
                now=float(self._clock()),
                ttl_seconds=self.lease_ttl_seconds,
            )
            return await self.execution_ports.lifecycle.settle_cancel_convergence(
                tx,
                convergence,
                resolution_snapshot or {},
                {
                    "checkpoint_id": checkpoint_id,
                    "namespace": "native",
                    "status": "cancelled",
                    "cancel_id": outcome.cancel_id,
                    "generation": outcome.generation,
                },
                {
                    "event_id": event_id,
                    "cancel_id": outcome.cancel_id,
                    "generation": outcome.generation,
                },
                (
                    {
                        "delivery_id": delivery_id,
                        "sink_kind": "workflow.cancel",
                        "idempotency_key": (
                            f"workflow-cancel:{run_id}:{outcome.generation}"
                        ),
                        "payload": {
                            "run_id": run_id,
                            "status": "cancelled",
                            "cancel_id": outcome.cancel_id,
                            "generation": outcome.generation,
                        },
                    },
                ),
                now=float(self._clock()),
            )

        settled = await self.execution_ports.unit_of_work.run_atomic(
            converge, fault_label="workflow:cancel-converge"
        )
        return WorkflowRunResult(
            run_id,
            WorkflowRunStatus.CANCELLED,
            {
                "cancel_id": settled.cancel_id,
                "generation": settled.generation,
                "event_id": event_id,
                "delivery_id": delivery_id,
            },
        )

    cancel = request_cancel

    async def recover_expired(self) -> list[RecoveryRecord]:
        """Enumerate expired workflow leases and recover via Port pipeline."""

        async def expire_transaction(tx: WorkflowTransaction):
            return await self.recovery.recover_expired(now=float(self._clock()), transaction=tx)

        records = await self.execution_ports.unit_of_work.run_atomic(
            expire_transaction, fault_label="workflow:recover-expired"
        )
        return list(records)

    async def recover(
        self, run_id: str, context: WorkflowContext | None = None, **_: object
    ) -> WorkflowRunResult:
        """Recover workflow via classify/repair/quarantine pipeline."""
        from .recovery import quarantine_checkpoint, repair_head_projection

        # Read recovery snapshot to get candidate and checkpoint state
        if not hasattr(self.execution_ports, "recovery"):
            # Fallback for stores without recovery port
            return await self._execute(
                run_id,
                state=None,
                responses=None,
                context=context or WorkflowContext(),
                precreated=None,
            )

        recovery_store = self.execution_ports.recovery
        snapshot = recovery_store.read_recovery_snapshot(run_id)

        # If no checkpoint head, quarantine
        if snapshot.candidate.checkpoint_head is None:
            async def quarantine_tx(tx: WorkflowTransaction):
                return await quarantine_checkpoint(
                    self.recovery,
                    run_id=run_id,
                    reason="no_checkpoint_head",
                    checkpoint=None,
                    transaction=tx,
                )

            record = await self.execution_ports.unit_of_work.run_atomic(
                quarantine_tx, fault_label="workflow:recover-quarantine"
            )
            return WorkflowRunResult(
                run_id,
                WorkflowRunStatus.BLOCKED,
                error={"action": record.action, "reason": record.reason},
            )

        # Try to read and repair checkpoint head
        try:
            checkpoint = await self.checkpoint.read(
                run_id, snapshot.candidate.checkpoint_head
            )

            async def repair_tx(tx: WorkflowTransaction):
                return await repair_head_projection(
                    self.recovery, checkpoint, transaction=tx
                )

            record = await self.execution_ports.unit_of_work.run_atomic(
                repair_tx, fault_label="workflow:recover-repair"
            )

            if record is not None:
                # Repair was needed and applied
                return WorkflowRunResult(
                    run_id,
                    WorkflowRunStatus(_STATUS.get(RunState(record.status), WorkflowRunStatus.BLOCKED)),
                    {"action": record.action, "reason": record.reason},
                )
        except Exception as exc:
            # Checkpoint read or repair failed, quarantine
            async def quarantine_tx(tx: WorkflowTransaction):
                return await quarantine_checkpoint(
                    self.recovery,
                    run_id=run_id,
                    reason=f"checkpoint_repair_failed:{exc.__class__.__name__}",
                    checkpoint=None,
                    transaction=tx,
                )

            record = await self.execution_ports.unit_of_work.run_atomic(
                quarantine_tx, fault_label="workflow:recover-quarantine"
            )
            return WorkflowRunResult(
                run_id,
                WorkflowRunStatus.BLOCKED,
                error={"action": record.action, "reason": record.reason},
            )

        # After repair (or if no repair needed), proceed with execution
        return await self._execute(
            run_id,
            state=None,
            responses=None,
            context=context or WorkflowContext(),
            precreated=None,
        )

    async def history(self, run_id: str, *, limit: int | None = None):
        return await self._replay().history(run_id, limit=limit)

    def _replay(self):  # type: ignore[no-untyped-def]
        from .replay import WorkflowReplay

        return WorkflowReplay(
            execution_ports=self.execution_ports,
            native_store=self.native_store,
            registry=self.registry,
            owner_id=self.owner,
            clock=self._clock,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )

    async def fork_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        expected_version: int,
        state_patch: Mapping[str, JsonValue] | None = None,
        fork_key: str | None = None,
        dangerous_confirmation: DangerousEffectConfirmation | None = None,
    ) -> dict[str, JsonValue]:
        return await self._replay().fork_checkpoint(
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            expected_version=expected_version,
            state_patch=state_patch,
            fork_key=fork_key,
            dangerous_confirmation=dangerous_confirmation,
        )

    fork = fork_checkpoint


__all__ = (
    "RegisteredWorkflow",
    "WorkflowRegistry",
    "WorkflowRunResult",
    "WorkflowRunner",
    "manifest_hash",
)
