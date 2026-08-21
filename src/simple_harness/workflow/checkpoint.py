# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable workflow checkpoint authority contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast

from simple_harness.contracts import JsonValue, RunId, validate_json_value
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.sqlite.database import Database
from simple_harness.execution.uow import ExecutionLease

from .contracts import WorkflowRunStatus
from .execution_ports import (
    CheckpointExecutionAdapter,
    ResumeCommitBinding,
    WorkflowActivation,
    WorkflowBlobReferencePort,
    WorkflowExecutionPorts,
    WorkflowOperationConflict,
    WorkflowTransaction,
)
from .lease import WorkflowLease


@dataclass(frozen=True, slots=True)
class PendingInterrupt:
    interrupt_id: str
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        payload = copy.deepcopy(dict(self.payload))
        validate_json_value(payload)
        object.__setattr__(self, "payload", MappingProxyType(payload))


@dataclass(frozen=True, slots=True)
class WorkflowCheckpoint:
    run_id: str
    workflow_name: str
    workflow_version: str
    manifest_hash: str
    status: WorkflowRunStatus
    active_nodes: tuple[str, ...]
    values: Mapping[str, JsonValue]
    attempts: Mapping[str, int]
    loop_counters: Mapping[str, int]
    pending_interrupt: PendingInterrupt | None
    revision: int
    error_code: str | None
    recovery_action: str | None
    checkpoint_namespace: str = "native"
    completed_nodes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = copy.deepcopy(dict(self.values))
        validate_json_value(values)
        object.__setattr__(self, "values", MappingProxyType(values))
        object.__setattr__(self, "attempts", MappingProxyType(dict(self.attempts)))
        object.__setattr__(self, "loop_counters", MappingProxyType(dict(self.loop_counters)))
        object.__setattr__(self, "active_nodes", tuple(self.active_nodes))
        object.__setattr__(self, "completed_nodes", tuple(self.completed_nodes))

    @property
    def checkpoint_id(self) -> str:
        return f"{self.run_id}:{self.revision}"

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "workflow_version": self.workflow_version,
            "manifest_hash": self.manifest_hash,
            "status": self.status,
            "active_nodes": self.active_nodes,
            "values": copy.deepcopy(dict(self.values)),
            "attempts": dict(self.attempts),
            "loop_counters": dict(self.loop_counters),
            "pending_interrupt": self.pending_interrupt,
            "revision": self.revision,
            "error_code": self.error_code,
            "recovery_action": self.recovery_action,
            "checkpoint_namespace": self.checkpoint_namespace,
            "completed_nodes": self.completed_nodes,
        }


class WorkflowCheckpointPort(Protocol):
    transaction_owner: object

    def bind_execution_adapter(self, adapter: CheckpointExecutionAdapter) -> None: ...

    async def load(self, run_id: str) -> WorkflowCheckpoint | None: ...

    async def history(
        self, run_id: str, *, limit: int | None = None
    ) -> tuple[WorkflowCheckpoint, ...]: ...

    async def commit(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        expected_revision: int | None,
        lease: WorkflowLease,
        transaction: WorkflowTransaction,
    ) -> WorkflowCheckpoint: ...

    async def fork(
        self,
        *,
        source: WorkflowCheckpoint,
        target_run_id: str,
        values: Mapping[str, JsonValue],
        transaction: WorkflowTransaction,
    ) -> WorkflowCheckpoint: ...


def _stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _strict_object(value: object, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise WorkflowOperationConflict(f"{name} must be a JSON object")
    validate_json_value(value, path=f"$.{name}")
    return cast(dict[str, JsonValue], copy.deepcopy(value))


def _config_identity(configurable: Mapping[str, JsonValue]) -> tuple[str, int, float]:
    owner = configurable.get("workflow_owner_id")
    epoch = configurable.get("workflow_lease_epoch")
    now = configurable.get("logical_timestamp")
    if not isinstance(owner, str) or not owner:
        raise WorkflowOperationConflict("workflow owner identity is required")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise WorkflowOperationConflict("workflow lease epoch is required")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise WorkflowOperationConflict("frozen workflow logical timestamp is required")
    return owner, epoch, float(now)


class SqliteNativeCheckpointStore:
    """Single durable Native store backed by the canonical execution transaction."""

    __slots__ = ("_blob_references", "_database", "_execution_ports")

    def __init__(
        self,
        execution_ports: WorkflowExecutionPorts,
        *,
        blob_references: WorkflowBlobReferencePort,
    ) -> None:
        owner = execution_ports.unit_of_work.transaction_owner
        if not isinstance(owner, Database):
            raise TypeError("SQLite Native store requires the canonical Database owner")
        if execution_ports.checkpoint.transaction_owner is not owner:
            raise ValueError("Native store authorities have different transaction owners")
        self._execution_ports = execution_ports
        self._database = owner
        self._blob_references = blob_references

    @property
    def transaction_owner(self) -> object:
        return self._database

    @staticmethod
    def _activation_authority(
        configurable: Mapping[str, JsonValue],
    ) -> WorkflowActivation:
        activation_raw = configurable.get("workflow_activation")
        if not isinstance(activation_raw, Mapping):
            raise WorkflowOperationConflict("workflow activation is incomplete")
        run_id = cast(str, activation_raw.get("run_id"))
        owner_id = cast(str, activation_raw.get("owner_id"))
        runtime_namespace = cast(str, activation_raw.get("runtime_namespace"))
        runtime_epoch = cast(int, activation_raw.get("runtime_epoch"))
        expires_at = cast(float, activation_raw.get("expires_at"))
        run_fence_epoch = cast(int, activation_raw.get("run_fence_epoch"))
        workflow_namespace = cast(str, activation_raw.get("workflow_namespace"))
        workflow_epoch = cast(int, activation_raw.get("workflow_epoch"))
        return WorkflowActivation(
            ExecutionLease(
                run_id,
                runtime_namespace,
                owner_id,
                runtime_epoch,
                expires_at,
            ),
            RunFenceLease(RunId(run_id), run_fence_epoch, owner_id, runtime_epoch),
            WorkflowLease(
                run_id,
                owner_id,
                workflow_epoch,
                expires_at,
                runtime_epoch,
                workflow_namespace,
            ),
        )

    @classmethod
    def _resume_authority(
        cls, configurable: Mapping[str, JsonValue]
    ) -> tuple[ResumeCommitBinding, WorkflowActivation] | None:
        binding_raw = configurable.get("resume_binding")
        if binding_raw is None:
            return None
        if not isinstance(binding_raw, Mapping):
            raise WorkflowOperationConflict("resume binding is incomplete")
        binding = ResumeCommitBinding(
            cast(str, binding_raw.get("receipt_id")),
            cast(int, binding_raw.get("expected_receipt_version")),
            cast(int, binding_raw.get("target_run_revision")),
            cast(str, binding_raw.get("request_fingerprint")),
        )
        return binding, cls._activation_authority(configurable)

    async def _release_activation(
        self,
        transaction: WorkflowTransaction,
        *,
        configurable: Mapping[str, JsonValue],
        outcome: Mapping[str, JsonValue],
        now: float,
    ) -> None:
        connection = self._connection(transaction)
        run_id = cast(str, configurable.get("run_id"))
        row = connection.execute("SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowOperationConflict("workflow Run disappeared before release")
        await self._execution_ports.lifecycle.release_activation(
            transaction,
            self._activation_authority(configurable),
            int(row["version"]),
            outcome,
            now=now,
        )

    async def _settle_resume(
        self,
        transaction: WorkflowTransaction,
        *,
        configurable: Mapping[str, JsonValue],
        committed_checkpoint: str,
        outcome: Mapping[str, JsonValue],
        now: float,
    ) -> None:
        authority = self._resume_authority(configurable)
        if authority is None:
            return
        binding, activation = authority
        await self._execution_ports.lifecycle.settle_resume(
            transaction,
            binding,
            activation,
            committed_checkpoint,
            outcome,
            now=now,
        )

    async def _atomic(self, label, operation):  # type: ignore[no-untyped-def]
        return await self._execution_ports.unit_of_work.run_atomic(
            operation, fault_label=f"workflow_native:{label}"
        )

    @staticmethod
    def _connection(transaction: WorkflowTransaction) -> sqlite3.Connection:
        connection = getattr(transaction, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise WorkflowOperationConflict("Native store received a non-SQLite transaction")
        return connection

    @staticmethod
    def _assert_writer(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        namespace: str,
        expected_head: str | None,
        configurable: Mapping[str, JsonValue],
    ) -> tuple[str, int, float]:
        owner, epoch, now = _config_identity(configurable)
        lease = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, namespace),
        ).fetchone()
        if (
            lease is None
            or str(lease["owner_id"]) != owner
            or int(lease["epoch"]) != epoch
            or float(lease["expires_at"]) <= now
        ):
            raise WorkflowOperationConflict("stale workflow lease")
        latest = connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints WHERE run_id=? AND namespace=? ORDER BY"
            " version DESC LIMIT 1",
            (run_id, namespace),
        ).fetchone()
        actual_head = None if latest is None else str(latest["checkpoint_id"])
        if actual_head != expected_head:
            raise WorkflowOperationConflict("workflow checkpoint head changed")
        run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise WorkflowOperationConflict("workflow Run is missing")
        if str(run["state"]) in {"completed", "failed", "cancelled"}:
            raise WorkflowOperationConflict("terminal workflow Run rejects stale writes")
        return owner, epoch, now

    @staticmethod
    def _write_native_operation(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        run_id: str,
        namespace: str,
        base_checkpoint_id: str,
        operation_kind: str,
        identity_key: str,
        payload: Mapping[str, JsonValue],
        now: float,
    ) -> dict[str, JsonValue]:
        detached = _strict_object(dict(payload), "native_operation")
        payload_json = json.dumps(
            detached, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        existing = connection.execute(
            "SELECT * FROM workflow_native_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            if (
                str(existing["run_id"]) != run_id
                or str(existing["namespace"]) != namespace
                or str(existing["base_checkpoint_id"]) != base_checkpoint_id
                or str(existing["operation_kind"]) != operation_kind
                or str(existing["identity_key"]) != identity_key
                or str(existing["payload_hash"]) != payload_hash
            ):
                raise WorkflowOperationConflict("Native operation replay changed")
            return cast(dict[str, JsonValue], json.loads(str(existing["payload_json"])))
        connection.execute(
            """
            INSERT INTO workflow_native_operations(
                operation_id,run_id,namespace,base_checkpoint_id,operation_kind,
                identity_key,payload_json,payload_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id,
                run_id,
                namespace,
                base_checkpoint_id,
                operation_kind,
                identity_key,
                payload_json,
                payload_hash,
                now,
            ),
        )
        return detached

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row):  # type: ignore[no-untyped-def]
        from .native import NativeSnapshotEnvelope, NativeTask

        raw = _strict_object(json.loads(str(row["checkpoint_json"])), "checkpoint")
        frontier_raw = raw.get("frontier", [])
        if not isinstance(frontier_raw, list):
            raise WorkflowOperationConflict("corrupt Native frontier")
        frontier = tuple(NativeTask(**cast(dict, item)) for item in frontier_raw)
        return NativeSnapshotEnvelope(
            thread_id=cast(str, raw["thread_id"]),
            checkpoint_ns=cast(str, raw["checkpoint_ns"]),
            checkpoint_id=cast(str, raw["checkpoint_id"]),
            parent_checkpoint_id=cast(str | None, raw.get("parent_checkpoint_id")),
            run_id=cast(str, raw["run_id"]),
            state_schema_version=cast(int, raw["state_schema_version"]),
            step=cast(int, raw["step"]),
            state=cast(dict, raw["state"]),
            frontier=frontier,
            completed_activations={
                str(key): tuple(cast(list[str], value))
                for key, value in cast(dict, raw.get("completed_activations", {})).items()
            },
            join_firings=tuple(cast(list[str], raw.get("join_firings", []))),
            node_writes=cast(dict, raw.get("node_writes", {})),
            interrupt=cast(dict | None, raw.get("interrupt")),
            metadata=cast(dict, raw.get("metadata", {})),
            engine_kind=cast(str, raw.get("engine_kind", "simple-harness-native")),
            snapshot_version=cast(int, raw.get("snapshot_version", 1)),
        )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        *,
        snapshot,  # type: ignore[no-untyped-def]
        lease_epoch: int,
        version: int,
        now: float,
    ) -> None:
        raw = snapshot.to_dict()
        checkpoint_json = json.dumps(
            raw, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        connection.execute(
            """
            INSERT INTO workflow_checkpoints(
                checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,
                lease_epoch,version,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                snapshot.checkpoint_id,
                snapshot.run_id,
                snapshot.checkpoint_ns,
                checkpoint_json,
                hashlib.sha256(checkpoint_json.encode()).hexdigest(),
                lease_epoch,
                version,
                now,
            ),
        )

    async def ensure_genesis(self, *, operation_id: str, snapshot, configurable):  # type: ignore[no-untyped-def]
        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE run_id=? AND namespace=? ORDER BY version"
                " DESC LIMIT 1",
                (snapshot.run_id, snapshot.checkpoint_ns),
            ).fetchone()
            if existing is not None:
                loaded = self._snapshot_from_row(existing)
                if loaded.to_dict() != snapshot.to_dict():
                    raise WorkflowOperationConflict("Native genesis replay changed")
                return loaded
            _, epoch, now = self._assert_writer(
                connection,
                run_id=snapshot.run_id,
                namespace=snapshot.checkpoint_ns,
                expected_head=None,
                configurable=configurable,
            )
            self._write_native_operation(
                connection,
                operation_id=operation_id,
                run_id=snapshot.run_id,
                namespace=snapshot.checkpoint_ns,
                base_checkpoint_id=snapshot.checkpoint_id,
                operation_kind="genesis",
                identity_key=snapshot.checkpoint_id,
                payload={
                    "snapshot_hash": (
                        hashlib.sha256(
                            json.dumps(snapshot.to_dict(), sort_keys=True).encode()
                        ).hexdigest()
                    )
                },
                now=now,
            )
            self._insert_snapshot(
                connection, snapshot=snapshot, lease_epoch=epoch, version=0, now=now
            )
            await self._execution_ports.checkpoint.mark_running_on_claim(
                transaction,
                run_id=snapshot.run_id,
                checkpoint_namespace=snapshot.checkpoint_ns,
                lease_epoch=epoch,
                claim_epoch=epoch,
                now=now,
            )
            return snapshot

        return await self._atomic("genesis", operation)

    async def load_execution(
        self,
        *,
        run_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None = None,
    ):  # type: ignore[no-untyped-def]
        from dataclasses import replace

        from .contracts import StatePatch
        from .native import NativeExecution

        row = self._database.connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE run_id=? AND namespace=? "
            "AND (? IS NULL OR checkpoint_id=?) ORDER BY version DESC LIMIT 1",
            (run_id, checkpoint_ns, checkpoint_id, checkpoint_id),
        ).fetchone()
        if row is None:
            raise WorkflowOperationConflict("Native checkpoint is missing")
        snapshot = self._snapshot_from_row(row)
        if snapshot.thread_id != thread_id:
            raise WorkflowOperationConflict("Native thread identity changed")
        operations = self._database.connection.execute(
            "SELECT operation_kind,identity_key,payload_json FROM workflow_native_operations WHERE"
            " run_id=? AND namespace=? AND base_checkpoint_id=? ORDER BY created_at,operation_id",
            (run_id, checkpoint_ns, snapshot.checkpoint_id),
        ).fetchall()
        pending = {}
        first_attempts = {}
        routes = {}
        consumed: list[str] = []
        retries: dict[str, tuple[int, float]] = {}
        durable_failure: dict[str, JsonValue] | None = None
        for operation in operations:
            payload = _strict_object(json.loads(str(operation["payload_json"])), "native_operation")
            kind = str(operation["operation_kind"])
            key = str(operation["identity_key"])
            if kind == "task_result":
                pending[key] = StatePatch(cast(dict, payload["patch"]))
                first = payload.get("first_attempt_time")
                if isinstance(first, (int, float)) and not isinstance(first, bool):
                    first_attempts[key] = float(first)
                for interrupt_id in cast(list[str], payload.get("consumed_interrupt_ids", [])):
                    if interrupt_id not in consumed:
                        consumed.append(interrupt_id)
            elif kind == "route":
                routes[key] = cast(dict, payload["selection"])
            elif kind == "retry":
                retries[key] = (
                    cast(int, payload["retry_attempt"]),
                    cast(float, payload["next_attempt_at"]),
                )
            elif kind == "interrupt":
                snapshot = replace(snapshot, interrupt=cast(dict, payload["interrupt"]))
            elif kind in {"failure", "engine_failure"}:
                durable_failure = payload
        if durable_failure is not None:
            from .errors import WorkflowNodeError

            raise WorkflowNodeError(
                code=cast(str, durable_failure["error_code"]),
                message_ref=cast(str, durable_failure["message_ref"]),
                retryable=cast(bool, durable_failure["retryable"]),
                node_id=cast(str | None, durable_failure.get("node_id")),
            )
        if retries:
            snapshot = replace(
                snapshot,
                frontier=tuple(
                    (
                        replace(
                            task,
                            retry_attempt=retries[task.task_id][0],
                            next_attempt_at=retries[task.task_id][1],
                        )
                        if task.task_id in retries
                        else task
                    )
                    for task in snapshot.frontier
                ),
            )
        return NativeExecution(snapshot, pending, first_attempts, routes, tuple(consumed))

    async def history(self, *, run_id: str, limit: int | None = None):  # type: ignore[no-untyped-def]
        if limit is not None and limit < 0:
            raise ValueError("history limit cannot be negative")
        if limit == 0:
            return ()
        parameters: list[object] = [run_id]
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            parameters.append(limit)
        rows = self._database.connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE run_id=? ORDER BY version DESC"
            + limit_clause,
            parameters,
        ).fetchall()
        return tuple(self._snapshot_from_row(row) for row in reversed(rows))

    async def _commit_operation(
        self,
        *,
        operation_id: str,
        expected_head: str,
        kind: str,
        identity_key: str,
        payload: Mapping[str, JsonValue],
        configurable: Mapping[str, JsonValue],
        blob_refs: Sequence[str] = (),
        resume_outcome: Mapping[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        run_id = cast(str, configurable.get("run_id"))
        namespace = cast(str, configurable.get("checkpoint_ns", ""))

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing = connection.execute(
                "SELECT created_at FROM workflow_native_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                return self._write_native_operation(
                    connection,
                    operation_id=operation_id,
                    run_id=run_id,
                    namespace=namespace,
                    base_checkpoint_id=expected_head,
                    operation_kind=kind,
                    identity_key=identity_key,
                    payload=payload,
                    now=float(existing["created_at"]),
                )
            _, _, now = self._assert_writer(
                connection,
                run_id=run_id,
                namespace=namespace,
                expected_head=expected_head,
                configurable=configurable,
            )
            await self._blob_references.validate_references(
                transaction,
                run_id=run_id,
                owner_kind=kind,
                owner_id=identity_key,
                blob_refs=blob_refs,
            )
            saved = self._write_native_operation(
                connection,
                operation_id=operation_id,
                run_id=run_id,
                namespace=namespace,
                base_checkpoint_id=expected_head,
                operation_kind=kind,
                identity_key=identity_key,
                payload=payload,
                now=now,
            )
            if resume_outcome is not None:
                if self._resume_authority(configurable) is None:
                    await self._release_activation(
                        transaction,
                        configurable=configurable,
                        outcome=resume_outcome,
                        now=now,
                    )
                else:
                    await self._settle_resume(
                        transaction,
                        configurable=configurable,
                        committed_checkpoint=expected_head,
                        outcome=resume_outcome,
                        now=now,
                    )
            return saved

        return await self._atomic(kind, operation)

    async def commit_task_result(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task,
        execution_info,
        patch,
        blob_refs,
        consumed_interrupt_ids,
        configurable,
    ):  # type: ignore[no-untyped-def]
        await self._commit_operation(
            operation_id=operation_id,
            expected_head=expected_head,
            kind="task_result",
            identity_key=task.task_id,
            payload={
                "patch": patch.to_dict(),
                "first_attempt_time": execution_info.node_first_attempt_time,
                "consumed_interrupt_ids": list(consumed_interrupt_ids),
            },
            configurable=configurable,
            blob_refs=blob_refs,
        )

    async def commit_route_selection(
        self,
        *,
        operation_id: str,
        expected_head: str,
        source: str,
        selected_route: str,
        next_frontier_payload_hash: str,
        task_id: str,
        configurable,
    ):  # type: ignore[no-untyped-def]
        selection = cast(
            dict[str, JsonValue],
            {
                "source": source,
                "selected_route": selected_route,
                "next_frontier_payload_hash": next_frontier_payload_hash,
            },
        )
        saved = await self._commit_operation(
            operation_id=operation_id,
            expected_head=expected_head,
            kind="route",
            identity_key=task_id,
            payload={"selection": selection},
            configurable=configurable,
        )
        return cast(dict[str, JsonValue], saved["selection"])

    async def commit_retry(
        self,
        *,
        operation_id: str,
        expected_head: str,
        task,
        error,
        next_attempt_at: float,
        configurable,
    ):  # type: ignore[no-untyped-def]
        run_id = cast(str, configurable.get("run_id"))
        namespace = cast(str, configurable.get("checkpoint_ns", ""))
        payload = {
            "retry_attempt": task.retry_attempt + 1,
            "next_attempt_at": next_attempt_at,
            "error_type": type(error).__name__,
        }

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing = connection.execute(
                "SELECT created_at FROM workflow_native_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                self._write_native_operation(
                    connection,
                    operation_id=operation_id,
                    run_id=run_id,
                    namespace=namespace,
                    base_checkpoint_id=expected_head,
                    operation_kind="retry",
                    identity_key=task.task_id,
                    payload=payload,
                    now=float(existing["created_at"]),
                )
                return
            _, _, now = self._assert_writer(
                connection,
                run_id=run_id,
                namespace=namespace,
                expected_head=expected_head,
                configurable=configurable,
            )
            self._write_native_operation(
                connection,
                operation_id=operation_id,
                run_id=run_id,
                namespace=namespace,
                base_checkpoint_id=expected_head,
                operation_kind="retry",
                identity_key=task.task_id,
                payload=payload,
                now=now,
            )
            authority = self._resume_authority(configurable)
            if authority is not None:
                binding, activation = authority
                await self._execution_ports.lifecycle.defer_resume_retry(
                    transaction,
                    binding,
                    activation,
                    operation_id,
                    task.retry_attempt + 1,
                    next_attempt_at,
                    now=now,
                )

        await self._atomic("retry", operation)

    async def commit_interrupt(
        self, *, operation_id: str, expected_head: str, task, interrupt, configurable
    ):  # type: ignore[no-untyped-def]
        request = copy.deepcopy(dict(interrupt))
        interrupt_id = cast(str, request.get("interrupt_id"))
        run_id = cast(str, configurable["run_id"])
        namespace = cast(str, configurable.get("checkpoint_ns", ""))

        async def commit_and_open(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing = connection.execute(
                "SELECT created_at FROM workflow_native_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is None:
                _, epoch, now = self._assert_writer(
                    connection,
                    run_id=run_id,
                    namespace=namespace,
                    expected_head=expected_head,
                    configurable=configurable,
                )
            else:
                _, epoch, now = _config_identity(configurable)
            saved = self._write_native_operation(
                connection,
                operation_id=operation_id,
                run_id=run_id,
                namespace=namespace,
                base_checkpoint_id=expected_head,
                operation_kind="interrupt",
                identity_key=task.task_id,
                payload={"interrupt": request},
                now=now if existing is None else float(existing["created_at"]),
            )
            durable_request = cast(dict[str, JsonValue], saved["interrupt"])
            outcome = await self._execution_ports.checkpoint.open_decision(
                transaction,
                run_id=run_id,
                interrupt_id=interrupt_id,
                request=durable_request,
                checkpoint_namespace=namespace,
                checkpoint_id=expected_head,
                lease_epoch=epoch,
                now=now,
            )
            if existing is None:
                waiting_outcome = {
                    "status": "waiting",
                    "checkpoint_id": expected_head,
                    "decision_id": interrupt_id,
                }
                if self._resume_authority(configurable) is None:
                    await self._release_activation(
                        transaction,
                        configurable=configurable,
                        outcome=waiting_outcome,
                        now=now,
                    )
                else:
                    await self._settle_resume(
                        transaction,
                        configurable=configurable,
                        committed_checkpoint=expected_head,
                        outcome=waiting_outcome,
                        now=now,
                    )
            return outcome

        await self._atomic("interrupt", commit_and_open)

    async def commit_failure(
        self, *, operation_id: str, expected_head: str, task, error, configurable
    ):  # type: ignore[no-untyped-def]
        code = getattr(error, "code", "permanent")
        await self._commit_operation(
            operation_id=operation_id,
            expected_head=expected_head,
            kind="failure",
            identity_key=task.task_id,
            payload={
                "error_type": type(error).__name__,
                "error_code": str(getattr(code, "value", code)),
                "message_ref": str(getattr(error, "message_ref", "workflow_engine:permanent")),
                "retryable": bool(getattr(error, "retryable", False)),
                "node_id": getattr(error, "node_id", task.node_id),
            },
            configurable=configurable,
            resume_outcome={
                "status": "failed",
                "checkpoint_id": expected_head,
                "error_code": str(getattr(code, "value", code)),
            },
        )

    async def commit_engine_failure(
        self, *, operation_id: str, expected_head: str, frontier, error, configurable
    ):  # type: ignore[no-untyped-def]
        code = getattr(error, "code", "invalid_state")
        await self._commit_operation(
            operation_id=operation_id,
            expected_head=expected_head,
            kind="engine_failure",
            identity_key=",".join(sorted(task.task_id for task in frontier)),
            payload={
                "error_type": type(error).__name__,
                "error_code": str(getattr(code, "value", code)),
                "message_ref": str(getattr(error, "message_ref", "workflow_engine:invalid_state")),
                "retryable": bool(getattr(error, "retryable", False)),
                "node_id": getattr(error, "node_id", None),
            },
            configurable=configurable,
            resume_outcome={
                "status": "failed",
                "checkpoint_id": expected_head,
                "error_code": str(getattr(code, "value", code)),
            },
        )

    async def read_terminal_projection_prepare(
        self, *, operation_id: str, expected_head: str, configurable
    ):  # type: ignore[no-untyped-def]
        run_id = cast(str, configurable["run_id"])
        namespace = cast(str, configurable.get("checkpoint_ns", ""))

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            row = connection.execute(
                "SELECT * FROM terminal_projection_prepares WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is not None and (
                str(row["run_id"]) != run_id
                or str(row["namespace"]) != namespace
                or str(row["expected_head_id"]) != expected_head
            ):
                raise WorkflowOperationConflict("terminal projection prepare identity changed")
            return None if row is None else self._projection_receipt(row)

        return await self._atomic("projection_read", operation)

    @staticmethod
    def _projection_receipt(row):  # type: ignore[no-untyped-def]
        from .native import TerminalProjectionPrepareReceipt

        return TerminalProjectionPrepareReceipt(
            operation_id=str(row["operation_id"]),
            run_id=str(row["run_id"]),
            terminal_checkpoint_id=str(row["terminal_checkpoint_id"]),
            descriptor_digest=str(row["descriptor_digest"]),
            input_hash=str(row["input_hash"]),
            output=cast(dict, json.loads(str(row["output_json"]))),
            output_hash=str(row["output_hash"]),
            blob_refs=tuple(cast(list[str], json.loads(str(row["blob_refs_json"])))),
        )

    async def prepare_terminal_projection(
        self,
        *,
        operation_id: str,
        expected_head: str,
        descriptor_digest: str,
        input_hash: str,
        output,
        output_hash: str,
        blob_refs,
        configurable,
    ):  # type: ignore[no-untyped-def]
        run_id = cast(str, configurable["run_id"])
        namespace = cast(str, configurable.get("checkpoint_ns", ""))

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing = connection.execute(
                "SELECT * FROM terminal_projection_prepares WHERE operation_id=?", (operation_id,)
            ).fetchone()
            output_json = json.dumps(
                dict(output),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            actual_output_hash = hashlib.sha256(output_json.encode()).hexdigest()
            if output_hash != actual_output_hash:
                raise WorkflowOperationConflict("terminal projection output hash mismatch")
            if any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in (descriptor_digest, input_hash)
            ):
                raise WorkflowOperationConflict("terminal projection hashes are invalid")
            refs = tuple(blob_refs)
            if existing is not None:
                receipt = self._projection_receipt(existing)
                if (
                    receipt.run_id,
                    receipt.terminal_checkpoint_id,
                    receipt.descriptor_digest,
                    receipt.input_hash,
                    receipt.output_hash,
                    str(existing["output_json"]),
                    receipt.blob_refs,
                ) != (
                    run_id,
                    expected_head,
                    descriptor_digest,
                    input_hash,
                    output_hash,
                    output_json,
                    refs,
                ):
                    raise WorkflowOperationConflict("terminal projection prepare changed")
                if str(existing["namespace"]) != namespace:
                    raise WorkflowOperationConflict("terminal projection namespace changed")
                return receipt
            owner, epoch, now = self._assert_writer(
                connection,
                run_id=run_id,
                namespace=namespace,
                expected_head=expected_head,
                configurable=configurable,
            )
            await self._blob_references.validate_references(
                transaction,
                run_id=run_id,
                owner_kind="terminal_projection",
                owner_id=operation_id,
                blob_refs=refs,
            )
            connection.execute(
                """INSERT INTO terminal_projection_prepares(
                    operation_id,run_id,namespace,terminal_checkpoint_id,descriptor_digest,
                    input_hash,output_json,output_hash,blob_refs_json,owner_id,lease_epoch,
                    expected_head_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    run_id,
                    namespace,
                    expected_head,
                    descriptor_digest,
                    input_hash,
                    output_json,
                    output_hash,
                    json.dumps(list(refs), separators=(",", ":")),
                    owner,
                    epoch,
                    expected_head,
                    now,
                ),
            )
            return self._projection_receipt(
                connection.execute(
                    "SELECT * FROM terminal_projection_prepares WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            )

        return await self._atomic("projection_prepare", operation)

    async def commit_frontier(
        self,
        *,
        operation_id: str,
        expected_head: str,
        state,
        frontier,
        completed_activations,
        join_firings,
        consumed_interrupt_ids,
        intents,
        blob_refs,
        terminal_status,
        terminal_error,
        recovery_action,
        terminal_projection_prepare_id,
        configurable,
    ):  # type: ignore[no-untyped-def]
        from .native import NativeCommitResult, NativeSnapshotEnvelope

        run_id = cast(str, configurable["run_id"])
        namespace = cast(str, configurable.get("checkpoint_ns", ""))
        request_payload: dict[str, JsonValue] = {
            "expected_head": expected_head,
            "state": copy.deepcopy(dict(state)),
            "frontier": [task.to_dict() for task in frontier],
            "completed_activations": {
                str(key): list(value) for key, value in completed_activations.items()
            },
            "join_firings": list(join_firings),
            "consumed_interrupt_ids": list(consumed_interrupt_ids),
            "intents": [copy.deepcopy(dict(intent)) for intent in intents],
            "blob_refs": list(blob_refs),
            "terminal_status": terminal_status,
            "terminal_error": (
                None if terminal_error is None else copy.deepcopy(dict(terminal_error))
            ),
            "recovery_action": recovery_action,
            "terminal_projection_prepare_id": terminal_projection_prepare_id,
        }
        request_json = json.dumps(
            request_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()

        async def operation(transaction: WorkflowTransaction):  # type: ignore[no-untyped-def]
            connection = self._connection(transaction)
            existing_operation = connection.execute(
                "SELECT payload_json FROM workflow_native_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing_operation is not None:
                replay = _strict_object(
                    json.loads(str(existing_operation["payload_json"])), "frontier_receipt"
                )
                if replay.get("request_hash") != request_hash:
                    raise WorkflowOperationConflict("frontier operation replay changed")
                next_id = cast(str, replay["next_checkpoint_id"])
                row = connection.execute(
                    "SELECT * FROM workflow_checkpoints WHERE checkpoint_id=?", (next_id,)
                ).fetchone()
                if row is None:
                    raise WorkflowOperationConflict("frontier receipt lacks checkpoint")
                snapshot = self._snapshot_from_row(row)
                return NativeCommitResult(snapshot, tuple(cast(list[str], replay["event_ids"])))
            _, epoch, now = self._assert_writer(
                connection,
                run_id=run_id,
                namespace=namespace,
                expected_head=expected_head,
                configurable=configurable,
            )
            current_row = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE checkpoint_id=? AND run_id=? AND"
                " namespace=?",
                (expected_head, run_id, namespace),
            ).fetchone()
            assert current_row is not None
            current = self._snapshot_from_row(current_row)
            current_logical_time = current.metadata.get("logical_timestamp")
            current_deadline = current.metadata.get("deadline")
            if current_logical_time != configurable.get(
                "logical_timestamp"
            ) or current_deadline != configurable.get("deadline"):
                raise WorkflowOperationConflict("frozen workflow time authority changed")
            prepare_row: sqlite3.Row | None = None
            if terminal_projection_prepare_id is not None:
                from .native import (
                    NativeWorkflowExecutable,
                    TerminalProjectionDescriptor,
                )

                prepare_row = connection.execute(
                    "SELECT * FROM terminal_projection_prepares WHERE operation_id=?",
                    (terminal_projection_prepare_id,),
                ).fetchone()
                if (
                    prepare_row is None
                    or str(prepare_row["run_id"]) != run_id
                    or str(prepare_row["namespace"]) != namespace
                    or str(prepare_row["terminal_checkpoint_id"]) != expected_head
                    or prepare_row["consumed_at"] is not None
                ):
                    raise WorkflowOperationConflict("terminal projection prepare is not consumable")
                descriptor_raw = current.metadata.get("terminal_projection_descriptor")
                if not isinstance(descriptor_raw, dict):
                    raise WorkflowOperationConflict("terminal projection descriptor is missing")
                descriptor = TerminalProjectionDescriptor(
                    capability_id=cast(str, descriptor_raw["capability_id"]),
                    version=cast(str, descriptor_raw["version"]),
                    projector_fingerprint=cast(str, descriptor_raw["projector_fingerprint"]),
                    request_schema_hash=cast(str, descriptor_raw["request_schema_hash"]),
                    request_factory_hash=cast(str, descriptor_raw["request_factory_hash"]),
                )
                if descriptor.digest != str(prepare_row["descriptor_digest"]):
                    raise WorkflowOperationConflict("terminal projection descriptor changed")
                projection_request = NativeWorkflowExecutable._terminal_projection_request(
                    descriptor=descriptor,
                    state=state,
                    run_id=run_id,
                    workflow_name=cast(str, configurable.get("workflow_name")),
                    workflow_version=cast(str, configurable.get("workflow_version")),
                    status=cast(str, terminal_status),
                    error=terminal_error,
                    recovery_action=recovery_action,
                )
                projection_input_hash = hashlib.sha256(
                    json.dumps(
                        projection_request,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if projection_input_hash != str(prepare_row["input_hash"]):
                    raise WorkflowOperationConflict("terminal projection input authority changed")
                projection_output = _strict_object(
                    json.loads(str(prepare_row["output_json"])), "terminal_projection"
                )
                projected_state = copy.deepcopy(dict(state))
                projected_values = projected_state.get("values")
                projected_values = (
                    copy.deepcopy(dict(projected_values))
                    if isinstance(projected_values, Mapping)
                    else {}
                )
                projected_values["delivery_intents"] = cast(
                    JsonValue, copy.deepcopy(projection_output["intents"])
                )
                projected_state["values"] = projected_values
                expected_terminal_intents = NativeWorkflowExecutable.terminal_intents(
                    projected_state,
                    run_id=run_id,
                    status=terminal_status,
                    error=terminal_error,
                    recovery_action=recovery_action,
                )
                expected_mappings = [
                    {
                        "intent_id": intent.intent_id or f"{run_id}:run-terminal",
                        "event_key": intent.event_key or "run:terminal",
                        "event_type": intent.event_type,
                        "channel": intent.channel or "final",
                        "payload": copy.deepcopy(intent.payload),
                    }
                    for intent in expected_terminal_intents
                ]
                if json.dumps(
                    expected_mappings, sort_keys=True, separators=(",", ":")
                ) != json.dumps(
                    [dict(intent) for intent in intents],
                    sort_keys=True,
                    separators=(",", ":"),
                ):
                    raise WorkflowOperationConflict("terminal projection intents changed")
                expected_blob_refs = tuple(
                    sorted(
                        set(NativeWorkflowExecutable._state_blob_refs(state))
                        | set(cast(list[str], projection_output["blob_refs"]))
                    )
                )
                if expected_blob_refs != tuple(blob_refs):
                    raise WorkflowOperationConflict("terminal projection blob refs changed")
            await self._blob_references.validate_references(
                transaction,
                run_id=run_id,
                owner_kind="workflow_checkpoint",
                owner_id=operation_id,
                blob_refs=blob_refs,
            )
            next_id = _stable_id(run_id, operation_id)
            metadata = copy.deepcopy(dict(current.metadata))
            snapshot = NativeSnapshotEnvelope(
                thread_id=current.thread_id,
                checkpoint_ns=namespace,
                checkpoint_id=next_id,
                parent_checkpoint_id=current.checkpoint_id,
                run_id=run_id,
                state_schema_version=current.state_schema_version,
                step=current.step + 1,
                state=copy.deepcopy(dict(state)),
                frontier=tuple(frontier),
                completed_activations=dict(completed_activations),
                join_firings=tuple(join_firings),
                metadata=metadata,
            )
            next_version = int(current_row["version"]) + 1
            self._insert_snapshot(
                connection, snapshot=snapshot, lease_epoch=epoch, version=next_version, now=now
            )
            event_ids = []
            for intent in intents:
                intent_id = cast(str, intent.get("event_key", intent.get("intent_id")))
                outcome = await self._execution_ports.checkpoint.materialize_intent(
                    transaction,
                    run_id=run_id,
                    intent_id=intent_id,
                    intent=intent,
                    checkpoint_namespace=namespace,
                    checkpoint_id=next_id,
                    lease_epoch=epoch,
                    now=now,
                )
                assert isinstance(outcome, dict)
                event_ids.append(cast(str, outcome["event_id"]))
            if consumed_interrupt_ids:
                responses = {}
                for decision_id in consumed_interrupt_ids:
                    row = connection.execute(
                        "SELECT response_json FROM decisions WHERE decision_id=?", (decision_id,)
                    ).fetchone()
                    if row is None or row["response_json"] is None:
                        raise WorkflowOperationConflict("consumed decision has no response")
                    responses[decision_id] = json.loads(str(row["response_json"]))
                await self._execution_ports.checkpoint.consume_decisions(
                    transaction,
                    run_id=run_id,
                    checkpoint_id=next_id,
                    decision_ids=consumed_interrupt_ids,
                    responses=responses,
                    checkpoint_namespace=namespace,
                    lease_epoch=epoch,
                    now=now,
                )
            if terminal_projection_prepare_id is not None:
                if prepare_row is None:
                    raise WorkflowOperationConflict(
                        "terminal projection prepare disappeared before consume"
                    )
                await self._blob_references.validate_references(
                    transaction,
                    run_id=run_id,
                    owner_kind="terminal_projection",
                    owner_id=terminal_projection_prepare_id,
                    blob_refs=tuple(json.loads(str(prepare_row["blob_refs_json"]))),
                )
                connection.execute(
                    "UPDATE terminal_projection_prepares SET consumed_at=? WHERE operation_id=? AND"
                    " consumed_at IS NULL",
                    (now, terminal_projection_prepare_id),
                )
            if terminal_status is not None:
                await self._execution_ports.checkpoint.finalize_run(
                    transaction,
                    run_id=run_id,
                    terminal_checkpoint_id=next_id,
                    status=terminal_status,
                    outcome={
                        "error": copy.deepcopy(terminal_error),
                        "recovery_action": recovery_action,
                        "event_ids": event_ids,
                    },
                    checkpoint_namespace=namespace,
                    lease_epoch=epoch,
                    now=now,
                )
            self._write_native_operation(
                connection,
                operation_id=operation_id,
                run_id=run_id,
                namespace=namespace,
                base_checkpoint_id=expected_head,
                operation_kind="frontier",
                identity_key=next_id,
                payload={
                    "request_hash": request_hash,
                    "next_checkpoint_id": next_id,
                    "event_ids": event_ids,
                },
                now=now,
            )
            frontier_outcome = {
                "status": "terminal" if terminal_status is not None else "advanced",
                "checkpoint_id": next_id,
                "terminal_status": terminal_status,
                "output": copy.deepcopy(dict(state)),
            }
            # Native keeps driving after a non-terminal frontier commit.  Settling
            # the resume here would release its three-part activation before the
            # next node/effect handoff.  A resume receipt is settled only by a
            # durable exit; intermediate frontiers remain CLAIMED.
            if terminal_status is not None and self._resume_authority(configurable) is not None:
                await self._settle_resume(
                    transaction,
                    configurable=configurable,
                    committed_checkpoint=next_id,
                    outcome=frontier_outcome,
                    now=now,
                )
            elif terminal_status is not None:
                await self._release_activation(
                    transaction,
                    configurable=configurable,
                    outcome=frontier_outcome,
                    now=now,
                )
            return NativeCommitResult(snapshot, tuple(event_ids))

        return await self._atomic("frontier", operation)


__all__ = (
    "PendingInterrupt",
    "SqliteNativeCheckpointStore",
    "WorkflowCheckpoint",
    "WorkflowCheckpointPort",
)
