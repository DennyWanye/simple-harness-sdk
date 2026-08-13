# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Canonical execution-ledger authority used by workflow checkpoints."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from simple_harness.contracts import JsonValue, canonical_json

T = TypeVar("T")


class WorkflowOperationConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowOperationReceipt:
    operation_id: str
    adapter_method: str
    identity: tuple[str, ...]
    payload_hash: str
    outcome: JsonValue
    run_id: str
    namespace: str
    checkpoint_id: str | None
    lease_epoch: int
    created_at: float


class WorkflowTransaction(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    @property
    def is_open(self) -> bool: ...

    async def read_workflow_operation(
        self, operation_id: str
    ) -> WorkflowOperationReceipt | None: ...

    async def apply_workflow_operation(
        self,
        *,
        adapter_method: str,
        identity: tuple[str, ...],
        payload: Mapping[str, JsonValue],
    ) -> JsonValue: ...

    async def write_workflow_operation(
        self, receipt: WorkflowOperationReceipt
    ) -> None: ...


class WorkflowBlobReferencePort(Protocol):
    async def validate_references(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        owner_kind: str,
        owner_id: str,
        blob_refs: Sequence[str],
    ) -> None: ...


class WorkflowUnitOfWork(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    async def run_atomic(
        self,
        operation: Callable[[WorkflowTransaction], Awaitable[T]],
        *,
        fault_label: str,
    ) -> T: ...


def _operation_id(identity: Sequence[str]) -> str:
    return hashlib.sha256(canonical_json(list(identity)).encode()).hexdigest()


class CheckpointExecutionAdapter:
    """Receipt-first adapter; it never owns or commits a transaction."""

    __slots__ = ("transaction_owner",)

    def __init__(self, transaction_owner: object) -> None:
        self.transaction_owner = transaction_owner

    async def _apply(
        self,
        transaction: WorkflowTransaction,
        *,
        method: str,
        identity: tuple[str, ...],
        payload: Mapping[str, JsonValue],
        operation_id: str | None,
        run_id: str,
        namespace: str,
        checkpoint_id: str | None,
        lease_epoch: int,
        created_at: float,
    ) -> JsonValue:
        if not transaction.is_open or transaction.transaction_owner is not self.transaction_owner:
            raise WorkflowOperationConflict("foreign or closed workflow transaction")
        expected_id = _operation_id(identity)
        if operation_id is not None and operation_id != expected_id:
            raise WorkflowOperationConflict("operation id does not match durable identity")
        resolved_id = expected_id
        detached = copy.deepcopy(dict(payload))
        payload_hash = hashlib.sha256(canonical_json(detached).encode()).hexdigest()
        existing = await transaction.read_workflow_operation(resolved_id)
        if existing is not None:
            if existing.adapter_method != method or existing.identity != identity:
                raise WorkflowOperationConflict("operation id reused across adapter methods")
            if existing.payload_hash != payload_hash:
                raise WorkflowOperationConflict("operation payload changed")
            return copy.deepcopy(existing.outcome)
        outcome = await transaction.apply_workflow_operation(
            adapter_method=method, identity=identity, payload=detached
        )
        await transaction.write_workflow_operation(
            WorkflowOperationReceipt(
                resolved_id,
                method,
                identity,
                payload_hash,
                copy.deepcopy(outcome),
                run_id,
                namespace,
                checkpoint_id,
                lease_epoch,
                created_at,
            )
        )
        return copy.deepcopy(outcome)

    async def mark_running_on_claim(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        checkpoint_namespace: str,
        lease_epoch: int,
        claim_epoch: int,
        now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        identity = (run_id, checkpoint_namespace, str(lease_epoch), str(claim_epoch))
        return await self._apply(
            transaction, method="mark_running_on_claim", identity=identity,
            payload={"run_id": run_id, "checkpoint_namespace": checkpoint_namespace,
                     "lease_epoch": lease_epoch, "claim_epoch": claim_epoch, "now": now},
            operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace, checkpoint_id=None,
            lease_epoch=lease_epoch, created_at=now,
        )

    async def consume_decisions(
        self, transaction: WorkflowTransaction, *, run_id: str, checkpoint_id: str,
        decision_ids: Sequence[str], responses: Mapping[str, JsonValue],
        checkpoint_namespace: str, lease_epoch: int, now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        ordered = tuple(sorted(decision_ids))
        identity = (run_id, checkpoint_id, *ordered)
        return await self._apply(
            transaction, method="consume_decisions", identity=identity,
            payload={"run_id": run_id, "checkpoint_id": checkpoint_id,
                     "decision_ids": list(ordered), "responses": copy.deepcopy(dict(responses)),
                     "checkpoint_namespace": checkpoint_namespace,
                     "lease_epoch": lease_epoch, "now": now},
            operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace, checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch, created_at=now,
        )

    async def open_decision(
        self, transaction: WorkflowTransaction, *, run_id: str, interrupt_id: str,
        request: Mapping[str, JsonValue], operation_id: str | None = None,
        checkpoint_namespace: str = "native", checkpoint_id: str | None = None,
        lease_epoch: int = 1, now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, interrupt_id)
        return await self._apply(
            transaction, method="open_decision", identity=identity,
            payload={"run_id": run_id, "interrupt_id": interrupt_id,
                     "request": copy.deepcopy(dict(request)),
                     "checkpoint_namespace": checkpoint_namespace,
                     "checkpoint_id": checkpoint_id, "lease_epoch": lease_epoch,
                     "now": now}, operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace, checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch, created_at=now,
        )

    async def materialize_intent(
        self, transaction: WorkflowTransaction, *, run_id: str, intent_id: str,
        intent: Mapping[str, JsonValue], operation_id: str | None = None,
        checkpoint_namespace: str = "native", checkpoint_id: str | None = None,
        lease_epoch: int = 1, now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, intent_id)
        return await self._apply(
            transaction, method="materialize_intent", identity=identity,
            payload={"run_id": run_id, "intent_id": intent_id,
                     "intent": copy.deepcopy(dict(intent)),
                     "checkpoint_namespace": checkpoint_namespace,
                     "checkpoint_id": checkpoint_id, "lease_epoch": lease_epoch,
                     "now": now}, operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace, checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch, created_at=now,
        )

    async def link_effects(
        self, transaction: WorkflowTransaction, *, run_id: str,
        checkpoint_namespace: str, checkpoint_id: str, effect_ids: Sequence[str],
        lease_epoch: int, now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        ordered = tuple(sorted(effect_ids))
        identity = (run_id, checkpoint_namespace, checkpoint_id, *ordered)
        return await self._apply(
            transaction, method="link_effects", identity=identity,
            payload={"run_id": run_id, "checkpoint_namespace": checkpoint_namespace,
                     "checkpoint_id": checkpoint_id, "effect_ids": list(ordered),
                     "lease_epoch": lease_epoch, "now": now},
            operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace, checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch, created_at=now,
        )

    async def finalize_run(
        self, transaction: WorkflowTransaction, *, run_id: str,
        terminal_checkpoint_id: str, status: str,
        outcome: Mapping[str, JsonValue], operation_id: str | None = None,
        checkpoint_namespace: str = "native", lease_epoch: int = 1,
        now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, terminal_checkpoint_id)
        return await self._apply(
            transaction, method="finalize_run", identity=identity,
            payload={"run_id": run_id, "terminal_checkpoint_id": terminal_checkpoint_id,
                     "status": status, "outcome": copy.deepcopy(dict(outcome)),
                     "checkpoint_namespace": checkpoint_namespace,
                     "lease_epoch": lease_epoch, "now": now},
            operation_id=operation_id,
            run_id=run_id, namespace=checkpoint_namespace,
            checkpoint_id=terminal_checkpoint_id, lease_epoch=lease_epoch,
            created_at=now,
        )


@dataclass(frozen=True, slots=True)
class WorkflowExecutionPorts:
    unit_of_work: WorkflowUnitOfWork
    checkpoint: CheckpointExecutionAdapter

    def __post_init__(self) -> None:
        if self.unit_of_work.transaction_owner is not self.checkpoint.transaction_owner:
            raise ValueError("workflow execution ports do not share one transaction owner")


__all__ = (
    "CheckpointExecutionAdapter", "WorkflowBlobReferencePort", "WorkflowExecutionPorts", "WorkflowOperationConflict",
    "WorkflowOperationReceipt", "WorkflowTransaction", "WorkflowUnitOfWork",
)
