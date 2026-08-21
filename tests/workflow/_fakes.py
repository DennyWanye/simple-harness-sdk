# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Black-box fakes for the workflow port contracts."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, TypeVar

from simple_harness.workflow.checkpoint import WorkflowCheckpoint
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowExecutionPorts,
    WorkflowOperationReceipt,
)
from simple_harness.workflow.lease import WorkflowLease
from simple_harness.workflow.native import InMemoryNativeCheckpointStore
from simple_harness.workflow.recovery import RecoveryDecision, RecoveryDisposition
from simple_harness.workflow.trace import WorkflowTraceEvent

T = TypeVar("T")


class MemoryUnitOfWork:
    def __init__(self, transaction_owner: object | None = None) -> None:
        self.transaction_owner = transaction_owner or object()
        self.atomic_calls = 0
        self.fault_at: str | None = None
        self.receipts: dict[str, WorkflowOperationReceipt] = {}

    async def run_atomic(
        self, operation: Callable[[object], Awaitable[T]], *, fault_label: str
    ) -> T:
        self.atomic_calls += 1
        if self.fault_at == f"before:{fault_label}":
            raise RuntimeError(f"fault before {fault_label}")
        transaction = MemoryTransaction(self)
        result = await operation(transaction)
        transaction.is_open = False
        if self.fault_at == f"after:{fault_label}":
            raise RuntimeError(f"fault after {fault_label}")
        return result


class MemoryTransaction:
    def __init__(self, unit_of_work: MemoryUnitOfWork) -> None:
        self.transaction_owner = unit_of_work.transaction_owner
        self.is_open = True
        self._unit_of_work = unit_of_work

    async def read_workflow_operation(self, operation_id: str) -> WorkflowOperationReceipt | None:
        return self._unit_of_work.receipts.get(operation_id)

    async def apply_workflow_operation(self, **values: object) -> Any:
        return copy.deepcopy(values["payload"])

    async def write_workflow_operation(self, receipt: WorkflowOperationReceipt) -> None:
        if receipt.operation_id in self._unit_of_work.receipts:
            raise RuntimeError("duplicate operation receipt")
        self._unit_of_work.receipts[receipt.operation_id] = receipt


class MemoryCheckpointPort:
    def __init__(self, transaction_owner: object | None = None) -> None:
        self.transaction_owner = transaction_owner or object()
        self.current: dict[str, WorkflowCheckpoint] = {}
        self.history_by_run: dict[str, list[WorkflowCheckpoint]] = {}
        self.commit_labels: list[str] = []
        self._adapter: CheckpointExecutionAdapter | None = None

    def bind_execution_adapter(self, adapter: CheckpointExecutionAdapter) -> None:
        if self._adapter is not None:
            raise ValueError("checkpoint execution adapter already bound")
        if adapter.transaction_owner is not self.transaction_owner:
            raise ValueError("checkpoint execution adapter owner mismatch")
        self._adapter = adapter

    async def load(self, run_id: str) -> WorkflowCheckpoint | None:
        return self.current.get(run_id)

    async def history(
        self, run_id: str, *, limit: int | None = None
    ) -> tuple[WorkflowCheckpoint, ...]:
        rows = tuple(self.history_by_run.get(run_id, ()))
        return rows if limit is None else rows[-limit:]

    async def commit(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        expected_revision: int | None,
        lease: WorkflowLease,
        transaction: object,
    ) -> WorkflowCheckpoint:
        del transaction
        existing = self.current.get(checkpoint.run_id)
        actual = None if existing is None else existing.revision
        if actual != expected_revision:
            raise RuntimeError("checkpoint revision conflict")
        if lease.run_id != checkpoint.run_id:
            raise RuntimeError("lease/checkpoint run mismatch")
        saved = replace(checkpoint, revision=0 if existing is None else existing.revision + 1)
        self.current[checkpoint.run_id] = saved
        self.history_by_run.setdefault(checkpoint.run_id, []).append(saved)
        self.commit_labels.append(saved.status.value)
        return saved

    async def fork(
        self,
        *,
        source: WorkflowCheckpoint,
        target_run_id: str,
        values: Mapping[str, Any],
        transaction: object,
    ) -> WorkflowCheckpoint:
        del transaction
        forked = replace(
            source,
            run_id=target_run_id,
            values=copy.deepcopy(dict(values)),
            revision=0,
        )
        self.current[target_run_id] = forked
        self.history_by_run[target_run_id] = [forked]
        return forked


class RecordingRecoveryPort:
    def __init__(self) -> None:
        self.quarantined: list[tuple[str, str]] = []

    def classify(
        self, error: BaseException, *, attempt: int, max_attempts: int
    ) -> RecoveryDecision:
        if isinstance(error, RetryableNodeError) and attempt < max_attempts:
            return RecoveryDecision(RecoveryDisposition.RETRY, "retryable_node", 0.0)
        return RecoveryDecision(RecoveryDisposition.FAIL, "node_failed", None)

    async def quarantine(
        self,
        *,
        run_id: str,
        reason: str,
        checkpoint: WorkflowCheckpoint | None,
        transaction: object,
    ) -> None:
        del checkpoint, transaction
        self.quarantined.append((run_id, reason))

    async def recover_expired(self, *, now: float, transaction: object):  # type: ignore[no-untyped-def]
        del now, transaction
        return ()

    async def repair_head(self, checkpoint, *, transaction):  # type: ignore[no-untyped-def]
        del checkpoint, transaction


class RecordingTracePort:
    def __init__(self) -> None:
        self.events: list[WorkflowTraceEvent] = []

    async def append(self, event: WorkflowTraceEvent, *, transaction: object) -> None:
        del transaction
        self.events.append(event)


class RetryableNodeError(RuntimeError):
    pass


class MemoryNativeStore(InMemoryNativeCheckpointStore):
    def __init__(self, transaction_owner: object) -> None:
        super().__init__()
        self.transaction_owner = transaction_owner


class LegacyTerminalProjectionPort:
    def project_public(self, workflow_name, workflow_version, raw, engine_status):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, raw, engine_status


class NoTerminalCommitProjectionPort:
    def lookup(self, workflow_name, workflow_version, descriptor):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, descriptor


def memory_authorities():  # type: ignore[no-untyped-def]
    owner = object()
    unit_of_work = MemoryUnitOfWork(owner)
    checkpoint = MemoryCheckpointPort(owner)
    execution_ports = WorkflowExecutionPorts(
        unit_of_work=unit_of_work,
        checkpoint=CheckpointExecutionAdapter(owner),
        lifecycle=unit_of_work,
        recovery=unit_of_work,
        replay=unit_of_work,
    )
    return checkpoint, unit_of_work, execution_ports
