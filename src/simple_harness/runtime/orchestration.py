# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable Workflow catalog, launch-ticket, and Runtime admission contracts."""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
    validate_json_value,
)
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ExecutionLease,
    FaultHook,
)
from simple_harness.tools.schema import (
    validate_argument_resource_bounds,
    validate_tool_schema,
)
from simple_harness.workflow.lease import WorkflowLease

from .profiles import ProfileDescriptor

if TYPE_CHECKING:
    from simple_harness.execution.effects import EffectRecord
    from simple_harness.execution.recovery import WaitBlockerRecord
    from simple_harness.tools.contracts import ToolResult
    from simple_harness.workflow.execution_ports import (
        StartAdmissionRequest,
        WorkflowRecoveryWork,
        WorkflowRetryWake,
        WorkflowTerminalOutcome,
        WorkflowTransaction,
    )

    from .start_snapshot import RunStart, StartSnapshot
    from .workflow_spawn import WorkflowSpawnAdmissionOutcome, WorkflowSpawnToolOutcome


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _frozen_object(
    value: Mapping[str, JsonValue], *, path: str
) -> Mapping[str, FrozenJsonValue]:
    detached = copy.deepcopy(dict(value))
    validate_json_value(detached, path=path)
    frozen = freeze_json(detached)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    return MappingProxyType(dict(frozen))


@dataclass(frozen=True, slots=True)
class StartInputSchema:
    schema_ref: str
    canonical_schema: Mapping[str, JsonValue]
    schema_hash: str

    def __post_init__(self) -> None:
        _required(self.schema_ref, "schema_ref")
        if len(self.schema_ref.encode("utf-8")) > 4096:
            raise ValueError("schema_ref exceeds 4096 UTF-8 bytes")
        # Validate the caller-owned raw graph before any recursive copy/freeze.
        # The validator's iterative preflight is the resource/identity boundary
        # for cyclic or adversarially deep mappings.
        validate_tool_schema(self.canonical_schema)
        detached = copy.deepcopy(dict(self.canonical_schema))
        expected = hashlib.sha256(canonical_json(detached).encode()).hexdigest()
        if self.schema_hash != expected:
            raise ValueError("start input schema hash does not match schema")
        object.__setattr__(
            self,
            "canonical_schema",
            _frozen_object(detached, path="$.start_input_schema"),
        )

    def to_json(self) -> dict[str, JsonValue]:
        schema = thaw_json(cast(FrozenJsonValue, self.canonical_schema))
        if not isinstance(schema, dict):
            raise TypeError("start input schema must remain a JSON object")
        return {
            "schema_ref": self.schema_ref,
            "canonical_schema": schema,
            "schema_hash": self.schema_hash,
        }


@dataclass(frozen=True, slots=True)
class WorkflowProfileRegistration:
    descriptor: ProfileDescriptor
    workflow_name: str
    workflow_version: str
    start_input_schema: StartInputSchema

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ProfileDescriptor):
            raise TypeError("descriptor must be a ProfileDescriptor")
        for name in ("workflow_name", "workflow_version"):
            _required(getattr(self, name), name)
        if not isinstance(self.start_input_schema, StartInputSchema):
            raise TypeError("start_input_schema must be a StartInputSchema")
        if self.start_input_schema.schema_ref != self.descriptor.input_schema_ref:
            raise ValueError("start input schema ref differs from Profile descriptor")


@dataclass(frozen=True, slots=True)
class WorkflowCatalogProfileBinding:
    profile_key: str
    description: str
    use_when: str
    avoid_when: str
    input_schema_ref: str
    profile_fingerprint: str
    workflow_name: str
    workflow_version: str
    implementation_fingerprint: str
    checkpoint_namespace: str
    manifest_hash: str
    state_schema_version: int
    start_input_schema: StartInputSchema
    terminal_projection_descriptor: Mapping[str, JsonValue] | None
    terminal_request_factory_hash: str | None
    capability_snapshot: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        for name in (
            "profile_key",
            "description",
            "use_when",
            "avoid_when",
            "input_schema_ref",
            "profile_fingerprint",
            "workflow_name",
            "workflow_version",
            "implementation_fingerprint",
            "checkpoint_namespace",
            "manifest_hash",
        ):
            _required(getattr(self, name), name)
        if isinstance(self.state_schema_version, bool) or self.state_schema_version < 1:
            raise ValueError("state_schema_version must be positive")
        if not isinstance(self.start_input_schema, StartInputSchema):
            raise TypeError("start_input_schema must be a StartInputSchema")
        if self.start_input_schema.schema_ref != self.input_schema_ref:
            raise ValueError("start input schema ref differs from catalog binding")
        if self.terminal_request_factory_hash is not None:
            _required(
                self.terminal_request_factory_hash,
                "terminal_request_factory_hash",
            )
        if self.terminal_projection_descriptor is None:
            if self.terminal_request_factory_hash is not None:
                raise ValueError(
                    "terminal request factory requires a projection descriptor"
                )
        elif (
            self.terminal_request_factory_hash
            != self.terminal_projection_descriptor.get("request_factory_hash")
        ):
            raise ValueError(
                "terminal request factory hash differs from projection descriptor"
            )
        if self.terminal_projection_descriptor is not None:
            object.__setattr__(
                self,
                "terminal_projection_descriptor",
                _frozen_object(
                    self.terminal_projection_descriptor,
                    path="$.catalog.terminal_projection_descriptor",
                ),
            )
        object.__setattr__(
            self,
            "capability_snapshot",
            _frozen_object(
                self.capability_snapshot, path="$.catalog.capability_snapshot"
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowCatalogAuthority:
    authority_id: str
    generation: int
    version: int
    catalog_hash: str
    profiles: tuple[WorkflowCatalogProfileBinding, ...]

    def __post_init__(self) -> None:
        if self.authority_id != "model_spawnable":
            raise ValueError("workflow catalog authority_id must be model_spawnable")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("catalog generation must be positive")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("catalog version must be non-negative")
        _required(self.catalog_hash, "catalog_hash")
        ordered = tuple(sorted(self.profiles, key=lambda item: item.profile_key))
        if ordered != self.profiles or len(
            {item.profile_key for item in ordered}
        ) != len(ordered):
            raise ValueError("catalog profiles must be uniquely ordered by key")
        expected = workflow_catalog_hash(self.authority_id, self.generation, ordered)
        if self.catalog_hash != expected:
            raise ValueError("catalog hash does not match canonical profiles")

    def require(self, profile_key: str) -> WorkflowCatalogProfileBinding:
        for item in self.profiles:
            if item.profile_key == profile_key:
                return item
        raise KeyError(profile_key)


def workflow_catalog_hash(
    authority_id: str,
    generation: int,
    profiles: tuple[WorkflowCatalogProfileBinding, ...],
) -> str:
    payload = {
        "authority_id": authority_id,
        "generation": generation,
        "profiles": [
            {
                "profile_key": item.profile_key,
                "description": item.description,
                "use_when": item.use_when,
                "avoid_when": item.avoid_when,
                "input_schema_ref": item.input_schema_ref,
                "profile_fingerprint": item.profile_fingerprint,
                "workflow_name": item.workflow_name,
                "workflow_version": item.workflow_version,
                "implementation_fingerprint": item.implementation_fingerprint,
                "checkpoint_namespace": item.checkpoint_namespace,
                "manifest_hash": item.manifest_hash,
                "state_schema_version": item.state_schema_version,
                "start_input_schema": item.start_input_schema.to_json(),
                "terminal_projection_descriptor": (
                    None
                    if item.terminal_projection_descriptor is None
                    else thaw_json(
                        cast(FrozenJsonValue, item.terminal_projection_descriptor)
                    )
                ),
                "terminal_request_factory_hash": item.terminal_request_factory_hash,
                "capability_snapshot": thaw_json(
                    cast(FrozenJsonValue, item.capability_snapshot)
                ),
            }
            for item in profiles
        ],
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


_VERIFIED_CATALOG_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedWorkflowCatalogAuthority:
    authority: WorkflowCatalogAuthority
    registry_snapshot_id: str
    registry_snapshot_hash: str
    _factory_token: object

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        authority: WorkflowCatalogAuthority,
        registry_snapshot_id: str,
        registry_snapshot_hash: str,
    ) -> VerifiedWorkflowCatalogAuthority:
        if factory_token is not _VERIFIED_CATALOG_FACTORY:
            raise TypeError("verified workflow catalogs are SDK factory-only")
        self = object.__new__(cls)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "registry_snapshot_id", registry_snapshot_id)
        object.__setattr__(self, "registry_snapshot_hash", registry_snapshot_hash)
        object.__setattr__(self, "_factory_token", factory_token)
        return self

    def __copy__(self) -> None:
        raise TypeError("verified workflow catalogs cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("verified workflow catalogs cannot be copied")

    def _is_sdk_verified(self) -> bool:
        return self._factory_token is _VERIFIED_CATALOG_FACTORY


def _create_verified_workflow_catalog_authority(
    authority: WorkflowCatalogAuthority,
    registry_snapshot_hash: str,
) -> VerifiedWorkflowCatalogAuthority:
    snapshot_id = hashlib.sha256(
        f"simple-harness.workflow.registry-snapshot.v1|{registry_snapshot_hash}".encode()
    ).hexdigest()
    return VerifiedWorkflowCatalogAuthority._create(
        factory_token=_VERIFIED_CATALOG_FACTORY,
        authority=authority,
        registry_snapshot_id=snapshot_id,
        registry_snapshot_hash=registry_snapshot_hash,
    )


# Pre-v0.1 internal alias retained only while the implementation plan is in flight.
WorkflowCatalogProfile = WorkflowCatalogProfileBinding


@dataclass(frozen=True, slots=True)
class WorkflowSpawnSelection:
    """The only workflow-spawn fields visible to the Agent."""

    profile_key: str
    objective: str
    start_input: Mapping[str, JsonValue]
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.profile_key, "profile_key")
        objective = _required(self.objective, "objective")
        if len(objective.encode("utf-8")) > 32768:
            raise ValueError("workflow spawn objective exceeds 32768 UTF-8 bytes")
        if self.candidate_id is not None:
            _required(self.candidate_id, "candidate_id")
        validate_argument_resource_bounds(self.start_input)
        object.__setattr__(
            self,
            "start_input",
            _frozen_object(self.start_input, path="$.workflow_spawn.start_input"),
        )


@dataclass(frozen=True, slots=True)
class WorkflowCatalogSelectionProfile:
    profile_key: str
    description: str
    use_when: str
    avoid_when: str
    profile_fingerprint: str
    start_input_schema: StartInputSchema

    def __post_init__(self) -> None:
        for name in (
            "profile_key",
            "description",
            "use_when",
            "avoid_when",
            "profile_fingerprint",
        ):
            _required(getattr(self, name), name)
        if not isinstance(self.start_input_schema, StartInputSchema):
            raise TypeError("start_input_schema must be typed")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "profile_key": self.profile_key,
            "description": self.description,
            "use_when": self.use_when,
            "avoid_when": self.avoid_when,
            "profile_fingerprint": self.profile_fingerprint,
            "start_input_schema": self.start_input_schema.to_json(),
        }


def workflow_catalog_selection_hash(
    authority_id: str,
    generation: int,
    version: int,
    catalog_hash: str,
    profiles: tuple[WorkflowCatalogSelectionProfile, ...],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "authority_id": authority_id,
                "generation": generation,
                "version": version,
                "catalog_hash": catalog_hash,
                "profiles": [item.to_json() for item in profiles],
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowCatalogSelectionSnapshot:
    authority_id: str
    generation: int
    version: int
    catalog_hash: str
    profiles: tuple[WorkflowCatalogSelectionProfile, ...]
    canonical_hash: str

    def __post_init__(self) -> None:
        if self.authority_id != "model_spawnable":
            raise ValueError("workflow spawn catalog authority is invalid")
        if self.generation < 1 or self.version < 0:
            raise ValueError("workflow spawn catalog generation/version is invalid")
        _required(self.catalog_hash, "catalog_hash")
        profiles = tuple(self.profiles)
        if (
            len(profiles) > 32
            or profiles != tuple(sorted(profiles, key=lambda item: item.profile_key))
            or len({item.profile_key for item in profiles}) != len(profiles)
        ):
            raise ValueError("workflow spawn catalog profiles are invalid")
        expected = workflow_catalog_selection_hash(
            self.authority_id,
            self.generation,
            self.version,
            self.catalog_hash,
            profiles,
        )
        if self.canonical_hash != expected:
            raise ValueError("workflow spawn catalog snapshot hash differs")


def workflow_catalog_selection_to_json(
    snapshot: WorkflowCatalogSelectionSnapshot,
) -> dict[str, JsonValue]:
    if not isinstance(snapshot, WorkflowCatalogSelectionSnapshot):
        raise TypeError("snapshot must be a WorkflowCatalogSelectionSnapshot")
    return {
        "authority_id": snapshot.authority_id,
        "generation": snapshot.generation,
        "version": snapshot.version,
        "catalog_hash": snapshot.catalog_hash,
        "profiles": [item.to_json() for item in snapshot.profiles],
        "canonical_hash": snapshot.canonical_hash,
    }


def workflow_catalog_selection_from_json(
    value: Mapping[str, object],
) -> WorkflowCatalogSelectionSnapshot:
    if not isinstance(value, Mapping):
        raise TypeError("workflow catalog selection snapshot must be an object")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        raise TypeError("workflow catalog selection profiles must be an array")
    profiles: list[WorkflowCatalogSelectionProfile] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, Mapping):
            raise TypeError("workflow catalog selection profile must be an object")
        raw_schema = raw_profile.get("start_input_schema")
        if not isinstance(raw_schema, Mapping):
            raise TypeError("workflow catalog selection schema must be an object")
        canonical_schema = raw_schema.get("canonical_schema")
        if not isinstance(canonical_schema, Mapping):
            raise TypeError("workflow catalog selection schema body must be an object")
        schema = StartInputSchema(
            schema_ref=str(raw_schema.get("schema_ref", "")),
            canonical_schema=cast(Mapping[str, JsonValue], canonical_schema),
            schema_hash=str(raw_schema.get("schema_hash", "")),
        )
        profiles.append(
            WorkflowCatalogSelectionProfile(
                profile_key=str(raw_profile.get("profile_key", "")),
                description=str(raw_profile.get("description", "")),
                use_when=str(raw_profile.get("use_when", "")),
                avoid_when=str(raw_profile.get("avoid_when", "")),
                profile_fingerprint=str(
                    raw_profile.get("profile_fingerprint", "")
                ),
                start_input_schema=schema,
            )
        )
    authority_id = value.get("authority_id")
    generation = value.get("generation")
    version = value.get("version")
    catalog_hash = value.get("catalog_hash")
    canonical_hash = value.get("canonical_hash")
    if not isinstance(authority_id, str) or not isinstance(catalog_hash, str):
        raise TypeError("workflow catalog selection identity is malformed")
    if not isinstance(canonical_hash, str):
        raise TypeError("workflow catalog selection hash is malformed")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        raise TypeError("workflow catalog selection revision is malformed")
    return WorkflowCatalogSelectionSnapshot(
        authority_id,
        generation,
        version,
        catalog_hash,
        tuple(profiles),
        canonical_hash,
    )


def workflow_catalog_selection_from_authority(
    authority: WorkflowCatalogAuthority,
) -> WorkflowCatalogSelectionSnapshot:
    if not isinstance(authority, WorkflowCatalogAuthority):
        raise TypeError("authority must be a WorkflowCatalogAuthority")
    profiles = tuple(
        WorkflowCatalogSelectionProfile(
            profile_key=item.profile_key,
            description=item.description,
            use_when=item.use_when,
            avoid_when=item.avoid_when,
            profile_fingerprint=item.profile_fingerprint,
            start_input_schema=item.start_input_schema,
        )
        for item in authority.profiles
    )
    return WorkflowCatalogSelectionSnapshot(
        authority_id=authority.authority_id,
        generation=authority.generation,
        version=authority.version,
        catalog_hash=authority.catalog_hash,
        profiles=profiles,
        canonical_hash=workflow_catalog_selection_hash(
            authority.authority_id,
            authority.generation,
            authority.version,
            authority.catalog_hash,
            profiles,
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkflowSpawnOrigin:
    parent_run_id: str
    parent_request_id: str
    turn_id: str
    internal_tool_call_id: str

    def __post_init__(self) -> None:
        for name in (
            "parent_run_id",
            "parent_request_id",
            "turn_id",
            "internal_tool_call_id",
        ):
            _required(getattr(self, name), name)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "parent_run_id": self.parent_run_id,
            "parent_request_id": self.parent_request_id,
            "turn_id": self.turn_id,
            "internal_tool_call_id": self.internal_tool_call_id,
        }


@dataclass(frozen=True, slots=True)
class WorkflowSpawnIssueAuthority:
    react_checkpoint_revision: int
    execution_lease: ExecutionLease
    run_fence: RunFenceLease
    workflow_lease: WorkflowLease | None
    effect_id: str
    effect_handoff_attempt: int
    effect_request_hash: str

    def __post_init__(self) -> None:
        if self.react_checkpoint_revision < 0:
            raise ValueError("react_checkpoint_revision must be non-negative")
        _required(self.effect_id, "effect_id")
        _required(self.effect_request_hash, "effect_request_hash")
        if self.effect_handoff_attempt < 1:
            raise ValueError("effect_handoff_attempt must be positive")
        if (
            self.execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
            or self.run_fence.run_id.value != self.execution_lease.run_id
            or self.run_fence.owner_id != self.execution_lease.owner_id
            or self.run_fence.runtime_lease_epoch != self.execution_lease.epoch
        ):
            raise ValueError("workflow spawn issue authority is not runtime co-fenced")
        if self.workflow_lease is not None and (
            self.workflow_lease.run_id != self.execution_lease.run_id
            or self.workflow_lease.owner_id != self.execution_lease.owner_id
            or self.workflow_lease.runtime_lease_epoch != self.execution_lease.epoch
        ):
            raise ValueError("workflow spawn issue Workflow lease is not co-fenced")


def _domain_hash(domain: str, value: JsonValue) -> str:
    return hashlib.sha256(f"{domain}|{canonical_json(value)}".encode()).hexdigest()


def workflow_spawn_operation_id(origin: WorkflowSpawnOrigin) -> str:
    if not isinstance(origin, WorkflowSpawnOrigin):
        raise TypeError("origin must be a WorkflowSpawnOrigin")
    return _domain_hash("workflow-spawn/operation/v1", origin.to_json())


def workflow_spawn_child_command_id(spawn_operation_id: str) -> str:
    _required(spawn_operation_id, "spawn_operation_id")
    return _domain_hash(
        "workflow-spawn/child-command/v1", {"operation_id": spawn_operation_id}
    )


def workflow_spawn_child_request_id(spawn_operation_id: str) -> str:
    _required(spawn_operation_id, "spawn_operation_id")
    return _domain_hash(
        "workflow-spawn/request/v1", {"operation_id": spawn_operation_id}
    )


def workflow_spawn_child_run_id(spawn_operation_id: str) -> str:
    _required(spawn_operation_id, "spawn_operation_id")
    return _domain_hash(
        "workflow-spawn/run/v1", {"operation_id": spawn_operation_id}
    )


class WorkflowSpawnReadyActivationState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class WorkflowSpawnContinuationClaim:
    spawn_operation_id: str
    ticket_receipt_id: str
    parent_run_id: str
    owner_id: str
    runtime_lease_epoch: int
    run_fence_epoch: int
    workflow_lease_epoch: int | None
    claim_epoch: int
    expires_at: float
    version: int

    def __post_init__(self) -> None:
        for name in (
            "spawn_operation_id",
            "ticket_receipt_id",
            "parent_run_id",
            "owner_id",
        ):
            _required(getattr(self, name), name)
        for name in (
            "runtime_lease_epoch",
            "run_fence_epoch",
            "claim_epoch",
            "version",
        ):
            value = getattr(self, name)
            minimum = 0 if name == "version" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} is invalid")
        if self.workflow_lease_epoch is not None and (
            isinstance(self.workflow_lease_epoch, bool)
            or self.workflow_lease_epoch < 1
        ):
            raise ValueError("workflow_lease_epoch must be positive or null")
        if not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise ValueError("expires_at must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class WorkflowSpawnContinuationReady:
    ready_receipt_id: str
    spawn_operation_id: str
    ticket_receipt_id: str
    effect_id: str
    handoff_attempt: int
    evidence_ref: str
    version: int
    created_at: float

    def __post_init__(self) -> None:
        for name in (
            "ready_receipt_id",
            "spawn_operation_id",
            "ticket_receipt_id",
            "effect_id",
            "evidence_ref",
        ):
            _required(getattr(self, name), name)
        if self.handoff_attempt < 1 or self.version < 0:
            raise ValueError("ready receipt attempt/version is invalid")
        if not math.isfinite(self.created_at) or self.created_at < 0:
            raise ValueError("created_at must be finite and non-negative")


_WORKFLOW_SPAWN_ACTIVATION_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnReadyActivation:
    ready_receipt: WorkflowSpawnContinuationReady
    continuation_claim: WorkflowSpawnContinuationClaim
    execution_lease: ExecutionLease
    run_fence: RunFenceLease
    workflow_lease: WorkflowLease | None
    blocker_id: str
    activation_receipt_id: str
    activation_version: int
    predecessor_activation_receipt_id: str | None
    state: WorkflowSpawnReadyActivationState
    _factory_token: object

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        ready_receipt: WorkflowSpawnContinuationReady,
        continuation_claim: WorkflowSpawnContinuationClaim,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        workflow_lease: WorkflowLease | None,
        blocker_id: str,
        activation_receipt_id: str,
        activation_version: int,
        predecessor_activation_receipt_id: str | None,
        state: WorkflowSpawnReadyActivationState,
    ) -> WorkflowSpawnReadyActivation:
        if factory_token is not _WORKFLOW_SPAWN_ACTIVATION_FACTORY:
            raise TypeError("workflow spawn activations are SDK factory-only")
        for value, expected, name in (
            (ready_receipt, WorkflowSpawnContinuationReady, "ready_receipt"),
            (continuation_claim, WorkflowSpawnContinuationClaim, "continuation_claim"),
            (execution_lease, ExecutionLease, "execution_lease"),
            (run_fence, RunFenceLease, "run_fence"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} has the wrong authority type")
        _required(blocker_id, "blocker_id")
        _required(activation_receipt_id, "activation_receipt_id")
        if predecessor_activation_receipt_id is not None:
            _required(
                predecessor_activation_receipt_id,
                "predecessor_activation_receipt_id",
            )
        if activation_version < 1:
            raise ValueError("activation_version must be positive")
        if (
            ready_receipt.spawn_operation_id
            != continuation_claim.spawn_operation_id
            or ready_receipt.ticket_receipt_id
            != continuation_claim.ticket_receipt_id
            or continuation_claim.parent_run_id != execution_lease.run_id
            or continuation_claim.owner_id != execution_lease.owner_id
            or continuation_claim.runtime_lease_epoch != execution_lease.epoch
            or continuation_claim.run_fence_epoch != run_fence.epoch
            or run_fence.owner_id != execution_lease.owner_id
            or run_fence.runtime_lease_epoch != execution_lease.epoch
        ):
            raise ValueError("workflow spawn activation authorities are not co-fenced")
        if workflow_lease is None:
            if continuation_claim.workflow_lease_epoch is not None:
                raise ValueError("workflow continuation expected a Workflow lease")
        elif (
            workflow_lease.run_id != execution_lease.run_id
            or workflow_lease.owner_id != execution_lease.owner_id
            or workflow_lease.runtime_lease_epoch != execution_lease.epoch
            or workflow_lease.epoch != continuation_claim.workflow_lease_epoch
        ):
            raise ValueError("workflow spawn Workflow lease is not co-fenced")
        self = object.__new__(cls)
        values = locals()
        for field_name in cls.__dataclass_fields__:
            object.__setattr__(
                self,
                field_name,
                factory_token if field_name == "_factory_token" else values[field_name],
            )
        return self

    def __copy__(self) -> None:
        raise TypeError("workflow spawn activations cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("workflow spawn activations cannot be copied")


def _create_workflow_spawn_ready_activation(
    **values: object,
) -> WorkflowSpawnReadyActivation:
    return WorkflowSpawnReadyActivation._create(
        factory_token=_WORKFLOW_SPAWN_ACTIVATION_FACTORY,
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class WorkflowLaunchRequest:
    request_key: str
    candidate_id: str | None
    profile_key: str
    catalog_generation: int
    session_id: str
    request_id: str
    turn_id: str
    requested_run_id: str | None
    requested_trace_id: str | None
    requested_thread_id: str | None
    tool_catalog_generation: int
    objective: str
    start_input: Mapping[str, JsonValue]
    spawn_origin: WorkflowSpawnOrigin
    root_run_id: str
    attachment_policy: AttachmentPolicy
    child_command_id: str

    def __post_init__(self) -> None:
        for name in (
            "request_key",
            "profile_key",
            "session_id",
            "request_id",
            "turn_id",
            "objective",
            "root_run_id",
            "child_command_id",
        ):
            _required(getattr(self, name), name)
        if not isinstance(self.spawn_origin, WorkflowSpawnOrigin):
            raise TypeError("spawn_origin must be a WorkflowSpawnOrigin")
        if self.request_key != workflow_spawn_operation_id(self.spawn_origin):
            raise ValueError("workflow launch request key differs from spawn origin")
        if self.turn_id != self.spawn_origin.turn_id:
            raise ValueError("workflow launch turn differs from spawn origin")
        if self.attachment_policy is not AttachmentPolicy.ATTACHED:
            raise ValueError("workflow_spawn child must remain attached")
        if self.child_command_id != workflow_spawn_child_command_id(self.request_key):
            raise ValueError("workflow spawn child command identity differs")
        if len(self.objective.encode("utf-8")) > 32768:
            raise ValueError("workflow launch objective exceeds 32768 UTF-8 bytes")
        if self.candidate_id is not None:
            _required(self.candidate_id, "candidate_id")
        for name in (
            "requested_run_id",
            "requested_trace_id",
            "requested_thread_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _required(value, name)
        if isinstance(self.catalog_generation, bool) or self.catalog_generation < 1:
            raise ValueError("catalog_generation must be positive")
        if (
            isinstance(self.tool_catalog_generation, bool)
            or self.tool_catalog_generation < 1
        ):
            raise ValueError("tool_catalog_generation must be positive")
        validate_argument_resource_bounds(self.start_input)
        object.__setattr__(
            self,
            "start_input",
            _frozen_object(self.start_input, path="$.workflow_launch.start_input"),
        )


@dataclass(frozen=True, slots=True)
class WorkflowLaunchTicket:
    ticket_receipt_id: str
    payload_hash: str
    candidate_id: str | None
    profile_key: str
    catalog_generation: int

    def __post_init__(self) -> None:
        _required(self.ticket_receipt_id, "ticket_receipt_id")
        _required(self.payload_hash, "payload_hash")
        _required(self.profile_key, "profile_key")
        if self.candidate_id is not None:
            _required(self.candidate_id, "candidate_id")
        if isinstance(self.catalog_generation, bool) or self.catalog_generation < 1:
            raise ValueError("catalog_generation must be positive")


_VERIFIED_TICKET_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedWorkflowLaunchTicket:
    ticket_receipt_id: str
    ticket_id: str
    candidate_id: str | None
    profile_key: str
    catalog_generation: int
    catalog_authority_version: int
    catalog_hash: str
    description: str
    use_when: str
    avoid_when: str
    input_schema_ref: str
    profile_fingerprint: str
    workflow_name: str
    workflow_version: str
    implementation_fingerprint: str
    checkpoint_namespace: str
    manifest_hash: str
    state_schema_version: int
    start_input_schema: StartInputSchema
    terminal_projection_descriptor: Mapping[str, FrozenJsonValue] | None
    terminal_request_factory_hash: str | None
    capability_snapshot: Mapping[str, FrozenJsonValue]
    session_id: str
    request_id: str
    turn_id: str
    requested_run_id: str | None
    requested_trace_id: str | None
    requested_thread_id: str | None
    resolved_run_id: str
    resolved_trace_id: str
    resolved_thread_id: str
    tool_catalog_generation: int
    objective: str
    objective_hash: str
    start_input_hash: str
    spawn_origin: WorkflowSpawnOrigin
    parent_run_id: str
    root_run_id: str
    attachment_policy: AttachmentPolicy
    child_command_id: str
    _factory_token: object

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        values: Mapping[str, object],
    ) -> VerifiedWorkflowLaunchTicket:
        if factory_token is not _VERIFIED_TICKET_FACTORY:
            raise TypeError("verified launch tickets are SDK factory-only")
        if set(values) != {
            name for name in cls.__dataclass_fields__ if name != "_factory_token"
        }:
            raise TypeError("verified workflow ticket fields differ from contract")
        self = object.__new__(cls)
        for field_name in cls.__dataclass_fields__:
            if field_name == "_factory_token":
                object.__setattr__(self, field_name, factory_token)
            else:
                value = values[field_name]
                if field_name == "start_input_schema" and not isinstance(
                    value, StartInputSchema
                ):
                    raise TypeError("verified ticket schema must be typed")
                if field_name in {
                    "terminal_projection_descriptor",
                    "capability_snapshot",
                } and value is not None:
                    if not isinstance(value, Mapping):
                        raise TypeError(f"{field_name} must be a JSON object")
                    value = _frozen_object(value, path=f"$.ticket.{field_name}")
                if field_name == "spawn_origin" and not isinstance(
                    value, WorkflowSpawnOrigin
                ):
                    raise TypeError("verified ticket spawn_origin must be typed")
                if field_name == "attachment_policy":
                    value = AttachmentPolicy(value)
                    if value is not AttachmentPolicy.ATTACHED:
                        raise ValueError("verified workflow spawn must remain attached")
                object.__setattr__(self, field_name, value)
        return self

    def __copy__(self) -> None:
        raise TypeError("verified launch tickets cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("verified launch tickets cannot be copied")

    def _is_sdk_verified(self) -> bool:
        return self._factory_token is _VERIFIED_TICKET_FACTORY


def _create_verified_workflow_launch_ticket(
    values: Mapping[str, object],
) -> VerifiedWorkflowLaunchTicket:
    return VerifiedWorkflowLaunchTicket._create(
        factory_token=_VERIFIED_TICKET_FACTORY,
        values=values,
    )


_VERIFIED_GRAPH_UNAVAILABLE_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedWorkflowGraphUnavailable:
    ticket_receipt_id: str
    profile_key: str
    workflow_name: str
    workflow_version: str
    expected_implementation_hash: str
    registry_content_digest: str
    activation_receipt_id: str
    parent_run_id: str
    owner_id: str
    runtime_lease_epoch: int
    run_fence_epoch: int
    workflow_lease_epoch: int | None
    continuation_claim_epoch: int
    observed_kind: str
    observed_implementation_hash: str | None
    _factory_token: object

    def __new__(cls, *_args: object, **_kwargs: object) -> None:
        raise TypeError("workflow graph-unavailable proofs are SDK factory-only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        values: Mapping[str, object],
    ) -> VerifiedWorkflowGraphUnavailable:
        if factory_token is not _VERIFIED_GRAPH_UNAVAILABLE_FACTORY:
            raise TypeError("workflow graph-unavailable proofs are SDK factory-only")
        expected_fields = {
            name for name in cls.__dataclass_fields__ if name != "_factory_token"
        }
        if set(values) != expected_fields:
            raise TypeError("workflow graph-unavailable proof fields differ")
        for name in (
            "ticket_receipt_id",
            "profile_key",
            "workflow_name",
            "workflow_version",
            "expected_implementation_hash",
            "registry_content_digest",
            "activation_receipt_id",
            "parent_run_id",
            "owner_id",
        ):
            _required(values[name], name)  # type: ignore[arg-type]
        for name in (
            "runtime_lease_epoch",
            "run_fence_epoch",
            "continuation_claim_epoch",
        ):
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be positive")
        workflow_epoch = values["workflow_lease_epoch"]
        if workflow_epoch is not None and (
            isinstance(workflow_epoch, bool)
            or not isinstance(workflow_epoch, int)
            or workflow_epoch < 1
        ):
            raise ValueError("workflow_lease_epoch must be positive or null")
        observed_kind = values["observed_kind"]
        observed_hash = values["observed_implementation_hash"]
        if observed_kind not in {"missing", "drift"}:
            raise ValueError("observed_kind must be missing or drift")
        if (observed_kind == "missing") != (observed_hash is None):
            raise ValueError("observed implementation hash differs from kind")
        if observed_hash is not None and not isinstance(observed_hash, str):
            raise ValueError("observed implementation hash must be a string")
        if isinstance(observed_hash, str):
            _required(observed_hash, "observed_implementation_hash")
        self = object.__new__(cls)
        for field_name in cls.__dataclass_fields__:
            object.__setattr__(
                self,
                field_name,
                (
                    factory_token
                    if field_name == "_factory_token"
                    else values[field_name]
                ),
            )
        return self

    def __copy__(self) -> None:
        raise TypeError("workflow graph-unavailable proofs cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("workflow graph-unavailable proofs cannot be copied")

    def _is_sdk_verified(self) -> bool:
        return self._factory_token is _VERIFIED_GRAPH_UNAVAILABLE_FACTORY


def _create_verified_workflow_graph_unavailable(
    values: Mapping[str, object],
) -> VerifiedWorkflowGraphUnavailable:
    return VerifiedWorkflowGraphUnavailable._create(
        factory_token=_VERIFIED_GRAPH_UNAVAILABLE_FACTORY,
        values=values,
    )


class RuntimeStartDisposition(StrEnum):
    START_NEW = "start_new"
    START_ORPHAN = "start_orphan"
    ATTACH_CURRENT = "attach_current"
    RECOVER_START = "recover_start"
    RECOVER_RESUME = "recover_resume"
    FOREIGN_ACTIVE = "foreign_active"
    WAITING = "waiting"
    CANCEL_PENDING = "cancel_pending"
    TERMINAL = "terminal"


class RuntimeStartDispatchState(StrEnum):
    CLAIMED = "claimed"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class RuntimeActivationClaim:
    owner_id: str
    namespace: str = RUNTIME_LEASE_NAMESPACE
    lease_ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        _required(self.owner_id, "owner_id")
        if self.namespace != RUNTIME_LEASE_NAMESPACE:
            raise ValueError("Runtime activation namespace must be runtime.kernel")
        if (
            isinstance(self.lease_ttl_seconds, bool)
            or not isinstance(self.lease_ttl_seconds, (int, float))
            or not math.isfinite(float(self.lease_ttl_seconds))
            or self.lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class RuntimeStartReceipt:
    ticket_receipt_id: str
    run_id: str
    trace_id: str
    thread_id: str
    committed_run_version: int
    start_snapshot_hash: str
    workflow_request_hash: str
    created_at: float

    def __post_init__(self) -> None:
        for name in (
            "ticket_receipt_id",
            "run_id",
            "trace_id",
            "thread_id",
            "start_snapshot_hash",
            "workflow_request_hash",
        ):
            _required(getattr(self, name), name)
        if (
            isinstance(self.committed_run_version, bool)
            or self.committed_run_version < 0
        ):
            raise ValueError("committed_run_version must be non-negative")
        if not math.isfinite(self.created_at) or self.created_at < 0:
            raise ValueError("created_at must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RuntimeStartActivation:
    execution_lease: ExecutionLease
    run_fence: RunFenceLease

    def __post_init__(self) -> None:
        if not isinstance(self.execution_lease, ExecutionLease) or not isinstance(
            self.run_fence, RunFenceLease
        ):
            raise TypeError("Runtime start activation requires typed authorities")
        if (
            self.execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
            or self.run_fence.run_id.value != self.execution_lease.run_id
            or self.run_fence.owner_id != self.execution_lease.owner_id
            or self.run_fence.runtime_lease_epoch != self.execution_lease.epoch
        ):
            raise ValueError("Runtime start activation authorities are not co-fenced")


@dataclass(frozen=True, slots=True)
class RuntimeStartDispatchClaim:
    claim_id: str
    run_id: str
    owner_id: str
    runtime_lease_epoch: int
    claim_epoch: int

    def __post_init__(self) -> None:
        for name in ("claim_id", "run_id", "owner_id"):
            _required(getattr(self, name), name)
        for name in ("runtime_lease_epoch", "claim_epoch"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeStartDispatchRecord:
    claim_id: str
    run_id: str
    owner_id: str
    runtime_lease_epoch: int
    claim_epoch: int
    expires_at: float
    version: int
    state: RuntimeStartDispatchState


@dataclass(frozen=True, slots=True)
class RuntimeStartAdmission:
    receipt: RuntimeStartReceipt
    disposition: RuntimeStartDisposition
    activation: RuntimeStartActivation | None = None
    dispatch_claim: RuntimeStartDispatchClaim | None = None
    recovery_work: WorkflowRecoveryWork | None = None
    workflow_terminal: WorkflowTerminalOutcome | None = None
    retry_wake: WorkflowRetryWake | None = None

    def __post_init__(self) -> None:
        from simple_harness.workflow.execution_ports import (
            WorkflowRecoveryReceiptKind,
        )

        if not isinstance(self.receipt, RuntimeStartReceipt):
            raise TypeError("receipt must be a RuntimeStartReceipt")
        present = {
            "activation": self.activation is not None,
            "dispatch_claim": self.dispatch_claim is not None,
            "recovery_work": self.recovery_work is not None,
            "workflow_terminal": self.workflow_terminal is not None,
            "retry_wake": self.retry_wake is not None,
        }
        disposition = RuntimeStartDisposition(self.disposition)
        object.__setattr__(self, "disposition", disposition)
        if disposition in {
            RuntimeStartDisposition.START_NEW,
            RuntimeStartDisposition.START_ORPHAN,
        }:
            expected = {"activation", "dispatch_claim"}
        elif disposition in {
            RuntimeStartDisposition.RECOVER_START,
            RuntimeStartDisposition.RECOVER_RESUME,
        }:
            expected = {"activation", "recovery_work"}
        elif disposition is RuntimeStartDisposition.TERMINAL:
            expected = {"workflow_terminal"}
        elif disposition is RuntimeStartDisposition.WAITING and self.retry_wake:
            expected = {"retry_wake"}
        else:
            expected = set()
        actual = {name for name, is_present in present.items() if is_present}
        if actual != expected:
            raise ValueError("Runtime start admission fields violate disposition")
        if (
            self.activation is not None
            and self.activation.execution_lease.run_id != self.receipt.run_id
        ):
            raise ValueError("Runtime start activation belongs to another Run")
        if self.dispatch_claim is not None:
            assert self.activation is not None
            if (
                self.dispatch_claim.run_id != self.receipt.run_id
                or self.dispatch_claim.owner_id
                != self.activation.execution_lease.owner_id
                or self.dispatch_claim.runtime_lease_epoch
                != self.activation.execution_lease.epoch
            ):
                raise ValueError("Runtime start dispatch claim is not co-fenced")
        if self.recovery_work is not None:
            assert self.activation is not None
            expected_kind = (
                WorkflowRecoveryReceiptKind.START
                if disposition is RuntimeStartDisposition.RECOVER_START
                else WorkflowRecoveryReceiptKind.RESUME
            )
            if (
                self.recovery_work.receipt_kind is not expected_kind
                or self.recovery_work.run_id != self.receipt.run_id
            ):
                raise ValueError("Runtime start recovery work identity differs")
        if (
            self.workflow_terminal is not None
            and self.workflow_terminal.run_id != self.receipt.run_id
        ):
            raise ValueError("Runtime terminal outcome belongs to another Run")
        if self.retry_wake is not None and self.retry_wake.run_id != self.receipt.run_id:
            raise ValueError("Runtime retry wake belongs to another Run")


class WorkflowLaunchTicketPort(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    async def publish_catalog(
        self,
        transaction: WorkflowTransaction,
        authority: VerifiedWorkflowCatalogAuthority,
        expected_version: int,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCatalogAuthority: ...

    async def read_catalog(
        self, transaction: WorkflowTransaction
    ) -> WorkflowCatalogAuthority: ...

    async def issue(
        self,
        transaction: WorkflowTransaction,
        request: WorkflowLaunchRequest,
        issue_authority: WorkflowSpawnIssueAuthority,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowLaunchTicket: ...

    async def read_issued(
        self, transaction: WorkflowTransaction, request_key: str
    ) -> tuple[WorkflowLaunchTicket, WorkflowLaunchRequest] | None: ...

    async def read_admitted(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
    ) -> RuntimeStartReceipt | None: ...

    async def claim_spawn_continuation(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        issue_authority: WorkflowSpawnIssueAuthority,
        ready: WorkflowSpawnContinuationReady | None,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnContinuationClaim: ...

    async def mark_spawn_continuation_ready(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        effect_snapshot: EffectRecord,
        evidence_ref: str,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnContinuationReady: ...

    def list_ready_spawn_continuations(
        self, snapshot_cursor: str | None, *, limit: int
    ) -> tuple[tuple[WorkflowSpawnContinuationReady, ...], str | None]: ...

    def read_spawn_ready_blocker(
        self, ready: WorkflowSpawnContinuationReady
    ) -> WaitBlockerRecord | None: ...

    async def consume_spawn_ready_and_claim_activation(
        self,
        transaction: WorkflowTransaction,
        ready: WorkflowSpawnContinuationReady,
        blocker_snapshot: WaitBlockerRecord,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnReadyActivation: ...

    async def read_spawn_ready_activation(
        self,
        transaction: WorkflowTransaction,
        parent_run_id: str,
        activation_receipt_id: str | None = None,
    ) -> WorkflowSpawnReadyActivation | None: ...

    async def reclaim_spawn_ready_activation(
        self,
        transaction: WorkflowTransaction,
        prior: WorkflowSpawnReadyActivation,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnReadyActivation: ...

    async def read_spawn_continuation_outcome(
        self, transaction: WorkflowTransaction, spawn_operation_id: str
    ) -> ToolResult | None: ...

    async def read_spawn_admission_outcome(
        self, transaction: WorkflowTransaction, spawn_operation_id: str
    ) -> WorkflowSpawnAdmissionOutcome | None: ...

    async def continue_spawn_admission(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        continuation: WorkflowSpawnContinuationClaim,
        start: RunStart,
        request: StartAdmissionRequest,
        snapshot: StartSnapshot,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnToolOutcome: ...

    async def settle_spawn_continuation_catalog_stale(
        self,
        transaction: WorkflowTransaction,
        continuation: WorkflowSpawnContinuationClaim,
        ready: WorkflowSpawnContinuationReady | None,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult: ...

    async def settle_spawn_continuation_graph_unavailable(
        self,
        transaction: WorkflowTransaction,
        continuation: WorkflowSpawnContinuationClaim,
        ready: WorkflowSpawnContinuationReady | None,
        evidence: VerifiedWorkflowGraphUnavailable,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult: ...

    async def settle_spawn_continuation_for_parent_terminal(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        ready_or_continuation: WorkflowSpawnContinuationReady
        | WorkflowSpawnContinuationClaim,
        parent_terminal_snapshot: WorkflowTerminalOutcome,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult: ...

    async def resume_admitted_runtime_start(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RuntimeStartAdmission: ...

    async def resume_spawn_child_start(
        self,
        transaction: WorkflowTransaction,
        child_run_id: str,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RuntimeStartAdmission: ...

    async def verify(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
    ) -> VerifiedWorkflowLaunchTicket: ...

    async def admit_runtime_start(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        start: RunStart,
        request: StartAdmissionRequest,
        snapshot: StartSnapshot,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RuntimeStartAdmission: ...


__all__ = (
    "ProfileDescriptor",
    "RuntimeActivationClaim",
    "RuntimeStartActivation",
    "RuntimeStartAdmission",
    "RuntimeStartDispatchClaim",
    "RuntimeStartDispatchRecord",
    "RuntimeStartDispatchState",
    "RuntimeStartDisposition",
    "RuntimeStartReceipt",
    "StartInputSchema",
    "VerifiedWorkflowCatalogAuthority",
    "VerifiedWorkflowGraphUnavailable",
    "VerifiedWorkflowLaunchTicket",
    "WorkflowCatalogAuthority",
    "WorkflowCatalogProfileBinding",
    "WorkflowLaunchRequest",
    "WorkflowLaunchTicket",
    "WorkflowLaunchTicketPort",
    "WorkflowProfileRegistration",
    "WorkflowSpawnContinuationClaim",
    "WorkflowSpawnContinuationReady",
    "WorkflowSpawnIssueAuthority",
    "WorkflowSpawnOrigin",
    "WorkflowSpawnReadyActivation",
    "WorkflowSpawnReadyActivationState",
    "WorkflowSpawnSelection",
    "workflow_catalog_hash",
    "workflow_catalog_selection_from_authority",
    "workflow_catalog_selection_from_json",
    "workflow_catalog_selection_hash",
    "workflow_catalog_selection_to_json",
    "workflow_spawn_child_command_id",
    "workflow_spawn_child_request_id",
    "workflow_spawn_child_run_id",
    "workflow_spawn_operation_id",
)
