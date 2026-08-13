# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Atomic SQLite command implementation for root execution lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar, cast

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.execution.budget import BudgetCharge, BudgetPolicy, BudgetSnapshot
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildCommandRecord,
    ChildCommandState,
    ChildLaunchResult,
    ChildSignalAckReceipt,
    ChildSignalAckResult,
    ChildSignalRecord,
    ChildSignalState,
    ChildTerminalReceipt,
    ChildTerminalResult,
    ProfileLaunchTicket,
    ProfileLaunchTicketState,
    child_launch_fingerprint,
)
from simple_harness.execution.delivery import (
    DeliveryConflictError,
    DeliveryRecord,
    DeliverySpec,
    DeliveryState,
    TerminalCommitResult,
    delivery_payload_json,
)
from simple_harness.execution.effects import EffectRecord, EffectState
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
)
from simple_harness.execution.recovery import (
    ReconciliationResolution,
    RecoveryKind,
    ResolutionOutcome,
    WaitActivationReceipt,
    WaitBlockerRecord,
    WaitBlockerSpec,
    recovery_identity,
)
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    AdmissionRecord,
    AdmissionState,
    ContinuationProgressReceipt,
    ContinuationProgressResult,
    ContinuationRecord,
    ContinuationState,
    ContinuationTerminalResult,
    DecisionRecord,
    DecisionState,
    ExecutionLease,
    FaultHook,
    RunRecord,
    RunState,
    UnitOfWorkConflict,
    UnitOfWorkNotFound,
    WorkflowCheckpoint,
)
from simple_harness.providers import ProviderTarget
from simple_harness.tools.contracts import ToolOutcome, ToolResult
from simple_harness.workflow.execution_ports import (
    WorkflowOperationConflict,
    WorkflowOperationReceipt,
    WorkflowTransaction,
)

from .database import Database

_WorkflowResult = TypeVar("_WorkflowResult")


class _SqliteWorkflowTransaction:
    __slots__ = ("_fault", "connection", "is_open", "transaction_owner")

    def __init__(self, owner: Database, connection: sqlite3.Connection, fault: FaultHook | None) -> None:
        self.transaction_owner = owner
        self.connection = connection
        self.is_open = True
        self._fault = fault

    async def read_workflow_operation(
        self, operation_id: str
    ) -> WorkflowOperationReceipt | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_operation_receipts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        identity = json.loads(str(row["identity_json"]))
        outcome = json.loads(str(row["outcome_json"]))
        if not isinstance(identity, list):
            raise WorkflowOperationConflict("corrupt workflow operation identity")
        return WorkflowOperationReceipt(
            operation_id=str(row["operation_id"]),
            adapter_method=str(row["adapter_method"]),
            identity=tuple(str(value) for value in identity),
            payload_hash=str(row["payload_hash"]),
            outcome=cast(JsonValue, outcome),
            run_id=str(row["run_id"]),
            namespace=str(row["namespace"]),
            checkpoint_id=(
                None if row["checkpoint_id"] is None else str(row["checkpoint_id"])
            ),
            lease_epoch=int(row["lease_epoch"]),
            created_at=float(row["created_at"]),
        )

    async def apply_workflow_operation(
        self,
        *,
        adapter_method: str,
        identity: tuple[str, ...],
        payload: Mapping[str, JsonValue],
    ) -> JsonValue:
        del identity
        _fault(self._fault, f"workflow_adapter.{adapter_method}.before_ledger")
        run_id = _required(payload.get("run_id"), "run_id")
        now_value = payload.get("now", 0.0)
        now = _time(now_value)
        if adapter_method == "mark_running_on_claim":
            changed = self.connection.execute(
                """
                UPDATE runs SET state='running', version=version+1, updated_at=?
                WHERE run_id=? AND state IN ('created','queued','waiting')
                """,
                (now, run_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT state,version FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise UnitOfWorkNotFound(run_id)
            outcome = {"changed": bool(changed), "state": str(row["state"]), "version": int(row["version"])}
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "consume_decisions":
            checkpoint_id = _required(payload.get("checkpoint_id"), "checkpoint_id")
            decision_ids = payload.get("decision_ids")
            responses = payload.get("responses")
            if not isinstance(decision_ids, list) or not isinstance(responses, dict):
                raise WorkflowOperationConflict("invalid decision consumption payload")
            for decision_id in decision_ids:
                resolved = _required(decision_id, "decision_id")
                if resolved not in responses:
                    raise WorkflowOperationConflict("decision response is missing")
                row = self.connection.execute(
                    "SELECT state,response_json FROM decisions WHERE decision_id=? AND run_id=?",
                    (resolved, run_id),
                ).fetchone()
                if row is None or str(row["state"]) == "open":
                    raise WorkflowOperationConflict("decision is not durably resolved")
                response_json = canonical_json(cast(JsonValue, responses[resolved]))
                self.connection.execute(
                    """
                    INSERT INTO workflow_decision_consumptions(
                        run_id,checkpoint_id,decision_id,response_json,consumed_at
                    ) VALUES (?,?,?,?,?)
                    """,
                    (run_id, checkpoint_id, resolved, response_json, now),
                )
            outcome = {"decision_ids": list(decision_ids), "checkpoint_id": checkpoint_id}
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "open_decision":
            interrupt_id = _required(payload.get("interrupt_id"), "interrupt_id")
            request = payload.get("request")
            if not isinstance(request, dict):
                raise WorkflowOperationConflict("decision request must be an object")
            existing = self.connection.execute(
                "SELECT request_json,state FROM decisions WHERE decision_id=?", (interrupt_id,)
            ).fetchone()
            request_json = canonical_json(request)
            if existing is not None:
                if str(existing["request_json"]) != request_json:
                    raise WorkflowOperationConflict("decision request changed")
                return {"decision_id": interrupt_id, "state": str(existing["state"])}
            self.connection.execute(
                """
                INSERT INTO decisions(
                    decision_id,run_id,kind,state,request_json,version,created_at
                ) VALUES (?,?,?,'open',?,0,?)
                """,
                (interrupt_id, run_id, str(request.get("kind", "workflow_interrupt")), request_json, now),
            )
            changed = self.connection.execute(
                """
                UPDATE runs SET state='waiting',version=version+1,updated_at=?
                WHERE run_id=? AND state='running'
                """,
                (now, run_id),
            ).rowcount
            if changed != 1:
                raise WorkflowOperationConflict("workflow interrupt requires running Run")
            event_id = hashlib.sha256(
                f"{run_id}|decision.open|{interrupt_id}".encode()
            ).hexdigest()
            sequence = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(durable_seq),0)+1 FROM run_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                """
                INSERT INTO run_events(
                    event_id,run_id,durable_seq,kind,payload_json,created_at
                ) VALUES(?,?,?,'decision.open',?,?)
                """,
                (
                    event_id,run_id,sequence,
                    canonical_json({"decision_id":interrupt_id,"kind":str(request.get("kind","workflow_interrupt"))}),
                    now,
                ),
            )
            outcome = cast(
                JsonValue,
                {"decision_id": interrupt_id, "state": "open", "event_id": event_id},
            )
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "materialize_intent":
            intent_id = _required(payload.get("intent_id"), "intent_id")
            intent = payload.get("intent")
            if not isinstance(intent, dict):
                raise WorkflowOperationConflict("workflow intent must be an object")
            event_id = hashlib.sha256(f"{run_id}|{intent_id}".encode()).hexdigest()
            sequence = int(self.connection.execute(
                "SELECT COALESCE(MAX(durable_seq),0)+1 FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0])
            self.connection.execute(
                "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, run_id, sequence, str(intent.get("event_type", "workflow.event")), canonical_json(intent), now),
            )
            outcome = {"event_id": event_id, "durable_seq": sequence}
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "link_effects":
            namespace = _required(payload.get("checkpoint_namespace"), "checkpoint_namespace")
            checkpoint_id = _required(payload.get("checkpoint_id"), "checkpoint_id")
            effect_ids = payload.get("effect_ids")
            if not isinstance(effect_ids, list):
                raise WorkflowOperationConflict("effect ids must be a list")
            for effect_id in effect_ids:
                self.connection.execute(
                    "INSERT INTO workflow_checkpoint_effect_links(run_id,namespace,checkpoint_id,effect_id,created_at) VALUES(?,?,?,?,?)",
                    (run_id, namespace, checkpoint_id, _required(effect_id, "effect_id"), now),
                )
            outcome = {"effect_ids": list(effect_ids), "checkpoint_id": checkpoint_id}
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "finalize_run":
            status = _required(payload.get("status"), "status")
            if status not in {"completed", "failed", "cancelled"}:
                raise WorkflowOperationConflict("invalid terminal workflow status")
            changed = self.connection.execute(
                """
                UPDATE runs SET state=?,version=version+1,updated_at=?
                WHERE run_id=? AND state NOT IN ('completed','failed','cancelled')
                """,
                (status, now, run_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT state,version FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise UnitOfWorkNotFound(run_id)
            if not changed and str(row["state"]) != status:
                raise WorkflowOperationConflict("Run already has another terminal state")
            outcome = {"state": str(row["state"]), "version": int(row["version"])}
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        raise WorkflowOperationConflict(f"unknown workflow adapter method: {adapter_method}")

    async def write_workflow_operation(
        self, receipt: WorkflowOperationReceipt
    ) -> None:
        _fault(self._fault, f"workflow_adapter.{receipt.adapter_method}.before_receipt")
        self.connection.execute(
            """
            INSERT INTO workflow_operation_receipts(
                operation_id,adapter_method,identity_json,payload_hash,outcome_json,
                run_id,namespace,checkpoint_id,lease_epoch,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.operation_id, receipt.adapter_method,
                canonical_json(list(receipt.identity)), receipt.payload_hash,
                canonical_json(receipt.outcome), receipt.run_id, receipt.namespace,
                receipt.checkpoint_id, receipt.lease_epoch, receipt.created_at,
            ),
        )
        _fault(self._fault, f"workflow_adapter.{receipt.adapter_method}.after_receipt")


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _time(value: object, name: str = "now") -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _object_json(value: Mapping[str, JsonValue], name: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a JSON object")
    return canonical_json(value)


def _fault(hook: FaultHook | None, point: str) -> None:
    if hook is not None:
        hook(point)


class SqliteExecutionUnitOfWork:
    __slots__ = ("database", "workflow_fault")

    def __init__(self, database: Database, *, workflow_fault: FaultHook | None = None) -> None:
        self.database = database
        self.workflow_fault = workflow_fault

    @property
    def transaction_owner(self) -> object:
        return self.database

    async def run_atomic(
        self,
        operation: Callable[[WorkflowTransaction], Awaitable[_WorkflowResult]],
        *,
        fault_label: str,
    ) -> _WorkflowResult:
        _fault(self.workflow_fault, f"{fault_label}.before_begin")
        with self.database.transaction() as connection:
            transaction = _SqliteWorkflowTransaction(
                self.database, connection, self.workflow_fault
            )
            try:
                result = await operation(transaction)
                _fault(self.workflow_fault, f"{fault_label}.before_commit")
            finally:
                transaction.is_open = False
        _fault(self.workflow_fault, f"{fault_label}.after_commit")
        return result

    def claim_runtime_activation(
        self,
        *,
        run_id: str,
        owner_id: str,
        namespace: str,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, ExecutionLease]:
        """Atomically acquire the durable owner lease and activate one Run."""

        run_id = _required(run_id, "run_id")
        owner_id = _required(owner_id, "owner_id")
        namespace = _required(namespace, "namespace")
        now = _time(now)
        if (
            not isinstance(lease_ttl_seconds, (int, float))
            or isinstance(lease_ttl_seconds, bool)
            or not math.isfinite(float(lease_ttl_seconds))
            or lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be finite and positive")
        expires_at = now + float(lease_ttl_seconds)
        with self.database.transaction() as connection:
            run_row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise UnitOfWorkNotFound(run_id)
            run = _run_record(run_row)
            if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                raise UnitOfWorkConflict("terminal Run cannot be activated")
            lease_row = connection.execute(
                "SELECT owner_id, epoch, expires_at FROM workflow_leases "
                "WHERE run_id = ? AND namespace = ?",
                (run_id, namespace),
            ).fetchone()
            if lease_row is not None and float(lease_row["expires_at"]) > now:
                if str(lease_row["owner_id"]) != owner_id:
                    raise UnitOfWorkConflict("Run already has an active runtime owner")
                lease = ExecutionLease(
                    run_id,
                    namespace,
                    owner_id,
                    int(lease_row["epoch"]),
                    float(lease_row["expires_at"]),
                )
                return run, lease
            epoch = 1 if lease_row is None else int(lease_row["epoch"]) + 1
            _fault(fault, "runtime_activation.lease.before_write")
            connection.execute(
                """
                INSERT INTO workflow_leases(run_id, namespace, owner_id, epoch, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, namespace) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    epoch = excluded.epoch,
                    expires_at = excluded.expires_at
                """,
                (run_id, namespace, owner_id, epoch, expires_at),
            )
            _fault(fault, "runtime_activation.lease.after_write")
            event_kind = (
                "run.recovered" if run.state is RunState.RUNNING else "run.activated"
            )
            _fault(fault, "runtime_activation.event.before_write")
            self._insert_event(
                connection,
                event_id=f"{run_id}:runtime:{namespace}:{epoch}:activated",
                run_id=run_id,
                kind=event_kind,
                payload={"owner_id": owner_id, "lease_epoch": epoch},
                now=now,
            )
            _fault(fault, "runtime_activation.event.after_write")
            if run.state in {RunState.CREATED, RunState.QUEUED}:
                _fault(fault, "runtime_activation.run.before_write")
                changed = connection.execute(
                    """
                    UPDATE runs SET state = 'running', version = version + 1, updated_at = ?
                    WHERE run_id = ? AND version = ? AND state IN ('created', 'queued')
                    """,
                    (now, run_id, run.version),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("runtime activation CAS failed")
                _fault(fault, "runtime_activation.run.after_write")
        _fault(fault, "runtime_activation.after_commit")
        activated = self.read_run(run_id)
        assert activated is not None
        return activated, ExecutionLease(run_id, namespace, owner_id, epoch, expires_at)

    def release_runtime_lease(
        self,
        lease: ExecutionLease,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> None:
        now = _time(now)
        with self.database.transaction() as connection:
            _fault(fault, "runtime_lease_release.before_write")
            changed = connection.execute(
                """
                UPDATE workflow_leases SET expires_at = ?
                WHERE run_id = ? AND namespace = ? AND owner_id = ? AND epoch = ?
                """,
                (now, lease.run_id, lease.namespace, lease.owner_id, lease.epoch),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("runtime lease release CAS failed")
            _fault(fault, "runtime_lease_release.after_write")
        _fault(fault, "runtime_lease_release.after_commit")

    def renew_runtime_lease(
        self,
        lease: ExecutionLease,
        *,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ExecutionLease:
        now = _time(now)
        if (
            not isinstance(lease_ttl_seconds, (int, float))
            or isinstance(lease_ttl_seconds, bool)
            or not math.isfinite(float(lease_ttl_seconds))
            or lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be finite and positive")
        expires_at = now + float(lease_ttl_seconds)
        with self.database.transaction() as connection:
            _fault(fault, "runtime_lease_renew.before_write")
            changed = connection.execute(
                """
                UPDATE workflow_leases SET expires_at = ?
                WHERE run_id = ? AND namespace = ? AND owner_id = ? AND epoch = ?
                  AND expires_at > ?
                """,
                (
                    expires_at,
                    lease.run_id,
                    lease.namespace,
                    lease.owner_id,
                    lease.epoch,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("runtime lease renew CAS failed")
            _fault(fault, "runtime_lease_renew.after_write")
        _fault(fault, "runtime_lease_renew.after_commit")
        return ExecutionLease(
            lease.run_id,
            lease.namespace,
            lease.owner_id,
            lease.epoch,
            expires_at,
        )

    def commit_runtime_state(
        self,
        *,
        run_id: str,
        expected_version: int,
        state: RunState,
        event_id: str,
        payload: Mapping[str, JsonValue],
        lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord:
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        state = RunState(state)
        if state not in {RunState.RUNNING, RunState.WAITING}:
            raise ValueError("runtime state command only supports running or waiting")
        now = _time(now)
        payload_json = _object_json(payload, "payload")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, lease, now=now)
            _fault(fault, "runtime_state.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind=f"run.{state.value}",
                payload=json.loads(payload_json),
                now=now,
            )
            _fault(fault, "runtime_state.event.after_write")
            _fault(fault, "runtime_state.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND version = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (state.value, now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("runtime state CAS failed")
            _fault(fault, "runtime_state.run.after_write")
        _fault(fault, "runtime_state.after_commit")
        record = self.read_run(run_id)
        assert record is not None
        return record

    def commit_runtime_wait_with_blocker(
        self,
        *,
        run_id: str,
        expected_version: int,
        event_id: str,
        payload: Mapping[str, JsonValue],
        blocker: WaitBlockerSpec,
        lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, WaitBlockerRecord]:
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        if lease.run_id != run_id:
            raise UnitOfWorkConflict("wait blocker lease belongs to another Run")
        payload_json = _object_json(payload, "payload")
        blocker_id = blocker.blocker_id
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, lease, now=now)
            existing = connection.execute(
                "SELECT * FROM run_wait_blockers WHERE blocker_id = ?",
                (blocker_id,),
            ).fetchone()
            if existing is not None:
                stored = _wait_blocker_record(existing)
                if (
                    stored.run_id != run_id
                    or stored.kind is not blocker.kind
                    or stored.ledger_identity != blocker.ledger_identity
                    or stored.handoff_attempt != blocker.handoff_attempt
                    or stored.observed_version != blocker.observed_version
                ):
                    raise UnitOfWorkConflict("wait blocker identity conflict")
                current = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                assert current is not None
                return _run_record(current), stored
            resolution = connection.execute(
                """
                SELECT resolution_id, outcome FROM reconciliation_resolutions
                WHERE kind = ? AND ledger_identity = ? AND handoff_attempt = ?
                """,
                (blocker.kind.value, blocker.ledger_identity, blocker.handoff_attempt),
            ).fetchone()
            if blocker.kind is RecoveryKind.PROVIDER:
                ledger = connection.execute(
                    """
                    SELECT run_id,state,version,handoff_attempt
                    FROM provider_invocations WHERE invocation_id=?
                    """,
                    (blocker.ledger_identity,),
                ).fetchone()
                unresolved_state = ProviderInvocationState.UNKNOWN.value
                completed_states = (ProviderInvocationState.SUCCEEDED.value,)
            else:
                ledger = connection.execute(
                    """
                    SELECT run_id,state,version,handoff_attempt
                    FROM execution_effects WHERE effect_id=?
                    """,
                    (blocker.ledger_identity,),
                ).fetchone()
                unresolved_state = EffectState.UNKNOWN.value
                completed_states = (
                    EffectState.SUCCEEDED.value,
                    EffectState.FAILED.value,
                    EffectState.REJECTED.value,
                )
            if (
                ledger is None
                or str(ledger["run_id"]) != run_id
                or int(ledger["handoff_attempt"]) != blocker.handoff_attempt
            ):
                raise UnitOfWorkConflict("wait blocker ledger identity conflict")
            resolution_outcome = (
                None if resolution is None else str(resolution["outcome"])
            )
            if resolution_outcome == ResolutionOutcome.COMPLETED.value:
                if (
                    str(ledger["state"]) not in completed_states
                    or int(ledger["version"]) != blocker.observed_version + 1
                ):
                    raise UnitOfWorkConflict(
                        "completed wait resolution and ledger differ"
                    )
            elif (
                str(ledger["state"]) != unresolved_state
                or int(ledger["version"]) != blocker.observed_version
            ):
                raise UnitOfWorkConflict(
                    "wait blocker observed ledger version is stale"
                )
            _fault(fault, "wait_blocker.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="run.waiting",
                payload=json.loads(payload_json),
                now=now,
            )
            _fault(fault, "wait_blocker.event.after_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = 'waiting', version = version + 1, updated_at = ?
                WHERE run_id = ? AND version = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("wait blocker Run CAS failed")
            _fault(fault, "wait_blocker.run.after_write")
            connection.execute(
                """
                INSERT INTO run_wait_blockers(
                    blocker_id, run_id, kind, ledger_identity, handoff_attempt,
                    observed_version, resolution_id, wake_consumed, created_at,
                    resolved_at, consumed_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, 1)
                """,
                (
                    blocker_id,
                    run_id,
                    blocker.kind.value,
                    blocker.ledger_identity,
                    blocker.handoff_attempt,
                    blocker.observed_version,
                    None if resolution is None else str(resolution["resolution_id"]),
                    now,
                    None if resolution is None else now,
                ),
            )
            _fault(fault, "wait_blocker.blocker.after_write")
        _fault(fault, "wait_blocker.after_commit")
        run = self.read_run(run_id)
        row = self.database.connection.execute(
            "SELECT * FROM run_wait_blockers WHERE blocker_id = ?", (blocker_id,)
        ).fetchone()
        assert run is not None and row is not None
        return run, _wait_blocker_record(row)

    def list_resolved_wait_blockers(
        self,
        *,
        owner_id: str,
        namespace: str,
        now: float,
    ) -> tuple[WaitBlockerRecord, ...]:
        owner_id = _required(owner_id, "owner_id")
        namespace = _required(namespace, "namespace")
        now = _time(now)
        rows = self.database.connection.execute(
            """
            SELECT blockers.* FROM run_wait_blockers AS blockers
            JOIN runs ON runs.run_id = blockers.run_id
            JOIN workflow_leases AS leases
              ON leases.run_id = blockers.run_id AND leases.namespace = ?
            WHERE blockers.resolution_id IS NOT NULL
              AND blockers.wake_consumed = 0 AND runs.state = 'waiting'
              AND ((leases.owner_id = ? AND leases.expires_at > ?)
                   OR leases.expires_at <= ?)
            ORDER BY blockers.created_at, blockers.blocker_id
            """,
            (namespace, owner_id, now, now),
        ).fetchall()
        return tuple(_wait_blocker_record(row) for row in rows)

    def consume_resolved_wait_and_claim_activation(
        self,
        *,
        blocker_id: str,
        owner_id: str,
        namespace: str,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, ExecutionLease, WaitActivationReceipt]:
        blocker_id = _required(blocker_id, "blocker_id")
        owner_id = _required(owner_id, "owner_id")
        namespace = _required(namespace, "namespace")
        now = _time(now)
        if not math.isfinite(lease_ttl_seconds) or lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be finite and positive")
        expires_at = now + float(lease_ttl_seconds)
        receipt_id = f"wait-activation:{blocker_id}"
        with self.database.transaction() as connection:
            receipt_row = connection.execute(
                "SELECT * FROM wait_activation_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if receipt_row is not None:
                receipt = _wait_activation_receipt(receipt_row)
                if receipt.owner_id != owner_id:
                    raise UnitOfWorkConflict(
                        "activation receipt belongs to another Runtime owner"
                    )
                run_row = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (receipt.run_id,)
                ).fetchone()
                lease_row = connection.execute(
                    "SELECT * FROM workflow_leases WHERE run_id = ? AND namespace = ?",
                    (receipt.run_id, namespace),
                ).fetchone()
                if run_row is None or lease_row is None:
                    raise UnitOfWorkConflict("activation receipt authority disappeared")
                if (
                    str(lease_row["owner_id"]) != receipt.owner_id
                    or int(lease_row["epoch"]) != receipt.runtime_lease_epoch
                ):
                    raise UnitOfWorkConflict("activation receipt was superseded")
                return (
                    _run_record(run_row),
                    ExecutionLease(
                        receipt.run_id,
                        namespace,
                        receipt.owner_id,
                        receipt.runtime_lease_epoch,
                        float(lease_row["expires_at"]),
                    ),
                    receipt,
                )
            blocker_row = connection.execute(
                """
                SELECT blockers.*, resolutions.outcome_hash
                FROM run_wait_blockers AS blockers
                JOIN reconciliation_resolutions AS resolutions
                  ON resolutions.resolution_id = blockers.resolution_id
                WHERE blockers.blocker_id = ? AND blockers.wake_consumed = 0
                """,
                (blocker_id,),
            ).fetchone()
            if blocker_row is None:
                raise UnitOfWorkConflict("wait blocker is not resolved and unconsumed")
            run_id = str(blocker_row["run_id"])
            run_row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ? AND state = 'waiting'", (run_id,)
            ).fetchone()
            if run_row is None:
                raise UnitOfWorkConflict("resolved blocker Run is not waiting")
            lease_row = connection.execute(
                "SELECT * FROM workflow_leases WHERE run_id = ? AND namespace = ?",
                (run_id, namespace),
            ).fetchone()
            if lease_row is not None and float(lease_row["expires_at"]) > now:
                if str(lease_row["owner_id"]) != owner_id:
                    raise UnitOfWorkConflict("foreign active Runtime owns resolved Run")
                epoch = int(lease_row["epoch"])
            else:
                epoch = 1 if lease_row is None else int(lease_row["epoch"]) + 1
            connection.execute(
                """
                INSERT INTO workflow_leases(run_id, namespace, owner_id, epoch, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, namespace) DO UPDATE SET
                    owner_id=excluded.owner_id, epoch=excluded.epoch,
                    expires_at=excluded.expires_at
                """,
                (run_id, namespace, owner_id, epoch, expires_at),
            )
            _fault(fault, "wait_activation.lease.after_write")
            changed = connection.execute(
                """
                UPDATE runs SET state='running', version=version+1, updated_at=?
                WHERE run_id=? AND state='waiting' AND version=?
                """,
                (now, run_id, int(run_row["version"])),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("wait activation Run CAS failed")
            outcome_hash = str(blocker_row["outcome_hash"])
            connection.execute(
                """
                INSERT INTO wait_activation_receipts(
                    receipt_id, blocker_id, run_id, owner_id,
                    runtime_lease_epoch, outcome_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, blocker_id, run_id, owner_id, epoch, outcome_hash, now),
            )
            _fault(fault, "wait_activation.receipt.after_write")
            connection.execute(
                """
                UPDATE run_wait_blockers SET wake_consumed=1, consumed_at=?,
                    version=version+1 WHERE blocker_id=? AND wake_consumed=0
                """,
                (now, blocker_id),
            )
            self._insert_event(
                connection,
                event_id=f"{run_id}:wait-activation:{blocker_id}",
                run_id=run_id,
                kind="run.recovered",
                payload={
                    "owner_id": owner_id,
                    "lease_epoch": epoch,
                    "blocker_id": blocker_id,
                },
                now=now,
            )
        _fault(fault, "wait_activation.after_commit")
        run = self.read_run(run_id)
        assert run is not None
        return (
            run,
            ExecutionLease(run_id, namespace, owner_id, epoch, expires_at),
            WaitActivationReceipt(
                receipt_id, blocker_id, run_id, owner_id, epoch, outcome_hash
            ),
        )

    def read_reconciliation_resolution(
        self, *, kind: str, ledger_identity: str, handoff_attempt: int
    ) -> ReconciliationResolution | None:
        row = self.database.connection.execute(
            """
            SELECT * FROM reconciliation_resolutions
            WHERE kind=? AND ledger_identity=? AND handoff_attempt=?
            """,
            (RecoveryKind(kind).value, ledger_identity, handoff_attempt),
        ).fetchone()
        return None if row is None else _reconciliation_resolution(row)

    def request_run_cancel(
        self,
        *,
        run_id: str,
        expected_version: int,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord:
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        existing = self.read_run(run_id)
        if existing is None:
            raise UnitOfWorkNotFound(run_id)
        if existing.state in {
            RunState.CANCEL_REQUESTED,
            RunState.CANCELLED,
            RunState.COMPLETED,
            RunState.FAILED,
        }:
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "runtime_cancel.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="run.cancel_requested",
                payload={},
                now=now,
            )
            _fault(fault, "runtime_cancel.event.after_write")
            _fault(fault, "runtime_cancel.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = 'cancel_requested', version = version + 1, updated_at = ?
                WHERE run_id = ? AND version = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'cancel_requested')
                """,
                (now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("runtime cancellation CAS failed")
            _fault(fault, "runtime_cancel.run.after_write")
        _fault(fault, "runtime_cancel.after_commit")
        record = self.read_run(run_id)
        assert record is not None
        return record

    def read_react_checkpoint(self, run_id: str) -> WorkflowCheckpoint | None:
        row = self.database.connection.execute(
            """
            SELECT * FROM workflow_checkpoints
            WHERE run_id = ? AND namespace = 'react.termination.v1'
            ORDER BY version DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else _workflow_checkpoint(row)

    def cas_react_checkpoint(
        self,
        *,
        run_id: str,
        lease: ExecutionLease,
        expected_version: int | None,
        checkpoint: Mapping[str, JsonValue],
        checkpoint_hash: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCheckpoint:
        run_id = _required(run_id, "run_id")
        if lease.run_id != run_id or lease.namespace != RUNTIME_LEASE_NAMESPACE:
            raise UnitOfWorkConflict("ReAct checkpoint requires canonical Run lease")
        checkpoint_json = _object_json(checkpoint, "checkpoint")
        actual_hash = hashlib.sha256(checkpoint_json.encode("utf-8")).hexdigest()
        if checkpoint_hash != actual_hash:
            raise UnitOfWorkConflict("ReAct checkpoint hash mismatch")
        if expected_version is not None and (
            isinstance(expected_version, bool) or expected_version < 0
        ):
            raise ValueError("expected_version must be non-negative or None")
        now = _time(now)
        next_version = 0 if expected_version is None else expected_version + 1
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, lease, now=now)
            latest = connection.execute(
                """
                SELECT version FROM workflow_checkpoints
                WHERE run_id = ? AND namespace = 'react.termination.v1'
                ORDER BY version DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            current_version = None if latest is None else int(latest["version"])
            if current_version != expected_version:
                raise UnitOfWorkConflict("ReAct checkpoint version CAS failed")
            _fault(fault, "react_checkpoint.before_write")
            connection.execute(
                """
                INSERT INTO workflow_checkpoints(
                    checkpoint_id, run_id, namespace, checkpoint_json,
                    checkpoint_hash, lease_epoch, version, created_at
                ) VALUES (?, ?, 'react.termination.v1', ?, ?, ?, ?, ?)
                """,
                (
                    f"{run_id}:react.termination.v1:{next_version}",
                    run_id,
                    checkpoint_json,
                    checkpoint_hash,
                    lease.epoch,
                    next_version,
                    now,
                ),
            )
            _fault(fault, "react_checkpoint.after_write")
        _fault(fault, "react_checkpoint.after_commit")
        result = self.read_react_checkpoint(run_id)
        assert result is not None
        return result

    def commit_root_terminal_with_deliveries(
        self,
        *,
        run_id: str,
        expected_version: int,
        terminal_state: RunState,
        event_id: str,
        terminal_payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        fence: RunFenceLease,
        execution_lease: ExecutionLease,
        terminal_fence_receipt_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> TerminalCommitResult:
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        terminal_fence_receipt_ref = _required(
            terminal_fence_receipt_ref, "terminal_fence_receipt_ref"
        )
        if isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        terminal_state = RunState(terminal_state)
        if terminal_state not in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise ValueError("root terminal command requires a terminal state")
        if fence.run_id.value != run_id:
            raise UnitOfWorkConflict("terminal fence belongs to another run")
        now = _time(now)
        payload = dict(terminal_payload)
        payload["terminal_fence_receipt_ref"] = terminal_fence_receipt_ref
        payload["fence_epoch"] = fence.epoch
        payload_json = _object_json(payload, "terminal_payload")
        items = tuple(deliveries)
        identities = [item.idempotency_key for item in items]
        if len(set(identities)) != len(identities):
            raise DeliveryConflictError("duplicate delivery idempotency key")

        existing_run = self.read_run(run_id)
        if existing_run is None:
            raise UnitOfWorkNotFound(run_id)
        if existing_run.parent_run_id is not None:
            raise UnitOfWorkConflict("only root runs can commit terminal deliveries")
        if existing_run.state in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            existing_event = self.database.connection.execute(
                "SELECT kind, payload_json FROM run_events WHERE event_id = ? AND run_id = ?",
                (event_id, run_id),
            ).fetchone()
            stored = tuple(
                _delivery_record(row)
                for row in self.database.connection.execute(
                    "SELECT * FROM delivery_outbox WHERE run_id = ? ORDER BY delivery_id",
                    (run_id,),
                ).fetchall()
            )
            requested_rows: list[tuple[str, str, str, str]] = []
            for item in items:
                replay_payload = _thaw(item.payload)
                replay_payload["terminal_fence_receipt_ref"] = (
                    terminal_fence_receipt_ref
                )
                replay_payload["fence_epoch"] = fence.epoch
                requested_rows.append(
                    (
                        item.delivery_id,
                        item.sink_kind,
                        item.idempotency_key,
                        delivery_payload_json(replay_payload),
                    )
                )
            requested = tuple(sorted(requested_rows))
            actual = tuple(
                sorted(
                    (
                        item.delivery_id,
                        item.sink_kind,
                        item.idempotency_key,
                        canonical_json(_thaw(item.payload)),
                    )
                    for item in stored
                )
            )
            if (
                existing_run.state is terminal_state
                and existing_event is not None
                and str(existing_event["kind"]) == f"run.{terminal_state.value}"
                and str(existing_event["payload_json"]) == payload_json
                and requested == actual
            ):
                return TerminalCommitResult(existing_run, stored)
            raise UnitOfWorkConflict("another root terminal intent already won")

        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            if (
                execution_lease.run_id != run_id
                or execution_lease.owner_id != fence.owner_id
            ):
                raise UnitOfWorkConflict("terminal runtime lease and Run fence differ")
            self._require_run_fence(
                connection,
                fence,
                execution_lease=execution_lease,
            )
            _fault(fault, "root_terminal.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind=f"run.{terminal_state.value}",
                payload=dict(payload),
                now=now,
            )
            _fault(fault, "root_terminal.event.after_write")
            for index, item in enumerate(items):
                _fault(fault, f"root_terminal.delivery.{index}.before_write")
                bound_payload = _thaw(item.payload)
                bound_payload["terminal_fence_receipt_ref"] = terminal_fence_receipt_ref
                bound_payload["fence_epoch"] = fence.epoch
                connection.execute(
                    """
                    INSERT INTO delivery_outbox(
                        delivery_id, run_id, sink_kind, idempotency_key,
                        payload_json, state, version, created_at, claimed_at, settled_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL)
                    """,
                    (
                        item.delivery_id,
                        run_id,
                        item.sink_kind,
                        item.idempotency_key,
                        delivery_payload_json(bound_payload),
                        now,
                    ),
                )
                _fault(fault, f"root_terminal.delivery.{index}.after_write")
            _fault(fault, "root_terminal.fence.before_write")
            changed = connection.execute(
                """
                UPDATE run_fences SET state = 'released', released_at = ?
                WHERE run_id = ? AND owner_id = ? AND epoch = ? AND state = 'active'
                """,
                (now, run_id, fence.owner_id, fence.epoch),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("terminal fence release CAS failed")
            _fault(fault, "root_terminal.fence.after_write")
            _fault(fault, "root_terminal.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND parent_run_id IS NULL AND version = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (terminal_state.value, now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("root terminal CAS failed")
            connection.execute(
                "UPDATE continuations SET state='quarantined',acked_at=?,"
                "version=version+1 WHERE run_id=? AND state IN ('pending','claimed')",
                (now, run_id),
            )
            _fault(fault, "root_terminal.run.after_write")
        _fault(fault, "root_terminal.after_commit")
        run = self.read_run(run_id)
        assert run is not None
        stored = tuple(
            record
            for item in items
            if (record := self.read_delivery(item.delivery_id)) is not None
        )
        return TerminalCommitResult(run, stored)

    def claim_delivery(
        self,
        *,
        sink_kinds: Sequence[str],
        now: float,
        claim_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord | None:
        now = _time(now)
        if not math.isfinite(claim_ttl_seconds) or claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be finite and positive")
        normalized = tuple(_required(item, "sink_kind") for item in sink_kinds)
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        claimed_id: str | None = None
        with self.database.transaction() as connection:
            _fault(fault, "delivery_claim.expired.before_write")
            connection.execute(
                """
                UPDATE delivery_outbox SET state = 'pending', version = version + 1,
                    claimed_at = NULL
                WHERE state = 'claimed' AND claimed_at <= ?
                """,
                (now - claim_ttl_seconds,),
            )
            _fault(fault, "delivery_claim.expired.after_write")
            row = connection.execute(
                f"""
                SELECT delivery_id, version FROM delivery_outbox
                WHERE state = 'pending' AND sink_kind IN ({placeholders})
                ORDER BY created_at, delivery_id LIMIT 1
                """,
                normalized,
            ).fetchone()
            if row is not None:
                claimed_id = str(row["delivery_id"])
                _fault(fault, "delivery_claim.delivery.before_write")
                changed = connection.execute(
                    """
                    UPDATE delivery_outbox SET state = 'claimed', version = version + 1,
                        claimed_at = ?
                    WHERE delivery_id = ? AND state = 'pending' AND version = ?
                    """,
                    (now, claimed_id, int(row["version"])),
                ).rowcount
                if changed != 1:
                    raise DeliveryConflictError("delivery claim CAS failed")
                _fault(fault, "delivery_claim.delivery.after_write")
        _fault(fault, "delivery_claim.after_commit")
        return None if claimed_id is None else self.read_delivery(claimed_id)

    def complete_delivery(
        self,
        delivery_id: str,
        *,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord:
        return self._settle_delivery(
            delivery_id,
            expected_version=expected_version,
            target_state=DeliveryState.DELIVERED,
            now=now,
            fault=fault,
            command="delivery_complete",
        )

    def release_delivery(
        self,
        delivery_id: str,
        *,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord:
        return self._settle_delivery(
            delivery_id,
            expected_version=expected_version,
            target_state=DeliveryState.PENDING,
            now=now,
            fault=fault,
            command="delivery_release",
        )

    def read_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM delivery_outbox WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return None if row is None else _delivery_record(row)

    def _settle_delivery(
        self,
        delivery_id: str,
        *,
        expected_version: int,
        target_state: DeliveryState,
        now: float,
        fault: FaultHook | None,
        command: str,
    ) -> DeliveryRecord:
        delivery_id = _required(delivery_id, "delivery_id")
        now = _time(now)
        existing = self.read_delivery(delivery_id)
        if existing is None:
            raise UnitOfWorkNotFound(delivery_id)
        if existing.state is target_state and existing.version == expected_version + 1:
            return existing
        with self.database.transaction() as connection:
            _fault(fault, f"{command}.delivery.before_write")
            changed = connection.execute(
                """
                UPDATE delivery_outbox SET state = ?, version = version + 1,
                    claimed_at = CASE WHEN ? = 'pending' THEN NULL ELSE claimed_at END,
                    settled_at = CASE WHEN ? = 'delivered' THEN ? ELSE settled_at END
                WHERE delivery_id = ? AND state = 'claimed' AND version = ?
                """,
                (
                    target_state.value,
                    target_state.value,
                    target_state.value,
                    now,
                    delivery_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise DeliveryConflictError("delivery settlement CAS failed")
            _fault(fault, f"{command}.delivery.after_write")
        _fault(fault, f"{command}.after_commit")
        result = self.read_delivery(delivery_id)
        assert result is not None
        return result

    def create_with_start_snapshot(
        self,
        *,
        execution_session_id: str,
        run_id: str,
        request_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord:
        execution_session_id = _required(execution_session_id, "execution_session_id")
        run_id = _required(run_id, "run_id")
        request_id = _required(request_id, "request_id")
        profile_key = _required(profile_key, "profile_key")
        driver_kind = _required(driver_kind, "driver_kind")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        snapshot_json = _object_json(snapshot, "snapshot")
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        existing = self._run_by_request(execution_session_id, request_id)
        if existing is not None:
            self._verify_existing_start(
                existing,
                run_id=run_id,
                profile_key=profile_key,
                driver_kind=driver_kind,
                snapshot_hash=snapshot_hash,
            )
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "root_start.session.before_write")
            connection.execute(
                "INSERT OR IGNORE INTO execution_sessions(session_id, created_at) VALUES (?, ?)",
                (execution_session_id, now),
            )
            _fault(fault, "root_start.session.after_write")
            _fault(fault, "root_start.run.before_write")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, execution_session_id, request_id, root_run_id,
                    parent_run_id, profile_key, driver_kind, state, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'created', 0, ?, ?)
                """,
                (
                    run_id,
                    execution_session_id,
                    request_id,
                    run_id,
                    profile_key,
                    driver_kind,
                    now,
                    now,
                ),
            )
            _fault(fault, "root_start.run.after_write")
            _fault(fault, "root_start.snapshot.before_write")
            connection.execute(
                """
                INSERT INTO run_start_snapshots(run_id, snapshot_json, snapshot_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, snapshot_json, snapshot_hash, now),
            )
            _fault(fault, "root_start.snapshot.after_write")
            _fault(fault, "root_start.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="run.created",
                payload={"profile_key": profile_key, "driver_kind": driver_kind},
                now=now,
            )
            _fault(fault, "root_start.event.after_write")
        _fault(fault, "root_start.after_commit")
        record = self.read_run(run_id)
        assert record is not None
        return record

    def start_admission(
        self,
        *,
        admission_id: str,
        run_id: str,
        prompt: Mapping[str, JsonValue],
        expires_at: float | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord:
        admission_id = _required(admission_id, "admission_id")
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        if expires_at is not None:
            expires_at = _time(expires_at, "expires_at")
        prompt_json = _object_json(prompt, "prompt")
        existing = self.read_admission(admission_id)
        if existing is not None:
            if (
                existing.run_id != run_id
                or canonical_json(json.loads(prompt_json))
                != canonical_json(_thaw(existing.prompt))
                or existing.expires_at != expires_at
            ):
                raise UnitOfWorkConflict(
                    "admission identity reused with different intent"
                )
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "admission_start.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = 'admission_pending', version = version + 1, updated_at = ?
                WHERE run_id = ? AND state = 'created'
                """,
                (now, run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("run is not eligible for admission")
            _fault(fault, "admission_start.run.after_write")
            _fault(fault, "admission_start.admission.before_write")
            connection.execute(
                """
                INSERT INTO run_admissions(
                    admission_id, run_id, state, prompt_json, response_json,
                    expires_at, version, created_at, resolved_at
                ) VALUES (?, ?, 'pending', ?, NULL, ?, 0, ?, NULL)
                """,
                (admission_id, run_id, prompt_json, expires_at, now),
            )
            _fault(fault, "admission_start.admission.after_write")
            _fault(fault, "admission_start.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="admission.started",
                payload={"admission_id": admission_id},
                now=now,
            )
            _fault(fault, "admission_start.event.after_write")
        _fault(fault, "admission_start.after_commit")
        result = self.read_admission(admission_id)
        assert result is not None
        return result

    def resolve_admission(
        self,
        *,
        admission_id: str,
        state: AdmissionState,
        response: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord:
        admission_id = _required(admission_id, "admission_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        state = AdmissionState(state)
        if state is AdmissionState.PENDING:
            raise ValueError("resolve_admission requires a terminal admission state")
        response_json = _object_json(response, "response")
        existing = self.read_admission(admission_id)
        if existing is None:
            raise UnitOfWorkNotFound(admission_id)
        if existing.state is not AdmissionState.PENDING:
            if (
                existing.state is state
                and canonical_json(_thaw(existing.response)) == response_json
            ):
                return existing
            raise UnitOfWorkConflict("admission already resolved differently")
        run_state = {
            AdmissionState.ALLOWED: RunState.QUEUED,
            AdmissionState.DENIED: RunState.FAILED,
            AdmissionState.EXPIRED: RunState.FAILED,
            AdmissionState.CANCELLED: RunState.CANCELLED,
        }[state]
        with self.database.transaction() as connection:
            _fault(fault, "admission_resolve.admission.before_write")
            changed = connection.execute(
                """
                UPDATE run_admissions
                SET state = ?, response_json = ?, version = version + 1, resolved_at = ?
                WHERE admission_id = ? AND state = 'pending' AND version = ?
                """,
                (state.value, response_json, now, admission_id, existing.version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("admission CAS failed")
            _fault(fault, "admission_resolve.admission.after_write")
            _fault(fault, "admission_resolve.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND state = 'admission_pending'
                """,
                (run_state.value, now, existing.run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("run admission state CAS failed")
            _fault(fault, "admission_resolve.run.after_write")
            _fault(fault, "admission_resolve.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=existing.run_id,
                kind=f"admission.{state.value}",
                payload={"admission_id": admission_id},
                now=now,
            )
            _fault(fault, "admission_resolve.event.after_write")
        _fault(fault, "admission_resolve.after_commit")
        result = self.read_admission(admission_id)
        assert result is not None
        return result

    def commit_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        kind: str,
        state: DecisionState,
        request: Mapping[str, JsonValue],
        response: Mapping[str, JsonValue] | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> DecisionRecord:
        decision_id = _required(decision_id, "decision_id")
        run_id = _required(run_id, "run_id")
        kind = _required(kind, "kind")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        state = DecisionState(state)
        request_json = _object_json(request, "request")
        response_json = None if response is None else _object_json(response, "response")
        if (state is DecisionState.OPEN) != (response is None):
            raise ValueError(
                "open decision must omit response; resolved decision requires it"
            )
        existing = self.read_decision(decision_id)
        if existing is not None:
            if (
                existing.run_id == run_id
                and existing.kind == kind
                and existing.state is state
                and canonical_json(_thaw(existing.request)) == request_json
                and (
                    (existing.response is None and response_json is None)
                    or canonical_json(_thaw(existing.response)) == response_json
                )
            ):
                return existing
            if (
                existing.run_id == run_id
                and existing.kind == kind
                and existing.state is DecisionState.OPEN
                and state is not DecisionState.OPEN
                and canonical_json(_thaw(existing.request)) == request_json
                and response_json is not None
            ):
                run_state = {
                    DecisionState.ALLOWED: RunState.QUEUED,
                    DecisionState.DENIED: RunState.FAILED,
                    DecisionState.EXPIRED: RunState.FAILED,
                    DecisionState.CANCELLED: RunState.CANCELLED,
                }[state]
                with self.database.transaction() as connection:
                    _fault(fault, "decision_resolve.decision.before_write")
                    changed = connection.execute(
                        """
                        UPDATE decisions
                        SET state=?,response_json=?,version=version+1,resolved_at=?
                        WHERE decision_id=? AND run_id=? AND state='open' AND version=?
                        """,
                        (state.value,response_json,now,decision_id,run_id,existing.version),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("decision resolution CAS failed")
                    _fault(fault, "decision_resolve.decision.after_write")
                    changed = connection.execute(
                        """
                        UPDATE runs SET state=?,version=version+1,updated_at=?
                        WHERE run_id=? AND state='waiting'
                        """,
                        (run_state.value,now,run_id),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("decision Run resolution CAS failed")
                    _fault(fault, "decision_resolve.run.after_write")
                    self._insert_event(
                        connection,event_id=event_id,run_id=run_id,
                        kind=f"decision.{state.value}",
                        payload={"decision_id":decision_id,"kind":kind},now=now,
                    )
                    _fault(fault, "decision_resolve.event.after_write")
                _fault(fault, "decision_resolve.after_commit")
                result = self.read_decision(decision_id)
                assert result is not None
                return result
            raise UnitOfWorkConflict("decision identity reused with different intent")
        run_state = {
            DecisionState.OPEN: RunState.WAITING,
            DecisionState.ALLOWED: RunState.RUNNING,
            DecisionState.DENIED: RunState.FAILED,
            DecisionState.EXPIRED: RunState.FAILED,
            DecisionState.CANCELLED: RunState.CANCELLED,
        }[state]
        with self.database.transaction() as connection:
            _fault(fault, "decision.decision.before_write")
            connection.execute(
                """
                INSERT INTO decisions(
                    decision_id, run_id, kind, state, request_json, response_json,
                    version, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    kind,
                    state.value,
                    request_json,
                    response_json,
                    now,
                    None if state is DecisionState.OPEN else now,
                ),
            )
            _fault(fault, "decision.decision.after_write")
            _fault(fault, "decision.run.before_write")
            changed = connection.execute(
                "UPDATE runs SET state = ?, version = version + 1, updated_at = ? WHERE run_id = ?",
                (run_state.value, now, run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkNotFound(run_id)
            _fault(fault, "decision.run.after_write")
            _fault(fault, "decision.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind=f"decision.{state.value}",
                payload={"decision_id": decision_id, "kind": kind},
                now=now,
            )
            _fault(fault, "decision.event.after_write")
        _fault(fault, "decision.after_commit")
        result = self.read_decision(decision_id)
        assert result is not None
        return result

    def enqueue_continuation(
        self,
        *,
        continuation_id: str,
        run_id: str,
        payload: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord:
        continuation_id = _required(continuation_id, "continuation_id")
        run_id = _required(run_id, "run_id")
        now = _time(now)
        payload_json = _object_json(payload, "payload")
        existing = self.read_continuation(continuation_id)
        if existing is not None:
            if (
                existing.run_id == run_id
                and canonical_json(_thaw(existing.payload)) == payload_json
            ):
                return existing
            raise UnitOfWorkConflict(
                "continuation identity reused with different payload"
            )
        with self.database.transaction() as connection:
            run_row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise UnitOfWorkNotFound(run_id)
            if str(run_row["state"]) in {"completed", "failed", "cancelled"}:
                raise UnitOfWorkConflict("terminal Run rejects new continuations")
            row = connection.execute(
                "SELECT COALESCE(MAX(fifo_seq), 0) + 1 FROM continuations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0])
            _fault(fault, "continuation_enqueue.continuation.before_write")
            connection.execute(
                """
                INSERT INTO continuations(
                    continuation_id, run_id, fifo_seq, payload_json, state,
                    version, claimed_by, runtime_lease_epoch, claim_epoch,
                    ack_receipt_id, created_at, claimed_at, acked_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, 0,
                          NULL, ?, NULL, NULL)
                """,
                (continuation_id, run_id, sequence, payload_json, now),
            )
            _fault(fault, "continuation_enqueue.continuation.after_write")
        _fault(fault, "continuation_enqueue.after_commit")
        result = self.read_continuation(continuation_id)
        assert result is not None
        return result

    def claim_continuation(
        self,
        *,
        run_id: str,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord | None:
        run_id = _required(run_id, "run_id")
        now = _time(now)
        if (
            execution_lease.run_id != run_id
            or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
        ):
            raise UnitOfWorkConflict("continuation requires canonical Runtime lease")
        claimed_id: str | None = None
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            run_row = connection.execute(
                "SELECT state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise UnitOfWorkNotFound(run_id)
            if str(run_row["state"]) in {"completed", "failed", "cancelled"}:
                connection.execute(
                    "UPDATE continuations SET state='quarantined', acked_at=?, "
                    "version=version+1 WHERE run_id=? AND state IN ('pending','claimed')",
                    (now, run_id),
                )
                return None
            row = connection.execute(
                """
                SELECT * FROM continuations
                WHERE run_id = ? AND state IN ('pending','claimed')
                ORDER BY fifo_seq ASC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                claimed_id = str(row["continuation_id"])
                if str(row["state"]) == "claimed":
                    lease_row = connection.execute(
                        "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                        "WHERE run_id=? AND namespace=?",
                        (run_id, RUNTIME_LEASE_NAMESPACE),
                    ).fetchone()
                    if (
                        lease_row is not None
                        and str(lease_row["owner_id"]) == str(row["claimed_by"])
                        and int(lease_row["epoch"]) == int(row["runtime_lease_epoch"])
                        and float(lease_row["expires_at"]) > now
                    ):
                        return None
                _fault(fault, "continuation_claim.continuation.before_write")
                changed = connection.execute(
                    """
                    UPDATE continuations
                    SET state = 'claimed', version = version + 1,
                        claimed_by = ?, runtime_lease_epoch = ?, claimed_at = ?,
                        claim_epoch = claim_epoch + 1
                    WHERE continuation_id = ? AND state IN ('pending','claimed')
                      AND version = ?
                    """,
                    (
                        execution_lease.owner_id,
                        execution_lease.epoch,
                        now,
                        claimed_id,
                        int(row["version"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("continuation claim CAS failed")
                _fault(fault, "continuation_claim.continuation.after_write")
        _fault(fault, "continuation_claim.after_commit")
        return None if claimed_id is None else self.read_continuation(claimed_id)

    def commit_runtime_state_and_ack_continuation(
        self,
        *,
        run_id: str,
        expected_version: int,
        state: RunState,
        event_id: str,
        payload: Mapping[str, JsonValue],
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        receipt_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationProgressResult:
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        receipt_id = _required(receipt_id, "receipt_id")
        state = RunState(state)
        if state is not RunState.WAITING:
            raise ValueError("continuation progress command requires WAITING")
        now = _time(now)
        payload_json = _object_json(payload, "payload")
        outcome_json = canonical_json(
            {
                "run_id": run_id,
                "expected_version": expected_version,
                "state": state.value,
                "event_id": event_id,
                "payload": json.loads(payload_json),
            }
        )
        outcome_hash = hashlib.sha256(outcome_json.encode()).hexdigest()
        existing_receipt = self._read_continuation_progress_receipt(receipt_id)
        if existing_receipt is not None:
            return self._replay_continuation_progress(
                existing_receipt,
                continuation_claim=continuation_claim,
                execution_lease=execution_lease,
                outcome_hash=outcome_hash,
            )
        with self.database.transaction() as connection:
            receipt_row = connection.execute(
                "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if receipt_row is not None:
                return self._replay_continuation_progress(
                    _continuation_progress_receipt(receipt_row),
                    continuation_claim=continuation_claim,
                    execution_lease=execution_lease,
                    outcome_hash=outcome_hash,
                )
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_continuation_claim(
                connection, continuation_claim, execution_lease
            )
            _fault(fault, "continuation_progress.receipt.before_write")
            connection.execute(
                "INSERT INTO continuation_progress_receipts("
                "receipt_id,continuation_id,run_id,owner_id,runtime_lease_epoch,"
                "claim_epoch,outcome_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    continuation_claim.continuation_id,
                    run_id,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    continuation_claim.claim_epoch,
                    outcome_hash,
                    now,
                ),
            )
            _fault(fault, "continuation_progress.receipt.after_write")
            _fault(fault, "continuation_progress.continuation.before_write")
            changed = connection.execute(
                "UPDATE continuations SET state='acked',acked_at=?,ack_receipt_id=?,"
                "version=version+1 WHERE continuation_id=? AND state='claimed' "
                "AND claimed_by=? AND runtime_lease_epoch=? AND claim_epoch=? AND version=?",
                (
                    now,
                    receipt_id,
                    continuation_claim.continuation_id,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    continuation_claim.claim_epoch,
                    continuation_claim.version,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation progress claim CAS failed")
            _fault(fault, "continuation_progress.continuation.after_write")
            _fault(fault, "continuation_progress.run.before_write")
            changed = connection.execute(
                "UPDATE runs SET state='waiting',version=version+1,updated_at=? "
                "WHERE run_id=? AND version=? AND state NOT IN ('completed','failed','cancelled')",
                (now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation progress Run CAS failed")
            _fault(fault, "continuation_progress.run.after_write")
            _fault(fault, "continuation_progress.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="run.waiting",
                payload=dict(payload),
                now=now,
            )
            _fault(fault, "continuation_progress.event.after_write")
        _fault(fault, "continuation_progress.after_commit")
        receipt = self._read_continuation_progress_receipt(receipt_id)
        run = self.read_run(run_id)
        continuation = self.read_continuation(continuation_claim.continuation_id)
        assert receipt is not None and run is not None and continuation is not None
        return ContinuationProgressResult(run, continuation, receipt)

    def commit_root_terminal_with_deliveries_and_ack_continuation(
        self,
        *,
        run_id: str,
        expected_version: int,
        terminal_state: RunState,
        event_id: str,
        terminal_payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        continuation_claim: ContinuationRecord,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        receipt_id: str,
        terminal_fence_receipt_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationTerminalResult:
        run_id = _required(run_id, "run_id")
        terminal_state = RunState(terminal_state)
        if terminal_state not in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise ValueError("continuation terminal state is invalid")
        receipt_id = _required(receipt_id, "receipt_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        payload = dict(terminal_payload)
        payload["terminal_fence_receipt_ref"] = terminal_fence_receipt_ref
        payload["fence_epoch"] = run_fence.epoch
        payload_json = _object_json(payload, "terminal_payload")
        items = tuple(deliveries)
        identities = [item.idempotency_key for item in items]
        if len(set(identities)) != len(identities):
            raise DeliveryConflictError("duplicate delivery idempotency key")
        outcome_hash = hashlib.sha256(
            canonical_json(
                {
                    "run_id": run_id,
                    "expected_version": expected_version,
                    "terminal_state": terminal_state.value,
                    "event_id": event_id,
                    "payload": json.loads(payload_json),
                    "deliveries": [
                        {
                            "delivery_id": item.delivery_id,
                            "sink_kind": item.sink_kind,
                            "idempotency_key": item.idempotency_key,
                            "payload": _thaw(item.payload),
                        }
                        for item in items
                    ],
                }
            ).encode()
        ).hexdigest()
        existing = self._read_continuation_progress_receipt(receipt_id)
        if existing is not None:
            return self._replay_continuation_terminal(
                existing,
                continuation_claim=continuation_claim,
                execution_lease=execution_lease,
                outcome_hash=outcome_hash,
            )
        with self.database.transaction() as connection:
            receipt_row = connection.execute(
                "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if receipt_row is not None:
                return self._replay_continuation_terminal(
                    _continuation_progress_receipt(receipt_row),
                    continuation_claim=continuation_claim,
                    execution_lease=execution_lease,
                    outcome_hash=outcome_hash,
                )
            prior_row = connection.execute(
                "SELECT receipt_id FROM continuation_progress_receipts "
                "WHERE continuation_id=?",
                (continuation_claim.continuation_id,),
            ).fetchone()
            if prior_row is not None:
                raise UnitOfWorkConflict(
                    "continuation was already committed with another receipt"
                )
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection, run_fence, execution_lease=execution_lease
            )
            self._require_continuation_claim(
                connection, continuation_claim, execution_lease
            )
            _fault(fault, "continuation_terminal.receipt.before_write")
            connection.execute(
                "INSERT INTO continuation_progress_receipts("
                "receipt_id,continuation_id,run_id,owner_id,runtime_lease_epoch,"
                "claim_epoch,outcome_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    continuation_claim.continuation_id,
                    run_id,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    continuation_claim.claim_epoch,
                    outcome_hash,
                    now,
                ),
            )
            _fault(fault, "continuation_terminal.receipt.after_write")
            _fault(fault, "continuation_terminal.continuation.before_write")
            changed = connection.execute(
                "UPDATE continuations SET state='acked',acked_at=?,ack_receipt_id=?,"
                "version=version+1 WHERE continuation_id=? AND state='claimed' "
                "AND claimed_by=? AND runtime_lease_epoch=? AND claim_epoch=? AND version=?",
                (
                    now,
                    receipt_id,
                    continuation_claim.continuation_id,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    continuation_claim.claim_epoch,
                    continuation_claim.version,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation terminal claim CAS failed")
            _fault(fault, "continuation_terminal.continuation.after_write")
            _fault(fault, "continuation_terminal.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind=f"run.{terminal_state.value}",
                payload=payload,
                now=now,
            )
            _fault(fault, "continuation_terminal.event.after_write")
            for index, item in enumerate(items):
                _fault(fault, f"continuation_terminal.delivery.{index}.before_write")
                bound = _thaw(item.payload)
                bound["terminal_fence_receipt_ref"] = terminal_fence_receipt_ref
                bound["fence_epoch"] = run_fence.epoch
                connection.execute(
                    "INSERT INTO delivery_outbox("
                    "delivery_id,run_id,sink_kind,idempotency_key,payload_json,state,"
                    "version,created_at,claimed_at,settled_at) "
                    "VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL)",
                    (
                        item.delivery_id,
                        run_id,
                        item.sink_kind,
                        item.idempotency_key,
                        delivery_payload_json(bound),
                        now,
                    ),
                )
                _fault(fault, f"continuation_terminal.delivery.{index}.after_write")
            _fault(fault, "continuation_terminal.fence.before_write")
            changed = connection.execute(
                "UPDATE run_fences SET state='released',released_at=? "
                "WHERE run_id=? AND owner_id=? AND runtime_lease_epoch=? "
                "AND epoch=? AND state='active'",
                (
                    now,
                    run_id,
                    run_fence.owner_id,
                    run_fence.runtime_lease_epoch,
                    run_fence.epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation terminal fence CAS failed")
            _fault(fault, "continuation_terminal.fence.after_write")
            _fault(fault, "continuation_terminal.run.before_write")
            changed = connection.execute(
                "UPDATE runs SET state=?,version=version+1,updated_at=? "
                "WHERE run_id=? AND parent_run_id IS NULL AND version=? "
                "AND state NOT IN ('completed','failed','cancelled')",
                (terminal_state.value, now, run_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation terminal Run CAS failed")
            connection.execute(
                "UPDATE continuations SET state='quarantined',acked_at=?,version=version+1 "
                "WHERE run_id=? AND state IN ('pending','claimed')",
                (now, run_id),
            )
            _fault(fault, "continuation_terminal.run.after_write")
        _fault(fault, "continuation_terminal.after_commit")
        receipt = self._read_continuation_progress_receipt(receipt_id)
        assert receipt is not None
        return self._replay_continuation_terminal(
            receipt,
            continuation_claim=continuation_claim,
            execution_lease=execution_lease,
            outcome_hash=outcome_hash,
        )

    def issue_profile_launch_ticket(
        self,
        ticket: ProfileLaunchTicket,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ProfileLaunchTicket:
        """Persist one immutable ticket; issuance never creates a child."""

        if ticket.state is not ProfileLaunchTicketState.ISSUED:
            raise ValueError("new profile launch ticket must be issued")
        now = _time(now)
        existing = self.read_profile_launch_ticket(ticket.ticket_id)
        if existing is not None:
            if existing == ticket:
                return existing
            raise UnitOfWorkConflict("profile launch ticket identity conflict")
        with self.database.transaction() as connection:
            parent = connection.execute(
                "SELECT state FROM runs WHERE run_id = ?", (ticket.parent_run_id,)
            ).fetchone()
            if parent is None:
                raise UnitOfWorkNotFound(ticket.parent_run_id)
            if str(parent["state"]) in {"completed", "failed", "cancelled"}:
                raise UnitOfWorkConflict("terminal parent cannot issue a launch ticket")
            _fault(fault, "profile_ticket_issue.ticket.before_write")
            connection.execute(
                """
                INSERT INTO profile_launch_tickets(
                    ticket_id, parent_run_id, profile_key, catalog_generation,
                    fingerprint, state, child_run_id, issued_at, claimed_at
                ) VALUES (?, ?, ?, ?, ?, 'issued', NULL, ?, NULL)
                """,
                (
                    ticket.ticket_id,
                    ticket.parent_run_id,
                    ticket.profile_key,
                    ticket.catalog_generation,
                    ticket.fingerprint,
                    now,
                ),
            )
            _fault(fault, "profile_ticket_issue.ticket.after_write")
        _fault(fault, "profile_ticket_issue.after_commit")
        result = self.read_profile_launch_ticket(ticket.ticket_id)
        assert result is not None
        return result

    def claim_profile_launch_and_commit_child(
        self,
        *,
        ticket_id: str,
        expected_catalog_generation: int,
        launch_request: Mapping[str, JsonValue],
        command_id: str,
        child_run_id: str,
        request_id: str,
        attachment_policy: AttachmentPolicy,
        start_snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildLaunchResult:
        """Consume the ticket and commit the complete child start atomically."""

        ticket_id = _required(ticket_id, "ticket_id")
        command_id = _required(command_id, "command_id")
        child_run_id = _required(child_run_id, "child_run_id")
        request_id = _required(request_id, "request_id")
        event_id = _required(event_id, "event_id")
        if (
            isinstance(expected_catalog_generation, bool)
            or expected_catalog_generation < 1
        ):
            raise ValueError("expected_catalog_generation must be positive")
        attachment_policy = AttachmentPolicy(attachment_policy)
        now = _time(now)
        launch_json = _object_json(launch_request, "launch_request")
        launch_fingerprint = child_launch_fingerprint(dict(launch_request))
        snapshot_json = _object_json(start_snapshot, "start_snapshot")
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        profile_key = _required(launch_request.get("profile_key"), "profile_key")
        driver_kind = _required(launch_request.get("driver_kind"), "driver_kind")
        generation = launch_request.get("catalog_generation")
        if generation != expected_catalog_generation:
            raise UnitOfWorkConflict("launch request catalog generation is stale")

        existing_ticket = self.read_profile_launch_ticket(ticket_id)
        if existing_ticket is None:
            raise UnitOfWorkNotFound(ticket_id)
        if existing_ticket.state is ProfileLaunchTicketState.CLAIMED:
            command = self.read_child_command(command_id)
            if (
                existing_ticket.child_run_id == child_run_id
                and command is not None
                and command.ticket_id == ticket_id
                and command.child_run_id == child_run_id
                and self._same_existing_child_launch(
                    command,
                    request_id=request_id,
                    profile_key=profile_key,
                    driver_kind=driver_kind,
                    attachment_policy=attachment_policy,
                    snapshot_hash=snapshot_hash,
                )
            ):
                return ChildLaunchResult(existing_ticket, command, child_run_id)
            raise UnitOfWorkConflict("profile launch ticket was already consumed")
        if existing_ticket.state is not ProfileLaunchTicketState.ISSUED:
            raise UnitOfWorkConflict("profile launch ticket is not claimable")
        if (
            existing_ticket.catalog_generation != expected_catalog_generation
            or existing_ticket.profile_key != profile_key
            or existing_ticket.fingerprint != launch_fingerprint
        ):
            raise UnitOfWorkConflict("profile launch ticket binding mismatch")

        with self.database.transaction() as connection:
            parent = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (existing_ticket.parent_run_id,)
            ).fetchone()
            if parent is None:
                raise UnitOfWorkNotFound(existing_ticket.parent_run_id)
            if str(parent["state"]) in {"completed", "failed", "cancelled"}:
                raise UnitOfWorkConflict("terminal parent cannot launch a child")
            _fault(fault, "child_launch.run.before_write")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, execution_session_id, request_id, root_run_id,
                    parent_run_id, profile_key, driver_kind, state, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'created', 0, ?, ?)
                """,
                (
                    child_run_id,
                    str(parent["execution_session_id"]),
                    request_id,
                    str(parent["root_run_id"]),
                    existing_ticket.parent_run_id,
                    profile_key,
                    driver_kind,
                    now,
                    now,
                ),
            )
            _fault(fault, "child_launch.run.after_write")
            _fault(fault, "child_launch.snapshot.before_write")
            connection.execute(
                """
                INSERT INTO run_start_snapshots(run_id, snapshot_json, snapshot_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (child_run_id, snapshot_json, snapshot_hash, now),
            )
            _fault(fault, "child_launch.snapshot.after_write")
            _fault(fault, "child_launch.ticket.before_write")
            changed = connection.execute(
                """
                UPDATE profile_launch_tickets
                SET state = 'claimed', child_run_id = ?, claimed_at = ?
                WHERE ticket_id = ? AND state = 'issued' AND child_run_id IS NULL
                """,
                (child_run_id, now, ticket_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("profile launch ticket claim CAS failed")
            _fault(fault, "child_launch.ticket.after_write")
            _fault(fault, "child_launch.command.before_write")
            connection.execute(
                """
                INSERT INTO child_commands(
                    command_id, parent_run_id, child_run_id, ticket_id,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    command_id,
                    existing_ticket.parent_run_id,
                    child_run_id,
                    ticket_id,
                    now,
                    now,
                ),
            )
            _fault(fault, "child_launch.command.after_write")
            _fault(fault, "child_launch.link.before_write")
            connection.execute(
                """
                INSERT INTO run_links(parent_run_id, child_run_id, attachment_policy, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    existing_ticket.parent_run_id,
                    child_run_id,
                    attachment_policy.value,
                    now,
                ),
            )
            _fault(fault, "child_launch.link.after_write")
            if attachment_policy is not AttachmentPolicy.DETACHED:
                _fault(fault, "child_launch.parent.before_write")
                changed = connection.execute(
                    """
                    UPDATE runs SET state = 'waiting', version = version + 1, updated_at = ?
                    WHERE run_id = ? AND state NOT IN ('completed', 'failed', 'cancelled')
                    """,
                    (now, existing_ticket.parent_run_id),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("parent wait transition CAS failed")
                _fault(fault, "child_launch.parent.after_write")
            _fault(fault, "child_launch.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=child_run_id,
                kind="child.created",
                payload={
                    "command_id": command_id,
                    "ticket_id": ticket_id,
                    "launch": json.loads(launch_json),
                },
                now=now,
            )
            _fault(fault, "child_launch.event.after_write")
        _fault(fault, "child_launch.after_commit")
        ticket = self.read_profile_launch_ticket(ticket_id)
        command = self.read_child_command(command_id)
        assert ticket is not None and command is not None
        return ChildLaunchResult(ticket, command, child_run_id)

    def finalize_child_and_enqueue_parent_signal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        signal_id: str,
        signal_payload: Mapping[str, JsonValue],
        event_id: str,
        receipt_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildTerminalResult:
        return self._commit_child_terminal(
            command_id=command_id,
            expected_child_version=expected_child_version,
            terminal_state=terminal_state,
            terminal_payload=signal_payload,
            signal_id=signal_id,
            event_id=event_id,
            receipt_id=receipt_id,
            run_fence=run_fence,
            execution_lease=execution_lease,
            now=now,
            fault=fault,
            expect_detached=False,
        )

    def commit_detached_child_terminal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        terminal_payload: Mapping[str, JsonValue],
        event_id: str,
        receipt_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildTerminalResult:
        return self._commit_child_terminal(
            command_id=command_id,
            expected_child_version=expected_child_version,
            terminal_state=terminal_state,
            terminal_payload=terminal_payload,
            signal_id=None,
            event_id=event_id,
            receipt_id=receipt_id,
            run_fence=run_fence,
            execution_lease=execution_lease,
            now=now,
            fault=fault,
            expect_detached=True,
        )

    def _commit_child_terminal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        terminal_payload: Mapping[str, JsonValue],
        signal_id: str | None,
        event_id: str,
        receipt_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None,
        expect_detached: bool,
    ) -> ChildTerminalResult:
        command_id = _required(command_id, "command_id")
        if signal_id is not None:
            signal_id = _required(signal_id, "signal_id")
        event_id = _required(event_id, "event_id")
        receipt_id = _required(receipt_id, "receipt_id")
        if isinstance(expected_child_version, bool) or expected_child_version < 0:
            raise ValueError("expected_child_version must be non-negative")
        terminal_state = RunState(terminal_state)
        if terminal_state not in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise ValueError("child finalization requires a terminal run state")
        payload_json = _object_json(terminal_payload, "terminal_payload")
        outcome_hash = hashlib.sha256(
            canonical_json(
                {
                    "command_id": command_id,
                    "expected_child_version": expected_child_version,
                    "terminal_state": terminal_state.value,
                    "signal_id": signal_id,
                    "event_id": event_id,
                    "payload": json.loads(payload_json),
                }
            ).encode()
        ).hexdigest()
        now = _time(now)
        existing = self._read_child_terminal_receipt(receipt_id)
        if existing is not None:
            return self._replay_child_terminal(
                existing,
                command_id=command_id,
                terminal_state=terminal_state,
                outcome_hash=outcome_hash,
                signal_id=signal_id,
                event_id=event_id,
                run_fence=run_fence,
                execution_lease=execution_lease,
            )
        existing_command = self._read_child_terminal_receipt_for_command(command_id)
        if existing_command is not None:
            raise UnitOfWorkConflict(
                "child terminal command was already committed with another receipt"
            )
        with self.database.transaction() as connection:
            receipt_row = connection.execute(
                "SELECT * FROM child_terminal_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if receipt_row is not None:
                return self._replay_child_terminal(
                    _child_terminal_receipt(receipt_row),
                    command_id=command_id,
                    terminal_state=terminal_state,
                    outcome_hash=outcome_hash,
                    signal_id=signal_id,
                    event_id=event_id,
                    run_fence=run_fence,
                    execution_lease=execution_lease,
                )
            command_receipt = connection.execute(
                "SELECT receipt_id FROM child_terminal_receipts WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if command_receipt is not None:
                raise UnitOfWorkConflict(
                    "child terminal command was already committed with another receipt"
                )
            command_row = connection.execute(
                "SELECT * FROM child_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if command_row is None:
                raise UnitOfWorkNotFound(command_id)
            child_run_id = str(command_row["child_run_id"])
            parent_run_id = str(command_row["parent_run_id"])
            link = connection.execute(
                "SELECT attachment_policy FROM run_links "
                "WHERE parent_run_id=? AND child_run_id=?",
                (parent_run_id, child_run_id),
            ).fetchone()
            if link is None:
                raise UnitOfWorkConflict("child attachment policy is missing")
            policy = AttachmentPolicy(str(link["attachment_policy"]))
            if (policy is AttachmentPolicy.DETACHED) != expect_detached:
                raise UnitOfWorkConflict(
                    "child terminal path differs from durable policy"
                )
            if execution_lease.run_id != child_run_id:
                raise UnitOfWorkConflict("child terminal lease belongs to another Run")
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection, run_fence, execution_lease=execution_lease
            )
            _fault(fault, "child_terminal.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND version = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (
                    terminal_state.value,
                    now,
                    child_run_id,
                    expected_child_version,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("child terminal CAS failed")
            _fault(fault, "child_terminal.run.after_write")
            _fault(fault, "child_terminal.command.before_write")
            changed = connection.execute(
                """
                UPDATE child_commands SET state = 'acked', updated_at = ?
                WHERE command_id = ? AND state IN ('pending', 'scheduled')
                """,
                (now, command_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("child command terminal CAS failed")
            _fault(fault, "child_terminal.command.after_write")
            if signal_id is not None:
                _fault(fault, "child_terminal.signal.before_write")
                connection.execute(
                    """
                    INSERT INTO child_signals(
                        signal_id, parent_run_id, child_run_id, payload_json,
                        state, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (signal_id, parent_run_id, child_run_id, payload_json, now, now),
                )
                _fault(fault, "child_terminal.signal.after_write")
            _fault(fault, "child_terminal.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=child_run_id,
                kind=f"child.{terminal_state.value}",
                payload={
                    "command_id": command_id,
                    "signal_id": signal_id,
                    "terminal": dict(terminal_payload),
                },
                now=now,
            )
            _fault(fault, "child_terminal.event.after_write")
            _fault(fault, "child_terminal.receipt.before_write")
            connection.execute(
                "INSERT INTO child_terminal_receipts("
                "receipt_id,command_id,child_run_id,terminal_state,outcome_hash,"
                "signal_id,event_id,owner_id,runtime_lease_epoch,fence_epoch,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command_id,
                    child_run_id,
                    terminal_state.value,
                    outcome_hash,
                    signal_id,
                    event_id,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    run_fence.epoch,
                    now,
                ),
            )
            _fault(fault, "child_terminal.receipt.after_write")
            _fault(fault, "child_terminal.fence.before_write")
            changed = connection.execute(
                "UPDATE run_fences SET state='released',released_at=? "
                "WHERE run_id=? AND owner_id=? AND runtime_lease_epoch=? "
                "AND epoch=? AND state='active'",
                (
                    now,
                    child_run_id,
                    run_fence.owner_id,
                    run_fence.runtime_lease_epoch,
                    run_fence.epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("child terminal fence release CAS failed")
            _fault(fault, "child_terminal.fence.after_write")
            connection.execute(
                "UPDATE continuations SET state='quarantined',acked_at=?,"
                "version=version+1 WHERE run_id=? AND state IN ('pending','claimed')",
                (now, child_run_id),
            )
        _fault(fault, "child_terminal.after_commit")
        receipt = self._read_child_terminal_receipt(receipt_id)
        assert receipt is not None
        return ChildTerminalResult(
            receipt.child_run_id,
            receipt.terminal_state,
            receipt,
            None if signal_id is None else self.read_child_signal(signal_id),
        )

    def claim_next_child_signal(
        self,
        *,
        parent_run_id: str,
        owner_id: str,
        now: float,
        lease_seconds: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalRecord | None:
        """Claim only the durable oldest non-acked signal for one parent."""

        parent_run_id = _required(parent_run_id, "parent_run_id")
        owner_id = _required(owner_id, "owner_id")
        now = _time(now)
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        expires_at = now + float(lease_seconds)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM child_signals
                WHERE parent_run_id = ? AND state <> 'acked'
                ORDER BY created_at, signal_id
                LIMIT 1
                """,
                (parent_run_id,),
            ).fetchone()
            if row is None:
                return None
            state = ChildSignalState(str(row["state"]))
            if state is ChildSignalState.CLAIMED:
                claim_expires_at = float(row["claim_expires_at"])
                if claim_expires_at > now:
                    return None
            version = int(row["version"])
            claim_epoch = int(row["claim_epoch"])
            _fault(fault, "child_signal_claim.signal.before_write")
            changed = connection.execute(
                """
                UPDATE child_signals
                SET state = 'claimed', version = version + 1,
                    claimed_by = ?, claimed_at = ?, claim_expires_at = ?,
                    claim_epoch = claim_epoch + 1, updated_at = ?
                WHERE signal_id = ? AND version = ?
                  AND (
                    state = 'pending'
                    OR (state = 'claimed' AND claim_expires_at <= ?)
                  )
                """,
                (
                    owner_id,
                    now,
                    expires_at,
                    now,
                    str(row["signal_id"]),
                    version,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("child signal claim CAS failed")
            _fault(fault, "child_signal_claim.signal.after_write")
            expected_epoch = claim_epoch + 1
        _fault(fault, "child_signal_claim.after_commit")
        result = self.read_child_signal(str(row["signal_id"]))
        assert result is not None and result.claim_epoch == expected_epoch
        return result

    def ack_child_signal_and_commit_parent_progress(
        self,
        *,
        signal_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt_id: str,
        continuation_id: str,
        continuation_payload: Mapping[str, JsonValue],
        event_id: str,
        event_payload: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalAckResult:
        signal_id = _required(signal_id, "signal_id")
        owner_id = _required(owner_id, "owner_id")
        receipt_id = _required(receipt_id, "receipt_id")
        continuation_id = _required(continuation_id, "continuation_id")
        event_id = _required(event_id, "event_id")
        if isinstance(claim_epoch, bool) or claim_epoch < 1:
            raise ValueError("claim_epoch must be positive")
        continuation_json = _object_json(continuation_payload, "continuation_payload")
        event_json = _object_json(event_payload, "event_payload")
        continuation_hash = hashlib.sha256(
            continuation_json.encode("utf-8")
        ).hexdigest()
        event_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        now = _time(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM child_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            if row is None:
                raise UnitOfWorkNotFound(signal_id)
            signal = _child_signal_record(row)
            if signal.state is ChildSignalState.ACKED:
                receipt_row = connection.execute(
                    "SELECT * FROM child_signal_ack_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if receipt_row is None:
                    raise UnitOfWorkConflict(
                        "child signal was already acknowledged differently"
                    )
                receipt = _child_signal_ack_receipt(receipt_row)
                if (
                    signal.ack_receipt_id == receipt_id
                    and receipt.signal_id == signal_id
                    and receipt.parent_run_id == signal.parent_run_id
                    and receipt.owner_id == owner_id
                    and receipt.claim_epoch == claim_epoch
                    and receipt.continuation_id == continuation_id
                    and receipt.event_id == event_id
                    and receipt.continuation_payload_hash == continuation_hash
                    and receipt.event_payload_hash == event_hash
                ):
                    return ChildSignalAckResult(signal, receipt)
                raise UnitOfWorkConflict(
                    "child signal was already acknowledged differently"
                )
            if (
                signal.state is not ChildSignalState.CLAIMED
                or signal.claimed_by != owner_id
                or signal.claim_epoch != claim_epoch
                or signal.claim_expires_at is None
                or signal.claim_expires_at <= now
            ):
                raise UnitOfWorkConflict("child signal ack claim is stale or expired")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(fifo_seq), 0) + 1 FROM continuations WHERE run_id = ?",
                    (signal.parent_run_id,),
                ).fetchone()[0]
            )
            _fault(fault, "child_signal_ack.continuation.before_write")
            connection.execute(
                """
                INSERT INTO continuations(
                    continuation_id, run_id, fifo_seq, payload_json, state,
                    version, claimed_by, created_at, claimed_at, acked_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?, NULL, NULL)
                """,
                (
                    continuation_id,
                    signal.parent_run_id,
                    sequence,
                    continuation_json,
                    now,
                ),
            )
            _fault(fault, "child_signal_ack.continuation.after_write")
            _fault(fault, "child_signal_ack.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=signal.parent_run_id,
                kind="child.signal_acked",
                payload=json.loads(event_json),
                now=now,
            )
            _fault(fault, "child_signal_ack.event.after_write")
            _fault(fault, "child_signal_ack.receipt.before_write")
            connection.execute(
                """
                INSERT INTO child_signal_ack_receipts(
                    receipt_id, signal_id, parent_run_id, owner_id, claim_epoch,
                    continuation_id, event_id, continuation_payload_hash,
                    event_payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    signal_id,
                    signal.parent_run_id,
                    owner_id,
                    claim_epoch,
                    continuation_id,
                    event_id,
                    continuation_hash,
                    event_hash,
                    now,
                ),
            )
            _fault(fault, "child_signal_ack.receipt.after_write")
            _fault(fault, "child_signal_ack.signal.before_write")
            changed = connection.execute(
                """
                UPDATE child_signals
                SET state = 'acked', version = version + 1,
                    acked_at = ?, ack_receipt_id = ?, updated_at = ?
                WHERE signal_id = ? AND state = 'claimed'
                  AND claimed_by = ? AND claim_epoch = ?
                  AND claim_expires_at > ?
                """,
                (
                    now,
                    receipt_id,
                    now,
                    signal_id,
                    owner_id,
                    claim_epoch,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("child signal ack CAS failed")
            _fault(fault, "child_signal_ack.signal.after_write")
            link = connection.execute(
                "SELECT attachment_policy FROM run_links WHERE parent_run_id = ? AND child_run_id = ?",
                (signal.parent_run_id, signal.child_run_id),
            ).fetchone()
            if link is None:
                raise UnitOfWorkConflict("child signal has no durable run link")
            if str(link[0]) != AttachmentPolicy.DETACHED.value:
                _fault(fault, "child_signal_ack.parent.before_write")
                changed = connection.execute(
                    """
                    UPDATE runs SET state = 'queued', version = version + 1, updated_at = ?
                    WHERE run_id = ? AND state = 'waiting'
                    """,
                    (now, signal.parent_run_id),
                ).rowcount
                if changed != 1:
                    parent = connection.execute(
                        "SELECT state FROM runs WHERE run_id = ?",
                        (signal.parent_run_id,),
                    ).fetchone()
                    if parent is None or str(parent["state"]) not in {
                        RunState.QUEUED.value,
                        RunState.RUNNING.value,
                    }:
                        raise UnitOfWorkConflict("attached parent wake CAS failed")
                _fault(fault, "child_signal_ack.parent.after_write")
        _fault(fault, "child_signal_ack.after_commit")
        result_signal = self.read_child_signal(signal_id)
        result_receipt = self.read_child_signal_ack_receipt(receipt_id)
        assert result_signal is not None and result_receipt is not None
        return ChildSignalAckResult(result_signal, result_receipt)

    def read_profile_launch_ticket(self, ticket_id: str) -> ProfileLaunchTicket | None:
        row = self.database.connection.execute(
            "SELECT * FROM profile_launch_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return None if row is None else _profile_launch_ticket(row)

    def read_child_command(self, command_id: str) -> ChildCommandRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_commands WHERE command_id = ?", (command_id,)
        ).fetchone()
        return None if row is None else _child_command_record(row)

    def read_child_command_for_run(
        self, child_run_id: str
    ) -> ChildCommandRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_commands WHERE child_run_id = ?", (child_run_id,)
        ).fetchone()
        return None if row is None else _child_command_record(row)

    def read_child_attachment_policy(self, child_run_id: str) -> AttachmentPolicy:
        row = self.database.connection.execute(
            "SELECT attachment_policy FROM run_links WHERE child_run_id = ?",
            (child_run_id,),
        ).fetchone()
        if row is None:
            raise UnitOfWorkNotFound(child_run_id)
        return AttachmentPolicy(str(row["attachment_policy"]))

    def list_child_signal_parent_run_ids(self) -> tuple[str, ...]:
        rows = self.database.connection.execute(
            "SELECT DISTINCT parent_run_id FROM child_signals "
            "WHERE state <> 'acked' ORDER BY parent_run_id"
        ).fetchall()
        return tuple(str(row["parent_run_id"]) for row in rows)

    def read_child_signal(self, signal_id: str) -> ChildSignalRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_signals WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        return None if row is None else _child_signal_record(row)

    def read_child_signal_ack_receipt(
        self, receipt_id: str
    ) -> ChildSignalAckReceipt | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_signal_ack_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        return None if row is None else _child_signal_ack_receipt(row)

    def _same_existing_child_launch(
        self,
        command: ChildCommandRecord,
        *,
        request_id: str,
        profile_key: str,
        driver_kind: str,
        attachment_policy: AttachmentPolicy,
        snapshot_hash: str,
    ) -> bool:
        row = self.database.connection.execute(
            """
            SELECT runs.request_id, runs.profile_key, runs.driver_kind,
                   snapshots.snapshot_hash, links.attachment_policy
            FROM runs
            JOIN run_start_snapshots AS snapshots ON snapshots.run_id = runs.run_id
            JOIN run_links AS links ON links.child_run_id = runs.run_id
            WHERE runs.run_id = ? AND links.parent_run_id = ?
            """,
            (command.child_run_id, command.parent_run_id),
        ).fetchone()
        return row is not None and tuple(row) == (
            request_id,
            profile_key,
            driver_kind,
            snapshot_hash,
            attachment_policy.value,
        )

    def prepare_effect(
        self,
        *,
        effect_id: EffectId,
        run_id: RunId,
        call_id: CallId,
        tool_name: str,
        arguments: dict[str, object],
        request_hash: str,
        authorization_receipt_ref: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        raw_call_id: str | None = None,
        turn_ordinal: int = 0,
        call_ordinal: int = 0,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        tool_name = _required(tool_name, "tool_name")
        authorization_receipt_ref = _required(
            authorization_receipt_ref, "authorization_receipt_ref"
        )
        if len(request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_hash
        ):
            raise ValueError("request_hash must be lowercase SHA-256")
        if (
            run_fence.run_id != run_id
            or run_fence.owner_id != execution_lease.owner_id
            or execution_lease.run_id != run_id.value
        ):
            raise UnitOfWorkConflict("effect runtime lease and Run fence differ")
        now = _time(now)
        arguments_json = canonical_json(arguments)  # type: ignore[arg-type]
        if raw_call_id is not None:
            raw_call_id = _required(raw_call_id, "raw_call_id")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (turn_ordinal, call_ordinal)
        ):
            raise ValueError("effect ordinals must be non-negative integers")
        existing = self.read_effect(effect_id)
        if existing is not None:
            if (
                existing.run_id != run_id
                or existing.call_id != call_id
                or existing.tool_name != tool_name
                or existing.request_hash != request_hash
                or canonical_json(thaw_json(existing.arguments)) != arguments_json
                or existing.raw_call_id != raw_call_id
                or existing.turn_ordinal != turn_ordinal
                or existing.call_ordinal != call_ordinal
            ):
                raise UnitOfWorkConflict("effect identity reused with different intent")
            if existing.state is EffectState.PREPARED and (
                existing.fence_epoch != run_fence.epoch
                or existing.authorization_receipt_ref != authorization_receipt_ref
            ):
                raise UnitOfWorkConflict(
                    "prepared effect authority must use explicit refresh CAS"
                )
            return existing
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection,
                run_fence,
                execution_lease=execution_lease,
            )
            conflict = connection.execute(
                "SELECT effect_id FROM execution_effects WHERE run_id = ? AND call_id = ?",
                (run_id.value, call_id.value),
            ).fetchone()
            if conflict is not None:
                raise UnitOfWorkConflict("call_id is already bound to another effect")
            _fault(fault, "effect_prepare.before_write")
            connection.execute(
                """
                INSERT INTO execution_effects(
                    effect_id, run_id, call_id, raw_call_id, turn_ordinal,
                    call_ordinal, tool_name, arguments_json,
                    request_hash, authorization_receipt_ref, handoff_receipt_ref,
                    evidence_ref, fence_epoch, state, result_json, prepared_at,
                    handed_off_at, settled_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'prepared',
                          NULL, ?, NULL, NULL, 0)
                """,
                (
                    effect_id.value,
                    run_id.value,
                    call_id.value,
                    raw_call_id,
                    turn_ordinal,
                    call_ordinal,
                    tool_name,
                    arguments_json,
                    request_hash,
                    authorization_receipt_ref,
                    run_fence.epoch,
                    now,
                ),
            )
            _fault(fault, "effect_prepare.after_write")
        _fault(fault, "effect_prepare.after_commit")
        record = self.read_effect(effect_id)
        assert record is not None
        return record

    def read_effect(self, effect_id: EffectId) -> EffectRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id = ?",
            (effect_id.value,),
        ).fetchone()
        return None if row is None else _effect_record(row)

    def mark_effect_handed_off(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        run_fence: RunFenceLease,
        handoff_receipt_ref: str,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        handoff_receipt_ref = _required(handoff_receipt_ref, "handoff_receipt_ref")
        now = _time(now)
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection,
                run_fence,
                execution_lease=execution_lease,
            )
            if (
                run_fence.owner_id != execution_lease.owner_id
                or run_fence.run_id.value != execution_lease.run_id
            ):
                raise UnitOfWorkConflict("effect runtime lease and Run fence differ")
            effect_run = connection.execute(
                "SELECT run_id FROM execution_effects WHERE effect_id = ?",
                (effect_id.value,),
            ).fetchone()
            if (
                effect_run is None
                or str(effect_run["run_id"]) != execution_lease.run_id
            ):
                raise UnitOfWorkConflict("effect handoff lease belongs to another Run")
            _fault(fault, "effect_handoff.before_write")
            changed = connection.execute(
                """
                UPDATE execution_effects
                SET state = 'handed_off', handoff_receipt_ref = ?,
                    handed_off_at = ?, handoff_attempt = handoff_attempt + 1,
                    version = version + 1
                WHERE effect_id = ? AND state = 'prepared' AND version = ?
                  AND fence_epoch = ?
                """,
                (
                    handoff_receipt_ref,
                    now,
                    effect_id.value,
                    expected_version,
                    run_fence.epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("effect handoff CAS failed")
            _fault(fault, "effect_handoff.after_write")
        _fault(fault, "effect_handoff.after_commit")
        record = self.read_effect(effect_id)
        assert record is not None
        return record

    def settle_effect(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        result: ToolResult,
        evidence_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        evidence_ref = _required(evidence_ref, "evidence_ref")
        now = _time(now)
        existing = self.read_effect(effect_id)
        if existing is None:
            raise UnitOfWorkNotFound(effect_id.value)
        if result.call_id != existing.call_id:
            raise UnitOfWorkConflict("effect result call_id mismatch")
        state = EffectState(result.outcome.value)
        result_json = (
            None if state is EffectState.UNKNOWN else _tool_result_json(result)
        )
        with self.database.transaction() as connection:
            _fault(fault, "effect_settle.before_write")
            changed = connection.execute(
                """
                UPDATE execution_effects
                SET state = ?, result_json = ?, evidence_ref = ?, settled_at = ?,
                    version = version + 1
                WHERE effect_id = ? AND state IN ('handed_off', 'unknown')
                  AND version = ? AND fence_epoch = ?
                """,
                (
                    state.value,
                    result_json,
                    evidence_ref,
                    now,
                    effect_id.value,
                    expected_version,
                    expected_fence_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("effect settlement CAS failed")
            _fault(fault, "effect_settle.after_write")
        _fault(fault, "effect_settle.after_commit")
        record = self.read_effect(effect_id)
        assert record is not None
        return record

    def mark_effect_unknown(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        evidence_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        evidence_ref = _required(evidence_ref, "evidence_ref")
        now = _time(now)
        with self.database.transaction() as connection:
            _fault(fault, "effect_unknown.before_write")
            changed = connection.execute(
                """
                UPDATE execution_effects
                SET state = 'unknown', evidence_ref = ?, settled_at = ?,
                    result_json = NULL, version = version + 1
                WHERE effect_id = ? AND state = 'handed_off' AND version = ?
                  AND fence_epoch = ?
                """,
                (
                    evidence_ref,
                    now,
                    effect_id.value,
                    expected_version,
                    expected_fence_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("effect unknown CAS failed")
            _fault(fault, "effect_unknown.after_write")
        _fault(fault, "effect_unknown.after_commit")
        record = self.read_effect(effect_id)
        assert record is not None
        return record

    def record_tool_reconciliation(
        self,
        record: EffectRecord,
        *,
        outcome: ResolutionOutcome,
        result: ToolResult | None,
        evidence_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        outcome = ResolutionOutcome(outcome)
        evidence_ref = _required(evidence_ref, "evidence_ref")
        now = _time(now)
        if record.state not in {EffectState.HANDED_OFF, EffectState.UNKNOWN}:
            raise UnitOfWorkConflict("Tool reconciliation requires uncertain ledger")
        if record.handoff_attempt < 1:
            raise UnitOfWorkConflict("Tool reconciliation requires handoff attempt")
        if outcome is ResolutionOutcome.COMPLETED:
            if result is None:
                raise ValueError("completed Tool resolution requires result")
            if result.outcome.value == EffectState.UNKNOWN.value:
                raise ValueError("completed Tool resolution cannot remain unknown")
            if result.call_id != record.call_id:
                result = ToolResult(
                    record.call_id,
                    result.outcome,
                    cast(FrozenJsonValue, thaw_json(result.value)),
                    result.error_code,
                    result.public_message,
                    result.retryable,
                )
            payload: dict[str, JsonValue] = {
                "outcome": outcome.value,
                "evidence_ref": evidence_ref,
                "request_hash": record.request_hash,
                "result": json.loads(_tool_result_json(result)),
            }
        else:
            if result is not None:
                raise ValueError("not-started Tool resolution cannot carry result")
            payload = {
                "outcome": outcome.value,
                "evidence_ref": evidence_ref,
                "request_hash": record.request_hash,
                "result": None,
            }
        outcome_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        resolution_id = f"resolution:{recovery_identity(RecoveryKind.TOOL, record.effect_id.value, record.handoff_attempt)}"
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_effects WHERE effect_id=?",
                (record.effect_id.value,),
            ).fetchone()
            if row is None:
                raise UnitOfWorkNotFound(record.effect_id.value)
            current = _effect_record(row)
            if (
                current.run_id != record.run_id
                or current.call_id != record.call_id
                or current.request_hash != record.request_hash
                or current.handoff_attempt != record.handoff_attempt
            ):
                raise UnitOfWorkConflict("Tool reconciliation identity conflict")
            existing = connection.execute(
                """
                SELECT * FROM reconciliation_resolutions
                WHERE kind='tool' AND ledger_identity=? AND handoff_attempt=?
                """,
                (record.effect_id.value, record.handoff_attempt),
            ).fetchone()
            if existing is not None:
                stored = _reconciliation_resolution(existing)
                if stored.outcome_hash != outcome_hash:
                    raise UnitOfWorkConflict("Tool resolution outcome conflict")
                return current
            if current.version != record.version or current.state not in {
                EffectState.HANDED_OFF,
                EffectState.UNKNOWN,
            }:
                raise UnitOfWorkConflict("Tool reconciliation CAS failed")
            _fault(fault, "tool_reconciliation.resolution.before_write")
            connection.execute(
                """
                INSERT INTO reconciliation_resolutions(
                    resolution_id,kind,ledger_identity,handoff_attempt,outcome,
                    outcome_hash,evidence_ref,payload_json,created_at
                ) VALUES (?, 'tool', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_id,
                    record.effect_id.value,
                    record.handoff_attempt,
                    outcome.value,
                    outcome_hash,
                    evidence_ref,
                    canonical_json(payload),
                    now,
                ),
            )
            _fault(fault, "tool_reconciliation.resolution.after_write")
            if outcome is ResolutionOutcome.COMPLETED:
                assert result is not None
                changed = connection.execute(
                    """
                    UPDATE execution_effects SET state=?, result_json=?, evidence_ref=?,
                        settled_at=?, version=version+1
                    WHERE effect_id=? AND state IN ('handed_off','unknown')
                      AND version=? AND request_hash=? AND handoff_attempt=?
                    """,
                    (
                        result.outcome.value,
                        _tool_result_json(result),
                        evidence_ref,
                        now,
                        record.effect_id.value,
                        record.version,
                        record.request_hash,
                        record.handoff_attempt,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("Tool completed recovery CAS failed")
                _fault(fault, "tool_reconciliation.ledger.after_write")
            connection.execute(
                """
                UPDATE run_wait_blockers SET resolution_id=?, resolved_at=?,
                    version=version+1
                WHERE kind='tool' AND ledger_identity=? AND handoff_attempt=?
                  AND resolution_id IS NULL
                """,
                (resolution_id, now, record.effect_id.value, record.handoff_attempt),
            )
            _fault(fault, "tool_reconciliation.blocker.after_write")
        _fault(fault, "tool_reconciliation.after_commit")
        result_record = self.read_effect(record.effect_id)
        assert result_record is not None
        return result_record

    def reauthorize_effect_not_started(
        self,
        record: EffectRecord,
        *,
        authorization_receipt_ref: str,
        resolution: ReconciliationResolution,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        authorization_receipt_ref = _required(
            authorization_receipt_ref, "authorization_receipt_ref"
        )
        now = _time(now)
        if (
            resolution.kind is not RecoveryKind.TOOL
            or resolution.ledger_identity != record.effect_id.value
            or resolution.handoff_attempt != record.handoff_attempt
            or resolution.outcome is not ResolutionOutcome.CONFIRMED_NOT_STARTED
        ):
            raise UnitOfWorkConflict("Tool reauthorization resolution mismatch")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection, run_fence, execution_lease=execution_lease
            )
            row = connection.execute(
                "SELECT outcome_hash,evidence_ref FROM reconciliation_resolutions WHERE resolution_id=?",
                (resolution.resolution_id,),
            ).fetchone()
            if (
                row is None
                or str(row["outcome_hash"]) != resolution.outcome_hash
                or str(row["evidence_ref"]) != resolution.evidence_ref
            ):
                raise UnitOfWorkConflict("Tool reauthorization evidence mismatch")
            _fault(fault, "effect_reauthorize.before_write")
            changed = connection.execute(
                """
                UPDATE execution_effects
                SET state='prepared', authorization_receipt_ref=?, fence_epoch=?,
                    handoff_receipt_ref=NULL, handed_off_at=NULL, settled_at=NULL,
                    evidence_ref=?, rehandoff_count=rehandoff_count+1,
                    version=version+1
                WHERE effect_id=? AND state='unknown' AND version=?
                  AND request_hash=? AND handoff_attempt=? AND rehandoff_count=0
                """,
                (
                    authorization_receipt_ref,
                    run_fence.epoch,
                    resolution.evidence_ref,
                    record.effect_id.value,
                    record.version,
                    record.request_hash,
                    record.handoff_attempt,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("Tool reauthorization CAS failed")
            _fault(fault, "effect_reauthorize.after_write")
        _fault(fault, "effect_reauthorize.after_commit")
        refreshed = self.read_effect(record.effect_id)
        assert refreshed is not None
        return refreshed

    def refresh_prepared_effect_authority(
        self,
        record: EffectRecord,
        *,
        authorization_receipt_ref: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> EffectRecord:
        authorization_receipt_ref = _required(
            authorization_receipt_ref, "authorization_receipt_ref"
        )
        now = _time(now)
        if record.state is not EffectState.PREPARED:
            raise UnitOfWorkConflict("Tool authority refresh requires PREPARED")
        if record.handoff_attempt == 0 and record.rehandoff_count != 0:
            raise UnitOfWorkConflict("initial PREPARED has invalid retry count")
        if record.handoff_attempt > 0 and record.rehandoff_count != 1:
            raise UnitOfWorkConflict("retry PREPARED lacks one reauthorization")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(
                connection, run_fence, execution_lease=execution_lease
            )
            if record.handoff_attempt > 0:
                resolution = connection.execute(
                    """
                    SELECT evidence_ref FROM reconciliation_resolutions
                    WHERE kind='tool' AND ledger_identity=? AND handoff_attempt=?
                      AND outcome='confirmed_not_started'
                    """,
                    (record.effect_id.value, record.handoff_attempt),
                ).fetchone()
                if (
                    resolution is None
                    or str(resolution["evidence_ref"]) != record.evidence_ref
                ):
                    raise UnitOfWorkConflict("Tool refresh resolution mismatch")
            if (
                record.run_id.value != execution_lease.run_id
                or record.run_id != run_fence.run_id
            ):
                raise UnitOfWorkConflict(
                    "Tool refresh authority belongs to another Run"
                )
            _fault(fault, "effect_refresh.before_write")
            changed = connection.execute(
                """
                UPDATE execution_effects
                SET fence_epoch=?, authorization_receipt_ref=?, version=version+1
                WHERE effect_id=? AND state='prepared' AND version=?
                  AND request_hash=? AND handoff_attempt=? AND rehandoff_count=?
                  AND COALESCE(evidence_ref, '')=COALESCE(?, '')
                """,
                (
                    run_fence.epoch,
                    authorization_receipt_ref,
                    record.effect_id.value,
                    record.version,
                    record.request_hash,
                    record.handoff_attempt,
                    record.rehandoff_count,
                    record.evidence_ref,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("Tool authority refresh CAS failed")
            _fault(fault, "effect_refresh.after_write")
        _fault(fault, "effect_refresh.after_commit")
        refreshed = self.read_effect(record.effect_id)
        assert refreshed is not None
        return refreshed

    async def acquire(
        self,
        run_id: RunId,
        execution_lease: ExecutionLease,
        *,
        now: float,
    ) -> RunFenceLease:
        now = _time(now)
        if (
            execution_lease.run_id != run_id.value
            or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
        ):
            raise UnitOfWorkConflict("Run fence requires the canonical Run lease")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            row = connection.execute(
                "SELECT owner_id, runtime_lease_epoch, epoch, state "
                "FROM run_fences WHERE run_id = ?",
                (run_id.value,),
            ).fetchone()
            if row is not None and tuple(row) == (
                execution_lease.owner_id,
                execution_lease.epoch,
                int(row["epoch"]),
                "active",
            ):
                return RunFenceLease(
                    run_id,
                    int(row["epoch"]),
                    execution_lease.owner_id,
                    execution_lease.epoch,
                )
            epoch = 1 if row is None else int(row["epoch"]) + 1
            connection.execute(
                """
                INSERT INTO run_fences(
                    run_id, owner_id, runtime_lease_epoch, epoch,
                    state, acquired_at, released_at
                ) VALUES (?, ?, ?, ?, 'active', ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    runtime_lease_epoch = excluded.runtime_lease_epoch,
                    epoch = excluded.epoch,
                    state = 'active', acquired_at = excluded.acquired_at,
                    released_at = NULL
                """,
                (
                    run_id.value,
                    execution_lease.owner_id,
                    execution_lease.epoch,
                    epoch,
                    now,
                ),
            )
        return RunFenceLease(
            run_id, epoch, execution_lease.owner_id, execution_lease.epoch
        )

    async def current_epoch(self, run_id: RunId) -> int:
        row = self.database.connection.execute(
            "SELECT epoch FROM run_fences WHERE run_id = ?", (run_id.value,)
        ).fetchone()
        if row is None:
            raise UnitOfWorkNotFound(run_id.value)
        return int(row[0])

    async def release(self, lease: RunFenceLease) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE run_fences SET state = 'released', released_at = ?
                WHERE run_id = ? AND owner_id = ? AND epoch = ? AND state = 'active'
                """,
                (time.time(), lease.run_id.value, lease.owner_id, lease.epoch),
            )

    def claim_provider_invocation(
        self,
        record: ProviderInvocationRecord,
        *,
        budget_policy: BudgetPolicy,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord:
        existing = self._provider_invocation_by_logical_call(
            record.run_id, record.request_id.value
        )
        if existing is not None:
            return existing
        with self.database.transaction() as connection:
            self._require_runtime_lease(
                connection, execution_lease, now=record.claimed_at
            )
            if execution_lease.run_id != record.run_id.value:
                raise UnitOfWorkConflict(
                    "provider invocation lease belongs to another Run"
                )
            existing_row = connection.execute(
                "SELECT * FROM provider_invocations WHERE run_id = ? AND request_id = ?",
                (record.run_id.value, record.request_id.value),
            ).fetchone()
            if existing_row is not None:
                return _provider_invocation_record(existing_row)
            budget_policy.authorize(
                self._provider_budget(connection, record.run_id),
                reservation_micros=record.budget_charge.amount_micros,
            )
            target_json = canonical_json(
                {
                    "provider_id": record.target.provider_id,
                    "model": record.target.model,
                    "pricing_key": record.target.pricing_key,
                    "endpoint_identity": record.target.endpoint_identity,
                    "adapter_key": record.target.adapter_key,
                }
            )
            estimator_json = (
                None
                if record.estimator_snapshot is None
                else canonical_json(_thaw_json(record.estimator_snapshot))
            )
            connection.execute(
                """
                INSERT INTO provider_invocations(
                    invocation_id, run_id, request_id, request_fingerprint,
                    request_json, target_json, target_digest, estimator_json, estimator_digest,
                    state, response_json, usage_json, error_code, claimed_at,
                    handed_off_at, settled_at, version, handoff_attempt, rehandoff_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, NULL, ?, NULL, NULL, ?, 0, 0)
                """,
                (
                    record.invocation_id,
                    record.run_id.value,
                    record.request_id.value,
                    record.request_fingerprint,
                    None
                    if record.request_json is None
                    else canonical_json(_thaw_json(record.request_json)),
                    target_json,
                    record.target_digest,
                    estimator_json,
                    record.estimator_digest,
                    canonical_json(_thaw_json(record.usage_json)),
                    record.claimed_at,
                    record.version,
                ),
            )
        stored = self.read_provider_invocation(record.invocation_id)
        assert stored is not None
        return stored

    def read_provider_invocation(
        self, invocation_id: str
    ) -> ProviderInvocationRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM provider_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        return None if row is None else _provider_invocation_record(row)

    def hand_off_provider_invocation(
        self,
        invocation_id: str,
        *,
        expected_version: int,
        handed_off_at: float,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord:
        handed_off_at = _time(handed_off_at, "handed_off_at")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=handed_off_at)
            invocation_run = connection.execute(
                "SELECT run_id FROM provider_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if (
                invocation_run is None
                or str(invocation_run["run_id"]) != execution_lease.run_id
            ):
                raise UnitOfWorkConflict(
                    "provider handoff lease belongs to another Run"
                )
            changed = connection.execute(
                """
                UPDATE provider_invocations
                SET state = 'handed_off', handed_off_at = ?,
                    handoff_attempt = handoff_attempt + 1, version = version + 1
                WHERE invocation_id = ? AND state = 'claimed' AND version = ?
                """,
                (handed_off_at, invocation_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("provider invocation handoff CAS failed")
        result = self.read_provider_invocation(invocation_id)
        assert result is not None
        return result

    def settle_provider_invocation(
        self,
        record: ProviderInvocationRecord,
        *,
        expected_version: int,
    ) -> ProviderInvocationRecord:
        if record.state not in {
            ProviderInvocationState.SUCCEEDED,
            ProviderInvocationState.FAILED,
            ProviderInvocationState.UNKNOWN,
        }:
            raise ValueError("provider settlement requires a terminal state")
        response_json = (
            None
            if record.response_json is None
            else canonical_json(_thaw_json(record.response_json))
        )
        usage_json = (
            None
            if record.usage_json is None
            else canonical_json(_thaw_json(record.usage_json))
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE provider_invocations
                SET state = ?, response_json = ?, usage_json = ?, error_code = ?,
                    settled_at = ?, version = version + 1
                WHERE invocation_id = ? AND state = 'handed_off' AND version = ?
                  AND request_fingerprint = ? AND target_digest = ?
                  AND COALESCE(estimator_digest, '') = COALESCE(?, '')
                """,
                (
                    record.state.value,
                    response_json,
                    usage_json,
                    record.error_code,
                    record.settled_at,
                    record.invocation_id,
                    expected_version,
                    record.request_fingerprint,
                    record.target_digest,
                    record.estimator_digest,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("provider invocation settlement CAS failed")
        result = self.read_provider_invocation(record.invocation_id)
        assert result is not None
        return result

    def record_provider_reconciliation(
        self,
        record: ProviderInvocationRecord,
        *,
        outcome: ResolutionOutcome,
        response_json: object | None,
        usage_json: object | None,
        budget_charge: BudgetCharge,
        evidence_ref: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ProviderInvocationRecord:
        outcome = ResolutionOutcome(outcome)
        evidence_ref = _required(evidence_ref, "evidence_ref")
        now = _time(now)
        if record.state is not ProviderInvocationState.UNKNOWN:
            raise UnitOfWorkConflict("Provider reconciliation requires unknown ledger")
        if record.handoff_attempt < 1:
            raise UnitOfWorkConflict("Provider reconciliation requires handoff attempt")
        if outcome is ResolutionOutcome.COMPLETED:
            if response_json is None or usage_json is None:
                raise ValueError(
                    "completed Provider resolution requires response and usage"
                )
            response_payload = json.loads(canonical_json(response_json))  # type: ignore[arg-type]
            usage_payload = json.loads(canonical_json(usage_json))  # type: ignore[arg-type]
            if not isinstance(response_payload, dict) or not isinstance(
                usage_payload, dict
            ):
                raise TypeError("Provider resolution payloads must be objects")
            if response_payload.get("request_id") != record.request_id.value:
                raise UnitOfWorkConflict(
                    "Provider resolution request identity mismatch"
                )
            usage_payload = dict(usage_payload)
            usage_payload["budget"] = budget_charge.to_json()
        else:
            if response_json is not None or usage_json is not None:
                raise ValueError(
                    "not-started Provider resolution cannot carry response"
                )
            response_payload = None
            usage_payload = None
        resolution_payload: dict[str, JsonValue] = {
            "outcome": outcome.value,
            "evidence_ref": evidence_ref,
            "request_fingerprint": record.request_fingerprint,
            "target_digest": record.target_digest,
            "response": response_payload,
            "usage": usage_payload,
        }
        outcome_hash = hashlib.sha256(
            canonical_json(resolution_payload).encode()
        ).hexdigest()
        resolution_id = f"resolution:{recovery_identity(RecoveryKind.PROVIDER, record.invocation_id, record.handoff_attempt)}"
        with self.database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM provider_invocations WHERE invocation_id=?",
                (record.invocation_id,),
            ).fetchone()
            if current_row is None:
                raise UnitOfWorkNotFound(record.invocation_id)
            current = _provider_invocation_record(current_row)
            if (
                current.run_id != record.run_id
                or current.request_id != record.request_id
                or current.request_fingerprint != record.request_fingerprint
                or current.target_digest != record.target_digest
                or current.handoff_attempt != record.handoff_attempt
            ):
                raise UnitOfWorkConflict("Provider reconciliation identity conflict")
            existing = connection.execute(
                """
                SELECT * FROM reconciliation_resolutions
                WHERE kind='provider' AND ledger_identity=? AND handoff_attempt=?
                """,
                (record.invocation_id, record.handoff_attempt),
            ).fetchone()
            if existing is not None:
                stored = _reconciliation_resolution(existing)
                if stored.outcome_hash != outcome_hash:
                    raise UnitOfWorkConflict("Provider resolution outcome conflict")
                return current
            if (
                current.state is not ProviderInvocationState.UNKNOWN
                or current.version != record.version
            ):
                raise UnitOfWorkConflict("Provider reconciliation CAS failed")
            _fault(fault, "provider_reconciliation.resolution.before_write")
            connection.execute(
                """
                INSERT INTO reconciliation_resolutions(
                    resolution_id,kind,ledger_identity,handoff_attempt,outcome,
                    outcome_hash,evidence_ref,payload_json,created_at
                ) VALUES (?, 'provider', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_id,
                    record.invocation_id,
                    record.handoff_attempt,
                    outcome.value,
                    outcome_hash,
                    evidence_ref,
                    canonical_json(resolution_payload),
                    now,
                ),
            )
            _fault(fault, "provider_reconciliation.resolution.after_write")
            if outcome is ResolutionOutcome.COMPLETED:
                changed = connection.execute(
                    """
                    UPDATE provider_invocations
                    SET state='succeeded', response_json=?, usage_json=?,
                        error_code=NULL, settled_at=?, version=version+1
                    WHERE invocation_id=? AND state='unknown' AND version=?
                      AND request_fingerprint=? AND target_digest=?
                      AND handoff_attempt=?
                    """,
                    (
                        canonical_json(response_payload),
                        canonical_json(usage_payload),
                        now,
                        record.invocation_id,
                        record.version,
                        record.request_fingerprint,
                        record.target_digest,
                        record.handoff_attempt,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("Provider completed recovery CAS failed")
                _fault(fault, "provider_reconciliation.ledger.after_write")
            connection.execute(
                """
                UPDATE run_wait_blockers SET resolution_id=?, resolved_at=?,
                    version=version+1
                WHERE kind='provider' AND ledger_identity=? AND handoff_attempt=?
                  AND resolution_id IS NULL
                """,
                (resolution_id, now, record.invocation_id, record.handoff_attempt),
            )
            _fault(fault, "provider_reconciliation.blocker.after_write")
        _fault(fault, "provider_reconciliation.after_commit")
        result = self.read_provider_invocation(record.invocation_id)
        assert result is not None
        return result

    def reauthorize_provider_not_started(
        self,
        record: ProviderInvocationRecord,
        *,
        resolution: ReconciliationResolution,
        execution_lease: ExecutionLease,
        now: float,
    ) -> ProviderInvocationRecord:
        now = _time(now)
        if (
            resolution.kind is not RecoveryKind.PROVIDER
            or resolution.ledger_identity != record.invocation_id
            or resolution.handoff_attempt != record.handoff_attempt
            or resolution.outcome is not ResolutionOutcome.CONFIRMED_NOT_STARTED
        ):
            raise UnitOfWorkConflict("Provider reauthorization resolution mismatch")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            if execution_lease.run_id != record.run_id.value:
                raise UnitOfWorkConflict("Provider retry lease belongs to another Run")
            row = connection.execute(
                "SELECT outcome_hash,evidence_ref FROM reconciliation_resolutions WHERE resolution_id=?",
                (resolution.resolution_id,),
            ).fetchone()
            if (
                row is None
                or str(row["outcome_hash"]) != resolution.outcome_hash
                or str(row["evidence_ref"]) != resolution.evidence_ref
            ):
                raise UnitOfWorkConflict("Provider reauthorization evidence mismatch")
            changed = connection.execute(
                """
                UPDATE provider_invocations SET state='claimed', error_code=NULL,
                    handed_off_at=NULL, settled_at=NULL, rehandoff_count=rehandoff_count+1,
                    version=version+1
                WHERE invocation_id=? AND state='unknown' AND version=?
                  AND request_fingerprint=? AND target_digest=?
                  AND handoff_attempt=? AND rehandoff_count=0
                """,
                (
                    record.invocation_id,
                    record.version,
                    record.request_fingerprint,
                    record.target_digest,
                    record.handoff_attempt,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("Provider reauthorization CAS failed")
        refreshed = self.read_provider_invocation(record.invocation_id)
        assert refreshed is not None
        return refreshed

    def list_incomplete_provider_invocations(
        self,
    ) -> tuple[ProviderInvocationRecord, ...]:
        rows = self.database.connection.execute(
            """
            SELECT * FROM provider_invocations
            WHERE state IN ('claimed', 'handed_off', 'unknown')
            ORDER BY claimed_at, invocation_id
            """
        ).fetchall()
        return tuple(_provider_invocation_record(row) for row in rows)

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot:
        return self._provider_budget(self.database.connection, run_id)

    def _provider_budget(
        self, connection: sqlite3.Connection, run_id: RunId
    ) -> BudgetSnapshot:
        committed = 0
        reserved = 0
        unknown = False
        rows = connection.execute(
            "SELECT state, usage_json FROM provider_invocations WHERE run_id = ?",
            (run_id.value,),
        ).fetchall()
        for row in rows:
            usage = json.loads(str(row["usage_json"]))
            charge = BudgetCharge.from_json(usage["budget"])
            state = ProviderInvocationState(str(row["state"]))
            if charge.amount_micros is None:
                unknown = True
            elif state in {
                ProviderInvocationState.CLAIMED,
                ProviderInvocationState.HANDED_OFF,
            }:
                reserved += charge.amount_micros
            else:
                committed += charge.amount_micros
        return BudgetSnapshot(committed, reserved, unknown)

    def _provider_invocation_by_logical_call(
        self, run_id: RunId, request_id: str
    ) -> ProviderInvocationRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM provider_invocations WHERE run_id = ? AND request_id = ?",
            (run_id.value, request_id),
        ).fetchone()
        return None if row is None else _provider_invocation_record(row)

    def read_run(self, run_id: str) -> RunRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _run_record(row)

    def read_start_snapshot(self, run_id: str) -> Mapping[str, JsonValue] | None:
        row = self.database.connection.execute(
            "SELECT snapshot_json FROM run_start_snapshots WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["snapshot_json"]))
        if not isinstance(value, dict):
            raise TypeError("stored start snapshot is not a JSON object")
        return value

    def list_recoverable_root_runs(self) -> tuple[RunRecord, ...]:
        rows = self.database.connection.execute(
            """
            SELECT * FROM runs
            WHERE parent_run_id IS NULL
              AND state IN ('created', 'queued', 'running', 'cancel_requested')
            ORDER BY created_at ASC, run_id ASC
            """
        ).fetchall()
        return tuple(_run_record(row) for row in rows)

    def list_recoverable_child_runs(self) -> tuple[RunRecord, ...]:
        rows = self.database.connection.execute(
            """
            SELECT * FROM runs
            WHERE parent_run_id IS NOT NULL
              AND state IN ('created', 'queued', 'running', 'cancel_requested')
            ORDER BY created_at ASC, run_id ASC
            """
        ).fetchall()
        return tuple(_run_record(row) for row in rows)

    def read_admission(self, admission_id: str) -> AdmissionRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM run_admissions WHERE admission_id = ?", (admission_id,)
        ).fetchone()
        return None if row is None else _admission_record(row)

    def read_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return None if row is None else _decision_record(row)

    def read_continuation(self, continuation_id: str) -> ContinuationRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM continuations WHERE continuation_id = ?",
            (continuation_id,),
        ).fetchone()
        return None if row is None else _continuation_record(row)

    def _run_by_request(self, session_id: str, request_id: str) -> RunRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM runs WHERE execution_session_id = ? AND request_id = ?",
            (session_id, request_id),
        ).fetchone()
        return None if row is None else _run_record(row)

    def _require_runtime_lease(
        self,
        connection: sqlite3.Connection,
        lease: ExecutionLease,
        *,
        now: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT owner_id, epoch, expires_at FROM workflow_leases
            WHERE run_id = ? AND namespace = ?
            """,
            (lease.run_id, lease.namespace),
        ).fetchone()
        if (
            row is None
            or str(row["owner_id"]) != lease.owner_id
            or int(row["epoch"]) != lease.epoch
            or float(row["expires_at"]) <= now
        ):
            raise UnitOfWorkConflict("runtime lease is stale or expired")

    def _require_continuation_claim(
        self,
        connection: sqlite3.Connection,
        claim: ContinuationRecord,
        lease: ExecutionLease,
    ) -> None:
        if claim.run_id != lease.run_id:
            raise UnitOfWorkConflict("continuation claim belongs to another Run")
        row = connection.execute(
            "SELECT state,version,claimed_by,runtime_lease_epoch,claim_epoch "
            "FROM continuations WHERE continuation_id=?",
            (claim.continuation_id,),
        ).fetchone()
        if row is None or tuple(row) != (
            "claimed",
            claim.version,
            lease.owner_id,
            lease.epoch,
            claim.claim_epoch,
        ):
            raise UnitOfWorkConflict("continuation claim is stale")

    def _read_continuation_progress_receipt(
        self, receipt_id: str
    ) -> ContinuationProgressReceipt | None:
        row = self.database.connection.execute(
            "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        return None if row is None else _continuation_progress_receipt(row)

    def _read_child_terminal_receipt(
        self, receipt_id: str
    ) -> ChildTerminalReceipt | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_terminal_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        return None if row is None else _child_terminal_receipt(row)

    def _read_child_terminal_receipt_for_command(
        self, command_id: str
    ) -> ChildTerminalReceipt | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_terminal_receipts WHERE command_id=?",
            (command_id,),
        ).fetchone()
        return None if row is None else _child_terminal_receipt(row)

    def _replay_child_terminal(
        self,
        receipt: ChildTerminalReceipt,
        *,
        command_id: str,
        terminal_state: RunState,
        outcome_hash: str,
        signal_id: str | None,
        event_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
    ) -> ChildTerminalResult:
        if (
            run_fence.owner_id != execution_lease.owner_id
            or run_fence.runtime_lease_epoch != execution_lease.epoch
        ):
            raise UnitOfWorkConflict("child terminal authorities differ")
        expected = (
            command_id,
            terminal_state.value,
            outcome_hash,
            signal_id,
            event_id,
            execution_lease.owner_id,
            execution_lease.epoch,
            run_fence.epoch,
        )
        actual = (
            receipt.command_id,
            receipt.terminal_state,
            receipt.outcome_hash,
            receipt.signal_id,
            receipt.event_id,
            receipt.owner_id,
            receipt.runtime_lease_epoch,
            receipt.fence_epoch,
        )
        if actual != expected or run_fence.run_id.value != receipt.child_run_id:
            raise UnitOfWorkConflict("child terminal receipt differs")
        return ChildTerminalResult(
            receipt.child_run_id,
            receipt.terminal_state,
            receipt,
            None if signal_id is None else self.read_child_signal(signal_id),
        )

    def _replay_continuation_progress(
        self,
        receipt: ContinuationProgressReceipt,
        *,
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        outcome_hash: str,
    ) -> ContinuationProgressResult:
        expected = (
            continuation_claim.continuation_id,
            continuation_claim.run_id,
            execution_lease.owner_id,
            execution_lease.epoch,
            continuation_claim.claim_epoch,
            outcome_hash,
        )
        actual = (
            receipt.continuation_id,
            receipt.run_id,
            receipt.owner_id,
            receipt.runtime_lease_epoch,
            receipt.claim_epoch,
            receipt.outcome_hash,
        )
        if actual != expected:
            raise UnitOfWorkConflict("continuation progress receipt differs")
        run = self.read_run(receipt.run_id)
        continuation = self.read_continuation(receipt.continuation_id)
        if run is None or continuation is None:
            raise UnitOfWorkConflict("continuation progress receipt is orphaned")
        return ContinuationProgressResult(run, continuation, receipt)

    def _replay_continuation_terminal(
        self,
        receipt: ContinuationProgressReceipt,
        *,
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        outcome_hash: str,
    ) -> ContinuationTerminalResult:
        progress = self._replay_continuation_progress(
            receipt,
            continuation_claim=continuation_claim,
            execution_lease=execution_lease,
            outcome_hash=outcome_hash,
        )
        deliveries = tuple(
            _delivery_record(row)
            for row in self.database.connection.execute(
                "SELECT * FROM delivery_outbox WHERE run_id=? ORDER BY delivery_id",
                (receipt.run_id,),
            ).fetchall()
        )
        return ContinuationTerminalResult(
            TerminalCommitResult(progress.run, deliveries),
            progress.continuation,
            progress.receipt,
        )

    def _require_run_fence(
        self,
        connection: sqlite3.Connection,
        fence: RunFenceLease,
        *,
        execution_lease: ExecutionLease,
    ) -> None:
        row = connection.execute(
            "SELECT owner_id, runtime_lease_epoch, epoch, state "
            "FROM run_fences WHERE run_id = ?",
            (fence.run_id.value,),
        ).fetchone()
        if (
            row is None
            or tuple(row)
            != (
                fence.owner_id,
                fence.runtime_lease_epoch,
                fence.epoch,
                "active",
            )
            or fence.runtime_lease_epoch != execution_lease.epoch
        ):
            raise UnitOfWorkConflict("Run fence is stale or inactive")

    def _verify_existing_start(
        self,
        record: RunRecord,
        *,
        run_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot_hash: str,
    ) -> None:
        row = self.database.connection.execute(
            "SELECT snapshot_hash FROM run_start_snapshots WHERE run_id = ?",
            (record.run_id,),
        ).fetchone()
        if (
            record.run_id != run_id
            or record.profile_key != profile_key
            or record.driver_kind != driver_kind
            or row is None
            or row[0] != snapshot_hash
        ):
            raise UnitOfWorkConflict(
                "request identity reused with different root intent"
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, JsonValue],
        now: float,
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(durable_seq), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO run_events(event_id, run_id, durable_seq, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, run_id, sequence, kind, canonical_json(payload), now),
        )


def _thaw(value: object) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    raise TypeError("expected JSON object")


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(type(value).__name__)


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        execution_session_id=str(row["execution_session_id"]),
        request_id=str(row["request_id"]),
        root_run_id=str(row["root_run_id"]),
        parent_run_id=None
        if row["parent_run_id"] is None
        else str(row["parent_run_id"]),
        profile_key=str(row["profile_key"]),
        driver_kind=str(row["driver_kind"]),
        state=RunState(row["state"]),
        version=int(row["version"]),
    )


def _workflow_checkpoint(row: sqlite3.Row) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        run_id=str(row["run_id"]),
        namespace=str(row["namespace"]),
        checkpoint=freeze_json(json.loads(str(row["checkpoint_json"]))),
        checkpoint_hash=str(row["checkpoint_hash"]),
        lease_epoch=int(row["lease_epoch"]),
        version=int(row["version"]),
    )


def _admission_record(row: sqlite3.Row) -> AdmissionRecord:
    prompt = freeze_json(json.loads(str(row["prompt_json"])))
    response = (
        None
        if row["response_json"] is None
        else freeze_json(json.loads(str(row["response_json"])))
    )
    return AdmissionRecord(
        admission_id=str(row["admission_id"]),
        run_id=str(row["run_id"]),
        state=AdmissionState(row["state"]),
        prompt=prompt,
        response=response,
        expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
        version=int(row["version"]),
    )


def _decision_record(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=str(row["decision_id"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        state=DecisionState(row["state"]),
        request=freeze_json(json.loads(str(row["request_json"]))),
        response=(
            None
            if row["response_json"] is None
            else freeze_json(json.loads(str(row["response_json"])))
        ),
        version=int(row["version"]),
    )


def _continuation_record(row: sqlite3.Row) -> ContinuationRecord:
    return ContinuationRecord(
        continuation_id=str(row["continuation_id"]),
        run_id=str(row["run_id"]),
        fifo_seq=int(row["fifo_seq"]),
        payload=freeze_json(json.loads(str(row["payload_json"]))),
        state=ContinuationState(row["state"]),
        version=int(row["version"]),
        claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
        runtime_lease_epoch=(
            None
            if row["runtime_lease_epoch"] is None
            else int(row["runtime_lease_epoch"])
        ),
        claim_epoch=int(row["claim_epoch"]),
        ack_receipt_id=(
            None if row["ack_receipt_id"] is None else str(row["ack_receipt_id"])
        ),
    )


def _continuation_progress_receipt(row: sqlite3.Row) -> ContinuationProgressReceipt:
    return ContinuationProgressReceipt(
        receipt_id=str(row["receipt_id"]),
        continuation_id=str(row["continuation_id"]),
        run_id=str(row["run_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        claim_epoch=int(row["claim_epoch"]),
        outcome_hash=str(row["outcome_hash"]),
    )


def _child_terminal_receipt(row: sqlite3.Row) -> ChildTerminalReceipt:
    return ChildTerminalReceipt(
        receipt_id=str(row["receipt_id"]),
        command_id=str(row["command_id"]),
        child_run_id=str(row["child_run_id"]),
        terminal_state=str(row["terminal_state"]),
        outcome_hash=str(row["outcome_hash"]),
        signal_id=None if row["signal_id"] is None else str(row["signal_id"]),
        event_id=str(row["event_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        fence_epoch=int(row["fence_epoch"]),
    )


def _delivery_record(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        delivery_id=str(row["delivery_id"]),
        run_id=str(row["run_id"]),
        sink_kind=str(row["sink_kind"]),
        idempotency_key=str(row["idempotency_key"]),
        payload=freeze_json(json.loads(str(row["payload_json"]))),
        state=DeliveryState(str(row["state"])),
        version=int(row["version"]),
    )


def _profile_launch_ticket(row: sqlite3.Row) -> ProfileLaunchTicket:
    return ProfileLaunchTicket(
        ticket_id=str(row["ticket_id"]),
        parent_run_id=str(row["parent_run_id"]),
        profile_key=str(row["profile_key"]),
        catalog_generation=int(row["catalog_generation"]),
        fingerprint=str(row["fingerprint"]),
        state=ProfileLaunchTicketState(str(row["state"])),
        child_run_id=(
            None if row["child_run_id"] is None else str(row["child_run_id"])
        ),
    )


def _child_command_record(row: sqlite3.Row) -> ChildCommandRecord:
    return ChildCommandRecord(
        command_id=str(row["command_id"]),
        parent_run_id=str(row["parent_run_id"]),
        child_run_id=str(row["child_run_id"]),
        ticket_id=str(row["ticket_id"]),
        state=ChildCommandState(str(row["state"])),
    )


def _child_signal_record(row: sqlite3.Row) -> ChildSignalRecord:
    return ChildSignalRecord(
        signal_id=str(row["signal_id"]),
        parent_run_id=str(row["parent_run_id"]),
        child_run_id=str(row["child_run_id"]),
        payload=freeze_json(json.loads(str(row["payload_json"]))),
        state=ChildSignalState(str(row["state"])),
        version=int(row["version"]),
        claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
        claimed_at=None if row["claimed_at"] is None else float(row["claimed_at"]),
        claim_expires_at=(
            None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
        ),
        claim_epoch=int(row["claim_epoch"]),
        acked_at=None if row["acked_at"] is None else float(row["acked_at"]),
        ack_receipt_id=(
            None if row["ack_receipt_id"] is None else str(row["ack_receipt_id"])
        ),
    )


def _child_signal_ack_receipt(row: sqlite3.Row) -> ChildSignalAckReceipt:
    return ChildSignalAckReceipt(
        receipt_id=str(row["receipt_id"]),
        signal_id=str(row["signal_id"]),
        parent_run_id=str(row["parent_run_id"]),
        owner_id=str(row["owner_id"]),
        claim_epoch=int(row["claim_epoch"]),
        continuation_id=str(row["continuation_id"]),
        event_id=str(row["event_id"]),
        continuation_payload_hash=str(row["continuation_payload_hash"]),
        event_payload_hash=str(row["event_payload_hash"]),
        created_at=float(row["created_at"]),
    )


def _tool_result_json(result: ToolResult) -> str:
    return canonical_json(
        {
            "call_id": result.call_id.value,
            "outcome": result.outcome.value,
            "value": thaw_json(result.value),
            "error_code": result.error_code,
            "public_message": result.public_message,
            "retryable": result.retryable,
        }
    )


def _tool_result(value: object) -> ToolResult | None:
    if value is None:
        return None
    payload = json.loads(str(value))
    return ToolResult(
        call_id=CallId(str(payload["call_id"])),
        outcome=ToolOutcome(str(payload["outcome"])),
        value=payload.get("value"),
        error_code=payload.get("error_code"),
        public_message=payload.get("public_message"),
        retryable=bool(payload.get("retryable", False)),
    )


def _effect_record(row: sqlite3.Row) -> EffectRecord:
    return EffectRecord(
        effect_id=EffectId(str(row["effect_id"])),
        run_id=RunId(str(row["run_id"])),
        call_id=CallId(str(row["call_id"])),
        tool_name=str(row["tool_name"]),
        request_hash=str(row["request_hash"]),
        arguments=freeze_json(json.loads(str(row["arguments_json"]))),
        state=EffectState(str(row["state"])),
        version=int(row["version"]),
        fence_epoch=int(row["fence_epoch"]),
        authorization_receipt_ref=str(row["authorization_receipt_ref"]),
        handoff_receipt_ref=(
            None
            if row["handoff_receipt_ref"] is None
            else str(row["handoff_receipt_ref"])
        ),
        evidence_ref=(
            None if row["evidence_ref"] is None else str(row["evidence_ref"])
        ),
        result=_tool_result(row["result_json"]),
        raw_call_id=(None if row["raw_call_id"] is None else str(row["raw_call_id"])),
        turn_ordinal=int(row["turn_ordinal"]),
        call_ordinal=int(row["call_ordinal"]),
        handoff_attempt=int(row["handoff_attempt"]),
        rehandoff_count=int(row["rehandoff_count"]),
    )


def _provider_invocation_record(row: sqlite3.Row) -> ProviderInvocationRecord:
    target = json.loads(str(row["target_json"]))
    usage = json.loads(str(row["usage_json"]))
    return ProviderInvocationRecord(
        invocation_id=str(row["invocation_id"]),
        run_id=RunId(str(row["run_id"])),
        request_id=RequestId(str(row["request_id"])),
        state=ProviderInvocationState(str(row["state"])),
        request_fingerprint=str(row["request_fingerprint"]),
        target=ProviderTarget(
            provider_id=str(target["provider_id"]),
            model=str(target["model"]),
            pricing_key=str(target["pricing_key"]),
            endpoint_identity=str(target["endpoint_identity"]),
            adapter_key=str(target["adapter_key"]),
        ),
        target_digest=str(row["target_digest"]),
        estimator_snapshot=(
            None
            if row["estimator_json"] is None
            else json.loads(str(row["estimator_json"]))
        ),
        estimator_digest=(
            None if row["estimator_digest"] is None else str(row["estimator_digest"])
        ),
        budget_charge=BudgetCharge.from_json(usage["budget"]),
        response_json=(
            None
            if row["response_json"] is None
            else json.loads(str(row["response_json"]))
        ),
        usage_json=usage,
        error_code=(None if row["error_code"] is None else str(row["error_code"])),
        claimed_at=float(row["claimed_at"]),
        handed_off_at=(
            None if row["handed_off_at"] is None else float(row["handed_off_at"])
        ),
        settled_at=(None if row["settled_at"] is None else float(row["settled_at"])),
        version=int(row["version"]),
        request_json=(
            None
            if row["request_json"] is None
            else json.loads(str(row["request_json"]))
        ),
        handoff_attempt=int(row["handoff_attempt"]),
        rehandoff_count=int(row["rehandoff_count"]),
    )


def _wait_blocker_record(row: sqlite3.Row) -> WaitBlockerRecord:
    return WaitBlockerRecord(
        blocker_id=str(row["blocker_id"]),
        run_id=str(row["run_id"]),
        kind=RecoveryKind(str(row["kind"])),
        ledger_identity=str(row["ledger_identity"]),
        handoff_attempt=int(row["handoff_attempt"]),
        observed_version=int(row["observed_version"]),
        resolution_id=(
            None if row["resolution_id"] is None else str(row["resolution_id"])
        ),
        wake_consumed=bool(row["wake_consumed"]),
        version=int(row["version"]),
    )


def _reconciliation_resolution(row: sqlite3.Row) -> ReconciliationResolution:
    return ReconciliationResolution(
        resolution_id=str(row["resolution_id"]),
        kind=RecoveryKind(str(row["kind"])),
        ledger_identity=str(row["ledger_identity"]),
        handoff_attempt=int(row["handoff_attempt"]),
        outcome=ResolutionOutcome(str(row["outcome"])),
        outcome_hash=str(row["outcome_hash"]),
        evidence_ref=str(row["evidence_ref"]),
        payload=json.loads(str(row["payload_json"])),
    )


def _wait_activation_receipt(row: sqlite3.Row) -> WaitActivationReceipt:
    return WaitActivationReceipt(
        receipt_id=str(row["receipt_id"]),
        blocker_id=str(row["blocker_id"]),
        run_id=str(row["run_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        outcome_hash=str(row["outcome_hash"]),
    )


def _terminal_projection_prepare(row: sqlite3.Row):  # type: ignore[no-untyped-def]
    from simple_harness.workflow.native import TerminalProjectionPrepareReceipt

    return TerminalProjectionPrepareReceipt(
        operation_id=str(row["operation_id"]),
        run_id=str(row["run_id"]),
        terminal_checkpoint_id=str(row["terminal_checkpoint_id"]),
        descriptor_digest=str(row["descriptor_digest"]),
        input_hash=str(row["input_hash"]),
        output=json.loads(str(row["output_json"])),
        output_hash=str(row["output_hash"]),
        blob_refs=tuple(json.loads(str(row["blob_refs_json"]))),
    )


__all__ = ("SqliteExecutionUnitOfWork",)
