# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Framework-neutral contracts for DeskPet durable workflows."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypedDict

from simple_harness.contracts import freeze_json

from .errors import InvalidStatePatch, WorkflowDefinitionError

if TYPE_CHECKING:
    from simple_harness.workflows.capability_build.ports import (
        CapabilityActivatePort,
        CapabilityBuildAuthorizationPort,
        CapabilitySearchPort,
        CapabilitySourcePolicyPort,
        IsolatedBuildPort,
        PackageStorePort,
    )
    from simple_harness.workflows.durable_task.ports import (
        ArtifactPort,
        AuthorizationPort,
        CapabilityCatalogPort,
        ProposalPort,
        WorkspacePort,
    )
    from simple_harness.workflows.personal_v1.ports import PersonalWorkflowRuntimePort

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


def validate_json_value(value: object, *, path: str = "$") -> None:
    """Reject values that cannot be represented by strict JSON."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidStatePatch("non_finite_number", f"{path} must contain a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidStatePatch("non_string_key", f"{path} contains a non-string key")
            validate_json_value(item, path=f"{path}.{key}")
        return
    raise InvalidStatePatch(
        "non_json_value",
        f"{path} contains unsupported value type {type(value).__name__}",
    )


def canonical_json(value: JsonValue) -> str:
    validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class WorkflowRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    RETRYABLE = "retryable"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED_PENDING = "succeeded_pending"
    SUCCEEDED = "succeeded"
    WAITING = "waiting"
    RETRYABLE = "retryable"
    FAILED = "failed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.COMPLETED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.CANCELLED,
    }
)


class WorkflowState(TypedDict):
    schema_version: int
    workflow_name: str
    workflow_version: str
    thread_id: str
    run_id: str
    session_id: str
    active_nodes: list[str]
    active_step_id: str | None
    status: str
    values: dict[str, JsonValue]
    blob_refs: list[str]
    artifact_refs: list[str]
    receipt_refs: list[str]
    loop_counters: dict[str, int]
    budgets: dict[str, int]
    errors: list[dict[str, JsonValue]]


class StatePatch:
    """A defensive-copying, strict-JSON state update.

    The writer is intentionally absent. The runtime supplies the current node
    identity when validating and merging a patch, so handlers cannot forge it.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, JsonValue]) -> None:
        if not isinstance(values, Mapping):
            raise InvalidStatePatch("invalid_patch", "StatePatch requires a mapping")
        copied = copy.deepcopy(dict(values))
        validate_json_value(copied)
        self._values = copied

    @property
    def values(self) -> dict[str, JsonValue]:
        return copy.deepcopy(self._values)

    def to_dict(self) -> dict[str, JsonValue]:
        return self.values

    def __bool__(self) -> bool:
        return bool(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StatePatch) and self._values == other._values

    def __repr__(self) -> str:
        return f"StatePatch({self._values!r})"


class JsonType(StrEnum):
    JSON = "json"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


class ReducerKind(StrEnum):
    SINGLE_WRITER = "single_writer"
    DICT_DISJOINT = "dict_disjoint"
    STABLE_LIST = "stable_list"


@dataclass(frozen=True)
class ChannelSpec:
    value_type: JsonType | str
    reducer: ReducerKind | str
    allowed_writers: frozenset[str]
    item_id_key: str = "id"

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_writers", frozenset(self.allowed_writers))
        if not self.allowed_writers:
            raise WorkflowDefinitionError(
                "channel_without_writer", "A channel must allow at least one writer"
            )
        if not self.item_id_key:
            raise WorkflowDefinitionError("invalid_item_id_key", "item_id_key must not be empty")


@dataclass(frozen=True)
class NodeExecutionIdentity:
    workflow_name: str
    workflow_version: str
    thread_id: str
    run_id: str
    checkpoint_id: str
    checkpoint_ns: str
    task_id: str
    node_id: str
    attempt: int
    first_attempt_time: float | None = None
    # Native durable tasks carry both identities.  They are optional here so
    # historical v1-v4 fixtures and third-party executables that still build
    # the legacy positional shape remain readable.  New contracts that need
    # loop/work-item identity (notably DeepResearch v5 progress/effects) must
    # reject a missing value at their own boundary.
    activation_id: str | None = None
    invocation_key: str | None = None

    @classmethod
    def from_execution_info(
        cls,
        *,
        workflow_name: str,
        workflow_version: str,
        node_id: str,
        execution_info: object,
        state: Mapping[str, object] | None = None,
    ) -> NodeExecutionIdentity:
        state = state or {}
        thread_id = getattr(execution_info, "thread_id", None) or state.get("thread_id")
        run_id = getattr(execution_info, "run_id", None) or state.get("run_id")
        required = {
            "thread_id": thread_id,
            "run_id": run_id,
            "checkpoint_id": getattr(execution_info, "checkpoint_id", None),
            "task_id": getattr(execution_info, "task_id", None),
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise InvalidStatePatch(
                "missing_execution_identity",
                f"Runtime execution_info is missing: {', '.join(missing)}",
            )
        attempt = getattr(execution_info, "node_attempt", 1)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise InvalidStatePatch(
                "invalid_node_attempt", "Runtime node_attempt must be a positive integer"
            )
        return cls(
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            thread_id=str(thread_id),
            run_id=str(run_id),
            checkpoint_id=str(required["checkpoint_id"]),
            checkpoint_ns=str(getattr(execution_info, "checkpoint_ns", "")),
            task_id=str(required["task_id"]),
            node_id=node_id,
            attempt=attempt,
            first_attempt_time=getattr(execution_info, "node_first_attempt_time", None),
            activation_id=(
                str(value) if (value := getattr(execution_info, "activation_id", None)) else None
            ),
            invocation_key=(
                str(value) if (value := getattr(execution_info, "invocation_key", None)) else None
            ),
        )


PHYSICAL_WORKFLOW_PORT_NAMES = frozenset(
    {
        "llm",
        "search",
        "fetch",
        "tool",
        "effect",
        "permission",
        "artifact",
        "output_contract",
        "receipt",
        "notifier",
        "evaluator",
        "blob",
        "retrieval",
        "semantic",
        "llm_extract",
        "llm_inference",
        "llm_repair",
        "subagent_scheduler",
    }
)


WORKFLOW_PORT_NAMES = frozenset(
    {
        "llm",
        "search",
        "fetch",
        "tool",
        "effect",
        "permission",
        "artifact",
        "output_contract",
        "receipt",
        "notifier",
        "evaluator",
        "clock",
        "control",
        "observer",
        "progress",
        "native_execution_policy",
        "blob",
        "snapshot",
        "retrieval",
        "semantic",
        "llm_extract",
        "llm_inference",
        "llm_repair",
        "deadline",
        "subagent_scheduler",
        "proposal",
        "capability_catalog",
        "workspace",
        "authorization",
        "personal_workflow_runtime",
        "capability_search",
        "source_policy",
        "isolated_build",
        "package_store",
        "activate",
    }
)


def _require_port(value: object, name: str, methods: tuple[str, ...]) -> None:
    missing = tuple(method for method in methods if not callable(getattr(value, method, None)))
    if missing:
        raise TypeError(f"{name} must implement {', '.join(missing)}")


@dataclass(frozen=True, slots=True)
class DurableTaskHostServices:
    """Frozen Host-owned services used by the durable task graph."""

    proposal: ProposalPort
    workspace: WorkspacePort
    capability_catalog: CapabilityCatalogPort | None = None
    authorization: AuthorizationPort | None = None
    artifact: ArtifactPort | None = None
    clock: object | None = None

    def __post_init__(self) -> None:
        _require_port(self.proposal, "proposal", ("propose",))
        _require_port(self.workspace, "workspace", ("execute_tools",))
        if self.capability_catalog is not None:
            _require_port(
                self.capability_catalog,
                "capability_catalog",
                ("is_capability_available", "get_capability_source", "get_capability_policy"),
            )
        if self.authorization is not None:
            _require_port(self.authorization, "authorization", ("grant_authorization",))
        if self.artifact is not None:
            _require_port(
                self.artifact,
                "artifact",
                ("check_completion_evidence", "completion_decision", "run_tests", "audit"),
            )
        if self.clock is not None and not callable(self.clock):
            raise TypeError("clock must be callable")

    def as_ports(self) -> Mapping[str, object]:
        ports = {"proposal": self.proposal, "workspace": self.workspace}
        ports.update(
            {
                name: value
                for name, value in (
                    ("capability_catalog", self.capability_catalog),
                    ("authorization", self.authorization),
                    ("artifact", self.artifact),
                    ("clock", self.clock),
                )
                if value is not None
            }
        )
        return MappingProxyType(ports)


@dataclass(frozen=True, slots=True)
class PersonalWorkflowHostServices:
    """Frozen Host-owned personal workflow interpreter service."""

    runtime: PersonalWorkflowRuntimePort

    def __post_init__(self) -> None:
        _require_port(self.runtime, "personal workflow runtime", ("execute",))

    def as_ports(self) -> Mapping[str, object]:
        return MappingProxyType({"personal_workflow_runtime": self.runtime})


@dataclass(frozen=True, slots=True)
class CapabilityBuildHostServices:
    """Frozen services for the capability-build durable-task specialization."""

    proposal: ProposalPort
    workspace: WorkspacePort
    search: CapabilitySearchPort
    source_policy: CapabilitySourcePolicyPort
    isolated_build: IsolatedBuildPort
    package_store: PackageStorePort
    activate: CapabilityActivatePort
    authorization: CapabilityBuildAuthorizationPort
    artifact: ArtifactPort | None = None
    clock: object | None = None

    def __post_init__(self) -> None:
        _require_port(self.proposal, "proposal", ("propose",))
        _require_port(self.workspace, "workspace", ("execute_tools",))
        for value, name, methods in (
            (self.search, "capability search", ("search",)),
            (self.source_policy, "source policy", ("authorize_source",)),
            (self.isolated_build, "isolated build", ("build",)),
            (self.package_store, "package store", ("store",)),
            (self.activate, "activate", ("activate",)),
            (self.authorization, "authorization", ("authorize_build",)),
        ):
            _require_port(value, name, methods)
        if self.artifact is not None:
            _require_port(
                self.artifact,
                "artifact",
                ("check_completion_evidence", "completion_decision", "run_tests", "audit"),
            )
        if self.clock is not None and not callable(self.clock):
            raise TypeError("clock must be callable")

    def as_ports(self) -> Mapping[str, object]:
        ports: dict[str, object] = {
            "proposal": self.proposal,
            "workspace": self.workspace,
            "capability_search": self.search,
            "source_policy": self.source_policy,
            "isolated_build": self.isolated_build,
            "package_store": self.package_store,
            "activate": self.activate,
            "authorization": self.authorization,
        }
        if self.artifact is not None:
            ports["artifact"] = self.artifact
        if self.clock is not None:
            ports["clock"] = self.clock
        return MappingProxyType(ports)


@dataclass(frozen=True, slots=True)
class WorkflowHostServices:
    """One immutable service composition root owned by a WorkflowRunner."""

    durable_task: DurableTaskHostServices | None = None
    personal_v1: PersonalWorkflowHostServices | None = None
    capability_build: CapabilityBuildHostServices | None = None

    def has_profile(self, profile_key: str) -> bool:
        return {
            "workflow.durable_task": self.durable_task,
            "workflow.personal_v1": self.personal_v1,
            "workflow.capability_build": self.capability_build,
        }.get(profile_key) is not None

    def ports_for(self, profile_key: str) -> Mapping[str, object]:
        groups = {
            "workflow.durable_task": self.durable_task,
            "workflow.personal_v1": self.personal_v1,
            "workflow.capability_build": self.capability_build,
        }
        if profile_key not in groups:
            raise KeyError(f"unknown workflow profile: {profile_key}")
        group = groups[profile_key]
        if group is None:
            raise KeyError(f"workflow profile services unavailable: {profile_key}")
        return group.as_ports()

    def bind_context(self, profile_key: str, context: WorkflowContext) -> WorkflowContext:
        if not self.has_profile(profile_key):
            return context
        if context.ports:
            raise ValueError("workflow ports are Runner-owned and cannot be supplied per call")
        return replace(context, ports=self.ports_for(profile_key))


@dataclass(frozen=True)
class WorkflowContext:
    ports: Mapping[str, object] = field(default_factory=dict)
    trace_id: str | None = None
    request_id: str | None = None
    turn_id: str | None = None
    identity: NodeExecutionIdentity | None = None
    _writer_node_id: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        unknown = sorted(set(self.ports) - WORKFLOW_PORT_NAMES)
        if unknown:
            raise WorkflowDefinitionError(
                "unknown_workflow_port",
                f"Unknown workflow ports: {', '.join(unknown)}",
            )
        object.__setattr__(self, "ports", MappingProxyType(dict(self.ports)))

    def port(self, name: str) -> object:
        if name not in WORKFLOW_PORT_NAMES:
            raise KeyError(f"Unknown workflow port: {name}")
        return self.ports[name]

    def for_node(
        self,
        identity: NodeExecutionIdentity,
        *,
        pure_before_interrupt: bool = False,
    ) -> WorkflowContext:
        ports = {} if pure_before_interrupt else self.ports
        return replace(self, ports=ports, identity=identity, _writer_node_id=identity.node_id)


@dataclass(frozen=True)
class PureRouteContext:
    """Immutable, capability-free input for deterministic route selection."""

    workflow_name: str
    workflow_version: str
    run_id: str
    checkpoint_id: str
    task_id: str
    source: str
    state: Mapping[str, JsonValue]
    logical_timestamp: float

    def __post_init__(self) -> None:
        state = copy.deepcopy(dict(self.state))
        validate_json_value(state, path="$.route.state")
        frozen = freeze_json(state)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "state", frozen)


NodeHandler: TypeAlias = Callable[[WorkflowState, WorkflowContext], Awaitable[StatePatch]]
RouteSelector: TypeAlias = Callable[[WorkflowState, PureRouteContext], str | Awaitable[str]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_delay_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    retryable_codes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "retryable_codes", frozenset(self.retryable_codes))
        if self.max_attempts < 1:
            raise WorkflowDefinitionError("invalid_retry_policy", "max_attempts must be at least 1")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise WorkflowDefinitionError(
                "invalid_retry_policy", "retry delays must not be negative"
            )
        if self.backoff_multiplier < 1:
            raise WorkflowDefinitionError(
                "invalid_retry_policy", "backoff_multiplier must be at least 1"
            )
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise WorkflowDefinitionError(
                "invalid_retry_policy",
                "max_delay_seconds must be at least initial_delay_seconds",
            )


class EffectKind(StrEnum):
    IDEMPOTENT_READ = "idempotent_read"
    DETERMINISTIC_REUSABLE = "deterministic_reusable"
    STAGED_FILE = "staged_file"
    OPAQUE_MANUAL = "opaque_manual"


@dataclass(frozen=True)
class EffectPolicy:
    policy_id: str
    version: str
    kind: EffectKind | str
    max_attempts: int = 1
    reusable_across_branches: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise WorkflowDefinitionError(
                "invalid_effect_policy", "Effect policy id and version are required"
            )
        try:
            kind = EffectKind(self.kind)
        except ValueError as exc:
            raise WorkflowDefinitionError(
                "unknown_effect_policy", f"Unknown effect policy: {self.kind}"
            ) from exc
        object.__setattr__(self, "kind", kind)
        if self.max_attempts < 1:
            raise WorkflowDefinitionError(
                "invalid_effect_policy", "Effect max_attempts must be at least 1"
            )
        if kind is EffectKind.OPAQUE_MANUAL and self.max_attempts != 1:
            raise WorkflowDefinitionError(
                "unsafe_opaque_retry", "Opaque effects may be attempted only once"
            )
        if self.reusable_across_branches and kind is not EffectKind.DETERMINISTIC_REUSABLE:
            raise WorkflowDefinitionError(
                "unsafe_branch_reuse",
                "Only deterministic reusable effects may cross branches",
            )


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolInventoryEntry:
    name: str
    access: ToolAccess | str
    spec_version: str
    schema_hash: str
    effect_policy: EffectPolicy | None = None
    effect_policy_hash: str | None = None
    outcome_parser_id: str | None = None
    outcome_parser_version: str | None = None
    outcome_parser_hash: str | None = None
    handler_id: str | None = None
    dispatch_kind: str = "sync"
    execution_build_digest: str | None = None
    completion_semantics: str = "sync"
    resource_scope_resolver_id: str | None = None
    resource_scope_resolver_version: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.spec_version or not self.schema_hash:
            raise WorkflowDefinitionError(
                "invalid_tool_inventory", "Tool name/spec version/schema hash are required"
            )
        try:
            access = ToolAccess(self.access)
        except ValueError as exc:
            raise WorkflowDefinitionError(
                "invalid_tool_inventory", f"Unknown tool access: {self.access}"
            ) from exc
        object.__setattr__(self, "access", access)
        parser_fields = (
            self.outcome_parser_id,
            self.outcome_parser_version,
            self.outcome_parser_hash,
        )
        if any(parser_fields) and not all(parser_fields):
            raise WorkflowDefinitionError(
                "incomplete_outcome_parser",
                "Outcome parser id, version and hash must be supplied together",
            )
        if self.dispatch_kind not in {
            "sync",
            "async",
            "context",
            "staged",
            "control",
            "provider",
        }:
            raise WorkflowDefinitionError("invalid_tool_dispatch", "Unknown Tool dispatch kind")
        if self.completion_semantics not in {"sync", "accepted_async"}:
            raise WorkflowDefinitionError(
                "invalid_tool_completion", "Unknown Tool completion semantics"
            )
        for name in (
            "schema_hash",
            "effect_policy_hash",
            "outcome_parser_hash",
            "execution_build_digest",
        ):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            ):
                raise WorkflowDefinitionError(
                    "invalid_tool_digest", f"{name} must be lowercase SHA-256"
                )
        resolver = (self.resource_scope_resolver_id, self.resource_scope_resolver_version)
        if any(resolver) and not all(resolver):
            raise WorkflowDefinitionError(
                "incomplete_resource_resolver",
                "Resource resolver id and version must be supplied together",
            )


class ExecutionInfoProtocol(Protocol):
    thread_id: str | None
    run_id: str | None
    checkpoint_id: str
    checkpoint_ns: str
    task_id: str
    node_attempt: int
    node_first_attempt_time: float | None


__all__ = [
    "PHYSICAL_WORKFLOW_PORT_NAMES",
    "TERMINAL_RUN_STATUSES",
    "WORKFLOW_PORT_NAMES",
    "ChannelSpec",
    "CapabilityBuildHostServices",
    "DurableTaskHostServices",
    "EffectKind",
    "EffectPolicy",
    "ExecutionInfoProtocol",
    "JsonPrimitive",
    "JsonType",
    "JsonValue",
    "NodeExecutionIdentity",
    "NodeHandler",
    "NodeStatus",
    "PureRouteContext",
    "ReducerKind",
    "RetryPolicy",
    "RouteSelector",
    "StatePatch",
    "ToolAccess",
    "ToolInventoryEntry",
    "WorkflowContext",
    "WorkflowHostServices",
    "PersonalWorkflowHostServices",
    "WorkflowRunStatus",
    "WorkflowState",
    "canonical_json",
    "validate_json_value",
]
