# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable native workflow execution kernel and terminal projection."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from simple_harness.contracts import (
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
    validate_json_value,
)

from .contracts import NodeExecutionIdentity, StatePatch, WorkflowContext, WorkflowState
from .control import ExecutionControl, WorkflowSuspended, bind_execution_control
from .errors import (
    InvalidStatePatch,
    StateMergeConflict,
    WorkflowErrorCode,
    WorkflowNodeError,
)

if TYPE_CHECKING:
    from simple_harness.contracts import FrozenJsonValue

    from .definition import CompiledWorkflow, WorkflowManifest

_METRIC_KEYS = frozenset(
    {
        "actual_requests",
        "hits",
        "empty",
        "timeouts",
        "cooldown_skips",
        "busy_skips",
        "queue_timeouts",
        "probes",
        "rescue_considered_count",
        "rescue_executed_count",
        "candidates",
    }
)
_DIAGNOSTIC_CODES = frozenset(
    {
        "artifact_missing",
        "blocked",
        "blob_unavailable",
        "body_bytes_below_threshold",
        "budget_exhausted",
        "captcha",
        "citation_count_below_threshold",
        "claim_pruned",
        "claim_unsupported",
        "cooldown",
        "degraded",
        "deadline_exhausted",
        "deterministic_repair",
        "direct_failure",
        "domain_count_below_threshold",
        "evidence_missing",
        "fetch_failure",
        "half_open_busy",
        "http_error",
        "insufficient_evidence",
        "insufficient_support",
        "invalid_response",
        "low_quality_content",
        "low_quality_evidence",
        "low_quality_source",
        "missing_exact_token",
        "no_results",
        "other",
        "parse_error",
        "partial_results",
        "provider_degraded",
        "provider_failure",
        "published_factual_below_threshold",
        "queue_timeout",
        "rate_limit",
        "search_degraded",
        "search_port_unavailable",
        "support_rate_below_threshold",
        "timeout",
    }
)
_STAGE_IDS = frozenset({"fetch", "score", "gap", "rerank", "synth", "cite", "persist"})
_RETRY_ACTION_IDS = frozenset({"retry_from_start"})
_PUBLIC_KEYS = frozenset({"metrics", "diagnostic_codes", "skipped_stage_ids", "retry_action_id"})
_REQUIRED_PUBLIC_KEYS = frozenset({"metrics", "diagnostic_codes", "skipped_stage_ids"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_MAX_METRIC = 1_000_000
_MAX_DIAGNOSTICS = 16
_MAX_SKIPPED_STAGES = 7
_CHECKPOINT_TYPE = "simple-harness-native-json-v1"
_ENGINE_KIND = "simple-harness-native"
_SNAPSHOT_VERSION = 1
_TERMINAL_REQUEST_SCHEMA: dict[str, JsonValue] = {
    "schema_version": 1,
    "fields": [
        "schema_version",
        "capability_id",
        "capability_version",
        "descriptor_digest",
        "workflow_name",
        "workflow_version",
        "run_id",
        "engine_status",
        "engine_error",
        "recovery_action",
        "state",
    ],
}
_TERMINAL_REQUEST_FACTORY_SPEC: dict[str, JsonValue] = {
    "schema_version": 1,
    "bindings": [
        ["schema_version", "literal:1"],
        ["capability_id", "descriptor.capability_id"],
        ["capability_version", "descriptor.version"],
        ["descriptor_digest", "descriptor.digest"],
        ["workflow_name", "workflow_name"],
        ["workflow_version", "workflow_version"],
        ["run_id", "run_id"],
        ["engine_status", "status"],
        ["engine_error", "error"],
        ["recovery_action", "recovery_action"],
        ["state", "state"],
    ],
}
TERMINAL_REQUEST_SCHEMA_HASH = hashlib.sha256(
    canonical_json(_TERMINAL_REQUEST_SCHEMA).encode()
).hexdigest()
TERMINAL_REQUEST_FACTORY_HASH = hashlib.sha256(
    canonical_json(_TERMINAL_REQUEST_FACTORY_SPEC).encode()
).hexdigest()


def _assert_terminal_request_factory_spec() -> None:
    actual = hashlib.sha256(canonical_json(_TERMINAL_REQUEST_FACTORY_SPEC).encode()).hexdigest()
    if actual != TERMINAL_REQUEST_FACTORY_HASH:
        raise InvalidStatePatch(
            "terminal_projection_request_factory_drift",
            "Terminal projection request factory implementation drifted",
        )


class _CommitUncertain(RuntimeError):
    """A store call may have committed before its response was lost."""


def _identity(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidStatePatch("invalid_native_identity", f"Native {field_name} is required")
    return value.strip()


def _stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeTask:
    task_id: str
    node_id: str
    invocation_key: str
    activation_id: str
    join_epoch: int = 0
    task_path: tuple[str, ...] = ()
    input: dict[str, JsonValue] = field(default_factory=dict)
    retry_attempt: int = 1
    next_attempt_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "node_id", "invocation_key", "activation_id"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if (
            isinstance(self.join_epoch, bool)
            or not isinstance(self.join_epoch, int)
            or self.join_epoch < 0
            or isinstance(self.retry_attempt, bool)
            or not isinstance(self.retry_attempt, int)
            or self.retry_attempt < 1
        ):
            raise InvalidStatePatch("invalid_native_task", "Native task counters are invalid")
        if not all(isinstance(item, str) and item for item in self.task_path):
            raise InvalidStatePatch("invalid_native_task", "Native task_path is invalid")
        detached = deepcopy(self.input)
        validate_json_value(detached, path="$.task.input")
        object.__setattr__(self, "input", detached)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "task_id": self.task_id,
            "node_id": self.node_id,
            "invocation_key": self.invocation_key,
            "activation_id": self.activation_id,
            "join_epoch": self.join_epoch,
            "task_path": list(self.task_path),
            "input": deepcopy(self.input),
            "retry_attempt": self.retry_attempt,
            "next_attempt_at": self.next_attempt_at,
        }


@dataclass(frozen=True, slots=True)
class NativeExecutionInfo:
    thread_id: str
    run_id: str
    checkpoint_id: str
    checkpoint_ns: str
    task_id: str
    node_attempt: int
    node_first_attempt_time: float | None
    activation_id: str
    invocation_key: str

    def __post_init__(self) -> None:
        for name in (
            "thread_id",
            "run_id",
            "checkpoint_id",
            "task_id",
            "activation_id",
            "invocation_key",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if not isinstance(self.checkpoint_ns, str):
            raise InvalidStatePatch("invalid_native_execution", "Native checkpoint_ns is invalid")
        if (
            isinstance(self.node_attempt, bool)
            or not isinstance(self.node_attempt, int)
            or self.node_attempt < 1
        ):
            raise InvalidStatePatch("invalid_native_execution", "Native node_attempt is invalid")


@dataclass(frozen=True, slots=True)
class NativeExecutionPolicy:
    max_parallel_tasks: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_parallel_tasks, bool)
            or not isinstance(self.max_parallel_tasks, int)
            or self.max_parallel_tasks < 1
        ):
            raise ValueError("max_parallel_tasks must be a positive integer")


@dataclass(frozen=True, slots=True)
class NodeTaskOutcome:
    task: NativeTask
    patch: StatePatch | None
    consumed_interrupt_ids: tuple[str, ...]
    error: BaseException | None
    identity: NodeExecutionIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.task, NativeTask) or not isinstance(
            self.identity, NodeExecutionIdentity
        ):
            raise TypeError("NodeTaskOutcome requires native task identity")
        if self.patch is not None and not isinstance(self.patch, StatePatch):
            raise TypeError("NodeTaskOutcome patch must be StatePatch")
        if not all(isinstance(item, str) and item for item in self.consumed_interrupt_ids):
            raise InvalidStatePatch("invalid_native_outcome", "Consumed interrupt ids are invalid")


@dataclass(frozen=True, slots=True)
class NativeSnapshotEnvelope:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    run_id: str
    state_schema_version: int
    step: int
    state: Mapping[str, JsonValue]
    frontier: tuple[NativeTask, ...]
    completed_activations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    join_firings: tuple[str, ...] = ()
    node_writes: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    interrupt: Mapping[str, JsonValue] | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    engine_kind: str = _ENGINE_KIND
    snapshot_version: int = _SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        for name in ("thread_id", "checkpoint_id", "run_id"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if not isinstance(self.checkpoint_ns, str):
            raise InvalidStatePatch("invalid_native_snapshot", "Native checkpoint_ns is invalid")
        if self.engine_kind != _ENGINE_KIND or self.snapshot_version != _SNAPSHOT_VERSION:
            raise InvalidStatePatch(
                "unsupported_native_snapshot", "Native snapshot version is unsupported"
            )
        if (
            isinstance(self.step, bool)
            or not isinstance(self.step, int)
            or self.step < 0
            or isinstance(self.state_schema_version, bool)
            or not isinstance(self.state_schema_version, int)
            or self.state_schema_version < 1
        ):
            raise InvalidStatePatch(
                "invalid_native_snapshot", "Native snapshot counters are invalid"
            )
        object.__setattr__(self, "frontier", tuple(self.frontier))
        object.__setattr__(self, "state", deepcopy(dict(self.state)))
        object.__setattr__(
            self,
            "completed_activations",
            {key: tuple(value) for key, value in self.completed_activations.items()},
        )
        object.__setattr__(self, "join_firings", tuple(self.join_firings))
        object.__setattr__(
            self,
            "node_writes",
            {key: deepcopy(dict(value)) for key, value in self.node_writes.items()},
        )
        object.__setattr__(
            self,
            "interrupt",
            None if self.interrupt is None else deepcopy(dict(self.interrupt)),
        )
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))
        canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "checkpoint_type": _CHECKPOINT_TYPE,
            "engine_kind": self.engine_kind,
            "snapshot_version": self.snapshot_version,
            "state_schema_version": self.state_schema_version,
            "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns,
            "checkpoint_id": self.checkpoint_id,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "run_id": self.run_id,
            "step": self.step,
            "state": deepcopy(dict(self.state)),
            "frontier": [task.to_dict() for task in self.frontier],
            "completed_activations": {
                key: list(value) for key, value in sorted(self.completed_activations.items())
            },
            "join_firings": list(self.join_firings),
            "node_writes": {key: deepcopy(dict(value)) for key, value in self.node_writes.items()},
            "interrupt": None if self.interrupt is None else deepcopy(dict(self.interrupt)),
            "metadata": deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class NativeExecution:
    snapshot: NativeSnapshotEnvelope
    pending_results: Mapping[str, StatePatch] = field(default_factory=dict)
    first_attempt_times: Mapping[str, float] = field(default_factory=dict)
    route_selections: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    pending_consumed_interrupt_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeCommitResult:
    snapshot: NativeSnapshotEnvelope
    materialized_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalProjectionDescriptor:
    capability_id: str
    version: str
    projector_fingerprint: str
    request_schema_hash: str
    request_factory_hash: str

    def __post_init__(self) -> None:
        _assert_terminal_request_factory_spec()
        for name in ("capability_id", "version"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        for name in (
            "projector_fingerprint",
            "request_schema_hash",
            "request_factory_hash",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise InvalidStatePatch(
                    "invalid_terminal_projection_descriptor",
                    f"{name} must be a lowercase SHA-256 digest",
                )
        if self.request_schema_hash != TERMINAL_REQUEST_SCHEMA_HASH:
            raise InvalidStatePatch(
                "terminal_projection_request_schema_drift",
                "Terminal projection request schema hash is unsupported",
            )
        if self.request_factory_hash != TERMINAL_REQUEST_FACTORY_HASH:
            raise InvalidStatePatch(
                "terminal_projection_request_factory_drift",
                "Terminal projection request factory hash is unsupported",
            )

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        version: str,
        projector_fingerprint: str,
    ) -> TerminalProjectionDescriptor:
        return cls(
            capability_id,
            version,
            projector_fingerprint,
            TERMINAL_REQUEST_SCHEMA_HASH,
            TERMINAL_REQUEST_FACTORY_HASH,
        )

    @property
    def digest(self) -> str:
        return _stable_id(
            self.capability_id,
            self.version,
            self.projector_fingerprint,
            self.request_schema_hash,
            self.request_factory_hash,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "capability_id": self.capability_id,
            "version": self.version,
            "projector_fingerprint": self.projector_fingerprint,
            "request_schema_hash": self.request_schema_hash,
            "request_factory_hash": self.request_factory_hash,
            "descriptor_digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    run_id: str
    workflow_name: str
    workflow_version: str
    checkpoint_id: str
    state_summary: Mapping[str, JsonValue]
    logical_timestamp: float
    deadline: float | None

    def __post_init__(self) -> None:
        for name in ("run_id", "workflow_name", "workflow_version", "checkpoint_id"):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        summary = deepcopy(dict(self.state_summary))
        validate_json_value(summary, path="$.projection.state_summary")
        frozen = freeze_json(cast(JsonValue, summary))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "state_summary", cast(Mapping[str, JsonValue], frozen))
        for name in ("logical_timestamp", "deadline"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise InvalidStatePatch(
                    "invalid_projection_time", f"{name} must be a frozen number"
                )


TerminalCommitProjector = Callable[
    [Mapping[str, JsonValue], ProjectionContext],
    Awaitable[Mapping[str, JsonValue]],
]


class TerminalProjectionPort(Protocol):
    def project_public(
        self,
        workflow_name: str,
        workflow_version: str,
        raw: object,
        engine_status: str,
    ) -> Mapping[str, JsonValue] | None: ...


class TerminalCommitProjectionPort(Protocol):
    def lookup(
        self,
        workflow_name: str,
        workflow_version: str,
        descriptor: TerminalProjectionDescriptor,
    ) -> TerminalCommitProjector | None: ...


class WorkflowProgressPort(Protocol):
    def freeze_patch(
        self,
        patch: StatePatch,
        *,
        identity: NodeExecutionIdentity,
        first_attempt_time: float,
        finished_at: float,
    ) -> StatePatch: ...


class WorkflowObserverPort(Protocol):
    async def node_started(self, identity: NodeExecutionIdentity) -> object: ...

    async def node_finished(
        self,
        identity: NodeExecutionIdentity,
        status: str,
        *,
        error: BaseException | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TerminalProjectionPrepareReceipt:
    operation_id: str
    run_id: str
    terminal_checkpoint_id: str
    descriptor_digest: str
    input_hash: str
    output: Mapping[str, JsonValue]
    output_hash: str
    blob_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        detached = deepcopy(dict(self.output))
        validate_json_value(detached, path="$.projection.output")
        frozen = freeze_json(cast(JsonValue, detached))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "output", cast(Mapping[str, JsonValue], frozen))
        object.__setattr__(self, "blob_refs", tuple(self.blob_refs))


@runtime_checkable
class NativeCheckpointStore(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    async def ensure_genesis(
        self,
        *,
        operation_id: str,
        snapshot: NativeSnapshotEnvelope,
        configurable: Mapping[str, JsonValue],
    ) -> NativeSnapshotEnvelope: ...

    async def load_execution(
        self,
        *,
        run_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None = None,
    ) -> NativeExecution: ...

    async def history(
        self, *, run_id: str, limit: int | None = None
    ) -> tuple[NativeSnapshotEnvelope, ...]: ...

    async def commit_task_result(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task: NativeTask,
        execution_info: NativeExecutionInfo,
        patch: StatePatch,
        blob_refs: Sequence[str],
        consumed_interrupt_ids: Sequence[str],
        configurable: Mapping[str, JsonValue],
    ) -> None: ...

    async def commit_route_selection(
        self,
        *,
        operation_id: str,
        expected_head: str,
        source: str,
        selected_route: str,
        next_frontier_payload_hash: str,
        task_id: str,
        configurable: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...

    async def commit_frontier(
        self,
        *,
        operation_id: str,
        expected_head: str,
        state: Mapping[str, JsonValue],
        frontier: Sequence[NativeTask],
        completed_activations: Mapping[str, tuple[str, ...]],
        join_firings: Sequence[str],
        consumed_interrupt_ids: Sequence[str],
        intents: Sequence[Mapping[str, JsonValue]],
        blob_refs: Sequence[str],
        terminal_status: str | None,
        terminal_error: Mapping[str, JsonValue] | None,
        recovery_action: str | None,
        terminal_projection_prepare_id: str | None,
        configurable: Mapping[str, JsonValue],
    ) -> NativeCommitResult: ...

    async def commit_retry(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task: NativeTask,
        error: BaseException,
        next_attempt_at: float,
        configurable: Mapping[str, JsonValue],
    ) -> None: ...

    async def commit_interrupt(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task: NativeTask,
        interrupt: Mapping[str, JsonValue],
        configurable: Mapping[str, JsonValue],
    ) -> None: ...

    async def commit_failure(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task: NativeTask,
        error: BaseException,
        configurable: Mapping[str, JsonValue],
    ) -> None: ...

    async def commit_engine_failure(
        self,
        *,
        operation_id: str,
        expected_head: str,
        frontier: Sequence[NativeTask],
        error: BaseException,
        configurable: Mapping[str, JsonValue],
    ) -> None: ...

    async def read_terminal_projection_prepare(
        self,
        *,
        operation_id: str,
        expected_head: str,
        configurable: Mapping[str, JsonValue],
    ) -> TerminalProjectionPrepareReceipt | None: ...

    async def prepare_terminal_projection(
        self,
        *,
        operation_id: str,
        expected_head: str,
        descriptor_digest: str,
        input_hash: str,
        output: Mapping[str, JsonValue],
        output_hash: str,
        blob_refs: Sequence[str],
        configurable: Mapping[str, JsonValue],
    ) -> TerminalProjectionPrepareReceipt: ...


class InMemoryNativeCheckpointStore:
    """Ephemeral deterministic store for evaluation and definition tests."""

    def __init__(self) -> None:
        self.transaction_owner = object()
        self.snapshot: NativeSnapshotEnvelope | None = None
        self.pending: dict[str, StatePatch] = {}
        self.materialized_intents: dict[str, dict[str, JsonValue]] = {}
        self.route_selections: dict[str, dict[str, JsonValue]] = {}
        self.retry_attempts: dict[str, int] = {}
        self.first_attempt_times: dict[str, float] = {}
        self.pending_consumed_interrupt_ids: list[str] = []
        self.interrupt: dict[str, JsonValue] | None = None
        self.failures: dict[str, str] = {}
        self.projection_prepares: dict[str, TerminalProjectionPrepareReceipt] = {}
        self.consumed_projection_prepares: set[str] = set()

    async def ensure_genesis(
        self,
        *,
        operation_id: str,
        snapshot: NativeSnapshotEnvelope,
        configurable: Mapping[str, JsonValue],
    ) -> NativeSnapshotEnvelope:
        del operation_id, configurable
        if self.snapshot is None:
            self.snapshot = snapshot
        elif self.snapshot.run_id != snapshot.run_id:
            raise InvalidStatePatch("checkpoint_conflict", "Genesis checkpoint identity changed")
        return self.snapshot

    async def load_execution(
        self,
        *,
        run_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None = None,
    ) -> NativeExecution:
        snapshot = self.snapshot
        if snapshot is None:
            raise InvalidStatePatch("checkpoint_missing", "Ephemeral workflow has no checkpoint")
        if (
            snapshot.run_id != run_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
            or (checkpoint_id is not None and snapshot.checkpoint_id != checkpoint_id)
        ):
            raise InvalidStatePatch("checkpoint_identity_mismatch", "Checkpoint identity changed")
        if self.retry_attempts:
            snapshot = replace(
                snapshot,
                frontier=tuple(
                    replace(
                        task,
                        retry_attempt=self.retry_attempts.get(task.task_id, task.retry_attempt),
                    )
                    for task in snapshot.frontier
                ),
            )
        return NativeExecution(
            snapshot,
            dict(self.pending),
            first_attempt_times=dict(self.first_attempt_times),
            route_selections=deepcopy(self.route_selections),
            pending_consumed_interrupt_ids=tuple(self.pending_consumed_interrupt_ids),
        )

    async def history(
        self, *, run_id: str, limit: int | None = None
    ) -> tuple[NativeSnapshotEnvelope, ...]:
        if limit is not None and limit < 0:
            raise ValueError("history limit cannot be negative")
        if self.snapshot is None or self.snapshot.run_id != run_id or limit == 0:
            return ()
        return (self.snapshot,)

    async def commit_task_result(
        self,
        *,
        expected_head: str,
        task: NativeTask,
        patch: StatePatch,
        execution_info: NativeExecutionInfo,
        consumed_interrupt_ids: Sequence[str],
        **_: object,
    ) -> None:
        self._require_head(expected_head)
        if not isinstance(patch, StatePatch):
            raise TypeError("patch must be StatePatch")
        existing = self.pending.setdefault(task.task_id, patch)
        if existing != patch:
            raise InvalidStatePatch("pending_write_conflict", "Pending task result changed")
        first = execution_info.node_first_attempt_time
        if first is not None:
            existing_first = self.first_attempt_times.setdefault(task.task_id, first)
            if existing_first != first:
                raise InvalidStatePatch("pending_write_conflict", "First attempt time changed")
        for interrupt_id in consumed_interrupt_ids:
            if interrupt_id not in self.pending_consumed_interrupt_ids:
                self.pending_consumed_interrupt_ids.append(interrupt_id)

    async def commit_route_selection(
        self,
        *,
        expected_head: str,
        source: str,
        selected_route: str,
        next_frontier_payload_hash: str,
        task_id: str,
        **_: object,
    ) -> Mapping[str, JsonValue]:
        self._require_head(expected_head)
        selection: dict[str, JsonValue] = {
            "source": _identity(source, "route source"),
            "selected_route": _identity(selected_route, "selected route"),
            "next_frontier_payload_hash": _identity(
                next_frontier_payload_hash, "frontier payload hash"
            ),
        }
        task_id = _identity(task_id, "route task_id")
        existing = self.route_selections.setdefault(task_id, selection)
        if existing != selection:
            raise InvalidStatePatch("route_nondeterminism", "Route selection changed")
        return deepcopy(existing)

    async def commit_frontier(
        self,
        *,
        operation_id: str,
        expected_head: str,
        state: Mapping[str, JsonValue],
        frontier: Sequence[NativeTask],
        completed_activations: Mapping[str, tuple[str, ...]],
        join_firings: Sequence[str],
        intents: Sequence[Mapping[str, JsonValue]],
        terminal_projection_prepare_id: str | None = None,
        **_: object,
    ) -> NativeCommitResult:
        snapshot = self._require_head(expected_head)
        next_snapshot = NativeSnapshotEnvelope(
            thread_id=snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            checkpoint_id=_stable_id(snapshot.run_id, operation_id),
            parent_checkpoint_id=snapshot.checkpoint_id,
            run_id=snapshot.run_id,
            state_schema_version=snapshot.state_schema_version,
            step=snapshot.step + 1,
            state=deepcopy(dict(state)),
            frontier=tuple(frontier),
            completed_activations=dict(completed_activations),
            join_firings=tuple(join_firings),
            # Start-snapshot metadata contains immutable recovery authorities
            # (notably the terminal projection descriptor and frozen logical
            # time).  A checkpoint advance must carry those authorities
            # forward verbatim; reconstructing a partial metadata mapping here
            # would make the next reopen either silently lose the pin or fail
            # only after work has already advanced.
            metadata=deepcopy(dict(snapshot.metadata)),
        )
        event_ids: list[str] = []
        for intent in intents:
            key = intent.get("event_key", intent.get("intent_id"))
            if not isinstance(key, str) or not key:
                raise InvalidStatePatch("invalid_delivery_intent", "Intent identity is required")
            detached = deepcopy(dict(intent))
            validate_json_value(detached, path="$.intent")
            existing = self.materialized_intents.setdefault(key, detached)
            if existing != detached:
                raise InvalidStatePatch("outbox_intent_conflict", "Intent content changed")
            event_ids.append(_stable_id(snapshot.run_id, key))
        self.snapshot = next_snapshot
        self.pending.clear()
        self.first_attempt_times.clear()
        self.pending_consumed_interrupt_ids.clear()
        self.route_selections.clear()
        self.interrupt = None
        if terminal_projection_prepare_id is not None:
            if terminal_projection_prepare_id not in self.projection_prepares:
                raise InvalidStatePatch(
                    "projection_prepare_missing",
                    "Terminal projection prepare receipt is missing",
                )
            self.consumed_projection_prepares.add(terminal_projection_prepare_id)
        return NativeCommitResult(next_snapshot, tuple(event_ids))

    async def commit_retry(
        self,
        *,
        expected_head: str,
        task: NativeTask,
        next_attempt_at: float,
        **_: object,
    ) -> None:
        self._require_head(expected_head)
        if not isinstance(next_attempt_at, (int, float)) or isinstance(next_attempt_at, bool):
            raise InvalidStatePatch("invalid_native_retry", "Retry time is invalid")
        self.retry_attempts[task.task_id] = task.retry_attempt + 1

    async def commit_interrupt(
        self,
        *,
        expected_head: str,
        task: NativeTask,
        interrupt: Mapping[str, JsonValue],
        **_: object,
    ) -> None:
        self._require_head(expected_head)
        detached = deepcopy(dict(interrupt))
        validate_json_value(detached, path="$.interrupt")
        existing = self.interrupt
        if existing is not None and existing != detached:
            raise InvalidStatePatch("interrupt_conflict", "Durable interrupt changed")
        if detached.get("task_id") not in {None, task.task_id}:
            raise InvalidStatePatch(
                "interrupt_identity_mismatch", "Interrupt belongs to another task"
            )
        self.interrupt = detached

    async def commit_failure(
        self,
        *,
        expected_head: str,
        task: NativeTask,
        error: BaseException,
        **_: object,
    ) -> None:
        self._require_head(expected_head)
        self.failures.setdefault(task.task_id, type(error).__name__)

    async def commit_engine_failure(
        self,
        *,
        expected_head: str,
        frontier: Sequence[NativeTask],
        error: BaseException,
        **_: object,
    ) -> None:
        self._require_head(expected_head)
        for task in frontier:
            self.failures.setdefault(task.task_id, type(error).__name__)

    async def read_terminal_projection_prepare(
        self,
        *,
        operation_id: str,
        expected_head: str,
        configurable: Mapping[str, JsonValue],
    ) -> TerminalProjectionPrepareReceipt | None:
        del configurable
        self._require_head(expected_head)
        return self.projection_prepares.get(operation_id)

    async def prepare_terminal_projection(
        self,
        *,
        operation_id: str,
        expected_head: str,
        descriptor_digest: str,
        input_hash: str,
        output: Mapping[str, JsonValue],
        output_hash: str,
        blob_refs: Sequence[str],
        **_: object,
    ) -> TerminalProjectionPrepareReceipt:
        snapshot = self._require_head(expected_head)
        receipt = TerminalProjectionPrepareReceipt(
            operation_id=operation_id,
            run_id=snapshot.run_id,
            terminal_checkpoint_id=expected_head,
            descriptor_digest=descriptor_digest,
            input_hash=input_hash,
            output=deepcopy(dict(output)),
            output_hash=output_hash,
            blob_refs=tuple(blob_refs),
        )
        existing = self.projection_prepares.setdefault(operation_id, receipt)
        if existing != receipt:
            raise InvalidStatePatch(
                "projection_prepare_conflict",
                "Terminal projection prepare changed",
            )
        return existing

    def _require_head(self, expected_head: str) -> NativeSnapshotEnvelope:
        snapshot = self.snapshot
        if snapshot is None:
            raise InvalidStatePatch("checkpoint_missing", "Ephemeral workflow has no checkpoint")
        if snapshot.checkpoint_id != expected_head:
            raise InvalidStatePatch("checkpoint_conflict", "Checkpoint head changed")
        return snapshot


@dataclass(frozen=True, slots=True)
class TerminalIntent:
    """One host-deliverable workflow event."""

    event_type: str
    payload: dict[str, JsonValue]
    intent_id: str = ""
    event_key: str = ""
    channel: str = ""


class NativeWorkflowExecutable:
    """The single durable owner of native node tasks and graph frontiers."""

    def __init__(
        self,
        workflow: CompiledWorkflow,
        store: NativeCheckpointStore,
        *,
        terminal_projection_port: TerminalProjectionPort,
        terminal_commit_projection_port: TerminalCommitProjectionPort,
        progress_port: WorkflowProgressPort | None = None,
        observer_port: WorkflowObserverPort | None = None,
    ) -> None:
        self.workflow = workflow
        self.store = store
        self.manifest: WorkflowManifest = workflow.manifest
        if terminal_projection_port is None or terminal_commit_projection_port is None:
            raise ValueError("native workflow terminal projection ports are required")
        self.terminal_projection_port = terminal_projection_port
        self.terminal_commit_projection_port = terminal_commit_projection_port
        self.progress_port = progress_port
        self.observer_port = observer_port

    def _descriptor(self) -> TerminalProjectionDescriptor | None:
        descriptor = getattr(self.manifest, "terminal_projection_descriptor", None)
        if descriptor is not None and not isinstance(descriptor, TerminalProjectionDescriptor):
            raise InvalidStatePatch(
                "invalid_terminal_projection_descriptor",
                "Workflow manifest has an invalid terminal projection descriptor",
            )
        return descriptor

    def _descriptor_metadata(self) -> dict[str, JsonValue] | None:
        descriptor = self._descriptor()
        return None if descriptor is None else descriptor.to_dict()

    def _assert_snapshot_descriptor(self, snapshot: NativeSnapshotEnvelope) -> None:
        expected = self._descriptor_metadata()
        actual = snapshot.metadata.get("terminal_projection_descriptor")
        if actual != expected:
            raise InvalidStatePatch(
                "terminal_projection_descriptor_drift",
                "Durable terminal projection descriptor differs from the manifest",
            )

    @staticmethod
    def _assert_snapshot_time_authority(
        snapshot: NativeSnapshotEnvelope, config: Mapping[str, JsonValue]
    ) -> None:
        for name in ("logical_timestamp", "deadline"):
            expected = config.get(name)
            actual = snapshot.metadata.get(name)
            if actual != expected:
                raise InvalidStatePatch(
                    "workflow_time_authority_drift",
                    f"Durable workflow {name} differs from the invocation",
                )

    def _config(
        self,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str,
        configurable: Mapping[str, JsonValue] | None,
    ) -> dict[str, JsonValue]:
        result = deepcopy(dict(configurable or {}))
        validate_json_value(result, path="$.configurable")
        result.setdefault("logical_timestamp", 0.0)
        for key, expected in (
            ("thread_id", thread_id),
            ("checkpoint_ns", checkpoint_ns),
            ("run_id", run_id),
            ("workflow_name", self.manifest.workflow_name),
            ("workflow_version", self.manifest.workflow_version),
        ):
            if key in result and result[key] != expected:
                raise InvalidStatePatch(
                    "conflicting_runtime_identity",
                    f"Configurable {key} conflicts with execution identity",
                )
            result[key] = expected
        return result

    def _entry_task(self, run_id: str, thread_id: str, checkpoint_ns: str) -> NativeTask:
        activation = _stable_id(run_id, thread_id, checkpoint_ns, "genesis")
        node_id = self.workflow.definition.entry_node
        return NativeTask(
            task_id=_stable_id(run_id, thread_id, checkpoint_ns, "genesis", node_id),
            node_id=node_id,
            invocation_key=f"entry:{node_id}",
            activation_id=activation,
            task_path=(node_id,),
        )

    async def ainvoke(
        self,
        state: WorkflowState | object,
        context: WorkflowContext,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str = "",
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> object:
        config = self._config(thread_id, run_id, checkpoint_ns, configurable)
        if state is not None:
            if not isinstance(state, Mapping):
                raise InvalidStatePatch(
                    "invalid_initial_state",
                    "Native workflow initial state must be a mapping",
                )
            initial = deepcopy(dict(state))
            validate_json_value(initial, path="$.initial_state")
            entry = self._entry_task(run_id, thread_id, checkpoint_ns)
            genesis_id = _stable_id(run_id, thread_id, checkpoint_ns, "genesis")
            genesis = NativeSnapshotEnvelope(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=genesis_id,
                parent_checkpoint_id=None,
                run_id=run_id,
                state_schema_version=self.manifest.state_schema_version,
                step=0,
                state=cast(dict[str, JsonValue], initial),
                frontier=(entry,),
                metadata={
                    "engine_kind": _ENGINE_KIND,
                    "terminal_projection_descriptor": self._descriptor_metadata(),
                    "logical_timestamp": config.get("logical_timestamp"),
                    "deadline": config.get("deadline"),
                },
            )
            await self.store.ensure_genesis(
                operation_id=_stable_id(run_id, genesis_id, "genesis"),
                snapshot=genesis,
                configurable=config,
            )
        return await self._drive(context, thread_id, run_id, checkpoint_ns, config, {})

    async def resume(
        self,
        responses: Mapping[str, JsonValue],
        context: WorkflowContext,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str = "",
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> object:
        copied = deepcopy(dict(responses))
        if not copied:
            raise InvalidStatePatch(
                "empty_resume", "A workflow resume requires an interrupt response"
            )
        validate_json_value(copied, path="$.resume")
        return await self._drive(
            context,
            thread_id,
            run_id,
            checkpoint_ns,
            self._config(thread_id, run_id, checkpoint_ns, configurable),
            copied,
        )

    async def astream(
        self,
        state: WorkflowState | object,
        context: WorkflowContext,
        **kwargs: object,
    ) -> AsyncIterator[object]:
        yield await self.ainvoke(state, context, **kwargs)  # type: ignore[arg-type]

    async def _drive(
        self,
        context: WorkflowContext,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str,
        config: Mapping[str, JsonValue],
        responses: Mapping[str, JsonValue],
    ) -> WorkflowState:
        while True:
            execution = await self.store.load_execution(
                run_id=run_id, thread_id=thread_id, checkpoint_ns=checkpoint_ns
            )
            snapshot = execution.snapshot
            self._assert_snapshot_descriptor(snapshot)
            self._assert_snapshot_time_authority(snapshot, config)
            if not snapshot.frontier:
                return deepcopy(dict(snapshot.state))  # type: ignore[return-value]
            if snapshot.step >= self.manifest.max_supersteps:
                error = WorkflowNodeError(
                    code=WorkflowErrorCode.INVALID_STATE,
                    message_ref="workflow_engine:max_supersteps",
                )
                await self.store.commit_engine_failure(
                    operation_id=_stable_id(run_id, snapshot.checkpoint_id, "max_supersteps"),
                    expected_head=snapshot.checkpoint_id,
                    frontier=snapshot.frontier,
                    error=error,
                    configurable=config,
                )
                raise error
            patches, consumed = await self._run_frontier_tasks(
                execution, context, config, responses
            )
            try:
                ordered_writes = [
                    (task.node_id, patches[task.task_id])
                    for task in sorted(snapshot.frontier, key=lambda item: item.task_id)
                ]
                delta = self.workflow.merge_patches(ordered_writes)
                state = self.workflow.reduce_state(cast(WorkflowState, snapshot.state), delta)
                state_value = cast(dict[str, JsonValue], dict(state))
                frontier, completed, firings = await self._next_frontier(
                    snapshot, state, context, execution.route_selections, config
                )
                terminal_status, terminal_error, recovery_action = self._terminal_projection(
                    state_value, frontier
                )
                projection_prepare = await self._prepare_terminal_projection(
                    snapshot=snapshot,
                    state=state_value,
                    status=terminal_status,
                    error=terminal_error,
                    recovery_action=recovery_action,
                    config=config,
                )
                if projection_prepare is None:
                    intents = self._terminal_intent_mappings(
                        state,
                        run_id=run_id,
                        status=terminal_status,
                        error=terminal_error,
                        recovery_action=recovery_action,
                    )
                    projection_blob_refs: tuple[str, ...] = ()
                else:
                    thawed_projection = thaw_json(
                        cast("FrozenJsonValue", projection_prepare.output)
                    )
                    assert isinstance(thawed_projection, dict)
                    projected = thawed_projection.get("intents")
                    assert isinstance(projected, list)
                    projected_values = deepcopy(dict(state_value))
                    values = projected_values.get("values")
                    values = deepcopy(dict(values)) if isinstance(values, Mapping) else {}
                    values["delivery_intents"] = deepcopy(projected)
                    projected_values["values"] = values
                    intents = self._terminal_intent_mappings(
                        projected_values,
                        run_id=run_id,
                        status=terminal_status,
                        error=terminal_error,
                        recovery_action=recovery_action,
                    )
                    projection_blob_refs = projection_prepare.blob_refs
                operation_id = _stable_id(
                    run_id,
                    snapshot.checkpoint_id,
                    "frontier",
                    *(task.task_id for task in snapshot.frontier),
                    "next",
                    *(task.task_id for task in frontier),
                    canonical_json(state_value),
                    canonical_json(list(intents)),
                )
                try:
                    result = await self.store.commit_frontier(
                        operation_id=operation_id,
                        expected_head=snapshot.checkpoint_id,
                        state=state_value,
                        frontier=frontier,
                        completed_activations=completed,
                        join_firings=firings,
                        consumed_interrupt_ids=tuple(consumed),
                        intents=intents,
                        blob_refs=tuple(
                            sorted(
                                set(self._state_blob_refs(state_value)) | set(projection_blob_refs)
                            )
                        ),
                        terminal_status=terminal_status,
                        terminal_error=terminal_error,
                        recovery_action=recovery_action,
                        terminal_projection_prepare_id=(
                            projection_prepare.operation_id
                            if projection_prepare is not None
                            else None
                        ),
                        configurable=config,
                    )
                except asyncio.CancelledError as exc:
                    raise _CommitUncertain from exc
                except Exception as exc:
                    raise _CommitUncertain from exc
            except _CommitUncertain as exc:
                assert exc.__cause__ is not None
                raise exc.__cause__
            except asyncio.CancelledError:
                raise
            except (InvalidStatePatch, StateMergeConflict, WorkflowNodeError) as exc:
                error = (
                    exc
                    if isinstance(exc, WorkflowNodeError)
                    else WorkflowNodeError(
                        code=WorkflowErrorCode.INVALID_STATE,
                        message_ref=f"workflow_engine:{exc.code}",
                    )
                )
                await self.store.commit_engine_failure(
                    operation_id=_stable_id(run_id, snapshot.checkpoint_id, "engine_failure"),
                    expected_head=snapshot.checkpoint_id,
                    frontier=snapshot.frontier,
                    error=error,
                    configurable=config,
                )
                raise error
            except Exception:  # noqa: BLE001 - durable public failure boundary
                error = WorkflowNodeError(
                    code=WorkflowErrorCode.INVALID_STATE,
                    message_ref="workflow_engine:frontier_failure",
                )
                await self.store.commit_engine_failure(
                    operation_id=_stable_id(run_id, snapshot.checkpoint_id, "engine_failure"),
                    expected_head=snapshot.checkpoint_id,
                    frontier=snapshot.frontier,
                    error=error,
                    configurable=config,
                )
                raise error from None
            if not result.snapshot.frontier:
                return deepcopy(dict(result.snapshot.state))  # type: ignore[return-value]

    @staticmethod
    def _terminal_projection_request(
        *,
        descriptor: TerminalProjectionDescriptor,
        state: Mapping[str, JsonValue],
        run_id: str,
        workflow_name: str,
        workflow_version: str,
        status: str,
        error: Mapping[str, JsonValue] | None,
        recovery_action: str | None,
    ) -> dict[str, JsonValue]:
        _assert_terminal_request_factory_spec()
        sources: dict[str, JsonValue] = {
            "literal:1": 1,
            "descriptor.capability_id": descriptor.capability_id,
            "descriptor.version": descriptor.version,
            "descriptor.digest": descriptor.digest,
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "run_id": run_id,
            "status": status,
            "error": deepcopy(dict(error)) if error is not None else None,
            "recovery_action": recovery_action,
            "state": deepcopy(dict(state)),
        }
        bindings = _TERMINAL_REQUEST_FACTORY_SPEC["bindings"]
        assert isinstance(bindings, list)
        request: dict[str, JsonValue] = {}
        for binding in bindings:
            assert isinstance(binding, list) and len(binding) == 2
            field, source = binding
            assert isinstance(field, str) and isinstance(source, str)
            request[field] = deepcopy(sources[source])
        if list(request) != _TERMINAL_REQUEST_SCHEMA["fields"]:
            raise InvalidStatePatch(
                "terminal_projection_request_factory_drift",
                "Terminal projection request factory differs from its schema",
            )
        validate_json_value(request, path="$.terminal_projection.request")
        return request

    async def _prepare_terminal_projection(
        self,
        *,
        snapshot: NativeSnapshotEnvelope,
        state: Mapping[str, JsonValue],
        status: str | None,
        error: Mapping[str, JsonValue] | None,
        recovery_action: str | None,
        config: Mapping[str, JsonValue],
    ) -> TerminalProjectionPrepareReceipt | None:
        descriptor = self._descriptor()
        if status is None or descriptor is None:
            return None
        request = self._terminal_projection_request(
            descriptor=descriptor,
            state=state,
            run_id=snapshot.run_id,
            workflow_name=self.manifest.workflow_name,
            workflow_version=self.manifest.workflow_version,
            status=status,
            error=error,
            recovery_action=recovery_action,
        )
        input_hash = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        operation_id = _stable_id(snapshot.run_id, snapshot.checkpoint_id, descriptor.digest)
        existing = await self.store.read_terminal_projection_prepare(
            operation_id=operation_id,
            expected_head=snapshot.checkpoint_id,
            configurable=config,
        )
        if existing is not None:
            if (
                existing.run_id != snapshot.run_id
                or existing.terminal_checkpoint_id != snapshot.checkpoint_id
                or existing.descriptor_digest != descriptor.digest
                or existing.input_hash != input_hash
            ):
                raise InvalidStatePatch(
                    "projection_prepare_conflict",
                    "Terminal projection prepare input changed",
                )
            return existing
        projector = self.terminal_commit_projection_port.lookup(
            self.manifest.workflow_name,
            self.manifest.workflow_version,
            descriptor,
        )
        if projector is None:
            raise InvalidStatePatch(
                "terminal_projection_unavailable",
                "Pinned terminal commit projector is unavailable",
            )
        logical_timestamp = snapshot.metadata.get("logical_timestamp", 0.0)
        deadline = snapshot.metadata.get("deadline")
        if (
            isinstance(logical_timestamp, bool)
            or not isinstance(logical_timestamp, (int, float))
            or (
                deadline is not None
                and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)))
            )
        ):
            raise InvalidStatePatch(
                "invalid_projection_time",
                "Projection timestamp and deadline must be frozen numbers",
            )
        context = ProjectionContext(
            snapshot.run_id,
            self.manifest.workflow_name,
            self.manifest.workflow_version,
            snapshot.checkpoint_id,
            state,
            float(logical_timestamp),
            float(deadline) if deadline is not None else None,
        )
        raw_output = await projector(request, context)
        output, blob_refs = _validate_projection_output(raw_output, status)
        output_hash = hashlib.sha256(canonical_json(output).encode()).hexdigest()
        try:
            return await self.store.prepare_terminal_projection(
                operation_id=operation_id,
                expected_head=snapshot.checkpoint_id,
                descriptor_digest=descriptor.digest,
                input_hash=input_hash,
                output=output,
                output_hash=output_hash,
                blob_refs=blob_refs,
                configurable=config,
            )
        except asyncio.CancelledError as exc:
            raise _CommitUncertain from exc
        except Exception as exc:
            raise _CommitUncertain from exc

    @staticmethod
    def _terminal_projection(
        state: Mapping[str, JsonValue], frontier: Sequence[NativeTask]
    ) -> tuple[str | None, dict[str, JsonValue] | None, str | None]:
        if frontier:
            return None, None, None
        values = state.get("values")
        values = values if isinstance(values, Mapping) else {}
        domain_status = str(values.get("terminal_status") or "").strip().lower()
        if domain_status in {"", "success", "completed"}:
            return "completed", None, None
        if domain_status in {"cancelled", "canceled"}:
            return "cancelled", None, None
        raw_error = values.get("terminal_error")
        if domain_status == "error":
            if isinstance(raw_error, Mapping):
                error: dict[str, JsonValue] = {
                    "code": str(raw_error.get("code") or "workflow_domain_error")[:80],
                    "message": str(raw_error.get("user_message") or "Workflow could not complete.")[
                        :500
                    ],
                }
                recovery = str(raw_error.get("recovery_action") or "Adjust the input and retry.")[
                    :500
                ]
            else:
                error = {
                    "code": "workflow_domain_error",
                    "message": "Workflow could not complete.",
                }
                recovery = "Adjust the input and retry."
            return "failed", error, recovery
        return (
            "failed",
            {
                "code": "invalid_domain_terminal_status",
                "message": "Workflow returned an invalid terminal status.",
            },
            "Retry the workflow and inspect its trace if the problem persists.",
        )

    @staticmethod
    def _state_blob_refs(state: Mapping[str, JsonValue]) -> tuple[str, ...]:
        raw = state.get("blob_refs", [])
        if not isinstance(raw, list):
            raise InvalidStatePatch("invalid_blob_refs", "blob_refs must be a list")
        refs = tuple(sorted({str(value) for value in raw}))
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in refs
        ):
            raise InvalidStatePatch("invalid_blob_refs", "blob_refs must contain SHA-256 digests")
        return refs

    @staticmethod
    def _patch_blob_refs(patch: StatePatch) -> tuple[str, ...]:
        values = patch.to_dict()
        return NativeWorkflowExecutable._state_blob_refs(values) if "blob_refs" in values else ()

    def _policy(self, context: WorkflowContext) -> NativeExecutionPolicy:
        raw = context.ports.get("native_execution_policy")
        if raw is None:
            return NativeExecutionPolicy()
        if isinstance(raw, NativeExecutionPolicy):
            return raw
        if isinstance(raw, Mapping):
            return NativeExecutionPolicy(int(raw.get("max_parallel_tasks", 1)))
        raise InvalidStatePatch(
            "invalid_native_execution_policy", "native execution policy is invalid"
        )

    def _identity(
        self,
        snapshot: NativeSnapshotEnvelope,
        task: NativeTask,
        first_attempt_time: float | None,
    ) -> tuple[NativeExecutionInfo, NodeExecutionIdentity]:
        first = first_attempt_time or time.time()
        info = NativeExecutionInfo(
            snapshot.thread_id,
            snapshot.run_id,
            snapshot.checkpoint_id,
            snapshot.checkpoint_ns,
            task.task_id,
            task.retry_attempt,
            first,
            task.activation_id,
            task.invocation_key,
        )
        return info, NodeExecutionIdentity.from_execution_info(
            workflow_name=self.manifest.workflow_name,
            workflow_version=self.manifest.workflow_version,
            node_id=task.node_id,
            execution_info=info,
            state=snapshot.state,
        )

    async def _run_task_worker(
        self,
        snapshot: NativeSnapshotEnvelope,
        task: NativeTask,
        context: WorkflowContext,
        config: Mapping[str, JsonValue],
        responses: Mapping[str, JsonValue],
        first_attempt_time: float | None,
    ) -> NodeTaskOutcome:
        info, identity = self._identity(snapshot, task, first_attempt_time)
        observer = self.observer_port
        if observer is not None:
            await observer.node_started(identity)
        control = ExecutionControl(task.task_id, responses)
        try:
            with bind_execution_control(control):
                patch = await self.workflow.run_node(
                    task.node_id,
                    cast(WorkflowState, snapshot.state),
                    context,
                    info,
                )
            if self.progress_port is not None:
                patch = self.progress_port.freeze_patch(
                    patch,
                    identity=identity,
                    first_attempt_time=float(info.node_first_attempt_time or time.time()),
                    finished_at=time.time(),
                )
                if not isinstance(patch, StatePatch):
                    raise InvalidStatePatch(
                        "invalid_progress_patch",
                        "Workflow progress port must return StatePatch",
                    )
                self.workflow.validate_patch(task.node_id, patch)
            try:
                await self.store.commit_task_result(
                    operation_id=_stable_id(
                        snapshot.run_id, snapshot.checkpoint_id, "task", task.task_id
                    ),
                    expected_head=snapshot.checkpoint_id,
                    task=task,
                    execution_info=info,
                    patch=patch,
                    blob_refs=self._patch_blob_refs(patch),
                    consumed_interrupt_ids=tuple(control.consumed_interrupt_ids),
                    configurable=config,
                )
            except asyncio.CancelledError as exc:
                raise _CommitUncertain from exc
            except Exception as exc:
                raise _CommitUncertain from exc
            if observer is not None:
                await observer.node_finished(identity, "succeeded_pending")
            return NodeTaskOutcome(
                task, patch, tuple(control.consumed_interrupt_ids), None, identity
            )
        except WorkflowSuspended as exc:
            return NodeTaskOutcome(
                task,
                None,
                tuple(control.consumed_interrupt_ids),
                exc,
                identity,
            )
        except asyncio.CancelledError:
            if observer is not None:
                await observer.node_finished(identity, "cancelled", error=asyncio.CancelledError())
            raise
        except _CommitUncertain as exc:
            assert exc.__cause__ is not None
            raise exc.__cause__
        except WorkflowNodeError as exc:
            return NodeTaskOutcome(
                task,
                None,
                tuple(control.consumed_interrupt_ids),
                exc,
                identity,
            )
        except Exception:  # noqa: BLE001 - raw node exceptions never cross durability
            error = WorkflowNodeError(
                code=WorkflowErrorCode.PERMANENT,
                message_ref="workflow_engine:unexpected_node_failure",
                node_id=task.node_id,
            )
            return NodeTaskOutcome(
                task,
                None,
                tuple(control.consumed_interrupt_ids),
                error,
                identity,
            )

    async def _run_frontier_tasks(
        self,
        execution: NativeExecution,
        context: WorkflowContext,
        config: Mapping[str, JsonValue],
        responses: Mapping[str, JsonValue],
    ) -> tuple[dict[str, StatePatch], list[str]]:
        snapshot = execution.snapshot
        patches = dict(execution.pending_results)
        pending = [
            task
            for task in sorted(snapshot.frontier, key=lambda item: item.task_id)
            if task.task_id not in patches
        ]
        outcomes: list[NodeTaskOutcome] = []
        if pending:
            nodes = [self.workflow.node(task.node_id) for task in pending]
            parallel = all(
                str(node.dispatch) == "parallel" and not node.barrier and not node.interrupt_capable
                for node in nodes
            )
            if parallel and self._policy(context).max_parallel_tasks > 1:
                semaphore = asyncio.Semaphore(self._policy(context).max_parallel_tasks)

                async def bounded(task: NativeTask) -> NodeTaskOutcome:
                    async with semaphore:
                        return await self._run_task_worker(
                            snapshot,
                            task,
                            context,
                            config,
                            responses,
                            execution.first_attempt_times.get(task.task_id),
                        )

                children = [asyncio.create_task(bounded(task)) for task in pending]
                try:
                    gathered = await asyncio.gather(*children, return_exceptions=True)
                except asyncio.CancelledError:
                    for child in children:
                        child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                    raise
                child_errors = [
                    (task.task_id, value)
                    for task, value in zip(pending, gathered, strict=True)
                    if isinstance(value, BaseException)
                ]
                if child_errors:
                    _, selected_error = min(child_errors, key=lambda item: item[0])
                    raise selected_error
                outcomes = [value for value in gathered if isinstance(value, NodeTaskOutcome)]
            else:
                for task in pending:
                    outcomes.append(
                        await self._run_task_worker(
                            snapshot,
                            task,
                            context,
                            config,
                            responses,
                            execution.first_attempt_times.get(task.task_id),
                        )
                    )

        errors = [outcome for outcome in outcomes if outcome.error is not None]
        suspends = [outcome for outcome in errors if isinstance(outcome.error, WorkflowSuspended)]
        if suspends:
            selected = min(suspends, key=lambda item: item.task.task_id)
            node = self.workflow.node(selected.task.node_id)
            if len(snapshot.frontier) != 1 or str(node.dispatch) == "parallel":
                error = WorkflowNodeError(
                    code=WorkflowErrorCode.INVALID_STATE,
                    message_ref="workflow_engine:parallel_interrupt_invariant",
                    node_id=selected.task.node_id,
                )
            else:
                assert isinstance(selected.error, WorkflowSuspended)
                await self.store.commit_interrupt(
                    operation_id=_stable_id(
                        snapshot.run_id,
                        snapshot.checkpoint_id,
                        "interrupt",
                        selected.task.task_id,
                    ),
                    expected_head=snapshot.checkpoint_id,
                    task=selected.task,
                    interrupt=selected.error.interrupt.to_dict(),
                    configurable=config,
                )
                if self.observer_port is not None:
                    await self.observer_port.node_finished(
                        selected.identity, "waiting", error=selected.error
                    )
                raise selected.error
            errors = [replace(selected, error=error)]

        failures: list[tuple[int, str, NodeTaskOutcome, WorkflowNodeError, bool]] = []
        for outcome in errors:
            error = outcome.error
            if not isinstance(error, WorkflowNodeError):
                error = WorkflowNodeError(
                    code=WorkflowErrorCode.PERMANENT,
                    message_ref="workflow_engine:unexpected_node_failure",
                    node_id=outcome.task.node_id,
                )
            node = self.workflow.node(outcome.task.node_id)
            retryable = bool(
                error.retryable
                and error.code.value in node.retry_policy.retryable_codes
                and outcome.task.retry_attempt < node.retry_policy.max_attempts
            )
            failures.append(
                (1 if retryable else 0, outcome.task.task_id, outcome, error, retryable)
            )
        if failures:
            _, _, selected, error, retryable = min(failures, key=lambda item: (item[0], item[1]))
            if retryable:
                node = self.workflow.node(selected.task.node_id)
                delay = min(
                    node.retry_policy.max_delay_seconds,
                    node.retry_policy.initial_delay_seconds
                    * (node.retry_policy.backoff_multiplier ** (selected.task.retry_attempt - 1)),
                )
                await self.store.commit_retry(
                    operation_id=_stable_id(
                        snapshot.run_id,
                        snapshot.checkpoint_id,
                        "retry",
                        selected.task.task_id,
                        selected.task.retry_attempt,
                    ),
                    expected_head=snapshot.checkpoint_id,
                    task=selected.task,
                    error=error,
                    next_attempt_at=time.time() + delay,
                    configurable=config,
                )
                status = "retryable"
            else:
                await self.store.commit_failure(
                    operation_id=_stable_id(
                        snapshot.run_id,
                        snapshot.checkpoint_id,
                        "failure",
                        selected.task.task_id,
                    ),
                    expected_head=snapshot.checkpoint_id,
                    task=selected.task,
                    error=error,
                    configurable=config,
                )
                status = "failed"
            if self.observer_port is not None:
                await self.observer_port.node_finished(selected.identity, status, error=error)
            raise error

        consumed = list(execution.pending_consumed_interrupt_ids)
        for outcome in outcomes:
            assert outcome.patch is not None
            patches[outcome.task.task_id] = outcome.patch
            consumed.extend(outcome.consumed_interrupt_ids)
        return patches, consumed

    async def _next_frontier(
        self,
        snapshot: NativeSnapshotEnvelope,
        state: WorkflowState,
        context: WorkflowContext,
        route_selections: Mapping[str, Mapping[str, JsonValue]],
        config: Mapping[str, JsonValue],
    ) -> tuple[tuple[NativeTask, ...], Mapping[str, tuple[str, ...]], tuple[str, ...]]:
        completed = {key: list(value) for key, value in snapshot.completed_activations.items()}
        firings = set(snapshot.join_firings)
        candidates: list[tuple[str, NativeTask, str]] = []
        tasks_by_node = {task.node_id: task for task in snapshot.frontier}
        for task in sorted(snapshot.frontier, key=lambda item: item.task_id):
            conditional = self.workflow.conditional_for(task.node_id)
            targets: list[str] = []
            if conditional is not None:
                stored = route_selections.get(task.task_id)
                if stored is not None:
                    route = str(stored["selected_route"])
                else:
                    route_state = deepcopy(dict(state))
                    route_state["logical_timestamp"] = cast(
                        JsonValue, snapshot.metadata["logical_timestamp"]
                    )
                    route = await self.workflow.route(
                        conditional,
                        cast(WorkflowState, route_state),
                        context,
                        NativeExecutionInfo(
                            snapshot.thread_id,
                            snapshot.run_id,
                            snapshot.checkpoint_id,
                            snapshot.checkpoint_ns,
                            task.task_id,
                            task.retry_attempt,
                            None,
                            task.activation_id,
                            task.invocation_key,
                        ),
                    )
                    target = conditional.routes[route]
                    try:
                        await self.store.commit_route_selection(
                            operation_id=_stable_id(
                                snapshot.run_id,
                                snapshot.checkpoint_id,
                                task.task_id,
                                task.node_id,
                            ),
                            expected_head=snapshot.checkpoint_id,
                            source=task.node_id,
                            selected_route=route,
                            next_frontier_payload_hash=_stable_id(task.task_id, route, target),
                            task_id=task.task_id,
                            configurable=config,
                        )
                    except asyncio.CancelledError as exc:
                        raise _CommitUncertain from exc
                    except Exception as exc:
                        raise _CommitUncertain from exc
                targets.append(conditional.routes[route])
            else:
                targets.extend(self.workflow.single_targets(task.node_id))
            activation = _stable_id(snapshot.checkpoint_id, task.task_id, task.join_epoch)
            for target in targets:
                if target != "__end__":
                    candidates.append((target, task, activation))
            for edge in self.workflow.join_edges_for(task.node_id):
                key = f"{edge.target}:{task.join_epoch}"
                token = f"{task.node_id}:{task.task_id}"
                values = completed.setdefault(key, [])
                if token not in values:
                    values.append(token)
                source_tasks = {value.split(":", 1)[0]: value.split(":", 1)[1] for value in values}
                if all(source in source_tasks for source in edge.sources) and key not in firings:
                    firings.add(key)
                    synthetic = tasks_by_node.get(edge.sources[0], task)
                    candidates.append(
                        (
                            edge.target,
                            synthetic,
                            _stable_id(
                                snapshot.run_id,
                                key,
                                *(source_tasks[source] for source in edge.sources),
                            ),
                        )
                    )
        next_tasks: dict[str, NativeTask] = {}
        for target, source, activation in sorted(
            candidates, key=lambda item: (item[0], item[1].task_id)
        ):
            epoch = source.join_epoch + (
                1 if self.workflow.is_cycle_edge(source.node_id, target) else 0
            )
            self.workflow.validate_loop_budget(source.node_id, target, state)
            invocation = f"{target}:{activation}:{epoch}"
            task_id = _stable_id(snapshot.run_id, snapshot.checkpoint_id, invocation)
            next_tasks[task_id] = NativeTask(
                task_id,
                target,
                invocation,
                activation,
                epoch,
                (*source.task_path, target),
            )
        return (
            tuple(next_tasks[key] for key in sorted(next_tasks)),
            {key: tuple(sorted(value)) for key, value in sorted(completed.items())},
            tuple(sorted(firings)),
        )

    def _project_terminal_public_state(
        self, state: Mapping[str, object], status: str | None
    ) -> Mapping[str, object]:
        if status is None:
            return state
        values = state.get("values")
        if not isinstance(values, Mapping) or "terminal_public" not in values:
            return state
        projected = self.terminal_projection_port.project_public(
            self.manifest.workflow_name,
            self.manifest.workflow_version,
            values["terminal_public"],
            status,
        )
        copied = deepcopy(dict(state))
        copied_values = deepcopy(dict(values))
        if projected is None:
            copied_values.pop("terminal_public", None)
        else:
            detached = deepcopy(dict(projected))
            validate_json_value(detached, path="$.terminal_public")
            copied_values["terminal_public"] = detached
        copied["values"] = copied_values
        return copied

    def _terminal_intent_mappings(
        self,
        state: Mapping[str, object],
        *,
        run_id: str,
        status: str | None,
        error: Mapping[str, JsonValue] | None,
        recovery_action: str | None,
    ) -> tuple[dict[str, JsonValue], ...]:
        projected_state = self._project_terminal_public_state(state, status)
        intents = self.terminal_intents(
            projected_state,
            run_id=run_id,
            status=status,
            error=error,
            recovery_action=recovery_action,
        )
        return tuple(
            {
                "intent_id": intent.intent_id or f"{run_id}:run-terminal",
                "event_key": intent.event_key or "run:terminal",
                "event_type": intent.event_type,
                "channel": intent.channel or "final",
                "payload": deepcopy(intent.payload),
            }
            for intent in intents
        )

    @staticmethod
    def terminal_intents(
        state: Mapping[str, object],
        *,
        run_id: str,
        status: str | None,
        error: Mapping[str, JsonValue] | None,
        recovery_action: str | None,
    ) -> tuple[TerminalIntent, ...]:
        if status is None:
            return ()
        if status not in _TERMINAL_STATUSES:
            raise InvalidStatePatch("invalid_terminal_status", "terminal status is not allowed")
        if not isinstance(run_id, str) or not run_id.strip():
            raise InvalidStatePatch("invalid_terminal_run", "terminal run_id is required")
        if not isinstance(state, Mapping):
            raise InvalidStatePatch("invalid_terminal_state", "terminal state must be an object")
        projected_error = _optional_json_object(error, "terminal error")
        if recovery_action is not None and (
            not isinstance(recovery_action, str) or not recovery_action.strip()
        ):
            raise InvalidStatePatch(
                "invalid_terminal_recovery", "terminal recovery action is invalid"
            )

        values = state.get("values", {})
        if not isinstance(values, Mapping):
            raise InvalidStatePatch("invalid_terminal_values", "terminal values must be an object")
        raw_public = values.get("terminal_public")
        if raw_public is None:
            payload: dict[str, JsonValue] = {
                "kind": "workflow_terminal",
                "status": status,
                "error": projected_error,
                "recovery_action": recovery_action,
                "card": None,
            }
        else:
            public = _terminal_public(raw_public)
            card: dict[str, JsonValue] = {
                "run_id": run_id.strip(),
                "status": status,
                "error": deepcopy(projected_error),
                "recovery_action": recovery_action,
                **deepcopy(public),
            }
            payload = {
                "kind": "final",
                "status": status,
                "error": projected_error,
                "recovery_action": recovery_action,
                "card": card,
                **public,
            }
        validate_json_value(payload, path="$.terminal")
        generic = _generic_delivery_intents(values.get("delivery_intents"), status)
        return (
            *generic,
            TerminalIntent(
                "workflow.final",
                payload,
                f"{run_id}:run-terminal",
                "run:terminal",
                "final",
            ),
        )


def _terminal_public(raw: object) -> dict[str, JsonValue]:
    if not isinstance(raw, Mapping):
        raise InvalidStatePatch("invalid_terminal_public", "terminal_public must be an object")
    keys = set(raw)
    if not all(isinstance(key, str) for key in keys):
        raise InvalidStatePatch("invalid_terminal_public", "terminal_public keys must be strings")
    if not _REQUIRED_PUBLIC_KEYS.issubset(keys) or not keys.issubset(_PUBLIC_KEYS):
        raise InvalidStatePatch("invalid_terminal_public", "terminal_public has an invalid schema")

    raw_metrics = raw["metrics"]
    if not isinstance(raw_metrics, Mapping) or any(
        not isinstance(key, str) or key not in _METRIC_KEYS for key in raw_metrics
    ):
        raise InvalidStatePatch("invalid_terminal_public", "terminal_public metrics are invalid")
    metrics: dict[str, JsonValue] = {}
    for key, value in raw_metrics.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_METRIC:
            raise InvalidStatePatch(
                "invalid_terminal_public", "terminal_public metric is out of bounds"
            )
        metrics[str(key)] = value

    diagnostics = _bounded_allowlist(
        raw["diagnostic_codes"],
        allowed=_DIAGNOSTIC_CODES,
        maximum=_MAX_DIAGNOSTICS,
        field="terminal diagnostic codes",
        unique=False,
    )
    skipped = _bounded_allowlist(
        raw["skipped_stage_ids"],
        allowed=_STAGE_IDS,
        maximum=_MAX_SKIPPED_STAGES,
        field="terminal skipped stages",
        unique=True,
    )
    result: dict[str, JsonValue] = {
        "metrics": metrics,
        "diagnostic_codes": diagnostics,
        "skipped_stage_ids": skipped,
    }
    if "retry_action_id" in raw:
        retry_action = raw["retry_action_id"]
        if not isinstance(retry_action, str) or retry_action not in _RETRY_ACTION_IDS:
            raise InvalidStatePatch("invalid_terminal_public", "terminal retry action is invalid")
        result["retry_action_id"] = retry_action
    return result


_DELIVERY_TEXT = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
_PRIVATE_DELIVERY_KEYS = frozenset(
    {
        "topic",
        "raw_query",
        "prompt",
        "messages",
        "credentials",
        "secrets",
        "private_state",
    }
)


def _delivery_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(character not in _DELIVERY_TEXT for character in value)
    ):
        raise InvalidStatePatch("invalid_delivery_intent", f"delivery {field} is invalid")
    return value


def _validate_delivery_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise InvalidStatePatch("invalid_delivery_intent", "delivery payload must be an object")
    payload = deepcopy(dict(value))
    validate_json_value(payload, path="$.delivery.payload")
    if len(canonical_json(payload).encode("utf-8")) > 32 * 1024:
        raise InvalidStatePatch("invalid_delivery_intent", "delivery payload exceeds 32KiB")
    items = 0

    def visit(current: JsonValue, depth: int) -> None:
        nonlocal items
        if depth > 8:
            raise InvalidStatePatch("invalid_delivery_intent", "delivery payload exceeds depth 8")
        if isinstance(current, dict):
            items += len(current)
            for key, child in current.items():
                lowered = key.lower()
                if lowered in _PRIVATE_DELIVERY_KEYS or lowered.startswith(("_private", "secret_")):
                    raise InvalidStatePatch(
                        "invalid_delivery_intent",
                        "delivery payload contains a private key",
                    )
                visit(child, depth + 1)
        elif isinstance(current, list):
            items += len(current)
            for child in current:
                visit(child, depth + 1)
        if items > 512:
            raise InvalidStatePatch("invalid_delivery_intent", "delivery payload exceeds 512 items")

    visit(payload, 1)
    return payload


def _generic_delivery_intents(raw: object, status: str) -> tuple[TerminalIntent, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > 16:
        raise InvalidStatePatch(
            "invalid_delivery_intent", "delivery_intents must contain at most 16 items"
        )
    normalized: list[tuple[str, str, str, str, TerminalIntent]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "intent_id",
            "kind",
            "channel",
            "payload",
        }:
            raise InvalidStatePatch("invalid_delivery_intent", "delivery intent schema differs")
        intent_id = _delivery_text(item["intent_id"], "intent_id", 128)
        if intent_id in seen:
            raise InvalidStatePatch("invalid_delivery_intent", "delivery intent_id must be unique")
        seen.add(intent_id)
        kind = _delivery_text(item["kind"], "kind", 64)
        if kind == "final":
            raise InvalidStatePatch("invalid_delivery_intent", "workflow.final is engine-owned")
        channel = _delivery_text(item["channel"], "channel", 64)
        raw_payload = _validate_delivery_payload(item["payload"])
        payload = {**raw_payload, "status": status}
        digest = hashlib.sha256(canonical_json(raw_payload).encode()).hexdigest()
        normalized.append(
            (
                intent_id,
                kind,
                channel,
                digest,
                TerminalIntent(
                    f"workflow.{kind}",
                    payload,
                    intent_id,
                    f"terminal:{intent_id}",
                    channel,
                ),
            )
        )
    normalized.sort(key=lambda value: value[:4])
    return tuple(value[4] for value in normalized)


def _validate_projection_output(
    raw: Mapping[str, JsonValue], status: str
) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
    if not isinstance(raw, Mapping) or set(raw) != {"intents", "blob_refs"}:
        raise InvalidStatePatch(
            "invalid_terminal_projection",
            "Terminal projection output must contain only intents and blob_refs",
        )
    intents = raw.get("intents")
    _generic_delivery_intents(intents, status)
    refs = raw.get("blob_refs")
    if not isinstance(refs, list):
        raise InvalidStatePatch(
            "invalid_terminal_projection", "Terminal blob_refs must be an array"
        )
    normalized: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            raise InvalidStatePatch(
                "invalid_terminal_projection", "Terminal blob ref must be a string"
            )
        digest = ref.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidStatePatch(
                "invalid_terminal_projection",
                "Terminal blob ref must contain a lowercase SHA-256 digest",
            )
        normalized.append(digest)
    if normalized != sorted(set(normalized)):
        raise InvalidStatePatch(
            "invalid_terminal_projection", "Terminal blob refs must be canonical"
        )
    output: dict[str, JsonValue] = {
        "intents": deepcopy(intents),
        "blob_refs": list(normalized),
    }
    validate_json_value(output, path="$.terminal_projection.output")
    return output, tuple(normalized)


def _bounded_allowlist(
    raw: object,
    *,
    allowed: frozenset[str],
    maximum: int,
    field: str,
    unique: bool,
) -> list[JsonValue]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
        or len(raw) > maximum
        or not all(isinstance(value, str) for value in raw)
    ):
        raise InvalidStatePatch("invalid_terminal_public", f"{field} are invalid")
    values = list(raw)
    if any(value not in allowed for value in values) or (
        unique and len(values) != len(set(values))
    ):
        raise InvalidStatePatch("invalid_terminal_public", f"{field} are invalid")
    return values


def _optional_json_object(
    value: Mapping[str, JsonValue] | None, field: str
) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidStatePatch("invalid_terminal_error", f"{field} must be an object")
    mutable = deepcopy(dict(value))
    try:
        validate_json_value(mutable, path="$.terminal.error")
    except (TypeError, ValueError) as exc:
        raise InvalidStatePatch("invalid_terminal_error", f"{field} must contain JSON") from exc
    return mutable


__all__ = (
    "TERMINAL_REQUEST_FACTORY_HASH",
    "TERMINAL_REQUEST_SCHEMA_HASH",
    "InMemoryNativeCheckpointStore",
    "NativeCheckpointStore",
    "NativeCommitResult",
    "NativeExecution",
    "NativeExecutionInfo",
    "NativeExecutionPolicy",
    "NativeSnapshotEnvelope",
    "NativeTask",
    "NativeWorkflowExecutable",
    "NodeTaskOutcome",
    "ProjectionContext",
    "TerminalCommitProjectionPort",
    "TerminalCommitProjector",
    "TerminalIntent",
    "TerminalProjectionDescriptor",
    "TerminalProjectionPort",
    "TerminalProjectionPrepareReceipt",
    "WorkflowObserverPort",
    "WorkflowProgressPort",
)
