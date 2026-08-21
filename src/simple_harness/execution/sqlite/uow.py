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
from typing import TYPE_CHECKING, TypeVar, cast

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
from simple_harness.execution.context_authority import (
    ProviderProjectionReceipt,
    ToolCatalogSnapshot,
    frozen_payload,
)
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
from simple_harness.providers import ProviderTarget, ProviderToolSpec

if TYPE_CHECKING:
    from simple_harness.execution.memory_outbox import CommittedTurnSpec
from simple_harness.tools.contracts import ToolOutcome, ToolResult
from simple_harness.tools.schema import (
    ArgumentsValidationError,
    SchemaDefinitionError,
    validate_arguments,
)
from simple_harness.workflow.execution_ports import (
    CancelConvergenceLease,
    CancelWorkflowOutcome,
    CancelWorkflowRequest,
    DangerousEffectConfirmation,
    DangerousEffectObservation,
    ForkPhase,
    ForkReceipt,
    ForkRequest,
    ForkWriteLease,
    PrecreatedStartDispatch,
    RecoveryCandidate,
    RecoveryClaim,
    RecoveryOutcome,
    RecoverySnapshot,
    ResumeAdmissionReceipt,
    ResumeAdmissionRequest,
    ResumeCommitBinding,
    ResumePhase,
    StartAdmissionReceipt,
    StartAdmissionRequest,
    StartClaimAction,
    StartMode,
    StartPhase,
    WorkflowActivation,
    WorkflowOperationConflict,
    WorkflowOperationReceipt,
    WorkflowRecoveryWork,
    WorkflowTransaction,
    start_admission_request_from_json,
    start_admission_request_to_json,
)
from simple_harness.workflow.lease import WorkflowLease

from .database import Database

if TYPE_CHECKING:
    from simple_harness.runtime.orchestration import (
        RuntimeActivationClaim,
        RuntimeStartActivation,
        RuntimeStartAdmission,
        RuntimeStartDispatchClaim,
        RuntimeStartDispatchRecord,
        RuntimeStartReceipt,
        VerifiedWorkflowCatalogAuthority,
        VerifiedWorkflowGraphUnavailable,
        VerifiedWorkflowLaunchTicket,
        WorkflowCatalogAuthority,
        WorkflowLaunchRequest,
        WorkflowLaunchTicket,
        WorkflowSpawnContinuationClaim,
        WorkflowSpawnContinuationReady,
        WorkflowSpawnIssueAuthority,
        WorkflowSpawnReadyActivation,
    )
    from simple_harness.runtime.start_snapshot import RunStart, StartSnapshot
    from simple_harness.runtime.workflow_spawn import (
        WorkflowSpawnAdmissionOutcome,
        WorkflowSpawnToolOutcome,
    )
    from simple_harness.workflow.execution_ports import WorkflowTerminalOutcome

_WorkflowResult = TypeVar("_WorkflowResult")


class _SqliteWorkflowTransaction:
    __slots__ = (
        "_after_commit_fault_points",
        "_fault",
        "connection",
        "is_open",
        "transaction_owner",
    )

    def __init__(
        self, owner: Database, connection: sqlite3.Connection, fault: FaultHook | None
    ) -> None:
        self.transaction_owner = owner
        self.connection = connection
        self.is_open = True
        self._fault = fault
        self._after_commit_fault_points: list[str] = []

    def register_after_commit_fault(self, point: str) -> None:
        if point not in self._after_commit_fault_points:
            self._after_commit_fault_points.append(point)

    async def read_workflow_operation(self, operation_id: str) -> WorkflowOperationReceipt | None:
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
            checkpoint_id=(None if row["checkpoint_id"] is None else str(row["checkpoint_id"])),
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
            receipt = self.connection.execute(
                "SELECT phase,version FROM workflow_start_admissions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if receipt is not None and str(receipt["phase"]) == StartPhase.CLAIMED.value:
                self.connection.execute(
                    "UPDATE workflow_start_admissions SET"
                    " phase='running',version=version+1,updated_at=? WHERE run_id=? AND"
                    " phase='claimed' AND version=?",
                    (now, run_id, int(receipt["version"])),
                )
            outcome: JsonValue = {
                "changed": bool(changed),
                "state": str(row["state"]),
                "version": int(row["version"]),
            }
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
            outcome = {
                "decision_ids": list(decision_ids),
                "checkpoint_id": checkpoint_id,
            }
            _fault(self._fault, f"workflow_adapter.{adapter_method}.after_ledger")
            return outcome
        if adapter_method == "open_decision":
            interrupt_id = _required(payload.get("interrupt_id"), "interrupt_id")
            request = payload.get("request")
            if not isinstance(request, dict):
                raise WorkflowOperationConflict("decision request must be an object")
            existing = self.connection.execute(
                "SELECT request_json,state FROM decisions WHERE decision_id=?",
                (interrupt_id,),
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
                (
                    interrupt_id,
                    run_id,
                    str(request.get("kind", "workflow_interrupt")),
                    request_json,
                    now,
                ),
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
            event_id = hashlib.sha256(f"{run_id}|decision.open|{interrupt_id}".encode()).hexdigest()
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
                    event_id,
                    run_id,
                    sequence,
                    canonical_json(
                        {
                            "decision_id": interrupt_id,
                            "kind": str(request.get("kind", "workflow_interrupt")),
                        }
                    ),
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
            sequence = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(durable_seq),0)+1 FROM run_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    event_id,
                    run_id,
                    sequence,
                    str(intent.get("event_type", "workflow.event")),
                    canonical_json(intent),
                    now,
                ),
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
                    "INSERT INTO"
                    " workflow_checkpoint_effect_links(run_id,namespace,checkpoint_id,effect_id,created_at)"  # noqa: E501
                    " VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        namespace,
                        checkpoint_id,
                        _required(effect_id, "effect_id"),
                        now,
                    ),
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

    async def write_workflow_operation(self, receipt: WorkflowOperationReceipt) -> None:
        _fault(self._fault, f"workflow_adapter.{receipt.adapter_method}.before_receipt")
        self.connection.execute(
            """
            INSERT INTO workflow_operation_receipts(
                operation_id,adapter_method,identity_json,payload_hash,outcome_json,
                run_id,namespace,checkpoint_id,lease_epoch,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.operation_id,
                receipt.adapter_method,
                canonical_json(list(receipt.identity)),
                receipt.payload_hash,
                canonical_json(receipt.outcome),
                receipt.run_id,
                receipt.namespace,
                receipt.checkpoint_id,
                receipt.lease_epoch,
                receipt.created_at,
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


def _positive_ttl(value: object) -> float:
    ttl = _time(value, "ttl_seconds")
    if ttl <= 0:
        raise ValueError("ttl_seconds must be positive")
    return ttl


def _object_json(value: Mapping[str, JsonValue], name: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a JSON object")
    return canonical_json(value)


def _fault(hook: FaultHook | None, point: str) -> None:
    if hook is not None:
        hook(point)


def _stage_pair(stage_id: str | None, stage_hash: str | None) -> None:
    if (stage_id is None) != (stage_hash is None):
        raise ValueError("context stage id/hash must be present together")
    if stage_id is not None:
        _required(stage_id, "context_stage_id")
        if len(stage_hash or "") != 64:
            raise ValueError("context_stage_hash must be a SHA-256 digest")


def _insert_committed_turn(
    connection: sqlite3.Connection,
    intent: CommittedTurnSpec,
    *,
    run_id: str,
    now: float,
) -> None:
    turn = intent.turn
    connection.execute(
        "INSERT INTO memory_outbox("
        "intent_id,run_id,turn_id,deployment_id,household_id,actor_id,session_id,"
        "payload_json,payload_hash,state,claim_owner,claim_epoch,claim_expires_at,"
        "attempt_count,retry_at,error_code,created_at,settled_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,0,NULL,0,?,NULL,?,NULL)",
        (
            intent.intent_id,
            run_id,
            turn.turn_id,
            turn.identity.deployment_id,
            turn.identity.household_id,
            turn.identity.actor_id,
            turn.identity.session_id,
            intent.payload_json,
            intent.payload_hash,
            now,
            now,
        ),
    )


def _validate_committed_turn(
    connection: sqlite3.Connection,
    intent: CommittedTurnSpec,
    *,
    run_id: str,
) -> None:
    row = connection.execute(
        "SELECT r.execution_session_id,b.deployment_id,b.household_id,b.actor_id "
        "FROM runs AS r JOIN agent_identity_bindings AS b "
        "ON b.session_id=r.execution_session_id WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise UnitOfWorkConflict("committed turn lacks trusted identity binding")
    turn = intent.turn
    if (
        turn.identity.session_id != str(row["execution_session_id"])
        or turn.identity.deployment_id != str(row["deployment_id"])
        or turn.identity.household_id != str(row["household_id"])
        or turn.identity.actor_id != str(row["actor_id"])
    ):
        raise UnitOfWorkConflict("committed turn identity binding differs")
    if intent.payload_json != canonical_json(turn.canonical_payload()):
        raise UnitOfWorkConflict("committed turn canonical payload differs")
    if intent.payload_hash != turn.payload_hash:
        raise UnitOfWorkConflict("committed turn payload hash differs")


def _verify_committed_turn_replay(
    connection: sqlite3.Connection,
    intent: CommittedTurnSpec | None,
    *,
    run_id: str,
) -> None:
    rows = connection.execute(
        "SELECT intent_id,payload_json,payload_hash FROM memory_outbox WHERE run_id=?",
        (run_id,),
    ).fetchall()
    if intent is None:
        if rows:
            raise UnitOfWorkConflict("committed-turn replay differs")
        return
    expected = (intent.intent_id, intent.payload_json, intent.payload_hash)
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise UnitOfWorkConflict("committed-turn replay differs")


def _consume_context_stage(
    connection: sqlite3.Connection,
    *,
    stage_id: str,
    stage_hash: str,
    kind: str,
    expected_snapshot: JsonValue | None,
    consumed_run_id: str | None,
    consumed_continuation_id: str | None,
    now: float,
) -> None:
    row = connection.execute(
        "SELECT kind,state,private_snapshot,private_snapshot_hash FROM "
        "context_preparation_staging WHERE stage_id=?",
        (stage_id,),
    ).fetchone()
    if row is None or str(row["kind"]) != kind or str(row["state"]) != "staged":
        raise UnitOfWorkConflict("context stage is not available for this command")
    raw = bytes(row["private_snapshot"])
    if str(row["private_snapshot_hash"]) != stage_hash:
        raise UnitOfWorkConflict("context stage hash differs")
    if hashlib.sha256(raw).hexdigest() != stage_hash:
        raise UnitOfWorkConflict("context stage bytes are corrupt")
    if not isinstance(expected_snapshot, Mapping) or (
        canonical_json(expected_snapshot).encode("utf-8") != raw
    ):
        raise UnitOfWorkConflict("command private context differs from durable stage")
    changed = connection.execute(
        "UPDATE context_preparation_staging SET state='consumed',private_snapshot=NULL,"
        "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,consumed_run_id=?,"
        "consumed_continuation_id=?,updated_at=? WHERE stage_id=? AND state='staged' "
        "AND private_snapshot_hash=?",
        (
            consumed_run_id,
            consumed_continuation_id,
            now,
            stage_id,
            stage_hash,
        ),
    ).rowcount
    if changed != 1:
        raise UnitOfWorkConflict("context stage consume CAS failed")


def _verify_stage_replay(
    connection: sqlite3.Connection,
    *,
    stage_id: str | None,
    stage_hash: str | None,
    consumed_run_id: str | None,
    consumed_continuation_id: str | None,
) -> None:
    _stage_pair(stage_id, stage_hash)
    if stage_id is None:
        return
    row = connection.execute(
        "SELECT state,private_snapshot_hash,consumed_run_id,consumed_continuation_id "
        "FROM context_preparation_staging WHERE stage_id=?",
        (stage_id,),
    ).fetchone()
    expected = ("consumed", stage_hash, consumed_run_id, consumed_continuation_id)
    if row is None or tuple(row) != expected:
        raise UnitOfWorkConflict("context stage replay differs")


class SqliteExecutionUnitOfWork:
    __slots__ = ("database", "workflow_fault")

    def __init__(self, database: Database, *, workflow_fault: FaultHook | None = None) -> None:
        self.database = database
        self.workflow_fault = workflow_fault

    def close(self) -> None:
        """Close the owned database (idempotent)."""
        self.database.close()

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
            transaction = _SqliteWorkflowTransaction(self.database, connection, self.workflow_fault)
            try:
                result = await operation(transaction)
                _fault(self.workflow_fault, f"{fault_label}.before_commit")
            finally:
                transaction.is_open = False
        for point in transaction._after_commit_fault_points:
            _fault(self.workflow_fault, point)
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
            workflow_namespace: str | None = None
            if run.driver_kind == "workflow":
                admission = connection.execute(
                    "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if admission is not None:
                    workflow_namespace = str(
                        json.loads(str(admission["request_json"]))["checkpoint_namespace"]
                    )
            lease_row = connection.execute(
                "SELECT owner_id, epoch, expires_at FROM workflow_leases "
                "WHERE run_id = ? AND namespace = ?",
                (run_id, namespace),
            ).fetchone()
            if lease_row is not None and float(lease_row["expires_at"]) > now:
                if str(lease_row["owner_id"]) != owner_id:
                    raise UnitOfWorkConflict("Run already has an active runtime owner")
                if workflow_namespace is not None:
                    projection = connection.execute(
                        "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                        "WHERE run_id=? AND namespace=?",
                        (run_id, workflow_namespace),
                    ).fetchone()
                    if projection is None or tuple(projection) != (
                        owner_id,
                        int(lease_row["epoch"]),
                        float(lease_row["expires_at"]),
                    ):
                        raise UnitOfWorkConflict(
                            "active workflow Runtime lacks its lease projection"
                        )
                lease = ExecutionLease(
                    run_id,
                    namespace,
                    owner_id,
                    int(lease_row["epoch"]),
                    float(lease_row["expires_at"]),
                )
                return run, lease
            fence_row = connection.execute(
                "SELECT runtime_lease_epoch FROM run_fences WHERE run_id=?",
                (run_id,),
            ).fetchone()
            prior_epoch = max(
                0 if lease_row is None else int(lease_row["epoch"]),
                0 if fence_row is None else int(fence_row["runtime_lease_epoch"]),
            )
            epoch = prior_epoch + 1
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
            if workflow_namespace is not None:
                _fault(fault, "runtime_activation.workflow_lease.before_write")
                connection.execute(
                    """
                    INSERT INTO workflow_leases(
                        run_id, namespace, owner_id, epoch, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, namespace) DO UPDATE SET
                        owner_id=excluded.owner_id,
                        epoch=excluded.epoch,
                        expires_at=excluded.expires_at
                    """,
                    (run_id, workflow_namespace, owner_id, epoch, expires_at),
                )
                _fault(fault, "runtime_activation.workflow_lease.after_write")
            event_kind = "run.recovered" if run.state is RunState.RUNNING else "run.activated"
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
            dispatch = connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at "
                "FROM runtime_start_dispatch_claims "
                "WHERE run_id=? AND state='claimed'",
                (lease.run_id,),
            ).fetchone()
            if dispatch is not None and tuple(dispatch) != (
                lease.owner_id,
                lease.epoch,
                lease.expires_at,
            ):
                raise UnitOfWorkConflict(
                    "runtime start dispatch record is not co-fenced with runtime"
                )
            projection_rows = connection.execute(
                """
                SELECT namespace, owner_id, epoch, expires_at
                FROM workflow_leases
                WHERE run_id = ? AND namespace != ?
                ORDER BY namespace
                """,
                (lease.run_id, lease.namespace),
            ).fetchall()
            owned_projection_rows: list[sqlite3.Row] = []
            for projection in projection_rows:
                same_authority = (
                    str(projection["owner_id"]) == lease.owner_id
                    and int(projection["epoch"]) == lease.epoch
                )
                if same_authority and float(projection["expires_at"]) != lease.expires_at:
                    raise UnitOfWorkConflict(
                        "workflow projection lease is not co-fenced with runtime"
                    )
                if not same_authority and float(projection["expires_at"]) > now:
                    raise UnitOfWorkConflict(
                        "workflow projection lease is not co-fenced with runtime"
                    )
                if same_authority:
                    owned_projection_rows.append(projection)
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
            if dispatch is not None:
                _fault(fault, "runtime_lease_release.dispatch.before_write")
                changed = connection.execute(
                    "UPDATE runtime_start_dispatch_claims SET"
                    " expires_at=?,version=version+1,updated_at=? WHERE run_id=? AND owner_id=? AND"
                    " runtime_lease_epoch=? AND expires_at=?",
                    (
                        now,
                        now,
                        lease.run_id,
                        lease.owner_id,
                        lease.epoch,
                        lease.expires_at,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("runtime start dispatch release CAS failed")
                _fault(fault, "runtime_lease_release.dispatch.after_write")
            for projection in owned_projection_rows:
                namespace = str(projection["namespace"])
                _fault(
                    fault,
                    f"runtime_lease_release.{namespace}.before_projection_write",
                )
                changed = connection.execute(
                    """
                    UPDATE workflow_leases SET expires_at = ?
                    WHERE run_id = ? AND namespace = ? AND owner_id = ?
                      AND epoch = ? AND expires_at = ?
                    """,
                    (
                        now,
                        lease.run_id,
                        namespace,
                        lease.owner_id,
                        lease.epoch,
                        lease.expires_at,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("workflow projection lease release CAS failed")
                _fault(
                    fault,
                    f"runtime_lease_release.{namespace}.after_projection_write",
                )
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
            dispatch = connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at "
                "FROM runtime_start_dispatch_claims "
                "WHERE run_id=? AND state='claimed'",
                (lease.run_id,),
            ).fetchone()
            if dispatch is not None and tuple(dispatch) != (
                lease.owner_id,
                lease.epoch,
                lease.expires_at,
            ):
                raise UnitOfWorkConflict(
                    "runtime start dispatch record is not co-fenced with runtime"
                )
            projection_rows = connection.execute(
                """
                SELECT namespace, owner_id, epoch, expires_at
                FROM workflow_leases
                WHERE run_id = ? AND namespace != ?
                ORDER BY namespace
                """,
                (lease.run_id, lease.namespace),
            ).fetchall()
            owned_projection_rows: list[sqlite3.Row] = []
            for projection in projection_rows:
                same_authority = (
                    str(projection["owner_id"]) == lease.owner_id
                    and int(projection["epoch"]) == lease.epoch
                )
                if same_authority and float(projection["expires_at"]) != lease.expires_at:
                    raise UnitOfWorkConflict(
                        "workflow projection lease is not co-fenced with runtime"
                    )
                if not same_authority and float(projection["expires_at"]) > now:
                    raise UnitOfWorkConflict(
                        "workflow projection lease is not co-fenced with runtime"
                    )
                if same_authority:
                    owned_projection_rows.append(projection)
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
            if dispatch is not None:
                _fault(fault, "runtime_lease_renew.dispatch.before_write")
                changed = connection.execute(
                    "UPDATE runtime_start_dispatch_claims SET"
                    " expires_at=?,version=version+1,updated_at=? WHERE run_id=? AND owner_id=? AND"
                    " runtime_lease_epoch=? AND expires_at=?",
                    (
                        expires_at,
                        now,
                        lease.run_id,
                        lease.owner_id,
                        lease.epoch,
                        lease.expires_at,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("runtime start dispatch renew CAS failed")
                _fault(fault, "runtime_lease_renew.dispatch.after_write")
            for projection in owned_projection_rows:
                namespace = str(projection["namespace"])
                _fault(
                    fault,
                    f"runtime_lease_renew.{namespace}.before_projection_write",
                )
                changed = connection.execute(
                    """
                    UPDATE workflow_leases SET expires_at = ?
                    WHERE run_id = ? AND namespace = ? AND owner_id = ?
                      AND epoch = ? AND expires_at = ? AND expires_at > ?
                    """,
                    (
                        expires_at,
                        lease.run_id,
                        namespace,
                        lease.owner_id,
                        lease.epoch,
                        lease.expires_at,
                        now,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("workflow projection lease renew CAS failed")
                _fault(
                    fault,
                    f"runtime_lease_renew.{namespace}.after_projection_write",
                )
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
            completed_states: tuple[str, ...]
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
            resolution_outcome = None if resolution is None else str(resolution["outcome"])
            if resolution_outcome == ResolutionOutcome.COMPLETED.value:
                if (
                    str(ledger["state"]) not in completed_states
                    or int(ledger["version"]) != blocker.observed_version + 1
                ):
                    raise UnitOfWorkConflict("completed wait resolution and ledger differ")
            elif (
                str(ledger["state"]) != unresolved_state
                or int(ledger["version"]) != blocker.observed_version
            ):
                raise UnitOfWorkConflict("wait blocker observed ledger version is stale")
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
                    raise UnitOfWorkConflict("activation receipt belongs to another Runtime owner")
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
            WaitActivationReceipt(receipt_id, blocker_id, run_id, owner_id, epoch, outcome_hash),
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
            open_decisions = connection.execute(
                "SELECT decision_id FROM decisions WHERE run_id=? AND state='open'",
                (run_id,),
            ).fetchall()
            for row in open_decisions:
                decision_id = str(row["decision_id"])
                _fault(fault, "runtime_cancel.decision.before_write")
                changed = connection.execute(
                    "UPDATE decisions SET state='cancelled',response_json=?,"
                    "version=version+1,resolved_at=? "
                    "WHERE decision_id=? AND run_id=? AND state='open'",
                    (
                        canonical_json({"reason": "run_cancelled"}),
                        now,
                        decision_id,
                        run_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("decision cancellation CAS failed")
                self._insert_event(
                    connection,
                    event_id=f"{event_id}:decision:{decision_id}",
                    run_id=run_id,
                    kind="decision.cancelled",
                    payload={"decision_id": decision_id},
                    now=now,
                )
                _fault(fault, "runtime_cancel.decision.after_write")
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

    def ack_spawn_child_continuation_and_continue_batch(
        self,
        *,
        run_id: str,
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCheckpoint:
        """ACK one child signal and restore its suspended ordered Tool batch."""

        run_id = _required(run_id, "run_id")
        now = _time(now)
        if continuation_claim.run_id != run_id or execution_lease.run_id != run_id:
            raise UnitOfWorkConflict("workflow spawn continuation belongs elsewhere")
        with self.database.transaction() as connection:
            wait = connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts WHERE continuation_id=?",
                (continuation_claim.continuation_id,),
            ).fetchone()
            if wait is None:
                raise UnitOfWorkNotFound(continuation_claim.continuation_id)
            latest = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE run_id=? "
                "AND namespace='react.termination.v1' "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if latest is None:
                raise UnitOfWorkConflict("workflow spawn ReAct checkpoint is missing")
            if str(wait["state"]) in {
                "acked_completion_pending",
                "acked",
            }:
                receipt = connection.execute(
                    "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
                    (wait["progress_receipt_id"],),
                ).fetchone()
                continuation = connection.execute(
                    "SELECT state,ack_receipt_id FROM continuations WHERE continuation_id=?",
                    (continuation_claim.continuation_id,),
                ).fetchone()
                if (
                    receipt is None
                    or continuation is None
                    or str(continuation["state"]) != "acked"
                    or continuation["ack_receipt_id"] != wait["progress_receipt_id"]
                    or str(receipt["owner_id"]) != continuation_claim.claimed_by
                    or int(receipt["runtime_lease_epoch"]) != continuation_claim.runtime_lease_epoch
                    or int(receipt["claim_epoch"]) != continuation_claim.claim_epoch
                ):
                    raise UnitOfWorkConflict("workflow spawn continuation replay differs")
                return _workflow_checkpoint(latest)
            if str(wait["state"]) != "claimed":
                raise UnitOfWorkConflict("workflow spawn child wait is not claimed")
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
            self._require_continuation_claim(connection, continuation_claim, execution_lease)
            run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or str(run["state"]) != RunState.RUNNING.value:
                raise UnitOfWorkConflict("workflow spawn continuation requires a RUNNING parent")
            checkpoint_payload = json.loads(str(latest["checkpoint_json"]))
            if (
                not isinstance(checkpoint_payload, dict)
                or checkpoint_payload.get("phase") != "child_wait"
                or int(latest["version"]) != int(wait["react_checkpoint_revision"])
                or str(latest["checkpoint_hash"]) != str(wait["react_checkpoint_hash"])
                or checkpoint_payload.get("workflow_spawn_operation_id")
                != str(wait["spawn_operation_id"])
                or checkpoint_payload.get("workflow_spawn_child_run_id")
                != str(wait["child_run_id"])
            ):
                raise UnitOfWorkConflict("workflow spawn suspended ReAct checkpoint differs")
            continuation_payload = thaw_json(continuation_claim.payload)
            if (
                not isinstance(continuation_payload, dict)
                or continuation_payload.get("kind") != "child_terminal"
                or continuation_payload.get("signal_id") != wait["child_signal_id"]
                or continuation_payload.get("child_run_id") != wait["child_run_id"]
                or not isinstance(continuation_payload.get("payload"), dict)
            ):
                raise UnitOfWorkConflict("workflow spawn child completion payload differs")
            pending_completion: dict[str, JsonValue] = {
                "schema_version": "workflow_child_completion.v1",
                "child_run_id": str(wait["child_run_id"]),
                "terminal": cast(JsonValue, continuation_payload["payload"]),
            }
            pending_json = canonical_json(pending_completion)
            pending_hash = hashlib.sha256(pending_json.encode()).hexdigest()
            append_id = self._derived_id(
                "workflow-spawn/child-completion-append/v1",
                str(wait["spawn_operation_id"]),
            )
            receipt_id = self._derived_id(
                "workflow-spawn/continuation-progress/v1",
                f"{continuation_claim.continuation_id}:{continuation_claim.claim_epoch}",
            )
            outcome_hash = hashlib.sha256(
                canonical_json(
                    {
                        "run_id": run_id,
                        "continuation_id": continuation_claim.continuation_id,
                        "claim_epoch": continuation_claim.claim_epoch,
                        "parent_wait_receipt_id": wait["parent_wait_receipt_id"],
                        "pending_child_completion_hash": pending_hash,
                    }
                ).encode()
            ).hexdigest()
            _fault(fault, "workflow:spawn_child_continue:before_progress_write")
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
            _fault(fault, "workflow:spawn_child_continue:after_progress_write")
            _fault(fault, "workflow:spawn_child_continue:before_continuation_write")
            changed = connection.execute(
                "UPDATE continuations SET state='acked',acked_at=?,ack_receipt_id=?,"
                "version=version+1 WHERE continuation_id=? AND state='claimed' "
                "AND claimed_by=? AND runtime_lease_epoch=? AND claim_epoch=? "
                "AND version=?",
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
                raise UnitOfWorkConflict("workflow spawn continuation ACK CAS failed")
            _fault(fault, "workflow:spawn_child_continue:after_continuation_write")
            next_wait_version = int(wait["version"]) + 1
            lifecycle_hash = hashlib.sha256(
                canonical_json(
                    {
                        "identity_hash": str(wait["identity_hash"]),
                        "state": "acked_completion_pending",
                        "version": next_wait_version,
                        "child_signal_id": wait["child_signal_id"],
                        "continuation_id": continuation_claim.continuation_id,
                        "progress_receipt_id": receipt_id,
                        "pending_child_completion_hash": pending_hash,
                    }
                ).encode()
            ).hexdigest()
            _fault(fault, "workflow:spawn_child_continue:before_wait_write")
            changed = connection.execute(
                "UPDATE workflow_spawn_child_wait_receipts SET "
                "state='acked_completion_pending',progress_receipt_id=?,"
                "pending_child_completion_json=?,pending_child_completion_hash=?,"
                "child_completion_append_id=?,version=?,lifecycle_hash=? "
                "WHERE parent_wait_receipt_id=? AND state='claimed' AND version=?",
                (
                    receipt_id,
                    pending_json,
                    pending_hash,
                    append_id,
                    next_wait_version,
                    lifecycle_hash,
                    str(wait["parent_wait_receipt_id"]),
                    int(wait["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn child-wait ACK CAS failed")
            _fault(fault, "workflow:spawn_child_continue:after_wait_write")
            next_checkpoint = dict(checkpoint_payload)
            next_checkpoint.update(
                {
                    "phase": "tool_batch_reserved",
                    "tool_result_progress": int(wait["next_tool_ordinal"]),
                    "workflow_spawn_wait_receipt_id": str(wait["parent_wait_receipt_id"]),
                    "pending_child_completion": pending_completion,
                    "pending_child_completion_hash": pending_hash,
                    "pending_child_completion_append_id": append_id,
                }
            )
            next_checkpoint.pop("workflow_spawn_operation_id", None)
            next_checkpoint.pop("workflow_spawn_child_run_id", None)
            checkpoint_json = canonical_json(cast(JsonValue, next_checkpoint))
            checkpoint_hash = hashlib.sha256(checkpoint_json.encode()).hexdigest()
            checkpoint_version = int(latest["version"]) + 1
            _fault(fault, "workflow:spawn_child_continue:before_checkpoint_write")
            connection.execute(
                "INSERT INTO workflow_checkpoints("
                "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
                "lease_epoch,version,created_at) VALUES(?,?,"
                "'react.termination.v1',?,?,?,?,?)",
                (
                    f"{run_id}:react.termination.v1:{checkpoint_version}",
                    run_id,
                    checkpoint_json,
                    checkpoint_hash,
                    execution_lease.epoch,
                    checkpoint_version,
                    now,
                ),
            )
            _fault(fault, "workflow:spawn_child_continue:after_checkpoint_write")
        _fault(fault, "workflow:spawn_child_continue:after_commit")
        result = self.read_react_checkpoint(run_id)
        assert result is not None
        return result

    def commit_pending_spawn_child_completion_and_react_ready(
        self,
        *,
        run_id: str,
        expected_checkpoint_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCheckpoint:
        """Append the public child completion once the Provider Tool batch is closed."""

        from simple_harness.contracts.messages import Message, MessageRole
        from simple_harness.runtime.context import _append_context_in_transaction

        run_id = _required(run_id, "run_id")
        now = _time(now)
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
            latest = connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE run_id=? "
                "AND namespace='react.termination.v1' "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if latest is None:
                raise UnitOfWorkConflict("workflow spawn ReAct checkpoint is missing")
            checkpoint_payload = json.loads(str(latest["checkpoint_json"]))
            if not isinstance(checkpoint_payload, dict):
                raise UnitOfWorkConflict("workflow spawn ReAct checkpoint is malformed")
            wait_receipt_id = checkpoint_payload.get("workflow_spawn_wait_receipt_id")
            completed_wait_receipt_id = checkpoint_payload.get(
                "last_workflow_spawn_wait_receipt_id"
            )
            wait = connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts WHERE parent_wait_receipt_id=?",
                (wait_receipt_id if wait_receipt_id is not None else completed_wait_receipt_id,),
            ).fetchone()
            if (
                wait is not None
                and str(wait["state"]) == "acked"
                and checkpoint_payload.get("phase") == "ready"
                and int(latest["version"]) == expected_checkpoint_version + 1
            ):
                return _workflow_checkpoint(latest)
            if (
                int(latest["version"]) != expected_checkpoint_version
                or checkpoint_payload.get("phase") != "tool_batch_reserved"
                or wait is None
                or str(wait["state"]) != "acked_completion_pending"
                or wait["pending_child_completion_json"] is None
                or wait["pending_child_completion_hash"] is None
                or wait["child_completion_append_id"] is None
            ):
                raise UnitOfWorkConflict("pending workflow child completion authority differs")
            response_payload = checkpoint_payload.get("provider_response_snapshot")
            if not isinstance(response_payload, dict):
                raise UnitOfWorkConflict("workflow spawn Provider response is missing")
            tool_calls = response_payload.get("tool_calls")
            if not isinstance(tool_calls, list) or checkpoint_payload.get(
                "tool_result_progress"
            ) != len(tool_calls):
                raise UnitOfWorkConflict("workflow spawn Tool batch is not fully appended")
            pending_json = str(wait["pending_child_completion_json"])
            if hashlib.sha256(pending_json.encode()).hexdigest() != str(
                wait["pending_child_completion_hash"]
            ) or checkpoint_payload.get("pending_child_completion_hash") != str(
                wait["pending_child_completion_hash"]
            ):
                raise UnitOfWorkConflict("pending workflow child completion payload differs")
            context_revision = checkpoint_payload.get("context_revision")
            if isinstance(context_revision, bool) or not isinstance(context_revision, int):
                raise UnitOfWorkConflict("workflow spawn Context revision is missing")
            completion = json.loads(pending_json)
            context = _append_context_in_transaction(
                connection,
                RunId(run_id),
                execution_lease,
                context_revision,
                str(wait["child_completion_append_id"]),
                (
                    Message(
                        MessageRole.USER,
                        canonical_json(cast(JsonValue, completion)),
                        name="workflow_child_completion",
                    ),
                ),
                now=now,
            )
            _fault(fault, "workflow:spawn_child_complete:after_context_write")
            next_checkpoint = dict(checkpoint_payload)
            next_checkpoint.update(
                {
                    "phase": "ready",
                    "provider_request_id": None,
                    "tool_batch_id": None,
                    "context_revision": None,
                    "provider_request_snapshot": None,
                    "provider_request_fingerprint": None,
                    "provider_response_snapshot": None,
                    "provider_response_digest": None,
                    "tool_result_progress": 0,
                    "workflow_spawn_wait_receipt_id": None,
                    "pending_child_completion": None,
                    "pending_child_completion_hash": None,
                    "pending_child_completion_append_id": None,
                    "last_workflow_spawn_wait_receipt_id": str(wait["parent_wait_receipt_id"]),
                    "last_observed_at": now,
                }
            )
            next_json = canonical_json(cast(JsonValue, next_checkpoint))
            next_hash = hashlib.sha256(next_json.encode()).hexdigest()
            next_version = int(latest["version"]) + 1
            _fault(fault, "workflow:spawn_child_complete:before_checkpoint_write")
            connection.execute(
                "INSERT INTO workflow_checkpoints("
                "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
                "lease_epoch,version,created_at) VALUES(?,?,"
                "'react.termination.v1',?,?,?,?,?)",
                (
                    f"{run_id}:react.termination.v1:{next_version}",
                    run_id,
                    next_json,
                    next_hash,
                    execution_lease.epoch,
                    next_version,
                    now,
                ),
            )
            _fault(fault, "workflow:spawn_child_complete:after_checkpoint_write")
            wait_version = int(wait["version"]) + 1
            lifecycle_hash = hashlib.sha256(
                canonical_json(
                    {
                        "identity_hash": str(wait["identity_hash"]),
                        "state": "acked",
                        "version": wait_version,
                        "progress_receipt_id": wait["progress_receipt_id"],
                        "child_completion_append_id": wait["child_completion_append_id"],
                        "child_completion_context_revision": context.revision,
                    }
                ).encode()
            ).hexdigest()
            _fault(fault, "workflow:spawn_child_complete:before_wait_write")
            changed = connection.execute(
                "UPDATE workflow_spawn_child_wait_receipts SET state='acked',"
                "child_completion_append_receipt_id=?,"
                "child_completion_context_revision=?,version=?,lifecycle_hash=? "
                "WHERE parent_wait_receipt_id=? "
                "AND state='acked_completion_pending' AND version=?",
                (
                    str(wait["child_completion_append_id"]),
                    context.revision,
                    wait_version,
                    lifecycle_hash,
                    str(wait["parent_wait_receipt_id"]),
                    int(wait["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn child completion ACK CAS failed")
            _fault(fault, "workflow:spawn_child_complete:after_wait_write")
        _fault(fault, "workflow:spawn_child_complete:after_commit")
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
        committed_turn: CommittedTurnSpec | None = None,
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
        if terminal_state is not RunState.COMPLETED and committed_turn is not None:
            raise UnitOfWorkConflict("only COMPLETED may create a committed turn")
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
                replay_payload["terminal_fence_receipt_ref"] = terminal_fence_receipt_ref
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
                _verify_committed_turn_replay(
                    self.database.connection,
                    committed_turn,
                    run_id=run_id,
                )
                return TerminalCommitResult(existing_run, stored)
            raise UnitOfWorkConflict("another root terminal intent already won")

        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=now)
            if execution_lease.run_id != run_id or execution_lease.owner_id != fence.owner_id:
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
            self._close_spawn_child_waits_for_parent_terminal(
                connection,
                parent_run_id=run_id,
                terminal_state=terminal_state,
                terminal_receipt_id=event_id,
                claimed_continuation_ack_receipt_id=None,
                now=now,
                fault=fault,
            )
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
            if committed_turn is not None:
                _validate_committed_turn(connection, committed_turn, run_id=run_id)
                _fault(fault, "root_terminal.committed_turn.before_write")
                _insert_committed_turn(
                    connection,
                    committed_turn,
                    run_id=run_id,
                    now=now,
                )
                _fault(fault, "root_terminal.committed_turn.after_write")
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
            record for item in items if (record := self.read_delivery(item.delivery_id)) is not None
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
        user_id: str = "harness-system",
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> RunRecord:
        execution_session_id = _required(execution_session_id, "execution_session_id")
        run_id = _required(run_id, "run_id")
        request_id = _required(request_id, "request_id")
        profile_key = _required(profile_key, "profile_key")
        driver_kind = _required(driver_kind, "driver_kind")
        event_id = _required(event_id, "event_id")
        user_id = _required(user_id, "user_id")
        _stage_pair(context_stage_id, context_stage_hash)
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
            owner = self.database.connection.execute(
                "SELECT user_id FROM execution_sessions WHERE session_id=?",
                (execution_session_id,),
            ).fetchone()
            if owner is None or str(owner[0]) != user_id:
                raise UnitOfWorkConflict("execution session belongs to another user")
            _verify_stage_replay(
                self.database.connection,
                stage_id=context_stage_id,
                stage_hash=context_stage_hash,
                consumed_run_id=run_id,
                consumed_continuation_id=None,
            )
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "root_start.session.before_write")
            connection.execute(
                "INSERT OR IGNORE INTO execution_users(user_id,created_at) VALUES(?,?)",
                (user_id, now),
            )
            owner = connection.execute(
                "SELECT user_id FROM execution_sessions WHERE session_id=?",
                (execution_session_id,),
            ).fetchone()
            if owner is not None and str(owner[0]) != user_id:
                raise UnitOfWorkConflict("execution session belongs to another user")
            connection.execute(
                "INSERT OR IGNORE INTO execution_sessions(session_id,user_id,created_at) "
                "VALUES(?,?,?)",
                (execution_session_id, user_id, now),
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
            if context_stage_id is not None and context_stage_hash is not None:
                _consume_context_stage(
                    connection,
                    stage_id=context_stage_id,
                    stage_hash=context_stage_hash,
                    kind="root",
                    expected_snapshot=snapshot.get("prepared_context"),
                    consumed_run_id=run_id,
                    consumed_continuation_id=None,
                    now=now,
                )
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
                or canonical_json(json.loads(prompt_json)) != canonical_json(_thaw(existing.prompt))
                or existing.expires_at != expires_at
            ):
                raise UnitOfWorkConflict("admission identity reused with different intent")
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
            raise ValueError("open decision must omit response; resolved decision requires it")
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
                        (
                            state.value,
                            response_json,
                            now,
                            decision_id,
                            run_id,
                            existing.version,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("decision resolution CAS failed")
                    _fault(fault, "decision_resolve.decision.after_write")
                    changed = connection.execute(
                        """
                        UPDATE runs SET state=?,version=version+1,updated_at=?
                        WHERE run_id=? AND state='waiting'
                        """,
                        (run_state.value, now, run_id),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("decision Run resolution CAS failed")
                    _fault(fault, "decision_resolve.run.after_write")
                    self._insert_event(
                        connection,
                        event_id=event_id,
                        run_id=run_id,
                        kind=f"decision.{state.value}",
                        payload={"decision_id": decision_id, "kind": kind},
                        now=now,
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
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord:
        continuation_id = _required(continuation_id, "continuation_id")
        run_id = _required(run_id, "run_id")
        now = _time(now)
        payload_json = _object_json(payload, "payload")
        _stage_pair(context_stage_id, context_stage_hash)
        existing = self.read_continuation(continuation_id)
        if existing is not None:
            if (
                existing.run_id == run_id
                and canonical_json(_thaw(existing.payload)) == payload_json
            ):
                _verify_stage_replay(
                    self.database.connection,
                    stage_id=context_stage_id,
                    stage_hash=context_stage_hash,
                    consumed_run_id=None,
                    consumed_continuation_id=continuation_id,
                )
                return existing
            raise UnitOfWorkConflict("continuation identity reused with different payload")
        with self.database.transaction() as connection:
            run_row = connection.execute(
                "SELECT r.state,s.user_id,r.execution_session_id FROM runs r "
                "JOIN execution_sessions s ON s.session_id=r.execution_session_id "
                "WHERE r.run_id=?",
                (run_id,),
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
                    ack_receipt_id, created_at, claimed_at, acked_at,
                    context_stage_id, context_stage_hash
                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, 0,
                          NULL, ?, NULL, NULL, ?, ?)
                """,
                (
                    continuation_id,
                    run_id,
                    sequence,
                    payload_json,
                    now,
                    context_stage_id,
                    context_stage_hash,
                ),
            )
            _fault(fault, "continuation_enqueue.continuation.after_write")
            if context_stage_id is not None and context_stage_hash is not None:
                _consume_context_stage(
                    connection,
                    stage_id=context_stage_id,
                    stage_hash=context_stage_hash,
                    kind="continuation",
                    expected_snapshot=payload.get("prepared_context"),
                    consumed_run_id=None,
                    consumed_continuation_id=continuation_id,
                    now=now,
                )
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
        if execution_lease.run_id != run_id or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE:
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
                spawn_wait = connection.execute(
                    "SELECT * FROM workflow_spawn_child_wait_receipts "
                    "WHERE continuation_id=? AND state='woken'",
                    (claimed_id,),
                ).fetchone()
                if spawn_wait is not None:
                    next_wait_version = int(spawn_wait["version"]) + 1
                    lifecycle_hash = hashlib.sha256(
                        canonical_json(
                            {
                                "identity_hash": str(spawn_wait["identity_hash"]),
                                "state": "claimed",
                                "version": next_wait_version,
                                "child_signal_id": spawn_wait["child_signal_id"],
                                "continuation_id": claimed_id,
                                "claim_owner": execution_lease.owner_id,
                                "runtime_lease_epoch": execution_lease.epoch,
                            }
                        ).encode()
                    ).hexdigest()
                    _fault(fault, "continuation_claim.spawn_wait.before_write")
                    changed = connection.execute(
                        """
                        UPDATE workflow_spawn_child_wait_receipts
                        SET state='claimed',version=?,lifecycle_hash=?
                        WHERE parent_wait_receipt_id=? AND state='woken'
                          AND version=?
                        """,
                        (
                            next_wait_version,
                            lifecycle_hash,
                            str(spawn_wait["parent_wait_receipt_id"]),
                            int(spawn_wait["version"]),
                        ),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("workflow spawn child-wait claim CAS failed")
                    _fault(fault, "continuation_claim.spawn_wait.after_write")
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
            self._require_continuation_claim(connection, continuation_claim, execution_lease)
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
        committed_turn: CommittedTurnSpec | None = None,
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
        if terminal_state is not RunState.COMPLETED and committed_turn is not None:
            raise UnitOfWorkConflict("only COMPLETED may create a committed turn")
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
                    "committed_turn_hash": (
                        None if committed_turn is None else committed_turn.payload_hash
                    ),
                }
            ).encode()
        ).hexdigest()
        existing = self._read_continuation_progress_receipt(receipt_id)
        if existing is not None:
            _verify_committed_turn_replay(
                self.database.connection,
                committed_turn,
                run_id=run_id,
            )
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
                _verify_committed_turn_replay(
                    connection,
                    committed_turn,
                    run_id=run_id,
                )
                return self._replay_continuation_terminal(
                    _continuation_progress_receipt(receipt_row),
                    continuation_claim=continuation_claim,
                    execution_lease=execution_lease,
                    outcome_hash=outcome_hash,
                )
            prior_row = connection.execute(
                "SELECT receipt_id FROM continuation_progress_receipts WHERE continuation_id=?",
                (continuation_claim.continuation_id,),
            ).fetchone()
            if prior_row is not None:
                raise UnitOfWorkConflict("continuation was already committed with another receipt")
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
            self._require_continuation_claim(connection, continuation_claim, execution_lease)
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
            self._close_spawn_child_waits_for_parent_terminal(
                connection,
                parent_run_id=run_id,
                terminal_state=terminal_state,
                terminal_receipt_id=event_id,
                claimed_continuation_ack_receipt_id=receipt_id,
                now=now,
                fault=fault,
            )
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
            if committed_turn is not None:
                _validate_committed_turn(connection, committed_turn, run_id=run_id)
                _fault(fault, "continuation_terminal.committed_turn.before_write")
                _insert_committed_turn(
                    connection,
                    committed_turn,
                    run_id=run_id,
                    now=now,
                )
                _fault(fault, "continuation_terminal.committed_turn.after_write")
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

    @staticmethod
    def _close_spawn_child_waits_for_parent_terminal(
        connection: sqlite3.Connection,
        *,
        parent_run_id: str,
        terminal_state: RunState,
        terminal_receipt_id: str,
        claimed_continuation_ack_receipt_id: str | None,
        now: float,
        fault: FaultHook | None,
    ) -> None:
        """Close child-wait lifecycle rows in the parent terminal tx.

        Once the child continuation is ACKed, a terminal parent must not append
        its queued public completion to Context.  The immutable queue entry is
        retained for audit and bound to the same terminal event instead.
        """

        rows = connection.execute(
            "SELECT * FROM workflow_spawn_child_wait_receipts "
            "WHERE parent_run_id=? AND state IN "
            "('unconsumed','woken','claimed','acked_completion_pending') "
            "ORDER BY parent_wait_receipt_id",
            (parent_run_id,),
        ).fetchall()
        for row in rows:
            prior_state = str(row["state"])
            phase_kind = (
                "child_active"
                if prior_state == "unconsumed"
                else (
                    "signal_pending"
                    if prior_state == "woken"
                    else (
                        "continuation_claimed" if prior_state == "claimed" else "completion_pending"
                    )
                )
            )
            child_cancel_request_id: str | None = None
            child_cancel_receipt_id: str | None = None
            reused_child_cancel_receipt_id: str | None = None
            if prior_state == "unconsumed":
                signal = connection.execute(
                    "SELECT signal_id FROM child_signals WHERE parent_run_id=? "
                    "AND child_run_id=? AND state<>'acked'",
                    (parent_run_id, str(row["child_run_id"])),
                ).fetchone()
                if signal is not None:
                    raise UnitOfWorkConflict(
                        "unconsumed spawn child-wait already has a child signal"
                    )
                child = connection.execute(
                    "SELECT state,version FROM runs WHERE run_id=?",
                    (str(row["child_run_id"]),),
                ).fetchone()
                if child is None:
                    raise UnitOfWorkConflict("spawn child Run is missing")
                existing_cancel = connection.execute(
                    "SELECT * FROM workflow_cancel_receipts WHERE run_id=? "
                    "ORDER BY generation DESC LIMIT 1",
                    (str(row["child_run_id"]),),
                ).fetchone()
                if str(child["state"]) == RunState.CANCEL_REQUESTED.value:
                    if existing_cancel is None or str(existing_cancel["phase"]) == "terminal":
                        raise UnitOfWorkConflict(
                            "cancel-requested spawn child lacks active cancel receipt"
                        )
                    reused_child_cancel_receipt_id = str(existing_cancel["cancel_id"])
                elif str(child["state"]) in {
                    RunState.RUNNING.value,
                    RunState.WAITING.value,
                }:
                    if existing_cancel is not None:
                        raise UnitOfWorkConflict(
                            "active spawn child has an inconsistent cancel receipt"
                        )
                    child_cancel_request_id = hashlib.sha256(
                        canonical_json(
                            {
                                "protocol": "workflow-spawn-parent-terminal-child-cancel-v1",
                                "parent_wait_receipt_id": str(row["parent_wait_receipt_id"]),
                                "child_run_id": str(row["child_run_id"]),
                            }
                        ).encode()
                    ).hexdigest()
                    child_cancel_receipt_id = child_cancel_request_id
                    blocker_rows = connection.execute(
                        "SELECT blocker_id,kind,ledger_identity,handoff_attempt,version "
                        "FROM run_wait_blockers WHERE run_id=? ORDER BY blocker_id",
                        (str(row["child_run_id"]),),
                    ).fetchall()
                    blocker_ids: list[JsonValue] = [
                        str(item["blocker_id"]) for item in blocker_rows
                    ]
                    blocker_snapshot: dict[str, JsonValue] = {
                        str(item["blocker_id"]): cast(
                            dict[str, JsonValue],
                            {
                                "kind": str(item["kind"]),
                                "ledger_identity": str(item["ledger_identity"]),
                                "handoff_attempt": int(item["handoff_attempt"]),
                                "observed_blocker_version": int(item["version"]),
                            },
                        )
                        for item in blocker_rows
                    }
                    _fault(fault, "root_terminal.child_cancel.run.before_write")
                    changed = connection.execute(
                        "UPDATE runs SET state='cancel_requested',version=version+1,"
                        "updated_at=? WHERE run_id=? AND version=? "
                        "AND state IN ('running','waiting')",
                        (
                            now,
                            str(row["child_run_id"]),
                            int(child["version"]),
                        ),
                    ).rowcount
                    if changed != 1:
                        raise UnitOfWorkConflict("spawn child parent-terminal cancel CAS failed")
                    _fault(fault, "root_terminal.child_cancel.run.after_write")
                    SqliteExecutionUnitOfWork._invalidate_cancel_activation(
                        connection,
                        str(row["child_run_id"]),
                        None,
                        now=now,
                        fault=fault,
                    )
                    _fault(fault, "root_terminal.child_cancel.receipt.before_write")
                    connection.execute(
                        "INSERT INTO workflow_cancel_receipts("
                        "cancel_id,run_id,generation,reason,phase,blocker_ids_json,"
                        "blocker_snapshot_json,terminal,version,created_at,updated_at) "
                        "VALUES(?,?,0,?,?,?,?,NULL,0,?,?)",
                        (
                            child_cancel_request_id,
                            str(row["child_run_id"]),
                            "attached parent reached terminal",
                            "cancelling" if blocker_ids else "requested",
                            canonical_json(blocker_ids),
                            canonical_json(blocker_snapshot),
                            now,
                            now,
                        ),
                    )
                    _fault(fault, "root_terminal.child_cancel.receipt.after_write")
                else:
                    raise UnitOfWorkConflict(
                        "spawn child is not cancellable during parent terminal"
                    )
            elif prior_state == "woken":
                continuation_id = row["continuation_id"]
                signal_id = row["child_signal_id"]
                if continuation_id is None or signal_id is None:
                    raise UnitOfWorkConflict("woken spawn child-wait lacks signal continuation")
                _fault(fault, "root_terminal.spawn_continuation.before_write")
                changed = connection.execute(
                    "UPDATE continuations SET state='quarantined',acked_at=?,"
                    "version=version+1 WHERE continuation_id=? "
                    "AND run_id=? AND state='pending'",
                    (now, continuation_id, parent_run_id),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("spawn signal continuation terminal quarantine failed")
                _fault(fault, "root_terminal.spawn_continuation.after_write")
            elif prior_state == "claimed":
                if claimed_continuation_ack_receipt_id is None:
                    raise UnitOfWorkConflict(
                        "claimed spawn child-wait requires continuation-aware terminal"
                    )
                continuation = connection.execute(
                    "SELECT state,ack_receipt_id FROM continuations "
                    "WHERE continuation_id=? AND run_id=?",
                    (row["continuation_id"], parent_run_id),
                ).fetchone()
                if (
                    continuation is None
                    or str(continuation["state"]) != "acked"
                    or str(continuation["ack_receipt_id"]) != claimed_continuation_ack_receipt_id
                ):
                    raise UnitOfWorkConflict("claimed spawn child-wait terminal ACK differs")
            terminal_hash = hashlib.sha256(
                canonical_json(
                    {
                        "parent_wait_receipt_id": str(row["parent_wait_receipt_id"]),
                        "spawn_operation_id": str(row["spawn_operation_id"]),
                        "phase_kind": phase_kind,
                        "terminal_receipt_id": terminal_receipt_id,
                        "terminal_state": terminal_state.value,
                        "child_signal_id": row["child_signal_id"],
                        "continuation_id": row["continuation_id"],
                        "pending_child_completion_hash": (
                            None
                            if row["pending_child_completion_hash"] is None
                            else str(row["pending_child_completion_hash"])
                        ),
                        "claimed_continuation_ack_receipt_id": (
                            claimed_continuation_ack_receipt_id
                            if phase_kind == "continuation_claimed"
                            else None
                        ),
                        "child_cancel_request_id": child_cancel_request_id,
                        "child_cancel_receipt_id": child_cancel_receipt_id,
                        "reused_child_cancel_receipt_id": reused_child_cancel_receipt_id,
                    }
                ).encode()
            ).hexdigest()
            next_version = int(row["version"]) + 1
            lifecycle_hash = hashlib.sha256(
                canonical_json(
                    {
                        "identity_hash": str(row["identity_hash"]),
                        "state": "acked_parent_terminal",
                        "version": next_version,
                        "phase_kind": phase_kind,
                        "terminal_receipt_id": terminal_receipt_id,
                        "terminal_state": terminal_state.value,
                        "terminal_hash": terminal_hash,
                    }
                ).encode()
            ).hexdigest()
            _fault(fault, "root_terminal.spawn_wait.before_write")
            changed = connection.execute(
                "UPDATE workflow_spawn_child_wait_receipts SET "
                "state='acked_parent_terminal',"
                "pending_completion_terminal_receipt_id=?,"
                "pending_completion_terminal_state=?,"
                "pending_completion_terminal_hash=?,"
                "parent_terminal_phase_kind=?,"
                "late_signal_quarantine_receipt_id=?,"
                "claimed_continuation_terminal_ack_receipt_id=?,"
                "progress_receipt_id=COALESCE(progress_receipt_id,?),"
                "child_cancel_request_id=?,child_cancel_receipt_id=?,"
                "reused_child_cancel_receipt_id=?,"
                "version=?,lifecycle_hash=? "
                "WHERE parent_wait_receipt_id=? "
                "AND state=? AND version=?",
                (
                    terminal_receipt_id,
                    terminal_state.value,
                    terminal_hash,
                    phase_kind,
                    terminal_receipt_id if phase_kind == "signal_pending" else None,
                    (
                        claimed_continuation_ack_receipt_id
                        if phase_kind == "continuation_claimed"
                        else None
                    ),
                    (
                        claimed_continuation_ack_receipt_id
                        if phase_kind == "continuation_claimed"
                        else None
                    ),
                    child_cancel_request_id,
                    child_cancel_receipt_id,
                    reused_child_cancel_receipt_id,
                    next_version,
                    lifecycle_hash,
                    str(row["parent_wait_receipt_id"]),
                    prior_state,
                    int(row["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("spawn child completion terminal closure CAS failed")
            _fault(fault, "root_terminal.spawn_wait.after_write")

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
        if isinstance(expected_catalog_generation, bool) or expected_catalog_generation < 1:
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
                "SELECT attachment_policy FROM run_links WHERE parent_run_id=? AND child_run_id=?",
                (parent_run_id, child_run_id),
            ).fetchone()
            if link is None:
                raise UnitOfWorkConflict("child attachment policy is missing")
            policy = AttachmentPolicy(str(link["attachment_policy"]))
            if (policy is AttachmentPolicy.DETACHED) != expect_detached:
                raise UnitOfWorkConflict("child terminal path differs from durable policy")
            if execution_lease.run_id != child_run_id:
                raise UnitOfWorkConflict("child terminal lease belongs to another Run")
            self._require_runtime_lease(connection, execution_lease, now=now)
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
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
            parent = connection.execute(
                "SELECT state FROM runs WHERE run_id=?",
                (parent_run_id,),
            ).fetchone()
            spawn_wait = connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id=? AND child_run_id=? "
                "AND state='acked_parent_terminal' "
                "AND parent_terminal_phase_kind='child_active'",
                (parent_run_id, str(row["child_run_id"])),
            ).fetchone()
            if parent is not None and str(parent["state"]) in {
                RunState.COMPLETED.value,
                RunState.FAILED.value,
                RunState.CANCELLED.value,
            }:
                self._quarantine_late_spawn_child_signal(
                    connection,
                    signal=row,
                    spawn_wait=spawn_wait,
                    owner_id=owner_id,
                    now=now,
                    expires_at=expires_at,
                    fault=fault,
                )
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

    def _quarantine_late_spawn_child_signal(
        self,
        connection: sqlite3.Connection,
        *,
        signal: sqlite3.Row,
        spawn_wait: sqlite3.Row | None,
        owner_id: str,
        now: float,
        expires_at: float,
        fault: FaultHook | None,
    ) -> None:
        signal_id = str(signal["signal_id"])
        parent_run_id = str(signal["parent_run_id"])
        continuation_id = f"child-signal:{signal_id}:terminal-quarantine"
        receipt_id = f"child-signal:{signal_id}:terminal-quarantine:receipt"
        event_id = f"child-signal:{signal_id}:terminal-quarantined"
        continuation_payload: dict[str, JsonValue] = {
            "kind": "child_terminal_quarantined",
            "signal_id": signal_id,
            "child_run_id": str(signal["child_run_id"]),
        }
        event_payload: dict[str, JsonValue] = {
            "signal_id": signal_id,
            "continuation_id": continuation_id,
            "receipt_id": receipt_id,
            "reason": "parent_terminal",
        }
        continuation_json = canonical_json(continuation_payload)
        event_json = canonical_json(event_payload)
        fifo_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(fifo_seq),0)+1 FROM continuations WHERE run_id=?",
                (parent_run_id,),
            ).fetchone()[0]
        )
        _fault(fault, "child_signal_quarantine.continuation.before_write")
        connection.execute(
            "INSERT INTO continuations(continuation_id,run_id,fifo_seq,"
            "payload_json,state,version,claimed_by,runtime_lease_epoch,"
            "claim_epoch,ack_receipt_id,created_at,claimed_at,acked_at) "
            "VALUES(?,?,?,?,'quarantined',0,NULL,NULL,0,NULL,?,NULL,?)",
            (
                continuation_id,
                parent_run_id,
                fifo_seq,
                continuation_json,
                now,
                now,
            ),
        )
        _fault(fault, "child_signal_quarantine.continuation.after_write")
        self._insert_event(
            connection,
            event_id=event_id,
            run_id=parent_run_id,
            kind="child.signal_quarantined",
            payload=event_payload,
            now=now,
        )
        signal_state = ChildSignalState(str(signal["state"]))
        if signal_state is ChildSignalState.PENDING:
            ack_owner = owner_id
            ack_epoch = 1
            claimed_at = now
            claim_expires_at = expires_at
        else:
            ack_owner = str(signal["claimed_by"])
            ack_epoch = int(signal["claim_epoch"])
            claimed_at = float(signal["claimed_at"])
            claim_expires_at = float(signal["claim_expires_at"])
        _fault(fault, "child_signal_quarantine.receipt.before_write")
        connection.execute(
            "INSERT INTO child_signal_ack_receipts("
            "receipt_id,signal_id,parent_run_id,owner_id,claim_epoch,"
            "continuation_id,event_id,continuation_payload_hash,"
            "event_payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                signal_id,
                parent_run_id,
                ack_owner,
                ack_epoch,
                continuation_id,
                event_id,
                hashlib.sha256(continuation_json.encode()).hexdigest(),
                hashlib.sha256(event_json.encode()).hexdigest(),
                now,
            ),
        )
        _fault(fault, "child_signal_quarantine.receipt.after_write")
        _fault(fault, "child_signal_quarantine.signal.before_write")
        changed = connection.execute(
            "UPDATE child_signals SET state='acked',version=version+1,"
            "claimed_by=?,claimed_at=?,claim_expires_at=?,claim_epoch=?,"
            "acked_at=?,ack_receipt_id=?,updated_at=? "
            "WHERE signal_id=? AND version=? AND state IN ('pending','claimed')",
            (
                ack_owner,
                claimed_at,
                claim_expires_at,
                ack_epoch,
                now,
                receipt_id,
                now,
                signal_id,
                int(signal["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("late child signal quarantine CAS failed")
        _fault(fault, "child_signal_quarantine.signal.after_write")
        if spawn_wait is None:
            return
        next_version = int(spawn_wait["version"]) + 1
        lifecycle_hash = hashlib.sha256(
            canonical_json(
                {
                    "identity_hash": str(spawn_wait["identity_hash"]),
                    "state": "acked_parent_terminal",
                    "version": next_version,
                    "phase_kind": "child_active",
                    "terminal_receipt_id": spawn_wait["pending_completion_terminal_receipt_id"],
                    "terminal_state": spawn_wait["pending_completion_terminal_state"],
                    "terminal_hash": spawn_wait["pending_completion_terminal_hash"],
                    "late_signal_quarantine_receipt_id": receipt_id,
                }
            ).encode()
        ).hexdigest()
        _fault(fault, "child_signal_quarantine.spawn_wait.before_write")
        changed = connection.execute(
            "UPDATE workflow_spawn_child_wait_receipts SET "
            "late_signal_quarantine_receipt_id=?,child_signal_id=?,"
            "continuation_id=?,version=?,lifecycle_hash=? "
            "WHERE parent_wait_receipt_id=? AND state='acked_parent_terminal' "
            "AND parent_terminal_phase_kind='child_active' AND version=? "
            "AND late_signal_quarantine_receipt_id IS NULL",
            (
                receipt_id,
                signal_id,
                continuation_id,
                next_version,
                lifecycle_hash,
                str(spawn_wait["parent_wait_receipt_id"]),
                int(spawn_wait["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("late child signal wait closure CAS failed")
        _fault(fault, "child_signal_quarantine.spawn_wait.after_write")

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
        continuation_hash = hashlib.sha256(continuation_json.encode("utf-8")).hexdigest()
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
                    raise UnitOfWorkConflict("child signal was already acknowledged differently")
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
                raise UnitOfWorkConflict("child signal was already acknowledged differently")
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
            spawn_wait = connection.execute(
                """
                SELECT * FROM workflow_spawn_child_wait_receipts
                WHERE parent_run_id=? AND child_run_id=? AND state='unconsumed'
                """,
                (signal.parent_run_id, signal.child_run_id),
            ).fetchone()
            if spawn_wait is not None:
                next_wait_version = int(spawn_wait["version"]) + 1
                lifecycle_hash = hashlib.sha256(
                    canonical_json(
                        {
                            "identity_hash": str(spawn_wait["identity_hash"]),
                            "state": "woken",
                            "version": next_wait_version,
                            "child_signal_id": signal_id,
                            "continuation_id": continuation_id,
                        }
                    ).encode()
                ).hexdigest()
                _fault(fault, "child_signal_ack.spawn_wait.before_write")
                changed = connection.execute(
                    """
                    UPDATE workflow_spawn_child_wait_receipts
                    SET state='woken',child_signal_id=?,continuation_id=?,
                        version=?,lifecycle_hash=?
                    WHERE parent_wait_receipt_id=? AND state='unconsumed'
                      AND version=?
                    """,
                    (
                        signal_id,
                        continuation_id,
                        next_wait_version,
                        lifecycle_hash,
                        str(spawn_wait["parent_wait_receipt_id"]),
                        int(spawn_wait["version"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("workflow spawn child-wait wake CAS failed")
                _fault(fault, "child_signal_ack.spawn_wait.after_write")
            link = connection.execute(
                "SELECT attachment_policy FROM run_links WHERE parent_run_id = ? AND"
                " child_run_id = ?",
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

    def read_child_command_for_run(self, child_run_id: str) -> ChildCommandRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM child_commands WHERE child_run_id = ?", (child_run_id,)
        ).fetchone()
        return None if row is None else _child_command_record(row)

    def is_workflow_spawn_child(self, child_run_id: str) -> bool:
        row = self.database.connection.execute(
            "SELECT workflow_ticket_receipt_id FROM child_commands WHERE child_run_id=?",
            (child_run_id,),
        ).fetchone()
        return row is not None and row["workflow_ticket_receipt_id"] is not None

    def read_child_terminal_result_for_run(self, child_run_id: str) -> ChildTerminalResult | None:
        receipt_row = self.database.connection.execute(
            "SELECT * FROM child_terminal_receipts WHERE child_run_id=?",
            (child_run_id,),
        ).fetchone()
        if receipt_row is None:
            return None
        receipt = _child_terminal_receipt(receipt_row)
        command = self.database.connection.execute(
            "SELECT state,parent_run_id FROM child_commands WHERE command_id=?",
            (receipt.command_id,),
        ).fetchone()
        run = self.database.connection.execute(
            "SELECT state,version FROM runs WHERE run_id=?", (child_run_id,)
        ).fetchone()
        event = self.database.connection.execute(
            "SELECT kind,payload_json FROM run_events WHERE event_id=? AND run_id=?",
            (receipt.event_id, child_run_id),
        ).fetchone()
        if command is None or run is None or event is None:
            raise UnitOfWorkConflict("workflow child terminal chain is incomplete")
        if (
            str(command["state"]) != ChildCommandState.ACKED.value
            or str(run["state"]) != receipt.terminal_state
            or str(event["kind"]) != f"child.{receipt.terminal_state}"
        ):
            raise UnitOfWorkConflict("workflow child terminal chain differs")
        event_payload = json.loads(str(event["payload_json"]))
        if not isinstance(event_payload, dict):
            raise UnitOfWorkConflict("workflow child terminal event is malformed")
        terminal_payload = event_payload.get("terminal")
        if not isinstance(terminal_payload, dict):
            raise UnitOfWorkConflict("workflow child terminal payload is malformed")
        expected_hash = hashlib.sha256(
            canonical_json(
                {
                    "command_id": receipt.command_id,
                    "expected_child_version": int(run["version"]) - 1,
                    "terminal_state": receipt.terminal_state,
                    "signal_id": receipt.signal_id,
                    "event_id": receipt.event_id,
                    "payload": terminal_payload,
                }
            ).encode()
        ).hexdigest()
        if expected_hash != receipt.outcome_hash:
            raise UnitOfWorkConflict("workflow child terminal outcome hash differs")
        signal = None if receipt.signal_id is None else self.read_child_signal(receipt.signal_id)
        if receipt.signal_id is not None:
            if signal is None or signal.parent_run_id != str(command["parent_run_id"]):
                raise UnitOfWorkConflict("workflow child terminal signal differs")
            if canonical_json(thaw_json(signal.payload)) != canonical_json(terminal_payload):
                raise UnitOfWorkConflict("workflow child terminal signal payload differs")
        return ChildTerminalResult(
            child_run_id,
            receipt.terminal_state,
            receipt,
            signal,
        )

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

    def read_child_signal_ack_receipt(self, receipt_id: str) -> ChildSignalAckReceipt | None:
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
                raise UnitOfWorkConflict("prepared effect authority must use explicit refresh CAS")
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
        workflow_lease: WorkflowLease | None = None,
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
            self._require_workflow_handoff_authority(
                connection,
                run_id=execution_lease.run_id,
                execution_lease=execution_lease,
                workflow_lease=workflow_lease,
                now=now,
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
            if effect_run is None or str(effect_run["run_id"]) != execution_lease.run_id:
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
        result_json = None if state is EffectState.UNKNOWN else _tool_result_json(result)
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
        resolution_id = f"resolution:{recovery_identity(RecoveryKind.TOOL, record.effect_id.value, record.handoff_attempt)}"  # noqa: E501
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
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
            row = connection.execute(
                "SELECT outcome_hash,evidence_ref FROM reconciliation_resolutions WHERE"
                " resolution_id=?",
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
            self._require_run_fence(connection, run_fence, execution_lease=execution_lease)
            if record.handoff_attempt > 0:
                resolution = connection.execute(
                    """
                    SELECT evidence_ref FROM reconciliation_resolutions
                    WHERE kind='tool' AND ledger_identity=? AND handoff_attempt=?
                      AND outcome='confirmed_not_started'
                    """,
                    (record.effect_id.value, record.handoff_attempt),
                ).fetchone()
                if resolution is None or str(resolution["evidence_ref"]) != record.evidence_ref:
                    raise UnitOfWorkConflict("Tool refresh resolution mismatch")
            if record.run_id.value != execution_lease.run_id or record.run_id != run_fence.run_id:
                raise UnitOfWorkConflict("Tool refresh authority belongs to another Run")
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
        return RunFenceLease(run_id, epoch, execution_lease.owner_id, execution_lease.epoch)

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

    def put_tool_catalog_snapshot(
        self,
        specs: Sequence[ProviderToolSpec],
        *,
        created_at: float | None = None,
    ) -> ToolCatalogSnapshot:
        ordered = tuple(specs)
        if len({spec.name for spec in ordered}) != len(ordered):
            raise ValueError("tool catalog contains duplicate names")
        payload: JsonValue = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": _thaw_json(spec.parameters),
            }
            for spec in ordered
        ]
        encoded = canonical_json(payload)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        now = _time(time.time() if created_at is None else created_at)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO tool_catalog_snapshots(content_fingerprint,specs_json,created_at) "
                "VALUES (?,?,?) ON CONFLICT(content_fingerprint) DO NOTHING",
                (fingerprint, encoded, now),
            )
        snapshot = self.read_tool_catalog_snapshot(content_fingerprint=fingerprint)
        assert snapshot is not None
        return snapshot

    def read_tool_catalog_snapshot(
        self,
        generation: int | None = None,
        *,
        content_fingerprint: str | None = None,
    ) -> ToolCatalogSnapshot | None:
        if (generation is None) == (content_fingerprint is None):
            raise ValueError("provide exactly one catalog identity")
        row = self.database.connection.execute(
            "SELECT * FROM tool_catalog_snapshots WHERE "
            + ("generation=?" if generation is not None else "content_fingerprint=?"),
            (generation if generation is not None else content_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        raw = json.loads(str(row["specs_json"]))
        if not isinstance(raw, list):
            raise RuntimeError("stored tool catalog is malformed")
        specs = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("parameters"), dict):
                raise RuntimeError("stored tool catalog spec is malformed")
            specs.append(
                ProviderToolSpec(str(item["name"]), str(item["description"]), item["parameters"])
            )
        return ToolCatalogSnapshot(
            int(row["generation"]),
            str(row["content_fingerprint"]),
            tuple(specs),
            float(row["created_at"]),
        )

    def current_tool_catalog_generation(self) -> int:
        row = self.database.connection.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM tool_catalog_snapshots"
        ).fetchone()
        return int(row[0])

    def list_provider_projection_receipts(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 256,
    ) -> tuple[ProviderProjectionReceipt, ...]:
        if after_sequence < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid projection cursor or limit")
        rows = self.database.connection.execute(
            "SELECT * FROM provider_projection_outbox WHERE sequence>? ORDER BY sequence LIMIT ?",
            (after_sequence, limit),
        ).fetchall()
        return tuple(_provider_projection_receipt(row) for row in rows)

    def claim_provider_invocation(
        self,
        record: ProviderInvocationRecord,
        *,
        budget_policy: BudgetPolicy,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord:
        existing = self._provider_invocation_by_logical_call(record.run_id, record.request_id.value)
        if existing is not None:
            return existing
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=record.claimed_at)
            if execution_lease.run_id != record.run_id.value:
                raise UnitOfWorkConflict("provider invocation lease belongs to another Run")
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
                """,  # noqa: E501
                (
                    record.invocation_id,
                    record.run_id.value,
                    record.request_id.value,
                    record.request_fingerprint,
                    (
                        None
                        if record.request_json is None
                        else canonical_json(_thaw_json(record.request_json))
                    ),
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

    def read_provider_invocation(self, invocation_id: str) -> ProviderInvocationRecord | None:
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
        workflow_lease: WorkflowLease | None = None,
    ) -> ProviderInvocationRecord:
        handed_off_at = _time(handed_off_at, "handed_off_at")
        with self.database.transaction() as connection:
            self._require_runtime_lease(connection, execution_lease, now=handed_off_at)
            self._require_workflow_handoff_authority(
                connection,
                run_id=execution_lease.run_id,
                execution_lease=execution_lease,
                workflow_lease=workflow_lease,
                now=handed_off_at,
            )
            invocation_run = connection.execute(
                "SELECT run_id FROM provider_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation_run is None or str(invocation_run["run_id"]) != execution_lease.run_id:
                raise UnitOfWorkConflict("provider handoff lease belongs to another Run")
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
        fault: FaultHook | None = None,
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
            None if record.usage_json is None else canonical_json(_thaw_json(record.usage_json))
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
            _fault(fault, "provider_settlement.ledger.after_write")
            _insert_provider_projection_receipt(connection, record)
            _fault(fault, "provider_settlement.outbox.after_write")
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
                raise ValueError("completed Provider resolution requires response and usage")
            response_payload = json.loads(canonical_json(response_json))  # type: ignore[arg-type]
            usage_payload = json.loads(canonical_json(usage_json))  # type: ignore[arg-type]
            if not isinstance(response_payload, dict) or not isinstance(usage_payload, dict):
                raise TypeError("Provider resolution payloads must be objects")
            if response_payload.get("request_id") != record.request_id.value:
                raise UnitOfWorkConflict("Provider resolution request identity mismatch")
            usage_payload = dict(usage_payload)
            usage_payload["budget"] = budget_charge.to_json()
        else:
            if response_json is not None or usage_json is not None:
                raise ValueError("not-started Provider resolution cannot carry response")
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
        outcome_hash = hashlib.sha256(canonical_json(resolution_payload).encode()).hexdigest()
        resolution_id = f"resolution:{recovery_identity(RecoveryKind.PROVIDER, record.invocation_id, record.handoff_attempt)}"  # noqa: E501
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
                projected_row = connection.execute(
                    "SELECT * FROM provider_invocations WHERE invocation_id=?",
                    (record.invocation_id,),
                ).fetchone()
                assert projected_row is not None
                _insert_provider_projection_receipt(
                    connection, _provider_invocation_record(projected_row)
                )
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
                "SELECT outcome_hash,evidence_ref FROM reconciliation_resolutions WHERE"
                " resolution_id=?",
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

    def _provider_budget(self, connection: sqlite3.Connection, run_id: RunId) -> BudgetSnapshot:
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

    def _read_child_terminal_receipt(self, receipt_id: str) -> ChildTerminalReceipt | None:
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
            "SELECT owner_id, runtime_lease_epoch, epoch, state FROM run_fences WHERE run_id = ?",
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
            raise UnitOfWorkConflict("request identity reused with different root intent")

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

    @staticmethod
    def _workflow_request_payload(
        request: StartAdmissionRequest,
    ) -> dict[str, JsonValue]:
        return start_admission_request_to_json(request)

    @staticmethod
    def _derived_id(domain: str, fingerprint: str) -> str:
        return hashlib.sha256(
            f"simple-harness.workflow.{domain}|{fingerprint}".encode()
        ).hexdigest()

    @classmethod
    def _start_identity(cls, request: StartAdmissionRequest) -> tuple[str, str, str, str, str]:
        payload_json = canonical_json(cls._workflow_request_payload(request))
        fingerprint = hashlib.sha256(payload_json.encode()).hexdigest()
        run_id = (
            request.resolved_run_id
            or request.requested_run_id
            or cls._derived_id("run", fingerprint)
        )
        trace_id = (
            request.resolved_trace_id
            or request.requested_trace_id
            or cls._derived_id("trace", fingerprint)
        )
        thread_id = (
            request.resolved_thread_id
            or request.requested_thread_id
            or cls._derived_id("thread", fingerprint)
        )
        return payload_json, fingerprint, run_id, trace_id, thread_id

    def _start_receipt(
        self,
        row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None = None,
        include_activation: bool = True,
        activation_override: WorkflowActivation | None = None,
    ) -> StartAdmissionReceipt:
        authority = self.database.connection if connection is None else connection
        activation = None
        if row["claim_owner"] is not None and str(row["phase"]) in {
            StartPhase.CLAIMED.value,
            StartPhase.RUNNING.value,
        }:
            run_id = str(row["run_id"])
            namespace = str(json.loads(str(row["request_json"]))["checkpoint_namespace"])
            if activation_override is not None:
                activation = activation_override
            elif include_activation:
                fence = authority.execute(
                    "SELECT epoch,owner_id,runtime_lease_epoch,state FROM run_fences "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if (
                    fence is None
                    or str(fence["state"]) != "active"
                    or str(fence["owner_id"]) != str(row["claim_owner"])
                    or int(fence["runtime_lease_epoch"]) != int(row["claim_epoch"])
                ):
                    raise UnitOfWorkConflict("start receipt claim fence authority changed")
                claim_epoch = int(row["claim_epoch"])
                claim_expiry = float(row["claim_expires_at"])
                owner_id = str(row["claim_owner"])
                activation = WorkflowActivation(
                    ExecutionLease(
                        run_id,
                        RUNTIME_LEASE_NAMESPACE,
                        owner_id,
                        claim_epoch,
                        claim_expiry,
                    ),
                    RunFenceLease(
                        RunId(run_id),
                        int(fence["epoch"]),
                        owner_id,
                        claim_epoch,
                    ),
                    WorkflowLease(
                        run_id,
                        owner_id,
                        claim_epoch,
                        claim_expiry,
                        claim_epoch,
                        namespace,
                    ),
                )
        outcome = (
            None
            if row["outcome_json"] is None
            else freeze_json(json.loads(str(row["outcome_json"])))
        )
        request_value = json.loads(str(row["request_json"]))
        if not isinstance(request_value, dict):
            raise UnitOfWorkConflict("stored workflow start request is invalid")
        return StartAdmissionReceipt(
            request=start_admission_request_from_json(request_value),
            request_id=str(row["request_id"]),
            request_key=str(row["request_key"]),
            request_fingerprint=str(row["request_fingerprint"]),
            run_id=str(row["run_id"]),
            trace_id=str(row["trace_id"]),
            thread_id=str(row["thread_id"]),
            phase=StartPhase(str(row["phase"])),
            version=int(row["version"]),
            claim_action=(
                None if row["claim_action"] is None else StartClaimAction(str(row["claim_action"]))
            ),
            claim_owner=None if row["claim_owner"] is None else str(row["claim_owner"]),
            claim_epoch=(None if row["claim_epoch"] is None else int(row["claim_epoch"])),
            claim_expires_at=(
                None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
            ),
            activation=activation,
            serialized_outcome=outcome,
        )

    def _read_workflow_activation(
        self,
        run_id: str,
        *,
        namespace: str,
        owner_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> WorkflowActivation:
        authority = self.database.connection if connection is None else connection
        runtime = authority.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        workflow = authority.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, namespace),
        ).fetchone()
        fence = authority.execute(
            "SELECT epoch,owner_id,runtime_lease_epoch,state FROM run_fences WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if runtime is None or workflow is None or fence is None or str(fence["state"]) != "active":
            raise UnitOfWorkConflict("workflow activation is incomplete")
        execution_lease = ExecutionLease(
            run_id,
            RUNTIME_LEASE_NAMESPACE,
            str(runtime["owner_id"]),
            int(runtime["epoch"]),
            float(runtime["expires_at"]),
        )
        run_fence = RunFenceLease(
            RunId(run_id),
            int(fence["epoch"]),
            str(fence["owner_id"]),
            int(fence["runtime_lease_epoch"]),
        )
        workflow_lease = WorkflowLease(
            run_id,
            str(workflow["owner_id"]),
            int(workflow["epoch"]),
            float(workflow["expires_at"]),
            int(runtime["epoch"]),
            namespace,
        )
        if execution_lease.owner_id != owner_id:
            raise UnitOfWorkConflict("workflow activation owner changed")
        return WorkflowActivation(execution_lease, run_fence, workflow_lease)

    @staticmethod
    def _assert_open_workflow_transaction(
        transaction: WorkflowTransaction,
    ) -> _SqliteWorkflowTransaction:
        if not isinstance(transaction, _SqliteWorkflowTransaction) or not transaction.is_open:
            raise UnitOfWorkConflict("workflow lifecycle requires an open canonical transaction")
        return transaction

    async def admit_start_standalone(
        self,
        transaction: WorkflowTransaction,
        request: StartAdmissionRequest,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> StartAdmissionReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        if request.mode is not StartMode.STANDALONE:
            raise UnitOfWorkConflict("standalone admission rejects precreated mode")
        payload_json, fingerprint, run_id, trace_id, thread_id = self._start_identity(request)
        existing = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE request_key=?",
            (request.request_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != fingerprint:
                raise UnitOfWorkConflict("start request key reused with different payload")
            return self._start_receipt(existing, connection=tx.connection)
        snapshot = json.loads(payload_json)
        snapshot.update({"resolved_run_id": run_id, "trace_id": trace_id, "thread_id": thread_id})
        snapshot_json = canonical_json(snapshot)
        _fault(fault, "workflow:admit_start_standalone:before_runs_write")
        tx.connection.execute(
            "INSERT OR IGNORE INTO execution_sessions(session_id,user_id,created_at) "
            "VALUES(?,'harness-system',?)",
            (request.session_id, now),
        )
        tx.connection.execute(
            "INSERT INTO"
            " runs(run_id,execution_session_id,request_id,root_run_id,parent_run_id,profile_key,driver_kind,state,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,NULL,?,?,'created',0,?,?)",
            (
                run_id,
                request.session_id,
                request.request_id,
                run_id,
                request.profile_key,
                "workflow",
                now,
                now,
            ),
        )
        _fault(fault, "workflow:admit_start_standalone:after_runs_write")
        _fault(fault, "workflow:admit_start_standalone:before_run_start_snapshots_write")
        tx.connection.execute(
            "INSERT INTO run_start_snapshots(run_id,snapshot_json,snapshot_hash,created_at)"
            " VALUES(?,?,?,?)",
            (
                run_id,
                snapshot_json,
                hashlib.sha256(snapshot_json.encode()).hexdigest(),
                now,
            ),
        )
        _fault(fault, "workflow:admit_start_standalone:after_run_start_snapshots_write")
        _fault(
            fault,
            "workflow:admit_start_standalone:before_workflow_start_admissions_write",
        )
        tx.connection.execute(
            "INSERT INTO"
            " workflow_start_admissions(request_key,request_id,request_fingerprint,request_json,mode,run_id,trace_id,thread_id,phase,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?, 'admitted',0,?,?)",
            (
                request.request_key,
                request.request_id,
                fingerprint,
                payload_json,
                request.mode.value,
                run_id,
                trace_id,
                thread_id,
                now,
                now,
            ),
        )
        _fault(
            fault,
            "workflow:admit_start_standalone:after_workflow_start_admissions_write",
        )
        return self._start_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_start_admissions WHERE request_key=?",
                (request.request_key,),
            ).fetchone(),
            connection=tx.connection,
        )

    @staticmethod
    def _require_runtime_and_fence(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
        require_exact_expiry: bool = True,
    ) -> None:
        runtime = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        fence = connection.execute(
            "SELECT owner_id,epoch,runtime_lease_epoch,state FROM run_fences WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if (
            execution_lease.run_id != run_id
            or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
            or (require_exact_expiry and execution_lease.expires_at <= now)
            or runtime is None
            or str(runtime["owner_id"]) != execution_lease.owner_id
            or int(runtime["epoch"]) != execution_lease.epoch
            or float(runtime["expires_at"]) <= now
            or (require_exact_expiry and float(runtime["expires_at"]) != execution_lease.expires_at)
            or (
                not require_exact_expiry
                and float(runtime["expires_at"]) < execution_lease.expires_at
            )
            or fence is None
            or tuple(fence)
            != (
                run_fence.owner_id,
                run_fence.epoch,
                run_fence.runtime_lease_epoch,
                "active",
            )
            or run_fence.run_id.value != run_id
            or run_fence.owner_id != execution_lease.owner_id
            or run_fence.runtime_lease_epoch != execution_lease.epoch
        ):
            raise UnitOfWorkConflict("precreated workflow authority is stale")

    @classmethod
    def _require_current_start_claim(
        cls,
        connection: sqlite3.Connection,
        *,
        receipt: StartAdmissionReceipt,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
    ) -> None:
        cls._require_runtime_and_fence(
            connection,
            run_id=receipt.run_id,
            execution_lease=execution_lease,
            run_fence=run_fence,
            now=now,
            require_exact_expiry=False,
        )
        runtime = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (receipt.run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        projection = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (receipt.run_id, receipt.request.checkpoint_namespace),
        ).fetchone()
        if (
            runtime is None
            or projection is None
            or receipt.claim_owner != execution_lease.owner_id
            or receipt.claim_epoch != execution_lease.epoch
            or receipt.claim_expires_at is None
            or receipt.claim_expires_at > float(runtime["expires_at"])
            or tuple(projection[:2]) != (execution_lease.owner_id, execution_lease.epoch)
            or float(projection["expires_at"]) != float(runtime["expires_at"])
            or float(projection["expires_at"]) <= now
        ):
            raise UnitOfWorkConflict("start claim authority is stale")

    @staticmethod
    def _require_workflow_handoff_authority(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        execution_lease: ExecutionLease,
        workflow_lease: WorkflowLease | None,
        now: float,
    ) -> None:
        run = connection.execute(
            "SELECT driver_kind FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise UnitOfWorkConflict("handoff Run does not exist")
        if str(run["driver_kind"]) != "workflow":
            if workflow_lease is not None:
                raise UnitOfWorkConflict("non-workflow handoff rejects workflow authority")
            return
        if workflow_lease is None:
            raise UnitOfWorkConflict("workflow handoff requires workflow lease")
        projection = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, workflow_lease.namespace),
        ).fetchone()
        runtime = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        admission = connection.execute(
            "SELECT json_extract(request_json,'$.checkpoint_namespace') AS namespace "
            "FROM workflow_start_admissions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        fence = connection.execute(
            "SELECT owner_id,runtime_lease_epoch,state FROM run_fences WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if (
            workflow_lease.run_id != run_id
            or workflow_lease.owner_id != execution_lease.owner_id
            or workflow_lease.runtime_lease_epoch != execution_lease.epoch
            or workflow_lease.expires_at != execution_lease.expires_at
            or admission is None
            or str(admission["namespace"]) != workflow_lease.namespace
            or runtime is None
            or tuple(runtime[:2]) != (execution_lease.owner_id, execution_lease.epoch)
            or float(runtime["expires_at"]) <= now
            or float(runtime["expires_at"]) < execution_lease.expires_at
            or projection is None
            or tuple(projection[:2]) != (workflow_lease.owner_id, workflow_lease.epoch)
            or float(projection["expires_at"]) != float(runtime["expires_at"])
            or float(projection["expires_at"]) < workflow_lease.expires_at
            or fence is None
            or tuple(fence) != (execution_lease.owner_id, execution_lease.epoch, "active")
        ):
            raise UnitOfWorkConflict("workflow handoff authority is stale")

    async def ensure_and_bind_precreated_start(
        self,
        transaction: WorkflowTransaction,
        request: StartAdmissionRequest,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        dispatch_claim: RuntimeStartDispatchClaim,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> PrecreatedStartDispatch:
        from simple_harness.runtime.start_snapshot import StartSnapshot
        from simple_harness.workflow.execution_ports import (
            PrecreatedStartAction,
            PrecreatedStartDispatch,
        )

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        tx = self._assert_open_workflow_transaction(transaction)
        if request.mode is not StartMode.PRECREATED:
            raise UnitOfWorkConflict("precreated admission requires precreated mode")
        payload_json, fingerprint, run_id, trace_id, thread_id = self._start_identity(request)
        receipt: StartAdmissionReceipt | None = None
        existing = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE request_key=?",
            (request.request_key,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["request_fingerprint"]) != fingerprint
                or str(existing["request_json"]) != payload_json
                or str(existing["run_id"]) != run_id
            ):
                raise UnitOfWorkConflict("start request key reused with different payload")
            receipt = self._start_receipt(existing, connection=tx.connection)
            if receipt.phase is StartPhase.SETTLED:
                return PrecreatedStartDispatch(
                    PrecreatedStartAction.SETTLED,
                    receipt,
                    serialized_outcome=receipt.serialized_outcome,
                )
        if dispatch_claim.run_id != run_id:
            raise UnitOfWorkConflict("dispatch claim belongs to another Run")
        dispatch = tx.connection.execute(
            "SELECT * FROM runtime_start_dispatch_claims WHERE claim_id=?",
            (dispatch_claim.claim_id,),
        ).fetchone()
        if dispatch is None:
            raise UnitOfWorkConflict("runtime start dispatch claim is missing")
        stable_claim = (
            str(dispatch["run_id"]),
            str(dispatch["owner_id"]),
            int(dispatch["runtime_lease_epoch"]),
            int(dispatch["claim_epoch"]),
        )
        if stable_claim != (
            dispatch_claim.run_id,
            dispatch_claim.owner_id,
            dispatch_claim.runtime_lease_epoch,
            dispatch_claim.claim_epoch,
        ):
            raise UnitOfWorkConflict("runtime start dispatch capability is stale")
        if existing is not None and str(dispatch["state"]) != "consumed":
            raise UnitOfWorkConflict("start request key reused with different payload")
        run = tx.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        snapshot = tx.connection.execute(
            "SELECT snapshot_json FROM run_start_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        if (
            run is None
            or str(run["driver_kind"]) != "workflow"
            or str(run["state"]) != "running"
            or snapshot is None
        ):
            raise UnitOfWorkConflict("precreated generic Run identity changed")
        parsed_snapshot = StartSnapshot.from_json(
            cast(dict[str, JsonValue], json.loads(str(snapshot["snapshot_json"])))
        )
        if parsed_snapshot.workflow_admission != request:
            raise UnitOfWorkConflict("precreated start snapshot changed")
        if existing is not None:
            assert receipt is not None
            self._require_current_start_claim(
                tx.connection,
                receipt=receipt,
                execution_lease=execution_lease,
                run_fence=run_fence,
                now=now,
            )
            if str(existing["claim_owner"]) != execution_lease.owner_id:
                raise UnitOfWorkConflict("workflow start receipt belongs to another activation")
            if receipt.claim_action is not StartClaimAction.NEW:
                raise UnitOfWorkConflict("first start replay no longer has NEW claim authority")
            return PrecreatedStartDispatch(
                PrecreatedStartAction.NEW_CLAIMED,
                receipt,
                activation=receipt.activation,
            )
        if str(dispatch["state"]) != "claimed" or float(dispatch["expires_at"]) <= now:
            raise UnitOfWorkConflict("runtime start dispatch claim is inactive")
        self._require_runtime_and_fence(
            tx.connection,
            run_id=run_id,
            execution_lease=execution_lease,
            run_fence=run_fence,
            now=now,
            require_exact_expiry=False,
        )
        current_runtime = tx.connection.execute(
            "SELECT expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        assert current_runtime is not None
        current_expiry = float(current_runtime["expires_at"])
        namespace = request.checkpoint_namespace
        _fault(fault, "workflow:ensure_precreated_start:before_workflow_leases_write")
        tx.connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?)",
            (
                run_id,
                namespace,
                execution_lease.owner_id,
                execution_lease.epoch,
                current_expiry,
            ),
        )
        _fault(fault, "workflow:ensure_precreated_start:after_workflow_leases_write")
        _fault(
            fault,
            "workflow:ensure_precreated_start:before_workflow_start_admissions_write",
        )
        tx.connection.execute(
            "INSERT INTO"
            " workflow_start_admissions(request_key,request_id,request_fingerprint,request_json,mode,run_id,trace_id,thread_id,phase,version,claim_action,claim_owner,claim_epoch,claim_expires_at,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?, 'claimed',0,'new',?,?,?,?,?)",
            (
                request.request_key,
                request.request_id,
                fingerprint,
                payload_json,
                request.mode.value,
                run_id,
                trace_id,
                thread_id,
                execution_lease.owner_id,
                execution_lease.epoch,
                current_expiry,
                now,
                now,
            ),
        )
        _fault(
            fault,
            "workflow:ensure_precreated_start:after_workflow_start_admissions_write",
        )
        _fault(
            fault,
            "workflow:ensure_precreated_start:before_runtime_start_dispatch_claims_write",
        )
        changed = tx.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET"
            " state='consumed',version=version+1,updated_at=? WHERE claim_id=? AND state='claimed'"
            " AND owner_id=? AND runtime_lease_epoch=? AND claim_epoch=?",
            (
                now,
                dispatch_claim.claim_id,
                dispatch_claim.owner_id,
                dispatch_claim.runtime_lease_epoch,
                dispatch_claim.claim_epoch,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("runtime start dispatch consume CAS failed")
        _fault(
            fault,
            "workflow:ensure_precreated_start:after_runtime_start_dispatch_claims_write",
        )
        tx.register_after_commit_fault("workflow:ensure_precreated_start:after_commit")
        stored = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE request_key=?",
            (request.request_key,),
        ).fetchone()
        assert stored is not None
        receipt = self._start_receipt(stored, connection=tx.connection)
        return PrecreatedStartDispatch(
            PrecreatedStartAction.NEW_CLAIMED,
            receipt,
            activation=receipt.activation,
        )

    async def recover_precreated_start(
        self,
        transaction: WorkflowTransaction,
        recovery_work: WorkflowRecoveryWork,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> PrecreatedStartDispatch:
        from simple_harness.workflow.execution_ports import (
            PrecreatedStartAction,
            PrecreatedStartDispatch,
            WorkflowRecoveryReceiptKind,
        )

        del ttl_seconds
        tx = self._assert_open_workflow_transaction(transaction)
        if recovery_work.receipt_kind is not WorkflowRecoveryReceiptKind.START:
            raise UnitOfWorkConflict("precreated start recovery work has wrong kind")
        row = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE request_key=?",
            (recovery_work.receipt_id,),
        ).fetchone()
        if row is None:
            raise UnitOfWorkConflict("precreated start receipt is missing")
        current = self._start_receipt(
            row,
            connection=tx.connection,
            activation_override=(
                recovery_work.receipt_snapshot.activation
                if int(row["version"]) == recovery_work.receipt_version
                else None
            ),
        )
        if (
            current.request != recovery_work.receipt_snapshot.request
            or current.request_fingerprint != recovery_work.request_fingerprint
        ):
            raise UnitOfWorkConflict("precreated start recovery identity changed")
        if current.phase is StartPhase.SETTLED:
            return PrecreatedStartDispatch(
                PrecreatedStartAction.SETTLED,
                current,
                serialized_outcome=current.serialized_outcome,
            )
        run = tx.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (current.run_id,)
        ).fetchone()
        if run is None or str(run["state"]) != RunState.RUNNING.value:
            raise UnitOfWorkConflict("precreated recovery requires RUNNING Run")
        if (
            current.version == recovery_work.receipt_version + 1
            and current.request == recovery_work.receipt_snapshot.request
            and current.request_fingerprint == recovery_work.request_fingerprint
            and current.phase == recovery_work.receipt_snapshot.phase
            and current.claim_owner == execution_lease.owner_id
        ):
            self._require_current_start_claim(
                tx.connection,
                receipt=current,
                execution_lease=execution_lease,
                run_fence=run_fence,
                now=now,
            )
            if current.claim_action is not StartClaimAction.RESUME:
                raise UnitOfWorkConflict("recovered start replay lacks RESUME claim authority")
            action = (
                PrecreatedStartAction.RESUME_RUNNING
                if current.phase is StartPhase.RUNNING
                else PrecreatedStartAction.RESUME_CLAIMED
            )
            return PrecreatedStartDispatch(
                action,
                current,
                activation=current.activation,
            )
        if current != recovery_work.receipt_snapshot:
            raise UnitOfWorkConflict("precreated start recovery snapshot changed")
        if (
            current.request_fingerprint != recovery_work.request_fingerprint
            or current.version != recovery_work.receipt_version
            or current.request.mode is not StartMode.PRECREATED
        ):
            raise UnitOfWorkConflict("precreated start recovery identity changed")
        if current.phase not in {StartPhase.CLAIMED, StartPhase.RUNNING}:
            raise UnitOfWorkConflict("precreated start recovery phase is invalid")
        self._require_runtime_and_fence(
            tx.connection,
            run_id=current.run_id,
            execution_lease=execution_lease,
            run_fence=run_fence,
            now=now,
            require_exact_expiry=False,
        )
        runtime = tx.connection.execute(
            "SELECT expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (current.run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        assert runtime is not None
        current_expiry = float(runtime["expires_at"])
        namespace = current.request.checkpoint_namespace
        projection = tx.connection.execute(
            "SELECT owner_id,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (current.run_id, namespace),
        ).fetchone()
        if (
            projection is not None
            and float(projection["expires_at"]) > now
            and str(projection["owner_id"]) != execution_lease.owner_id
        ):
            raise UnitOfWorkConflict("precreated workflow owner remains active")
        if current.phase is StartPhase.RUNNING:
            head = tx.connection.execute(
                "SELECT 1 FROM workflow_checkpoints WHERE run_id=? AND namespace=? LIMIT 1",
                (current.run_id, namespace),
            ).fetchone()
            if head is None:
                raise UnitOfWorkConflict("RUNNING workflow start has no genesis head")
        _fault(fault, "workflow:recover_precreated_start:before_workflow_leases_write")
        tx.connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(run_id,namespace) DO UPDATE SET"
            " owner_id=excluded.owner_id,epoch=excluded.epoch,expires_at=excluded.expires_at",
            (
                current.run_id,
                namespace,
                execution_lease.owner_id,
                execution_lease.epoch,
                current_expiry,
            ),
        )
        _fault(fault, "workflow:recover_precreated_start:after_workflow_leases_write")
        _fault(
            fault,
            "workflow:recover_precreated_start:before_workflow_start_admissions_write",
        )
        changed = tx.connection.execute(
            "UPDATE workflow_start_admissions SET version=version+1,"
            "claim_action='resume',claim_owner=?,claim_epoch=?,claim_expires_at=?,"
            "updated_at=? WHERE request_key=? AND version=? AND phase=?",
            (
                execution_lease.owner_id,
                execution_lease.epoch,
                current_expiry,
                now,
                current.request_key,
                current.version,
                current.phase.value,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("precreated start recovery CAS failed")
        _fault(
            fault,
            "workflow:recover_precreated_start:after_workflow_start_admissions_write",
        )
        tx.register_after_commit_fault("workflow:recover_precreated_start:after_commit")
        updated = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE request_key=?",
            (current.request_key,),
        ).fetchone()
        assert updated is not None
        receipt = self._start_receipt(updated, connection=tx.connection)
        action = (
            PrecreatedStartAction.RESUME_RUNNING
            if current.phase is StartPhase.RUNNING
            else PrecreatedStartAction.RESUME_CLAIMED
        )
        return PrecreatedStartDispatch(
            action,
            receipt,
            activation=receipt.activation,
        )

    def list_unsettled_start_admissions(
        self,
        snapshot_cursor: str | None,
        *,
        limit: int,
    ) -> tuple[tuple[StartAdmissionReceipt, ...], str | None]:
        _validate_workflow_page_limit(limit)
        rows = self.database.connection.execute(
            "SELECT * FROM workflow_start_admissions "
            "WHERE phase IN ('admitted','claimed','running') "
            "AND (? IS NULL OR request_key>?) ORDER BY request_key LIMIT ?",
            (snapshot_cursor, snapshot_cursor, limit + 1),
        ).fetchall()
        page = rows[:limit]
        return tuple(self._start_receipt(row) for row in page), (
            None if len(rows) <= limit else str(page[-1]["request_key"])
        )

    def list_unsettled_resume_admissions(
        self,
        snapshot_cursor: str | None,
        *,
        limit: int,
    ) -> tuple[tuple[ResumeAdmissionReceipt, ...], str | None]:
        _validate_workflow_page_limit(limit)
        rows = self.database.connection.execute(
            "SELECT * FROM workflow_resume_admissions WHERE phase IN"
            " ('admitted','claimed','retry_wait') AND (? IS NULL OR receipt_id>?) ORDER BY"
            " receipt_id LIMIT ?",
            (snapshot_cursor, snapshot_cursor, limit + 1),
        ).fetchall()
        page = rows[:limit]
        return tuple(self._resume_receipt(row) for row in page), (
            None if len(rows) <= limit else str(page[-1]["receipt_id"])
        )

    def _acquire_activation_rows(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        namespace: str,
        owner_id: str,
        now: float,
        ttl_seconds: float,
    ) -> WorkflowActivation:
        current = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        if (
            current is not None
            and float(current["expires_at"]) > now
            and str(current["owner_id"]) != owner_id
        ):
            raise UnitOfWorkConflict("runtime activation is owned by another worker")
        epoch = (0 if current is None else int(current["epoch"])) + 1
        if (
            current is not None
            and float(current["expires_at"]) > now
            and str(current["owner_id"]) == owner_id
        ):
            epoch = int(current["epoch"])
        expires_at = now + ttl_seconds
        connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(run_id,namespace) DO UPDATE SET"
            " owner_id=excluded.owner_id,epoch=excluded.epoch,expires_at=excluded.expires_at",
            (run_id, RUNTIME_LEASE_NAMESPACE, owner_id, epoch, expires_at),
        )
        connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(run_id,namespace) DO UPDATE SET"
            " owner_id=excluded.owner_id,epoch=excluded.epoch,expires_at=excluded.expires_at",
            (run_id, namespace, owner_id, epoch, expires_at),
        )
        fence = connection.execute(
            "SELECT epoch FROM run_fences WHERE run_id=?", (run_id,)
        ).fetchone()
        fence_epoch = (0 if fence is None else int(fence["epoch"])) + 1
        connection.execute(
            "INSERT INTO"
            " run_fences(run_id,epoch,owner_id,runtime_lease_epoch,state,acquired_at,released_at)"
            " VALUES(?,?,?,?, 'active',?,NULL) ON CONFLICT(run_id) DO UPDATE SET"
            " epoch=excluded.epoch,owner_id=excluded.owner_id,runtime_lease_epoch=excluded.runtime_lease_epoch,state='active',acquired_at=excluded.acquired_at,released_at=NULL",  # noqa: E501
            (run_id, fence_epoch, owner_id, epoch, now),
        )
        return WorkflowActivation(
            ExecutionLease(run_id, RUNTIME_LEASE_NAMESPACE, owner_id, epoch, expires_at),
            RunFenceLease(RunId(run_id), fence_epoch, owner_id, epoch),
            WorkflowLease(run_id, owner_id, epoch, expires_at, epoch, namespace),
        )

    async def claim_activation(
        self,
        transaction: WorkflowTransaction,
        run_id: str,
        expected_run_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation:
        tx = self._assert_open_workflow_transaction(transaction)
        receipt = tx.connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE run_id=?", (run_id,)
        ).fetchone()
        run = tx.connection.execute(
            "SELECT version,state FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if receipt is None or str(receipt["mode"]) != StartMode.STANDALONE.value:
            raise UnitOfWorkConflict("start receipt is not claimable")
        phase = str(receipt["phase"])
        if phase not in {
            StartPhase.ADMITTED.value,
            StartPhase.CLAIMED.value,
            StartPhase.RUNNING.value,
        }:
            raise UnitOfWorkConflict("start receipt is not claimable")
        namespace = str(json.loads(str(receipt["request_json"]))["checkpoint_namespace"])
        if phase in {StartPhase.CLAIMED.value, StartPhase.RUNNING.value}:
            runtime = tx.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id=? AND namespace=?",
                (run_id, RUNTIME_LEASE_NAMESPACE),
            ).fetchone()
            if runtime is not None and float(runtime["expires_at"]) > now:
                if phase == StartPhase.RUNNING.value:
                    raise UnitOfWorkConflict("running workflow activation has not expired")
                if str(runtime["owner_id"]) != owner_id:
                    raise UnitOfWorkConflict("workflow activation has an active owner")
                activation = self._read_workflow_activation(
                    run_id,
                    namespace=namespace,
                    owner_id=owner_id,
                    connection=tx.connection,
                )
                if (
                    str(receipt["claim_owner"]) != owner_id
                    or int(receipt["claim_epoch"]) != activation.workflow_lease.epoch
                    or float(receipt["claim_expires_at"]) > activation.execution_lease.expires_at
                ):
                    raise UnitOfWorkConflict("workflow activation receipt is stale")
                return activation
        if (
            run is None
            or int(run["version"]) != expected_run_version
            or str(run["state"])
            not in ({"running"} if phase == StartPhase.RUNNING.value else {"created", "queued"})
        ):
            raise UnitOfWorkConflict("workflow Run is not claimable")
        _fault(fault, "workflow:claim_activation:before_workflow_leases_write")
        activation = self._acquire_activation_rows(
            tx.connection,
            run_id=run_id,
            namespace=namespace,
            owner_id=owner_id,
            now=now,
            ttl_seconds=ttl_seconds,
        )
        _fault(fault, "workflow:claim_activation:after_workflow_leases_write")
        _fault(fault, "workflow:claim_activation:before_workflow_start_admissions_write")
        claim_action = (
            StartClaimAction.NEW if phase == StartPhase.ADMITTED.value else StartClaimAction.RESUME
        )
        changed = tx.connection.execute(
            "UPDATE workflow_start_admissions SET phase='claimed',version=version+1,"
            "claim_action=?,claim_owner=?,claim_epoch=?,claim_expires_at=?,"
            "updated_at=? WHERE run_id=? AND phase=? AND version=?",
            (
                claim_action.value,
                owner_id,
                activation.execution_lease.epoch,
                activation.execution_lease.expires_at,
                now,
                run_id,
                phase,
                int(receipt["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("start claim CAS failed")
        _fault(fault, "workflow:claim_activation:after_workflow_start_admissions_write")
        return activation

    async def bind_activation(
        self,
        transaction: WorkflowTransaction,
        run_id: str,
        expected_run_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation:
        del ttl_seconds
        tx = self._assert_open_workflow_transaction(transaction)
        run = tx.connection.execute("SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or int(run["version"]) != expected_run_version:
            raise UnitOfWorkConflict("precreated Run version changed")
        self._require_runtime_and_fence(
            tx.connection,
            run_id=run_id,
            execution_lease=execution_lease,
            run_fence=run_fence,
            now=now,
        )
        receipt = tx.connection.execute(
            "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if receipt is None:
            raise UnitOfWorkConflict("workflow start admission is missing")
        namespace = str(json.loads(str(receipt["request_json"]))["checkpoint_namespace"])
        workflow = tx.connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, namespace),
        ).fetchone()
        if workflow is None or tuple(workflow) != (
            execution_lease.owner_id,
            execution_lease.epoch,
            execution_lease.expires_at,
        ):
            raise UnitOfWorkConflict("workflow lease projection changed")
        return WorkflowActivation(
            execution_lease,
            run_fence,
            WorkflowLease(
                run_id,
                execution_lease.owner_id,
                execution_lease.epoch,
                execution_lease.expires_at,
                execution_lease.epoch,
                namespace,
            ),
        )

    async def renew_activation(
        self,
        transaction: WorkflowTransaction,
        activation: WorkflowActivation,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation:
        tx = self._assert_open_workflow_transaction(transaction)
        self._require_runtime_and_fence(
            tx.connection,
            run_id=activation.execution_lease.run_id,
            execution_lease=activation.execution_lease,
            run_fence=activation.run_fence,
            now=now,
        )
        expires_at = now + ttl_seconds
        _fault(fault, "workflow:renew_activation:before_runtime_lease_write")
        changed = tx.connection.execute(
            "UPDATE workflow_leases SET expires_at=? WHERE run_id=? AND namespace=? AND owner_id=?"
            " AND epoch=? AND expires_at=?",
            (
                expires_at,
                activation.execution_lease.run_id,
                RUNTIME_LEASE_NAMESPACE,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
                activation.execution_lease.expires_at,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("runtime lease renew CAS failed")
        _fault(fault, "workflow:renew_activation:after_runtime_lease_write")
        _fault(fault, "workflow:renew_activation:before_workflow_lease_write")
        changed = tx.connection.execute(
            "UPDATE workflow_leases SET expires_at=? WHERE run_id=? AND namespace=? AND owner_id=?"
            " AND epoch=? AND expires_at=?",
            (
                expires_at,
                activation.workflow_lease.run_id,
                activation.workflow_lease.namespace,
                activation.workflow_lease.owner_id,
                activation.workflow_lease.epoch,
                activation.workflow_lease.expires_at,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow lease renew CAS failed")
        _fault(fault, "workflow:renew_activation:after_workflow_lease_write")
        return WorkflowActivation(
            ExecutionLease(
                activation.execution_lease.run_id,
                RUNTIME_LEASE_NAMESPACE,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
                expires_at,
            ),
            activation.run_fence,
            WorkflowLease(
                activation.workflow_lease.run_id,
                activation.workflow_lease.owner_id,
                activation.workflow_lease.epoch,
                expires_at,
                activation.execution_lease.epoch,
                activation.workflow_lease.namespace,
            ),
        )

    async def release_activation(
        self,
        transaction: WorkflowTransaction,
        activation: WorkflowActivation,
        expected_run_version: int,
        outcome: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> None:
        tx = self._assert_open_workflow_transaction(transaction)
        fault = fault or tx._fault
        run = tx.connection.execute(
            "SELECT state,version,parent_run_id FROM runs WHERE run_id=?",
            (activation.execution_lease.run_id,),
        ).fetchone()
        if run is None or int(run["version"]) != expected_run_version:
            raise UnitOfWorkConflict("release Run version changed")
        terminal_status = outcome.get("terminal_status")
        if terminal_status is not None:
            if terminal_status != str(run["state"]):
                raise UnitOfWorkConflict("release terminal outcome differs from Run")
            if run["parent_run_id"] is not None:
                self._commit_native_child_terminal(
                    tx.connection,
                    activation=activation,
                    expected_run_version=expected_run_version,
                    terminal_state=RunState(str(terminal_status)),
                    output=outcome.get("output"),
                    now=now,
                    fault=fault,
                )
            self._record_workflow_terminal_outcome(
                tx.connection,
                activation=activation,
                expected_run_version=expected_run_version,
                terminal_state=RunState(str(terminal_status)),
                outcome=outcome,
                now=now,
                fault=fault,
            )
        _fault(fault, "workflow:release_activation:before_workflow_leases_write")
        tx.connection.execute(
            "DELETE FROM workflow_leases WHERE run_id=? AND namespace IN (?,?) AND owner_id=? AND"
            " epoch=?",
            (
                activation.execution_lease.run_id,
                RUNTIME_LEASE_NAMESPACE,
                activation.workflow_lease.namespace,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
            ),
        )
        _fault(fault, "workflow:release_activation:after_workflow_leases_write")
        _fault(fault, "workflow:release_activation:before_run_fences_write")
        changed = tx.connection.execute(
            "UPDATE run_fences SET state='released',released_at=? WHERE run_id=? AND owner_id=? AND"
            " epoch=? AND runtime_lease_epoch=? AND state='active'",
            (
                now,
                activation.execution_lease.run_id,
                activation.run_fence.owner_id,
                activation.run_fence.epoch,
                activation.execution_lease.epoch,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("activation release fence CAS failed")
        _fault(fault, "workflow:release_activation:after_run_fences_write")

    def _record_workflow_terminal_outcome(
        self,
        connection: sqlite3.Connection,
        *,
        activation: WorkflowActivation,
        expected_run_version: int,
        terminal_state: RunState,
        outcome: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None,
    ) -> None:
        """Project the Native terminal transaction into its durable verifier chain."""

        run_id = activation.execution_lease.run_id
        checkpoint = connection.execute(
            "SELECT checkpoint_id,namespace,version,checkpoint_hash "
            "FROM workflow_checkpoints WHERE run_id=? AND namespace=? "
            "ORDER BY version DESC LIMIT 1",
            (run_id, activation.workflow_lease.namespace),
        ).fetchone()
        if checkpoint is None:
            raise UnitOfWorkConflict("terminal workflow checkpoint is missing")

        terminal_payload_value = json.loads(canonical_json(cast(JsonValue, dict(outcome))))
        if not isinstance(terminal_payload_value, dict):
            raise UnitOfWorkConflict("terminal workflow payload is invalid")
        terminal_payload = cast(dict[str, JsonValue], terminal_payload_value)
        event_payload_json = canonical_json(terminal_payload)
        checkpoint_id = str(checkpoint["checkpoint_id"])
        identity = f"{run_id}|{checkpoint_id}|{terminal_state.value}"
        event_id = hashlib.sha256(
            f"simple-harness.workflow.terminal-event|{identity}".encode()
        ).hexdigest()
        fence_receipt_id = hashlib.sha256(
            f"simple-harness.workflow.terminal-fence|{identity}".encode()
        ).hexdigest()
        receipt_id = hashlib.sha256(
            f"simple-harness.workflow.terminal-receipt|{identity}".encode()
        ).hexdigest()

        delivery_rows = connection.execute(
            "SELECT delivery_id,sink_kind,idempotency_key,payload_json,created_at "
            "FROM delivery_outbox WHERE run_id=? ORDER BY delivery_id",
            (run_id,),
        ).fetchall()
        delivery_ids: list[JsonValue] = [str(row["delivery_id"]) for row in delivery_rows]
        delivery_facts: list[JsonValue] = [
            cast(
                JsonValue,
                {
                    "delivery_id": str(row["delivery_id"]),
                    "sink_kind": str(row["sink_kind"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "payload": cast(JsonValue, json.loads(str(row["payload_json"]))),
                    "created_at": float(row["created_at"]),
                },
            )
            for row in delivery_rows
        ]
        canonical_outcome: dict[str, JsonValue] = {
            "receipt_id": receipt_id,
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "state": terminal_state.value,
            "event_id": event_id,
            "delivery_ids": delivery_ids,
            "terminal_payload": terminal_payload,
            "delivery_facts": delivery_facts,
        }
        outcome_hash = hashlib.sha256(canonical_json(canonical_outcome).encode()).hexdigest()

        _fault(fault, "workflow:release_activation:before_terminal_event_write")
        self._insert_event(
            connection,
            event_id=event_id,
            run_id=run_id,
            kind=f"run.{terminal_state.value}",
            payload=terminal_payload,
            now=now,
        )
        _fault(fault, "workflow:release_activation:after_terminal_event_write")
        _fault(fault, "workflow:release_activation:before_terminal_fence_write")
        connection.execute(
            "INSERT INTO workflow_terminal_fence_receipts("
            "receipt_id,run_id,owner_id,runtime_lease_epoch,run_fence_epoch,created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                fence_receipt_id,
                run_id,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
                activation.run_fence.epoch,
                now,
            ),
        )
        _fault(fault, "workflow:release_activation:after_terminal_fence_write")
        _fault(fault, "workflow:release_activation:before_terminal_receipt_write")
        connection.execute(
            "INSERT INTO workflow_terminal_receipts("
            "receipt_id,run_id,checkpoint_id,checkpoint_namespace,checkpoint_version,"
            "checkpoint_hash,state,run_version,event_id,event_payload_hash,"
            "delivery_ids_json,delivery_facts_json,terminal_payload_json,"
            "terminal_fence_receipt_id,outcome_hash,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                run_id,
                checkpoint_id,
                str(checkpoint["namespace"]),
                int(checkpoint["version"]),
                str(checkpoint["checkpoint_hash"]),
                terminal_state.value,
                expected_run_version,
                event_id,
                hashlib.sha256(event_payload_json.encode()).hexdigest(),
                canonical_json(delivery_ids),
                canonical_json(cast(JsonValue, delivery_facts)),
                event_payload_json,
                fence_receipt_id,
                outcome_hash,
                now,
            ),
        )
        _fault(fault, "workflow:release_activation:after_terminal_receipt_write")

    def _commit_native_child_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        activation: WorkflowActivation,
        expected_run_version: int,
        terminal_state: RunState,
        output: JsonValue | None,
        now: float,
        fault: FaultHook | None,
    ) -> None:
        """Close a Native child inside its terminal checkpoint transaction."""

        child_run_id = activation.execution_lease.run_id
        command = connection.execute(
            "SELECT * FROM child_commands WHERE child_run_id=?", (child_run_id,)
        ).fetchone()
        if command is None:
            raise UnitOfWorkConflict("workflow child has no durable launch command")
        command_id = str(command["command_id"])
        parent_run_id = str(command["parent_run_id"])
        link = connection.execute(
            "SELECT attachment_policy FROM run_links WHERE parent_run_id=? AND child_run_id=?",
            (parent_run_id, child_run_id),
        ).fetchone()
        if link is None:
            raise UnitOfWorkConflict("workflow child attachment policy is missing")
        policy = AttachmentPolicy(str(link["attachment_policy"]))
        previous_version = expected_run_version - 1
        if previous_version < 0:
            raise UnitOfWorkConflict("workflow child terminal version is invalid")
        identity = f"{child_run_id}:{previous_version}:{terminal_state.value}"
        signal_id = None if policy is AttachmentPolicy.DETACHED else f"{identity}:signal"
        event_id = f"{identity}:event"
        receipt_id = f"{identity}:receipt"
        from simple_harness.workflow.native import NativeWorkflowExecutable

        terminal_state_value: Mapping[str, object] = (
            output if isinstance(output, Mapping) else {"values": {}}
        )
        terminal_intents = NativeWorkflowExecutable.terminal_intents(
            terminal_state_value,
            run_id=child_run_id,
            status=terminal_state.value,
            error=None,
            recovery_action=None,
        )
        if not terminal_intents or terminal_intents[-1].event_type != "workflow.final":
            raise UnitOfWorkConflict("workflow child terminal public projection is missing")
        result = cast(JsonValue, dict(terminal_intents[-1].payload))
        terminal_payload: dict[str, JsonValue] = {
            "status": terminal_state.value,
            "result": result,
        }
        payload_json = _object_json(terminal_payload, "terminal_payload")
        outcome_hash = hashlib.sha256(
            canonical_json(
                {
                    "command_id": command_id,
                    "expected_child_version": previous_version,
                    "terminal_state": terminal_state.value,
                    "signal_id": signal_id,
                    "event_id": event_id,
                    "payload": json.loads(payload_json),
                }
            ).encode()
        ).hexdigest()
        existing = connection.execute(
            "SELECT * FROM child_terminal_receipts WHERE command_id=?", (command_id,)
        ).fetchone()
        if existing is not None:
            receipt = _child_terminal_receipt(existing)
            expected = (
                receipt_id,
                child_run_id,
                terminal_state.value,
                outcome_hash,
                signal_id,
                event_id,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
                activation.run_fence.epoch,
            )
            actual = (
                receipt.receipt_id,
                receipt.child_run_id,
                receipt.terminal_state,
                receipt.outcome_hash,
                receipt.signal_id,
                receipt.event_id,
                receipt.owner_id,
                receipt.runtime_lease_epoch,
                receipt.fence_epoch,
            )
            if actual != expected:
                raise UnitOfWorkConflict("workflow child terminal receipt differs")
            return
        _fault(fault, "workflow:release_activation:before_child_command_write")
        changed = connection.execute(
            "UPDATE child_commands SET state='acked',updated_at=? "
            "WHERE command_id=? AND state IN ('pending','scheduled')",
            (now, command_id),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow child command terminal CAS failed")
        _fault(fault, "workflow:release_activation:after_child_command_write")
        if signal_id is not None:
            _fault(fault, "workflow:release_activation:before_child_signal_write")
            connection.execute(
                "INSERT INTO child_signals("
                "signal_id,parent_run_id,child_run_id,payload_json,state,version,"
                "created_at,updated_at) VALUES(?,?,?,?,'pending',0,?,?)",
                (signal_id, parent_run_id, child_run_id, payload_json, now, now),
            )
            _fault(fault, "workflow:release_activation:after_child_signal_write")
        _fault(fault, "workflow:release_activation:before_child_event_write")
        self._insert_event(
            connection,
            event_id=event_id,
            run_id=child_run_id,
            kind=f"child.{terminal_state.value}",
            payload={
                "command_id": command_id,
                "signal_id": signal_id,
                "terminal": terminal_payload,
            },
            now=now,
        )
        _fault(fault, "workflow:release_activation:after_child_event_write")
        _fault(fault, "workflow:release_activation:before_child_receipt_write")
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
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
                activation.run_fence.epoch,
                now,
            ),
        )
        _fault(fault, "workflow:release_activation:after_child_receipt_write")

    @staticmethod
    def _resume_payload(request: ResumeAdmissionRequest) -> dict[str, JsonValue]:
        return {
            "receipt_id": request.receipt_id,
            "run_id": request.run_id,
            "expected_run_version": request.expected_run_version,
            "expected_checkpoint_head": request.expected_checkpoint_head,
            "pending_interrupts": [list(item) for item in request.pending_interrupts],
            "responses": _thaw(request.responses),
            "responses_hash": request.responses_hash,
            "mode": request.mode.value,
        }

    @classmethod
    def _resume_fingerprint(cls, request: ResumeAdmissionRequest) -> tuple[str, str]:
        payload = cls._resume_payload(request)
        payload_json = canonical_json(payload)
        actual_responses_hash = hashlib.sha256(
            canonical_json(payload["responses"]).encode()
        ).hexdigest()
        if actual_responses_hash != request.responses_hash:
            raise UnitOfWorkConflict("resume response hash does not match content")
        return payload_json, hashlib.sha256(payload_json.encode()).hexdigest()

    def _resume_receipt(
        self,
        row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None = None,
        include_activation: bool = True,
    ) -> ResumeAdmissionReceipt:
        authority = self.database.connection if connection is None else connection
        payload = json.loads(str(row["request_json"]))
        pending = tuple((str(item[0]), str(item[1])) for item in payload["pending_interrupts"])
        request = ResumeAdmissionRequest(
            receipt_id=str(payload["receipt_id"]),
            run_id=str(payload["run_id"]),
            expected_run_version=int(payload["expected_run_version"]),
            expected_checkpoint_head=str(payload["expected_checkpoint_head"]),
            pending_interrupts=pending,
            responses=payload["responses"],
            responses_hash=str(payload["responses_hash"]),
            mode=StartMode(str(payload["mode"])),
        )
        payload_json, fingerprint = self._resume_fingerprint(request)
        if (
            str(row["request_json"]) != payload_json
            or str(row["request_fingerprint"]) != fingerprint
            or str(row["run_id"]) != request.run_id
            or str(row["mode"]) != request.mode.value
            or int(row["expected_run_version"]) != request.expected_run_version
            or str(row["expected_checkpoint_head"]) != request.expected_checkpoint_head
        ):
            raise UnitOfWorkConflict("resume receipt durable binding changed")
        activation = None
        if (
            include_activation
            and row["claim_owner"] is not None
            and str(row["phase"]) == ResumePhase.CLAIMED.value
        ):
            start = authority.execute(
                "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
                (request.run_id,),
            ).fetchone()
            if start is not None:
                activation = self._read_workflow_activation(
                    request.run_id,
                    namespace=str(json.loads(str(start["request_json"]))["checkpoint_namespace"]),
                    owner_id=str(row["claim_owner"]),
                    connection=authority,
                )
        outcome = (
            None
            if row["outcome_json"] is None
            else freeze_json(json.loads(str(row["outcome_json"])))
        )
        return ResumeAdmissionReceipt(
            request=request,
            request_fingerprint=str(row["request_fingerprint"]),
            phase=ResumePhase(str(row["phase"])),
            version=int(row["version"]),
            claim_owner=None if row["claim_owner"] is None else str(row["claim_owner"]),
            claim_epoch=None if row["claim_epoch"] is None else int(row["claim_epoch"]),
            claim_expires_at=(
                None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
            ),
            activation=activation,
            serialized_outcome=outcome,
            next_attempt_at=(
                None if row["next_attempt_at"] is None else float(row["next_attempt_at"])
            ),
        )

    async def admit_resume(
        self,
        transaction: WorkflowTransaction,
        request: ResumeAdmissionRequest,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        payload_json, fingerprint = self._resume_fingerprint(request)
        existing = tx.connection.execute(
            "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
            (request.receipt_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != fingerprint:
                raise UnitOfWorkConflict("resume receipt reused with different request")
            return self._resume_receipt(existing, connection=tx.connection)
        run = tx.connection.execute(
            "SELECT state,version FROM runs WHERE run_id=?", (request.run_id,)
        ).fetchone()
        head = tx.connection.execute(
            "SELECT checkpoint_id,checkpoint_json FROM workflow_checkpoints WHERE run_id=? ORDER BY"
            " version DESC LIMIT 1",
            (request.run_id,),
        ).fetchone()
        if (
            run is None
            or str(run["state"]) not in {"waiting", "queued"}
            or int(run["version"]) != request.expected_run_version
            or head is None
            or str(head["checkpoint_id"]) != request.expected_checkpoint_head
        ):
            raise UnitOfWorkConflict("resume authority snapshot changed")
        pending = tuple(
            sorted(
                (
                    str(row["decision_id"]),
                    hashlib.sha256(str(row["request_json"]).encode()).hexdigest(),
                )
                for row in tx.connection.execute(
                    "SELECT decision_id,request_json FROM decisions WHERE run_id=? AND"
                    " state!='open'",
                    (request.run_id,),
                ).fetchall()
            )
        )
        if pending != request.pending_interrupts:
            raise UnitOfWorkConflict("resume pending interrupts changed")
        _fault(fault, "workflow:admit_resume:before_workflow_resume_admissions_write")
        tx.connection.execute(
            "INSERT INTO"
            " workflow_resume_admissions(receipt_id,run_id,request_fingerprint,request_json,mode,expected_run_version,expected_checkpoint_head,phase,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,'admitted',0,?,?)",
            (
                request.receipt_id,
                request.run_id,
                fingerprint,
                payload_json,
                request.mode.value,
                request.expected_run_version,
                request.expected_checkpoint_head,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:admit_resume:after_workflow_resume_admissions_write")
        return self._resume_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
                (request.receipt_id,),
            ).fetchone(),
            connection=tx.connection,
        )

    async def resolve_decision_and_admit_resume(
        self,
        transaction: WorkflowTransaction,
        request: ResumeAdmissionRequest,
        *,
        decision_id: str,
        nonce: str,
        expected_decision_version: int,
        response: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        """Resolve one open Workflow interrupt and admit its resume atomically."""

        tx = self._assert_open_workflow_transaction(transaction)
        decision_id = _required(decision_id, "decision_id")
        nonce = _required(nonce, "nonce")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        if isinstance(expected_decision_version, bool) or expected_decision_version < 0:
            raise ValueError("expected_decision_version must be non-negative")
        response_json = _object_json(response, "response")
        row = tx.connection.execute(
            "SELECT run_id,kind,state,request_json,version FROM decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise UnitOfWorkNotFound(decision_id)
        durable_request = json.loads(str(row["request_json"]))
        if (
            str(row["run_id"]) != request.run_id
            or str(row["state"]) != DecisionState.OPEN.value
            or int(row["version"]) != expected_decision_version
            or str(durable_request.get("nonce") or decision_id) != nonce
        ):
            raise UnitOfWorkConflict("workflow decision resume authority changed")
        _fault(fault, "workflow:resolve_resume:before_decision_write")
        changed = tx.connection.execute(
            "UPDATE decisions SET state='allowed',response_json=?,version=version+1,resolved_at=? "
            "WHERE decision_id=? AND run_id=? AND state='open' AND version=?",
            (response_json, now, decision_id, request.run_id, expected_decision_version),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow decision resolution CAS failed")
        _fault(fault, "workflow:resolve_resume:after_decision_write")
        changed = tx.connection.execute(
            "UPDATE runs SET state='queued',version=version+1,updated_at=? "
            "WHERE run_id=? AND state='waiting' AND version=?",
            (now, request.run_id, request.expected_run_version - 1),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow decision Run resolution CAS failed")
        self._insert_event(
            tx.connection,
            event_id=event_id,
            run_id=request.run_id,
            kind="decision.allowed",
            payload={"decision_id": decision_id, "kind": str(row["kind"])},
            now=now,
        )
        _fault(fault, "workflow:resolve_resume:before_admit")
        receipt = await self.admit_resume(transaction, request, now=now, fault=fault)
        _fault(fault, "workflow:resolve_resume:after_admit")
        return receipt

    async def _claim_resume(
        self,
        transaction: WorkflowTransaction,
        receipt_id: str,
        expected_receipt_version: int,
        *,
        owner_id: str,
        now: float,
        ttl_seconds: float,
        execution_lease: ExecutionLease | None,
        run_fence: RunFenceLease | None,
        expected_mode: StartMode,
        fault: FaultHook | None,
    ) -> ResumeAdmissionReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if (
            row is None
            or int(row["version"]) != expected_receipt_version
            or str(row["phase"]) not in {"admitted", "retry_wait", "claimed"}
            or str(row["mode"]) != expected_mode.value
        ):
            raise UnitOfWorkConflict("resume receipt is not claimable")
        if str(row["phase"]) == ResumePhase.RETRY_WAIT.value and (
            row["next_attempt_at"] is None or float(row["next_attempt_at"]) > now
        ):
            raise UnitOfWorkConflict("resume retry is not due")
        if (
            str(row["phase"]) == "claimed"
            and row["claim_expires_at"] is not None
            and float(row["claim_expires_at"]) > now
        ):
            raise UnitOfWorkConflict("resume receipt has an active claimant")
        run_id = str(row["run_id"])
        start = tx.connection.execute(
            "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if start is None:
            raise UnitOfWorkConflict("resume start authority is missing")
        namespace = str(json.loads(str(start["request_json"]))["checkpoint_namespace"])
        if execution_lease is None:
            activation = self._acquire_activation_rows(
                tx.connection,
                run_id=run_id,
                namespace=namespace,
                owner_id=owner_id,
                now=now,
                ttl_seconds=ttl_seconds,
            )
        else:
            assert run_fence is not None
            self._require_runtime_and_fence(
                tx.connection,
                run_id=run_id,
                execution_lease=execution_lease,
                run_fence=run_fence,
                now=now,
            )
            activation = WorkflowActivation(
                execution_lease,
                run_fence,
                WorkflowLease(
                    run_id,
                    owner_id,
                    execution_lease.epoch,
                    execution_lease.expires_at,
                    execution_lease.epoch,
                    namespace,
                ),
            )
            workflow = tx.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND"
                " namespace=?",
                (run_id, namespace),
            ).fetchone()
            if workflow is None or tuple(workflow) != (
                owner_id,
                execution_lease.epoch,
                execution_lease.expires_at,
            ):
                raise UnitOfWorkConflict("precreated resume workflow projection changed")
        _fault(fault, "workflow:claim_resume:before_workflow_resume_admissions_write")
        changed = tx.connection.execute(
            "UPDATE workflow_resume_admissions SET phase='claimed',version=version+1,"
            "claim_owner=?,claim_epoch=?,claim_expires_at=?,next_attempt_at=NULL,"
            "updated_at=? WHERE receipt_id=? AND version=?",
            (
                owner_id,
                activation.execution_lease.epoch,
                activation.execution_lease.expires_at,
                now,
                receipt_id,
                expected_receipt_version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("resume claim CAS failed")
        _fault(fault, "workflow:claim_resume:after_workflow_resume_admissions_write")
        return self._resume_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone(),
            connection=tx.connection,
        )

    async def claim_resume_standalone(
        self,
        transaction: WorkflowTransaction,
        receipt_id: str,
        expected_receipt_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        return await self._claim_resume(
            transaction,
            receipt_id,
            expected_receipt_version,
            owner_id=owner_id,
            now=now,
            ttl_seconds=ttl_seconds,
            execution_lease=None,
            run_fence=None,
            expected_mode=StartMode.STANDALONE,
            fault=fault,
        )

    async def claim_resume_precreated(
        self,
        transaction: WorkflowTransaction,
        receipt_id: str,
        expected_receipt_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        return await self._claim_resume(
            transaction,
            receipt_id,
            expected_receipt_version,
            owner_id=execution_lease.owner_id,
            now=now,
            ttl_seconds=ttl_seconds,
            execution_lease=execution_lease,
            run_fence=run_fence,
            expected_mode=StartMode.PRECREATED,
            fault=fault,
        )

    async def settle_resume(
        self,
        transaction: WorkflowTransaction,
        binding: ResumeCommitBinding,
        activation: WorkflowActivation,
        committed_checkpoint: str,
        outcome: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
            (binding.receipt_id,),
        ).fetchone()
        if (
            row is None
            or int(row["version"]) != binding.expected_receipt_version
            or str(row["request_fingerprint"]) != binding.request_fingerprint
            or str(row["phase"]) != "claimed"
        ):
            raise UnitOfWorkConflict("resume settlement binding changed")
        self._require_runtime_and_fence(
            tx.connection,
            run_id=str(row["run_id"]),
            execution_lease=activation.execution_lease,
            run_fence=activation.run_fence,
            now=now,
            require_exact_expiry=False,
        )
        head = tx.connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints WHERE run_id=? ORDER BY version DESC"
            " LIMIT 1",
            (str(row["run_id"]),),
        ).fetchone()
        if head is None or str(head["checkpoint_id"]) != committed_checkpoint:
            raise UnitOfWorkConflict("resume committed checkpoint changed")
        outcome_json = canonical_json(dict(outcome))
        _fault(fault, "workflow:settle_resume:before_workflow_resume_admissions_write")
        changed = tx.connection.execute(
            "UPDATE workflow_resume_admissions SET"
            " phase='settled',version=version+1,committed_checkpoint=?,outcome_json=?,updated_at=?"
            " WHERE receipt_id=? AND version=? AND phase='claimed'",
            (
                committed_checkpoint,
                outcome_json,
                now,
                binding.receipt_id,
                binding.expected_receipt_version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("resume settlement CAS failed")
        _fault(fault, "workflow:settle_resume:after_workflow_resume_admissions_write")
        current_run = tx.connection.execute(
            "SELECT version FROM runs WHERE run_id=?", (str(row["run_id"]),)
        ).fetchone()
        if current_run is None:
            raise UnitOfWorkConflict("resume Run disappeared before release")
        await self.release_activation(
            transaction,
            activation,
            int(current_run["version"]),
            outcome,
            now=now,
            fault=fault,
        )
        return self._resume_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
                (binding.receipt_id,),
            ).fetchone(),
            connection=tx.connection,
        )

    async def defer_resume_retry(
        self,
        transaction: WorkflowTransaction,
        binding: ResumeCommitBinding,
        activation: WorkflowActivation,
        retry_operation_id: str,
        retry_attempt: int,
        next_attempt_at: float,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
            (binding.receipt_id,),
        ).fetchone()
        if (
            row is None
            or int(row["version"]) != binding.expected_receipt_version
            or str(row["request_fingerprint"]) != binding.request_fingerprint
            or str(row["phase"]) != ResumePhase.CLAIMED.value
        ):
            raise UnitOfWorkConflict("resume retry binding changed")
        self._require_runtime_and_fence(
            tx.connection,
            run_id=str(row["run_id"]),
            execution_lease=activation.execution_lease,
            run_fence=activation.run_fence,
            now=now,
            require_exact_expiry=False,
        )
        outcome_json = canonical_json(
            {
                "status": "retryable",
                "retry_operation_id": retry_operation_id,
                "retry_attempt": retry_attempt,
                "next_attempt_at": next_attempt_at,
            }
        )
        _fault(fault, "workflow:defer_resume_retry:before_workflow_resume_admissions_write")
        changed = tx.connection.execute(
            "UPDATE workflow_resume_admissions SET"
            " phase='retry_wait',version=version+1,retry_attempt=?,next_attempt_at=?,outcome_json=?,claim_owner=NULL,claim_epoch=NULL,claim_expires_at=NULL,updated_at=?"  # noqa: E501
            " WHERE receipt_id=? AND version=? AND phase='claimed'",
            (
                retry_attempt,
                next_attempt_at,
                outcome_json,
                now,
                binding.receipt_id,
                binding.expected_receipt_version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("resume retry CAS failed")
        _fault(fault, "workflow:defer_resume_retry:after_workflow_resume_admissions_write")
        tx.connection.execute(
            "DELETE FROM workflow_leases WHERE run_id=? AND namespace IN (?,?) AND owner_id=? AND"
            " epoch=?",
            (
                activation.execution_lease.run_id,
                RUNTIME_LEASE_NAMESPACE,
                activation.workflow_lease.namespace,
                activation.execution_lease.owner_id,
                activation.execution_lease.epoch,
            ),
        )
        tx.connection.execute(
            "UPDATE run_fences SET state='released',released_at=? WHERE run_id=? AND epoch=? AND"
            " state='active'",
            (now, activation.execution_lease.run_id, activation.run_fence.epoch),
        )
        return self._resume_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_resume_admissions WHERE receipt_id=?",
                (binding.receipt_id,),
            ).fetchone(),
            connection=tx.connection,
        )

    @staticmethod
    def _cancel_outcome(row: sqlite3.Row) -> CancelWorkflowOutcome:
        return CancelWorkflowOutcome(
            str(row["cancel_id"]),
            int(row["generation"]),
            str(row["phase"]),
            tuple(json.loads(str(row["blocker_ids_json"]))),
            None if row["terminal"] is None else bool(row["terminal"]),
        )

    @staticmethod
    def _cancel_resolution_snapshot(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, JsonValue] | None:
        frozen_blockers = json.loads(str(row["blocker_snapshot_json"]))
        if not isinstance(frozen_blockers, dict):
            raise UnitOfWorkConflict("cancel blocker snapshot is corrupt")
        resolved_snapshot: dict[str, JsonValue] = {}
        for blocker_id, frozen_identity in sorted(frozen_blockers.items()):
            if not isinstance(blocker_id, str) or not isinstance(frozen_identity, dict):
                raise UnitOfWorkConflict("cancel blocker snapshot is corrupt")
            blocker = connection.execute(
                "SELECT * FROM run_wait_blockers WHERE blocker_id=? AND run_id=?",
                (blocker_id, str(row["run_id"])),
            ).fetchone()
            if (
                blocker is None
                or str(blocker["kind"]) != frozen_identity.get("kind")
                or str(blocker["ledger_identity"]) != frozen_identity.get("ledger_identity")
                or int(blocker["handoff_attempt"]) != frozen_identity.get("handoff_attempt")
                or int(blocker["version"])
                < int(frozen_identity.get("observed_blocker_version", -1))
            ):
                raise UnitOfWorkConflict("cancel blocker identity changed")
            if blocker["resolution_id"] is None:
                return None
            resolution = connection.execute(
                """
                SELECT resolution_id,kind,ledger_identity,handoff_attempt,outcome,
                       outcome_hash
                FROM reconciliation_resolutions WHERE resolution_id=?
                """,
                (str(blocker["resolution_id"]),),
            ).fetchone()
            if (
                resolution is None
                or str(resolution["kind"]) != str(blocker["kind"])
                or str(resolution["ledger_identity"]) != str(blocker["ledger_identity"])
                or int(resolution["handoff_attempt"]) != int(blocker["handoff_attempt"])
            ):
                raise UnitOfWorkConflict("cancel blocker resolution changed")
            resolved_snapshot[blocker_id] = {
                "blocker_version": int(blocker["version"]),
                "resolution_id": str(resolution["resolution_id"]),
                "outcome": str(resolution["outcome"]),
                "outcome_hash": str(resolution["outcome_hash"]),
            }
        return resolved_snapshot

    def read_cancel_resolution_snapshot(self, cancel_id: str) -> Mapping[str, JsonValue] | None:
        row = self.database.connection.execute(
            "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?", (cancel_id,)
        ).fetchone()
        if row is None:
            raise UnitOfWorkConflict("cancel receipt does not exist")
        return self._cancel_resolution_snapshot(self.database.connection, row)

    def read_cancel_outcome(self, *, run_id: str, generation: int) -> CancelWorkflowOutcome | None:
        row = self.database.connection.execute(
            "SELECT * FROM workflow_cancel_receipts WHERE run_id=? AND generation=?",
            (run_id, generation),
        ).fetchone()
        return None if row is None else self._cancel_outcome(row)

    def read_cancel_request(self, *, run_id: str, generation: int) -> CancelWorkflowRequest | None:
        row = self.database.connection.execute(
            "SELECT cancel_id,run_id,reason,generation "
            "FROM workflow_cancel_receipts WHERE run_id=? AND generation=?",
            (run_id, generation),
        ).fetchone()
        if row is None:
            return None
        return CancelWorkflowRequest(
            str(row["cancel_id"]),
            str(row["run_id"]),
            str(row["reason"]),
            int(row["generation"]),
        )

    def verify_workflow_cancel_terminal(
        self, *, run_id: str, cancel_id: str, generation: int
    ) -> bool:
        row = self.database.connection.execute(
            """
            SELECT receipts.phase, receipts.terminal, receipts.outcome_json,
                   runs.state
            FROM workflow_cancel_receipts AS receipts
            JOIN runs ON runs.run_id = receipts.run_id
            WHERE receipts.cancel_id=? AND receipts.run_id=?
              AND receipts.generation=?
            """,
            (cancel_id, run_id, generation),
        ).fetchone()
        if (
            row is None
            or str(row["phase"]) != "terminal"
            or int(row["terminal"]) != 1
            or str(row["state"]) != RunState.CANCELLED.value
            or row["outcome_json"] is None
        ):
            return False
        outcome = json.loads(str(row["outcome_json"]))
        if not isinstance(outcome, dict):
            return False
        event = outcome.get("terminal_event")
        if not isinstance(event, dict):
            return False
        event_id = event.get("event_id")
        if (
            not isinstance(event_id, str)
            or event.get("cancel_id") != cancel_id
            or event.get("generation") != generation
        ):
            return False
        durable_event = self.database.connection.execute(
            "SELECT kind,payload_json FROM run_events WHERE event_id=? AND run_id=?",
            (event_id, run_id),
        ).fetchone()
        return (
            durable_event is not None
            and str(durable_event["kind"]) == "workflow.cancelled"
            and json.loads(str(durable_event["payload_json"])) == event
        )

    async def request_cancel(
        self,
        transaction: WorkflowTransaction,
        request: CancelWorkflowRequest,
        expected_run_version: int,
        activation: WorkflowActivation | None,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> CancelWorkflowOutcome:
        tx = self._assert_open_workflow_transaction(transaction)
        existing = tx.connection.execute(
            "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?",
            (request.cancel_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["run_id"]) != request.run_id
                or str(existing["reason"]) != request.reason
                or int(existing["generation"]) != request.expected_generation
            ):
                raise UnitOfWorkConflict("cancel identity reused differently")
            if activation is not None and not bool(existing["terminal"]):
                self._require_runtime_and_fence(
                    tx.connection,
                    run_id=request.run_id,
                    execution_lease=activation.execution_lease,
                    run_fence=activation.run_fence,
                    now=now,
                    require_exact_expiry=False,
                )
                projection = tx.connection.execute(
                    "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                    "WHERE run_id=? AND namespace=?",
                    (request.run_id, activation.workflow_lease.namespace),
                ).fetchone()
                if projection is None or tuple(projection) != (
                    activation.workflow_lease.owner_id,
                    activation.workflow_lease.epoch,
                    activation.workflow_lease.expires_at,
                ):
                    raise UnitOfWorkConflict("cancel workflow activation changed")
                self._invalidate_cancel_activation(
                    tx.connection,
                    request.run_id,
                    activation,
                    now=now,
                    fault=fault,
                )
            return self._cancel_outcome(existing)
        run = tx.connection.execute(
            "SELECT state,version FROM runs WHERE run_id=?", (request.run_id,)
        ).fetchone()
        if (
            run is None
            or int(run["version"]) != expected_run_version
            or str(run["state"]) in {"completed", "failed", "cancelled"}
        ):
            raise UnitOfWorkConflict("cancel Run version changed")
        if activation is not None:
            self._require_runtime_and_fence(
                tx.connection,
                run_id=request.run_id,
                execution_lease=activation.execution_lease,
                run_fence=activation.run_fence,
                now=now,
                require_exact_expiry=False,
            )
            projection = tx.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id=? AND namespace=?",
                (request.run_id, activation.workflow_lease.namespace),
            ).fetchone()
            if projection is None or tuple(projection) != (
                activation.workflow_lease.owner_id,
                activation.workflow_lease.epoch,
                activation.workflow_lease.expires_at,
            ):
                raise UnitOfWorkConflict("cancel workflow activation changed")
        current_generation = tx.connection.execute(
            "SELECT MAX(generation) FROM workflow_cancel_receipts WHERE run_id=?",
            (request.run_id,),
        ).fetchone()[0]
        expected_generation = 0 if current_generation is None else int(current_generation) + 1
        if request.expected_generation != expected_generation:
            raise UnitOfWorkConflict("cancel generation changed")
        blocker_rows = tx.connection.execute(
            """
            SELECT blocker_id, kind, ledger_identity, handoff_attempt, version
            FROM run_wait_blockers
            WHERE run_id=?
            ORDER BY blocker_id
            """,
            (request.run_id,),
        ).fetchall()
        blocker_ids = tuple(str(row["blocker_id"]) for row in blocker_rows)
        blocker_snapshot: dict[str, JsonValue] = {
            str(row["blocker_id"]): {
                "kind": str(row["kind"]),
                "ledger_identity": str(row["ledger_identity"]),
                "handoff_attempt": int(row["handoff_attempt"]),
                "observed_blocker_version": int(row["version"]),
            }
            for row in blocker_rows
        }
        phase = "cancelling" if blocker_ids else "requested"
        _fault(fault, "workflow:request_cancel:before_runs_write")
        tx.connection.execute(
            "UPDATE runs SET state='cancel_requested',version=version+1,updated_at=? WHERE run_id=?"
            " AND version=?",
            (now, request.run_id, expected_run_version),
        )
        _fault(fault, "workflow:request_cancel:after_runs_write")
        self._invalidate_cancel_activation(
            tx.connection,
            request.run_id,
            activation,
            now=now,
            fault=fault,
        )
        _fault(fault, "workflow:request_cancel:before_workflow_cancel_receipts_write")
        tx.connection.execute(
            "INSERT INTO"
            " workflow_cancel_receipts(cancel_id,run_id,generation,reason,phase,blocker_ids_json,blocker_snapshot_json,terminal,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,NULL,0,?,?)",
            (
                request.cancel_id,
                request.run_id,
                request.expected_generation,
                request.reason,
                phase,
                canonical_json(list(blocker_ids)),
                canonical_json(blocker_snapshot),
                now,
                now,
            ),
        )
        _fault(fault, "workflow:request_cancel:after_workflow_cancel_receipts_write")
        return self._cancel_outcome(
            tx.connection.execute(
                "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?",
                (request.cancel_id,),
            ).fetchone()
        )

    @staticmethod
    def _invalidate_cancel_activation(
        connection: sqlite3.Connection,
        run_id: str,
        activation: WorkflowActivation | None,
        *,
        now: float,
        fault: FaultHook | None,
    ) -> None:
        _fault(fault, "workflow:request_cancel:before_workflow_leases_write")
        if activation is None:
            connection.execute("DELETE FROM workflow_leases WHERE run_id=?", (run_id,))
        else:
            changed = connection.execute(
                "DELETE FROM workflow_leases WHERE run_id=? AND owner_id=? AND epoch=?",
                (
                    run_id,
                    activation.execution_lease.owner_id,
                    activation.execution_lease.epoch,
                ),
            ).rowcount
            if changed < 2:
                raise UnitOfWorkConflict("cancel activation projection changed")
        _fault(fault, "workflow:request_cancel:after_workflow_leases_write")
        _fault(fault, "workflow:request_cancel:before_run_fences_write")
        if activation is None:
            changed = connection.execute(
                "UPDATE run_fences SET epoch=epoch+1,state='cancelled',released_at=? "
                "WHERE run_id=? AND state='active'",
                (now, run_id),
            ).rowcount
        else:
            changed = connection.execute(
                "UPDATE run_fences SET epoch=epoch+1,state='cancelled',released_at=? "
                "WHERE run_id=? AND owner_id=? AND epoch=? "
                "AND runtime_lease_epoch=? AND state='active'",
                (
                    now,
                    run_id,
                    activation.run_fence.owner_id,
                    activation.run_fence.epoch,
                    activation.execution_lease.epoch,
                ),
            ).rowcount
        if activation is not None and changed != 1:
            raise UnitOfWorkConflict("cancel activation fence changed")
        _fault(fault, "workflow:request_cancel:after_run_fences_write")

    async def claim_cancel_convergence(
        self,
        transaction: WorkflowTransaction,
        cancel_id: str,
        expected_generation: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> CancelConvergenceLease:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?", (cancel_id,)
        ).fetchone()
        if (
            row is None
            or int(row["generation"]) != expected_generation
            or str(row["phase"]) == "terminal"
        ):
            raise UnitOfWorkConflict("cancel convergence is not claimable")
        if (
            row["convergence_expires_at"] is not None
            and float(row["convergence_expires_at"]) > now
            and str(row["convergence_owner"]) != owner_id
        ):
            raise UnitOfWorkConflict("cancel convergence has an active owner")
        epoch = int(row["convergence_epoch"]) + 1
        expires_at = now + ttl_seconds
        _fault(
            fault,
            "workflow:claim_cancel_convergence:before_workflow_cancel_receipts_write",
        )
        changed = tx.connection.execute(
            "UPDATE workflow_cancel_receipts SET"
            " phase='cancelling',convergence_owner=?,convergence_epoch=?,convergence_expires_at=?,version=version+1,updated_at=?"  # noqa: E501
            " WHERE cancel_id=? AND generation=? AND version=?",
            (
                owner_id,
                epoch,
                expires_at,
                now,
                cancel_id,
                expected_generation,
                int(row["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("cancel convergence CAS failed")
        _fault(
            fault,
            "workflow:claim_cancel_convergence:after_workflow_cancel_receipts_write",
        )
        return CancelConvergenceLease(
            str(row["run_id"]), expected_generation, owner_id, epoch, expires_at
        )

    async def settle_cancel_convergence(
        self,
        transaction: WorkflowTransaction,
        cancel_lease: CancelConvergenceLease,
        resolution_snapshot: Mapping[str, JsonValue],
        terminal_checkpoint: Mapping[str, JsonValue],
        terminal_event: Mapping[str, JsonValue],
        deliveries: Sequence[Mapping[str, JsonValue]],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> CancelWorkflowOutcome:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_cancel_receipts WHERE run_id=? AND generation=?",
            (cancel_lease.run_id, cancel_lease.generation),
        ).fetchone()
        if (
            row is None
            or (
                row["convergence_owner"],
                int(row["convergence_epoch"]),
                float(row["convergence_expires_at"]),
            )
            != (cancel_lease.owner_id, cancel_lease.epoch, cancel_lease.expires_at)
            or cancel_lease.expires_at <= now
        ):
            raise UnitOfWorkConflict("cancel convergence lease is stale")
        resolved_snapshot = self._cancel_resolution_snapshot(tx.connection, row)
        if resolved_snapshot is None:
            raise UnitOfWorkConflict("cancel blockers remain unresolved")
        if canonical_json(dict(resolution_snapshot)) != canonical_json(resolved_snapshot):
            raise UnitOfWorkConflict("cancel resolution snapshot changed")
        checkpoint_id = _required(terminal_checkpoint.get("checkpoint_id"), "checkpoint_id")
        namespace = _required(terminal_checkpoint.get("namespace", "native"), "namespace")
        checkpoint_payload = cast(dict[str, JsonValue], dict(terminal_checkpoint))
        checkpoint_json = canonical_json(checkpoint_payload)
        outcome = {
            "resolution_snapshot": dict(resolution_snapshot),
            "terminal_checkpoint": dict(terminal_checkpoint),
            "terminal_event": dict(terminal_event),
            "deliveries": [dict(item) for item in deliveries],
        }
        _fault(fault, "workflow:settle_cancel_convergence:before_runs_write")
        tx.connection.execute(
            "UPDATE runs SET state='cancelled',version=version+1,updated_at=? WHERE run_id=? AND"
            " state='cancel_requested'",
            (now, cancel_lease.run_id),
        )
        _fault(fault, "workflow:settle_cancel_convergence:after_runs_write")
        _fault(
            fault,
            "workflow:settle_cancel_convergence:before_workflow_checkpoints_write",
        )
        next_version = int(
            tx.connection.execute(
                "SELECT COALESCE(MAX(version),-1)+1 FROM workflow_checkpoints WHERE run_id=? AND"
                " namespace=?",
                (cancel_lease.run_id, namespace),
            ).fetchone()[0]
        )
        tx.connection.execute(
            "INSERT INTO"
            " workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                checkpoint_id,
                cancel_lease.run_id,
                namespace,
                checkpoint_json,
                hashlib.sha256(checkpoint_json.encode()).hexdigest(),
                cancel_lease.epoch,
                next_version,
                now,
            ),
        )
        _fault(
            fault,
            "workflow:settle_cancel_convergence:after_workflow_checkpoints_write",
        )
        event_id = _required(terminal_event.get("event_id"), "event_id")
        _fault(fault, "workflow:settle_cancel_convergence:before_run_events_write")
        self._insert_event(
            tx.connection,
            event_id=event_id,
            run_id=cancel_lease.run_id,
            kind="workflow.cancelled",
            payload=cast(dict[str, JsonValue], dict(terminal_event)),
            now=now,
        )
        _fault(fault, "workflow:settle_cancel_convergence:after_run_events_write")
        _fault(
            fault,
            "workflow:settle_cancel_convergence:before_delivery_outbox_write",
        )
        for item in deliveries:
            delivery_id = _required(item.get("delivery_id"), "delivery_id")
            sink_kind = _required(item.get("sink_kind"), "sink_kind")
            idempotency_key = _required(item.get("idempotency_key"), "idempotency_key")
            payload = item.get("payload")
            if not isinstance(payload, Mapping):
                raise UnitOfWorkConflict("cancel delivery payload must be an object")
            tx.connection.execute(
                "INSERT INTO"
                " delivery_outbox(delivery_id,run_id,sink_kind,idempotency_key,payload_json,state,version,created_at)"  # noqa: E501
                " VALUES(?,?,?,?,?,'pending',0,?)",
                (
                    delivery_id,
                    cancel_lease.run_id,
                    sink_kind,
                    idempotency_key,
                    canonical_json(dict(payload)),
                    now,
                ),
            )
        _fault(
            fault,
            "workflow:settle_cancel_convergence:after_delivery_outbox_write",
        )
        self._materialize_cancelled_attached_child_terminal(
            tx.connection,
            run_id=cancel_lease.run_id,
            cancel_id=str(row["cancel_id"]),
            owner_id=cancel_lease.owner_id,
            now=now,
            fault=fault,
        )
        _fault(
            fault,
            "workflow:settle_cancel_convergence:before_workflow_cancel_receipts_write",
        )
        tx.connection.execute(
            "UPDATE workflow_cancel_receipts SET"
            " phase='terminal',terminal=1,outcome_json=?,version=version+1,updated_at=? WHERE"
            " cancel_id=? AND convergence_owner=? AND convergence_epoch=?",
            (
                canonical_json(cast(JsonValue, outcome)),
                now,
                str(row["cancel_id"]),
                cancel_lease.owner_id,
                cancel_lease.epoch,
            ),
        )
        _fault(
            fault,
            "workflow:settle_cancel_convergence:after_workflow_cancel_receipts_write",
        )
        return self._cancel_outcome(
            tx.connection.execute(
                "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?",
                (str(row["cancel_id"]),),
            ).fetchone()
        )

    def _materialize_cancelled_attached_child_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        cancel_id: str,
        owner_id: str,
        now: float,
        fault: FaultHook | None,
    ) -> None:
        command = connection.execute(
            "SELECT commands.* FROM child_commands AS commands "
            "JOIN run_links AS links ON links.child_run_id=commands.child_run_id "
            "WHERE commands.child_run_id=? AND links.attachment_policy='attached'",
            (run_id,),
        ).fetchone()
        if command is None:
            return
        existing = connection.execute(
            "SELECT * FROM child_terminal_receipts WHERE child_run_id=?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["command_id"]) == str(command["command_id"])
                and str(existing["terminal_state"]) == RunState.CANCELLED.value
            ):
                return
            raise UnitOfWorkConflict("attached child terminal receipt differs")
        fence = connection.execute(
            "SELECT * FROM run_fences WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if fence is None or str(fence["state"]) != "cancelled":
            raise UnitOfWorkConflict("cancelled attached child lacks its cancelled Run fence")
        signal_id = f"{cancel_id}:child-signal"
        event_id = f"{cancel_id}:child-terminal-event"
        receipt_id = f"{cancel_id}:child-terminal-receipt"
        public_terminal: dict[str, JsonValue] = {
            "status": RunState.CANCELLED.value,
            "result": {
                "kind": "workflow_terminal",
                "status": RunState.CANCELLED.value,
                "error": None,
                "recovery_action": None,
                "card": None,
            },
        }
        public_json = canonical_json(public_terminal)
        _fault(fault, "workflow:settle_cancel_child:before_child_command_write")
        changed = connection.execute(
            "UPDATE child_commands SET state='acked',updated_at=? "
            "WHERE command_id=? AND state IN ('pending','scheduled')",
            (now, str(command["command_id"])),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("cancelled attached child command CAS failed")
        _fault(fault, "workflow:settle_cancel_child:after_child_command_write")
        _fault(fault, "workflow:settle_cancel_child:before_child_signal_write")
        connection.execute(
            "INSERT INTO child_signals("
            "signal_id,parent_run_id,child_run_id,payload_json,state,version,"
            "claim_epoch,created_at,updated_at) "
            "VALUES(?,?,?,?,'pending',0,0,?,?)",
            (
                signal_id,
                str(command["parent_run_id"]),
                run_id,
                public_json,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:settle_cancel_child:after_child_signal_write")
        _fault(fault, "workflow:settle_cancel_child:before_child_event_write")
        self._insert_event(
            connection,
            event_id=event_id,
            run_id=run_id,
            kind="child.terminal.cancelled",
            payload=public_terminal,
            now=now,
        )
        _fault(fault, "workflow:settle_cancel_child:after_child_event_write")
        _fault(fault, "workflow:settle_cancel_child:before_child_receipt_write")
        connection.execute(
            "INSERT INTO child_terminal_receipts("
            "receipt_id,command_id,child_run_id,terminal_state,outcome_hash,"
            "signal_id,event_id,owner_id,runtime_lease_epoch,fence_epoch,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                str(command["command_id"]),
                run_id,
                RunState.CANCELLED.value,
                hashlib.sha256(public_json.encode()).hexdigest(),
                signal_id,
                event_id,
                owner_id,
                int(fence["runtime_lease_epoch"]),
                int(fence["epoch"]),
                now,
            ),
        )
        _fault(fault, "workflow:settle_cancel_child:after_child_receipt_write")

    def list_candidates(
        self, snapshot_cursor: str | None
    ) -> tuple[tuple[RecoveryCandidate, ...], str | None]:
        rows = self._recovery_candidate_rows(
            self.database.connection, snapshot_cursor=snapshot_cursor, limit=101
        )
        candidates = [self._recovery_candidate(row) for row in rows[:100]]
        return tuple(candidates), (None if len(rows) <= 100 else str(rows[99]["run_id"]))

    @staticmethod
    def _recovery_candidate_rows(
        connection: sqlite3.Connection,
        *,
        snapshot_cursor: str | None,
        run_id: str | None = None,
        limit: int | None = None,
        include_terminal: bool = False,
    ) -> list[sqlite3.Row]:
        limit_clause = "" if limit is None else " LIMIT ?"
        parameters: list[object] = [
            1 if include_terminal else 0,
            snapshot_cursor,
            snapshot_cursor,
            run_id,
            run_id,
            RUNTIME_LEASE_NAMESPACE,
        ]
        if limit is not None:
            parameters.append(limit)
        return connection.execute(
            """
            WITH candidate_runs AS (
                SELECT runs.run_id, runs.state, runs.version,
                       json_extract(admissions.request_json, '$.checkpoint_namespace')
                           AS workflow_namespace
                FROM runs
                LEFT JOIN workflow_start_admissions AS admissions
                  ON admissions.run_id = runs.run_id
                WHERE runs.driver_kind = 'workflow'
                  AND (? = 1 OR runs.state NOT IN
                      ('reserved_fork','completed','failed','cancelled'))
                  AND (? IS NULL OR runs.run_id > ?)
                  AND (? IS NULL OR runs.run_id = ?)
            )
            SELECT candidate_runs.run_id,
                   candidate_runs.state,
                   candidate_runs.version,
                   candidate_runs.workflow_namespace,
                   runtime.owner_id AS runtime_owner,
                   runtime.epoch AS runtime_epoch,
                   runtime.expires_at AS runtime_expires_at,
                   workflow.owner_id AS workflow_owner,
                   workflow.epoch AS workflow_epoch,
                   workflow.expires_at AS workflow_expires_at,
                   fence.owner_id AS fence_owner,
                   fence.runtime_lease_epoch AS fence_runtime_epoch,
                   fence.epoch AS fence_epoch,
                   fence.state AS fence_state,
                   head.checkpoint_id AS checkpoint_head
            FROM candidate_runs
            LEFT JOIN workflow_leases AS runtime
              ON runtime.run_id = candidate_runs.run_id
             AND runtime.namespace = ?
            LEFT JOIN workflow_leases AS workflow
              ON workflow.run_id = candidate_runs.run_id
             AND workflow.namespace = candidate_runs.workflow_namespace
            LEFT JOIN run_fences AS fence
              ON fence.run_id = candidate_runs.run_id
            LEFT JOIN workflow_checkpoints AS head
              ON head.run_id = candidate_runs.run_id
             AND head.namespace = candidate_runs.workflow_namespace
             AND head.version = (
                 SELECT MAX(candidate_head.version)
                 FROM workflow_checkpoints AS candidate_head
                 WHERE candidate_head.run_id = candidate_runs.run_id
                   AND candidate_head.namespace = candidate_runs.workflow_namespace
             )
            ORDER BY candidate_runs.run_id
            """
            + limit_clause,
            parameters,
        ).fetchall()

    @staticmethod
    def _recovery_candidate(row: sqlite3.Row) -> RecoveryCandidate:
        def text(name: str) -> str | None:
            return None if row[name] is None else str(row[name])

        def integer(name: str) -> int | None:
            return None if row[name] is None else int(row[name])

        def number(name: str) -> float | None:
            return None if row[name] is None else float(row[name])

        return RecoveryCandidate(
            run_id=str(row["run_id"]),
            run_version=int(row["version"]),
            status=str(row["state"]),
            runtime_lease_owner=text("runtime_owner"),
            runtime_lease_epoch=integer("runtime_epoch"),
            runtime_lease_expires_at=number("runtime_expires_at"),
            workflow_lease_namespace=text("workflow_namespace"),
            workflow_lease_owner=text("workflow_owner"),
            workflow_lease_epoch=integer("workflow_epoch"),
            workflow_lease_expires_at=number("workflow_expires_at"),
            run_fence_owner=text("fence_owner"),
            run_fence_runtime_lease_epoch=integer("fence_runtime_epoch"),
            run_fence_epoch=integer("fence_epoch"),
            run_fence_state=text("fence_state"),
            checkpoint_head=text("checkpoint_head"),
        )

    def _recovery_snapshot_from_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> RecoverySnapshot:
        rows = self._recovery_candidate_rows(
            connection,
            snapshot_cursor=None,
            run_id=run_id,
            include_terminal=True,
        )
        if not rows:
            raise UnitOfWorkNotFound(run_id)
        candidate = self._recovery_candidate(rows[0])
        head = connection.execute(
            "SELECT checkpoint_hash FROM workflow_checkpoints "
            "WHERE run_id=? AND namespace=? AND checkpoint_id=?",
            (run_id, candidate.workflow_lease_namespace, candidate.checkpoint_head),
        ).fetchone()
        start_row = connection.execute(
            "SELECT snapshot_json FROM run_start_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        start = {} if start_row is None else json.loads(str(start_row["snapshot_json"]))
        manifest = start.get("manifest_hash")
        implementation = start.get("implementation_hash")
        blockers = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT blocker_id FROM run_wait_blockers WHERE run_id=? AND wake_consumed=0 ORDER"
                " BY blocker_id",
                (run_id,),
            ).fetchall()
        )
        return RecoverySnapshot(
            candidate,
            None if not isinstance(manifest, str) else manifest,
            None if not isinstance(implementation, str) else implementation,
            None if head is None else str(head["checkpoint_hash"]),
            blockers,
        )

    def read_recovery_snapshot(self, run_id: str) -> RecoverySnapshot:
        return self._recovery_snapshot_from_connection(self.database.connection, run_id)

    async def commit_recovery_outcome(
        self,
        transaction: WorkflowTransaction,
        candidate: RecoveryCandidate,
        expected_snapshot: RecoverySnapshot,
        outcome: RecoveryOutcome,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RecoveryOutcome:
        tx = self._assert_open_workflow_transaction(transaction)
        candidate_hash = hashlib.sha256(
            canonical_json(_recovery_candidate_json(candidate)).encode()
        ).hexdigest()
        snapshot_hash = hashlib.sha256(
            canonical_json(_recovery_snapshot_json(expected_snapshot)).encode()
        ).hexdigest()
        existing = tx.connection.execute(
            "SELECT * FROM workflow_recovery_receipts WHERE receipt_id=?",
            (outcome.receipt_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["candidate_hash"]) != candidate_hash
                or str(existing["snapshot_hash"]) != snapshot_hash
                or str(existing["previous_status"]) != outcome.previous_status
                or str(existing["status"]) != outcome.status
                or str(existing["action"]) != outcome.action
                or str(existing["reason"]) != outcome.reason
            ):
                raise UnitOfWorkConflict("recovery receipt changed")
            return RecoveryOutcome(
                str(existing["previous_status"]),
                str(existing["status"]),
                str(existing["action"]),
                str(existing["reason"]),
                str(existing["receipt_id"]),
            )
        current = self._recovery_snapshot_from_connection(tx.connection, candidate.run_id)
        if current != expected_snapshot or current.candidate != candidate:
            raise UnitOfWorkConflict("recovery snapshot changed")
        _fault(fault, "workflow:commit_recovery_outcome:before_runs_write")
        changed = tx.connection.execute(
            "UPDATE runs SET state=?,version=version+1,updated_at=? WHERE run_id=? AND state=? AND"
            " version=?",
            (
                outcome.status,
                now,
                candidate.run_id,
                candidate.status,
                candidate.run_version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("recovery Run CAS failed")
        _fault(fault, "workflow:commit_recovery_outcome:after_runs_write")
        _fault(
            fault,
            "workflow:commit_recovery_outcome:before_workflow_recovery_receipts_write",
        )
        tx.connection.execute(
            "INSERT INTO"
            " workflow_recovery_receipts(receipt_id,run_id,candidate_hash,snapshot_hash,previous_status,status,action,reason,created_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                outcome.receipt_id,
                candidate.run_id,
                candidate_hash,
                snapshot_hash,
                outcome.previous_status,
                outcome.status,
                outcome.action,
                outcome.reason,
                now,
            ),
        )
        _fault(
            fault,
            "workflow:commit_recovery_outcome:after_workflow_recovery_receipts_write",
        )
        return outcome

    async def claim_resolved_recovery(
        self,
        transaction: WorkflowTransaction,
        blocker_id: str,
        expected_resolution_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> RecoveryClaim:
        tx = self._assert_open_workflow_transaction(transaction)
        blocker = tx.connection.execute(
            "SELECT version,resolution_id FROM run_wait_blockers WHERE blocker_id=?",
            (blocker_id,),
        ).fetchone()
        if (
            blocker is None
            or blocker["resolution_id"] is None
            or int(blocker["version"]) != expected_resolution_version
        ):
            raise UnitOfWorkConflict("resolved recovery blocker changed")
        claim = tx.connection.execute(
            "SELECT * FROM workflow_recovery_claims WHERE blocker_id=?",
            (blocker_id,),
        ).fetchone()
        if claim is not None and float(claim["expires_at"]) > now:
            raise UnitOfWorkConflict("resolved recovery blocker has an active claimant")
        epoch = 1 if claim is None else int(claim["epoch"]) + 1
        expires = now + ttl_seconds
        run_id = str(
            tx.connection.execute(
                "SELECT run_id FROM run_wait_blockers WHERE blocker_id=?",
                (blocker_id,),
            ).fetchone()[0]
        )
        _fault(
            fault,
            "workflow:claim_resolved_recovery:before_workflow_recovery_claims_write",
        )
        if claim is None:
            tx.connection.execute(
                "INSERT INTO"
                " workflow_recovery_claims(blocker_id,run_id,resolution_version,owner_id,epoch,expires_at,created_at,updated_at)"  # noqa: E501
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    blocker_id,
                    run_id,
                    expected_resolution_version,
                    owner_id,
                    epoch,
                    expires,
                    now,
                    now,
                ),
            )
        else:
            tx.connection.execute(
                "UPDATE workflow_recovery_claims SET"
                " resolution_version=?,owner_id=?,epoch=?,expires_at=?,updated_at=? WHERE"
                " blocker_id=? AND epoch=?",
                (
                    expected_resolution_version,
                    owner_id,
                    epoch,
                    expires,
                    now,
                    blocker_id,
                    int(claim["epoch"]),
                ),
            )
        _fault(
            fault,
            "workflow:claim_resolved_recovery:after_workflow_recovery_claims_write",
        )
        return RecoveryClaim(blocker_id, owner_id, epoch, expires)

    def _fork_receipt(self, row: sqlite3.Row) -> ForkReceipt:
        raw = json.loads(str(row["request_json"]))
        confirmation_raw = raw.get("dangerous_confirmation")
        confirmation: DangerousEffectConfirmation | None = None
        if isinstance(confirmation_raw, dict):
            observations = tuple(
                DangerousEffectObservation(**item) for item in confirmation_raw["observations"]
            )
            confirmation = DangerousEffectConfirmation(
                str(confirmation_raw["scope"]),
                observations,
                str(confirmation_raw["digest"]),
            )
        request = ForkRequest(
            fork_id=str(raw["fork_id"]),
            fingerprint=str(raw["fingerprint"]),
            source_run_id=str(raw["source_run_id"]),
            source_namespace=str(raw["source_namespace"]),
            source_checkpoint_id=str(raw["source_checkpoint_id"]),
            source_run_version=int(raw["source_run_version"]),
            source_head=str(raw["source_head"]),
            engine_hash=str(raw["engine_hash"]),
            manifest_hash=str(raw["manifest_hash"]),
            implementation_hash=str(raw["implementation_hash"]),
            schema_hash=str(raw["schema_hash"]),
            patch=raw["patch"],
            dangerous_confirmation=confirmation,
        )
        outcome = (
            None
            if row["outcome_json"] is None
            else freeze_json(json.loads(str(row["outcome_json"])))
        )
        return ForkReceipt(
            request,
            str(row["target_run_id"]),
            str(row["target_trace_id"]),
            str(row["target_thread_id"]),
            str(row["target_checkpoint_id"]),
            ForkPhase(str(row["phase"])),
            int(row["version"]),
            None if row["claim_owner"] is None else str(row["claim_owner"]),
            None if int(row["claim_epoch"]) == 0 else int(row["claim_epoch"]),
            None if row["claim_expires_at"] is None else float(row["claim_expires_at"]),
            outcome,
        )

    def read_fork(self, fork_id: str) -> ForkReceipt | None:
        row = self.database.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?", (fork_id,)
        ).fetchone()
        return None if row is None else self._fork_receipt(row)

    @staticmethod
    def _fork_payload(request: ForkRequest) -> dict[str, JsonValue]:
        confirmation: JsonValue = None
        if request.dangerous_confirmation is not None:
            confirmation = {
                "scope": request.dangerous_confirmation.scope,
                "digest": request.dangerous_confirmation.digest,
                "observations": [
                    {
                        "effect_id": item.effect_id,
                        "kind": item.kind,
                        "state": item.state,
                        "ledger_version": item.ledger_version,
                        "request_hash": item.request_hash,
                        "handoff_attempt": item.handoff_attempt,
                    }
                    for item in request.dangerous_confirmation.observations
                ],
            }
        return {
            "fork_id": request.fork_id,
            "fingerprint": request.fingerprint,
            "source_run_id": request.source_run_id,
            "source_namespace": request.source_namespace,
            "source_checkpoint_id": request.source_checkpoint_id,
            "source_run_version": request.source_run_version,
            "source_head": request.source_head,
            "engine_hash": request.engine_hash,
            "manifest_hash": request.manifest_hash,
            "implementation_hash": request.implementation_hash,
            "schema_hash": request.schema_hash,
            "patch": _thaw(request.patch),
            "dangerous_confirmation": confirmation,
        }

    @staticmethod
    def _dangerous_confirmation_is_current(
        connection: sqlite3.Connection, request: ForkRequest
    ) -> bool:
        rows = connection.execute(
            "SELECT effect_id,'tool' AS kind,state,version,request_hash,handoff_attempt FROM"
            " execution_effects WHERE run_id=? AND state IN ('handed_off','unknown','succeeded')"
            " ORDER BY effect_id",
            (request.source_run_id,),
        ).fetchall()
        actual = tuple(
            DangerousEffectObservation(
                str(row["effect_id"]),
                str(row["kind"]),
                str(row["state"]),
                int(row["version"]),
                str(row["request_hash"]),
                int(row["handoff_attempt"]),
            )
            for row in rows
        )
        if not actual:
            return request.dangerous_confirmation is None
        confirmation = request.dangerous_confirmation
        if confirmation is None or confirmation.observations != actual:
            return False
        payload = [
            {
                "effect_id": item.effect_id,
                "kind": item.kind,
                "state": item.state,
                "ledger_version": item.ledger_version,
                "request_hash": item.request_hash,
                "handoff_attempt": item.handoff_attempt,
            }
            for item in actual
        ]
        expected = hashlib.sha256(
            canonical_json(
                cast(JsonValue, {"scope": confirmation.scope, "observations": payload})
            ).encode()
        ).hexdigest()
        return confirmation.digest == expected

    @classmethod
    def _validate_dangerous_confirmation(
        cls, connection: sqlite3.Connection, request: ForkRequest
    ) -> None:
        if not cls._dangerous_confirmation_is_current(connection, request):
            raise UnitOfWorkConflict("dangerous effect confirmation digest changed")

    @staticmethod
    def _fork_source_head_is_current(connection: sqlite3.Connection, request: ForkRequest) -> bool:
        run = connection.execute(
            "SELECT version FROM runs WHERE run_id=?", (request.source_run_id,)
        ).fetchone()
        head = connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints "
            "WHERE run_id=? AND namespace=? ORDER BY version DESC LIMIT 1",
            (request.source_run_id, request.source_namespace),
        ).fetchone()
        source_checkpoint = connection.execute(
            "SELECT 1 FROM workflow_checkpoints WHERE checkpoint_id=? AND run_id=? AND namespace=?",
            (
                request.source_checkpoint_id,
                request.source_run_id,
                request.source_namespace,
            ),
        ).fetchone()
        return (
            run is not None
            and int(run["version"]) == request.source_run_version
            and head is not None
            and str(head["checkpoint_id"]) == request.source_head
            and source_checkpoint is not None
        )

    def _rollback_fork_for_changed_source(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: float,
        fault: FaultHook | None,
    ) -> ForkReceipt:
        _fault(fault, "workflow:fork_source_changed:before_workflow_fork_receipts_write")
        changed = connection.execute(
            "UPDATE workflow_fork_receipts SET phase='rolled_back',version=version+1,"
            "outcome_json=?,claim_expires_at=NULL,updated_at=? "
            "WHERE fork_id=? AND version=? AND phase IN ('prepared','claimed','checkpointed')",
            (
                canonical_json({"reason": "source_or_effect_snapshot_changed"}),
                now,
                str(row["fork_id"]),
                int(row["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("fork source-change rollback CAS failed")
        _fault(fault, "workflow:fork_source_changed:after_workflow_fork_receipts_write")
        updated = connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
            (str(row["fork_id"]),),
        ).fetchone()
        assert updated is not None
        return self._fork_receipt(updated)

    async def prepare_fork(
        self,
        transaction: WorkflowTransaction,
        request: ForkRequest,
        expected_source_snapshot: RecoverySnapshot,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        payload = self._fork_payload(request)
        payload_json = canonical_json(payload)
        actual_fingerprint = hashlib.sha256(
            canonical_json(
                {key: value for key, value in payload.items() if key != "fingerprint"}
            ).encode()
        ).hexdigest()
        if request.fingerprint != actual_fingerprint:
            raise UnitOfWorkConflict("fork fingerprint changed")
        existing = tx.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?", (request.fork_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["fingerprint"]) != request.fingerprint:
                raise UnitOfWorkConflict("fork id reused with different request")
            return self._fork_receipt(existing)
        if (
            self._recovery_snapshot_from_connection(tx.connection, request.source_run_id)
            != expected_source_snapshot
        ):
            raise UnitOfWorkConflict("fork source snapshot changed")
        source = tx.connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (request.source_run_id,)
        ).fetchone()
        if source is None or not self._fork_source_head_is_current(tx.connection, request):
            raise UnitOfWorkConflict("fork source head changed")
        self._validate_dangerous_confirmation(tx.connection, request)
        target_run_id = self._derived_id("fork-run", request.fingerprint)
        target_trace_id = self._derived_id("fork-trace", request.fingerprint)
        target_thread_id = self._derived_id("fork-thread", request.fingerprint)
        target_checkpoint_id = self._derived_id("fork-checkpoint", request.fingerprint)
        target_request_id = self._derived_id("fork-request", request.fingerprint)
        source_admission = tx.connection.execute(
            "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
            (request.source_run_id,),
        ).fetchone()
        source_checkpoint = tx.connection.execute(
            "SELECT checkpoint_json FROM workflow_checkpoints "
            "WHERE checkpoint_id=? AND run_id=? AND namespace=?",
            (
                request.source_checkpoint_id,
                request.source_run_id,
                request.source_namespace,
            ),
        ).fetchone()
        if source_admission is None or source_checkpoint is None:
            raise UnitOfWorkConflict("fork source durable identity is incomplete")
        target_request = json.loads(str(source_admission["request_json"]))
        checkpoint_payload = json.loads(str(source_checkpoint["checkpoint_json"]))
        state = checkpoint_payload.get("state")
        if not isinstance(state, dict):
            raise UnitOfWorkConflict("fork source checkpoint state is invalid")
        target_state = {**state, **_thaw(request.patch)}
        target_request.update(
            {
                "request_key": f"workflow-fork:{request.fork_id}",
                "mode": StartMode.STANDALONE.value,
                "request_id": target_request_id,
                "requested_run_id": target_run_id,
                "requested_trace_id": target_trace_id,
                "requested_thread_id": target_thread_id,
                "start_input": target_state,
            }
        )
        target_request_json = canonical_json(target_request)
        target_request_fingerprint = hashlib.sha256(target_request_json.encode()).hexdigest()
        target_snapshot = {
            **target_request,
            "resolved_run_id": target_run_id,
            "trace_id": target_trace_id,
            "thread_id": target_thread_id,
            "fork_id": request.fork_id,
            "fork_source_checkpoint_id": request.source_checkpoint_id,
        }
        target_snapshot_json = canonical_json(target_snapshot)
        _fault(fault, "workflow:prepare_fork:before_runs_write")
        tx.connection.execute(
            "INSERT INTO"
            " runs(run_id,execution_session_id,request_id,root_run_id,parent_run_id,profile_key,driver_kind,state,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,'reserved_fork',0,?,?)",
            (
                target_run_id,
                str(source["execution_session_id"]),
                target_request_id,
                target_run_id,
                request.source_run_id,
                str(source["profile_key"]),
                "workflow",
                now,
                now,
            ),
        )
        _fault(fault, "workflow:prepare_fork:after_runs_write")
        _fault(fault, "workflow:prepare_fork:before_run_start_snapshots_write")
        tx.connection.execute(
            "INSERT INTO run_start_snapshots(run_id,snapshot_json,snapshot_hash,created_at) "
            "VALUES(?,?,?,?)",
            (
                target_run_id,
                target_snapshot_json,
                hashlib.sha256(target_snapshot_json.encode()).hexdigest(),
                now,
            ),
        )
        _fault(fault, "workflow:prepare_fork:after_run_start_snapshots_write")
        _fault(fault, "workflow:prepare_fork:before_workflow_start_admissions_write")
        tx.connection.execute(
            "INSERT INTO workflow_start_admissions(request_key,request_id,"
            "request_fingerprint,request_json,mode,run_id,trace_id,thread_id,phase,"
            "version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'admitted',0,?,?)",
            (
                str(target_request["request_key"]),
                target_request_id,
                target_request_fingerprint,
                target_request_json,
                StartMode.STANDALONE.value,
                target_run_id,
                target_trace_id,
                target_thread_id,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:prepare_fork:after_workflow_start_admissions_write")
        _fault(fault, "workflow:prepare_fork:before_workflow_fork_receipts_write")
        tx.connection.execute(
            "INSERT INTO"
            " workflow_fork_receipts(fork_id,fingerprint,request_json,source_run_id,source_namespace,source_checkpoint_id,source_run_version,source_head,target_run_id,target_trace_id,target_thread_id,target_checkpoint_id,phase,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'prepared',0,?,?)",
            (
                request.fork_id,
                request.fingerprint,
                payload_json,
                request.source_run_id,
                request.source_namespace,
                request.source_checkpoint_id,
                request.source_run_version,
                request.source_head,
                target_run_id,
                target_trace_id,
                target_thread_id,
                target_checkpoint_id,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:prepare_fork:after_workflow_fork_receipts_write")
        return self._fork_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
                (request.fork_id,),
            ).fetchone()
        )

    async def claim_fork(
        self,
        transaction: WorkflowTransaction,
        fork_id: str,
        expected_receipt_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ForkWriteLease:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?", (fork_id,)
        ).fetchone()
        if (
            row is not None
            and str(row["phase"]) == "claimed"
            and str(row["claim_owner"]) == owner_id
            and int(row["version"]) == expected_receipt_version + 1
            and float(row["claim_expires_at"]) > now
        ):
            return ForkWriteLease(
                fork_id,
                str(row["target_run_id"]),
                owner_id,
                int(row["claim_epoch"]),
                float(row["claim_expires_at"]),
                int(row["version"]),
                "write",
            )
        if (
            row is not None
            and row["claim_expires_at"] is not None
            and float(row["claim_expires_at"]) > now
        ):
            raise UnitOfWorkConflict("fork receipt has an active claimant")
        if (
            row is None
            or int(row["version"]) != expected_receipt_version
            or str(row["phase"]) not in {"prepared", "claimed", "checkpointed"}
        ):
            raise UnitOfWorkConflict("fork receipt is not claimable")
        epoch = int(row["claim_epoch"]) + 1
        mode = "commit_only" if str(row["phase"]) == "checkpointed" else "write"
        expires = now + ttl_seconds
        _fault(fault, "workflow:claim_fork:before_workflow_fork_receipts_write")
        tx.connection.execute(
            "UPDATE workflow_fork_receipts SET"
            " phase=?,version=version+1,claim_owner=?,claim_epoch=?,claim_expires_at=?,updated_at=?"
            " WHERE fork_id=? AND version=?",
            (
                str(row["phase"]) if mode == "commit_only" else "claimed",
                owner_id,
                epoch,
                expires,
                now,
                fork_id,
                expected_receipt_version,
            ),
        )
        _fault(fault, "workflow:claim_fork:after_workflow_fork_receipts_write")
        return ForkWriteLease(
            fork_id,
            str(row["target_run_id"]),
            owner_id,
            epoch,
            expires,
            expected_receipt_version + 1,
            mode,
        )

    async def checkpoint_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_target_head: str | None,
        checkpoint_operation_id: str,
        checkpoint: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
            (fork_lease.fork_id,),
        ).fetchone()
        raw = dict(checkpoint)
        raw["checkpoint_operation_id"] = checkpoint_operation_id
        checkpoint_json = canonical_json(raw)
        checkpoint_hash = hashlib.sha256(checkpoint_json.encode()).hexdigest()
        if fork_lease.mode == "commit_only":
            raise UnitOfWorkConflict("commit-only fork lease cannot rewrite checkpoint")
        if row is not None and str(row["phase"]) in {"checkpointed", "committed"}:
            target = tx.connection.execute(
                "SELECT checkpoint_hash,checkpoint_json FROM workflow_checkpoints "
                "WHERE checkpoint_id=? AND run_id=?",
                (str(row["target_checkpoint_id"]), fork_lease.target_run_id),
            ).fetchone()
            if (
                str(row["claim_owner"]) != fork_lease.owner_id
                or int(row["claim_epoch"]) != fork_lease.claim_epoch
                or target is None
                or str(target["checkpoint_hash"]) != checkpoint_hash
                or str(target["checkpoint_json"]) != checkpoint_json
            ):
                raise UnitOfWorkConflict("fork checkpoint replay changed")
            if str(row["phase"]) == "committed":
                return self._fork_receipt(row)
            request = self._fork_receipt(row).request
            if not self._fork_source_head_is_current(
                tx.connection, request
            ) or not self._dangerous_confirmation_is_current(tx.connection, request):
                return self._rollback_fork_for_changed_source(
                    tx.connection, row, now=now, fault=fault
                )
            return self._fork_receipt(row)
        if (
            row is None
            or str(row["phase"]) != "claimed"
            or (
                row["claim_owner"],
                int(row["claim_epoch"]),
                float(row["claim_expires_at"]),
            )
            != (fork_lease.owner_id, fork_lease.claim_epoch, fork_lease.expires_at)
            or fork_lease.expires_at <= now
        ):
            raise UnitOfWorkConflict("fork write lease is stale")
        request = self._fork_receipt(row).request
        if not self._fork_source_head_is_current(
            tx.connection, request
        ) or not self._dangerous_confirmation_is_current(tx.connection, request):
            return self._rollback_fork_for_changed_source(tx.connection, row, now=now, fault=fault)
        head = tx.connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints WHERE run_id=? ORDER BY version DESC"
            " LIMIT 1",
            (fork_lease.target_run_id,),
        ).fetchone()
        actual = None if head is None else str(head["checkpoint_id"])
        if actual != expected_target_head:
            raise UnitOfWorkConflict("fork target head changed")
        _fault(fault, "workflow:checkpoint_fork:before_workflow_checkpoints_write")
        tx.connection.execute(
            "INSERT INTO"
            " workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,1,0,?)",
            (
                str(row["target_checkpoint_id"]),
                fork_lease.target_run_id,
                request.source_namespace,
                checkpoint_json,
                checkpoint_hash,
                now,
            ),
        )
        _fault(fault, "workflow:checkpoint_fork:after_workflow_checkpoints_write")
        _fault(
            fault,
            "workflow:checkpoint_fork:before_workflow_fork_receipts_write",
        )
        tx.connection.execute(
            "UPDATE workflow_fork_receipts SET"
            " phase='checkpointed',version=version+1,target_checkpoint_hash=?,updated_at=? WHERE"
            " fork_id=? AND version=?",
            (
                checkpoint_hash,
                now,
                fork_lease.fork_id,
                fork_lease.expected_receipt_version,
            ),
        )
        _fault(
            fault,
            "workflow:checkpoint_fork:after_workflow_fork_receipts_write",
        )
        return self._fork_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
                (fork_lease.fork_id,),
            ).fetchone()
        )

    async def commit_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_receipt_version: int,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
            (fork_lease.fork_id,),
        ).fetchone()
        if row is not None and str(row["phase"]) == "committed":
            if (
                str(row["target_run_id"]) != fork_lease.target_run_id
                or str(row["claim_owner"]) != fork_lease.owner_id
                or int(row["claim_epoch"]) != fork_lease.claim_epoch
            ):
                raise UnitOfWorkConflict("fork commit replay changed")
            return self._fork_receipt(row)
        if (
            row is None
            or str(row["phase"]) != "checkpointed"
            or int(row["version"]) != expected_receipt_version
            or str(row["claim_owner"]) != fork_lease.owner_id
            or int(row["claim_epoch"]) != fork_lease.claim_epoch
        ):
            raise UnitOfWorkConflict("fork commit binding changed")
        request = self._fork_receipt(row).request
        if not self._fork_source_head_is_current(
            tx.connection, request
        ) or not self._dangerous_confirmation_is_current(tx.connection, request):
            return self._rollback_fork_for_changed_source(tx.connection, row, now=now, fault=fault)
        _fault(fault, "workflow:commit_fork:before_runs_write")
        tx.connection.execute(
            "UPDATE runs SET state='created',version=version+1,updated_at=? WHERE run_id=? AND"
            " state='reserved_fork'",
            (now, fork_lease.target_run_id),
        )
        _fault(fault, "workflow:commit_fork:after_runs_write")
        _fault(fault, "workflow:commit_fork:before_workflow_fork_receipts_write")
        tx.connection.execute(
            "UPDATE workflow_fork_receipts SET"
            " phase='committed',version=version+1,outcome_json=?,updated_at=? WHERE fork_id=? AND"
            " version=?",
            (
                canonical_json({"run_id": fork_lease.target_run_id}),
                now,
                fork_lease.fork_id,
                expected_receipt_version,
            ),
        )
        _fault(fault, "workflow:commit_fork:after_workflow_fork_receipts_write")
        return self._fork_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
                (fork_lease.fork_id,),
            ).fetchone()
        )

    async def rollback_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_receipt_version: int,
        reason: str,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
            (fork_lease.fork_id,),
        ).fetchone()
        if (
            row is None
            or int(row["version"]) != expected_receipt_version
            or str(row["phase"]) not in {"prepared", "claimed", "checkpointed"}
        ):
            raise UnitOfWorkConflict("fork rollback binding changed")
        _fault(fault, "workflow:rollback_fork:before_workflow_fork_receipts_write")
        tx.connection.execute(
            "UPDATE workflow_fork_receipts SET"
            " phase='rolled_back',version=version+1,outcome_json=?,updated_at=? WHERE fork_id=? AND"
            " version=?",
            (
                canonical_json({"reason": reason}),
                now,
                fork_lease.fork_id,
                expected_receipt_version,
            ),
        )
        _fault(fault, "workflow:rollback_fork:after_workflow_fork_receipts_write")
        return self._fork_receipt(
            tx.connection.execute(
                "SELECT * FROM workflow_fork_receipts WHERE fork_id=?",
                (fork_lease.fork_id,),
            ).fetchone()
        )

    async def publish_catalog(
        self,
        transaction: WorkflowTransaction,
        authority: VerifiedWorkflowCatalogAuthority,
        expected_version: int,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCatalogAuthority:
        tx = self._assert_open_workflow_transaction(transaction)
        from simple_harness.runtime.orchestration import (
            VerifiedWorkflowCatalogAuthority,
        )

        if (
            type(authority) is not VerifiedWorkflowCatalogAuthority
            or not authority._is_sdk_verified()
        ):
            raise UnitOfWorkConflict("workflow catalog authority was not SDK verified")
        durable_authority = authority.authority
        expected_snapshot_hash = hashlib.sha256(
            canonical_json(
                {
                    "generation": durable_authority.generation,
                    "profiles": [
                        _workflow_catalog_profile_json(item) for item in durable_authority.profiles
                    ],
                }
            ).encode()
        ).hexdigest()
        expected_snapshot_id = hashlib.sha256(
            ("simple-harness.workflow.registry-snapshot.v1|" + expected_snapshot_hash).encode()
        ).hexdigest()
        if (
            authority.registry_snapshot_hash != expected_snapshot_hash
            or authority.registry_snapshot_id != expected_snapshot_id
        ):
            raise UnitOfWorkConflict("workflow registry snapshot differs from catalog")
        if expected_version < 0:
            raise ValueError("expected catalog version must be non-negative")
        if durable_authority.version != expected_version + 1:
            raise UnitOfWorkConflict("catalog version is not the next CAS version")
        existing = tx.connection.execute(
            "SELECT * FROM workflow_catalog_authorities WHERE authority_id=?",
            (durable_authority.authority_id,),
        ).fetchone()
        if existing is not None:
            current = _workflow_catalog_authority(existing)
            if current == durable_authority:
                return current
            if current.version != expected_version:
                raise UnitOfWorkConflict("workflow catalog version changed")
            _fault(fault, "workflow:catalog:before_authority_write")
            changed = tx.connection.execute(
                "UPDATE workflow_catalog_authorities SET"
                " generation=?,version=?,catalog_hash=?,canonical_profiles=?,updated_at=? WHERE"
                " authority_id=? AND version=?",
                (
                    durable_authority.generation,
                    durable_authority.version,
                    durable_authority.catalog_hash,
                    _workflow_catalog_profiles_json(durable_authority),
                    now,
                    durable_authority.authority_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow catalog CAS failed")
        else:
            if expected_version != 0:
                raise UnitOfWorkConflict("initial workflow catalog version must be one")
            _fault(fault, "workflow:catalog:before_authority_write")
            tx.connection.execute(
                "INSERT INTO"
                " workflow_catalog_authorities(authority_id,generation,version,catalog_hash,canonical_profiles,updated_at)"  # noqa: E501
                " VALUES(?,?,?,?,?,?)",
                (
                    durable_authority.authority_id,
                    durable_authority.generation,
                    durable_authority.version,
                    durable_authority.catalog_hash,
                    _workflow_catalog_profiles_json(durable_authority),
                    now,
                ),
            )
        _fault(fault, "workflow:catalog:after_authority_write")
        tx.register_after_commit_fault("workflow:catalog:after_commit")
        return durable_authority

    async def read_catalog(self, transaction: WorkflowTransaction) -> WorkflowCatalogAuthority:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_catalog_authorities WHERE authority_id='model_spawnable'"
        ).fetchone()
        if row is None:
            raise UnitOfWorkNotFound("model_spawnable workflow catalog")
        return _workflow_catalog_authority(row)

    @classmethod
    def _require_workflow_spawn_issue_authority(
        cls,
        connection: sqlite3.Connection,
        *,
        request: WorkflowLaunchRequest,
        authority: WorkflowSpawnIssueAuthority,
        now: float,
        require_effect_fence: bool = True,
    ) -> str:
        from simple_harness.execution.effects import EffectState
        from simple_harness.runtime.orchestration import WorkflowSpawnIssueAuthority

        if not isinstance(authority, WorkflowSpawnIssueAuthority):
            raise TypeError("issue_authority must be a WorkflowSpawnIssueAuthority")
        parent_run_id = request.spawn_origin.parent_run_id
        if authority.execution_lease.run_id != parent_run_id:
            raise UnitOfWorkConflict("workflow spawn authority belongs to another Run")
        cls._require_runtime_and_fence(
            connection,
            run_id=parent_run_id,
            execution_lease=authority.execution_lease,
            run_fence=authority.run_fence,
            now=now,
            require_exact_expiry=False,
        )
        cls._require_workflow_handoff_authority(
            connection,
            run_id=parent_run_id,
            execution_lease=authority.execution_lease,
            workflow_lease=authority.workflow_lease,
            now=now,
        )
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (parent_run_id,)).fetchone()
        if (
            run is None
            or str(run["state"]) != RunState.RUNNING.value
            or str(run["request_id"]) != request.spawn_origin.parent_request_id
            or str(run["root_run_id"]) != request.root_run_id
            or str(run["execution_session_id"]) != request.session_id
        ):
            raise UnitOfWorkConflict("workflow spawn parent Run authority differs")
        snapshot = connection.execute(
            "SELECT snapshot_json,snapshot_hash FROM run_start_snapshots WHERE run_id=?",
            (parent_run_id,),
        ).fetchone()
        if snapshot is None:
            raise UnitOfWorkConflict("workflow spawn parent start snapshot is missing")
        snapshot_json = str(snapshot["snapshot_json"])
        try:
            snapshot_value = json.loads(snapshot_json)
        except (TypeError, ValueError) as exc:
            raise UnitOfWorkConflict("workflow spawn parent start snapshot is malformed") from exc
        if (
            not isinstance(snapshot_value, dict)
            or canonical_json(snapshot_value) != snapshot_json
            or hashlib.sha256(snapshot_json.encode()).hexdigest() != str(snapshot["snapshot_hash"])
            or snapshot_value.get("turn_id") != request.turn_id
            or snapshot_value.get("tool_catalog_generation") != request.tool_catalog_generation
        ):
            raise UnitOfWorkConflict("workflow spawn parent start snapshot differs")
        checkpoint = connection.execute(
            """
            SELECT version,checkpoint_json,checkpoint_hash
            FROM workflow_checkpoints
            WHERE run_id=? AND namespace='react.termination.v1'
            ORDER BY version DESC LIMIT 1
            """,
            (parent_run_id,),
        ).fetchone()
        if checkpoint is None:
            raise UnitOfWorkConflict("workflow spawn ReAct checkpoint is missing")
        checkpoint_json = str(checkpoint["checkpoint_json"])
        try:
            checkpoint_value = json.loads(checkpoint_json)
        except (TypeError, ValueError) as exc:
            raise UnitOfWorkConflict("workflow spawn ReAct checkpoint is malformed") from exc
        if (
            int(checkpoint["version"]) != authority.react_checkpoint_revision
            or not isinstance(checkpoint_value, dict)
            or canonical_json(checkpoint_value) != checkpoint_json
            or hashlib.sha256(checkpoint_json.encode()).hexdigest()
            != str(checkpoint["checkpoint_hash"])
            or checkpoint_value.get("phase") != "tool_batch_reserved"
        ):
            raise UnitOfWorkConflict("workflow spawn ReAct checkpoint authority differs")
        effect = connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (authority.effect_id,),
        ).fetchone()
        if (
            effect is None
            or str(effect["run_id"]) != parent_run_id
            or str(effect["state"]) != EffectState.HANDED_OFF.value
            or str(effect["tool_name"]) != "workflow_spawn"
            or str(effect["raw_call_id"]) != request.spawn_origin.internal_tool_call_id
            or str(effect["request_hash"]) != authority.effect_request_hash
            or int(effect["handoff_attempt"]) != authority.effect_handoff_attempt
            or (require_effect_fence and int(effect["fence_epoch"]) != authority.run_fence.epoch)
        ):
            raise UnitOfWorkConflict("workflow spawn Effect authority differs")
        authority_payload: dict[str, JsonValue] = {
            "react_checkpoint_revision": authority.react_checkpoint_revision,
            "execution_lease": {
                "run_id": authority.execution_lease.run_id,
                "namespace": authority.execution_lease.namespace,
                "owner_id": authority.execution_lease.owner_id,
                "epoch": authority.execution_lease.epoch,
            },
            "run_fence": {
                "run_id": authority.run_fence.run_id.value,
                "owner_id": authority.run_fence.owner_id,
                "runtime_lease_epoch": authority.run_fence.runtime_lease_epoch,
                "epoch": authority.run_fence.epoch,
            },
            "workflow_lease": (
                None
                if authority.workflow_lease is None
                else {
                    "run_id": authority.workflow_lease.run_id,
                    "namespace": authority.workflow_lease.namespace,
                    "owner_id": authority.workflow_lease.owner_id,
                    "epoch": authority.workflow_lease.epoch,
                    "runtime_lease_epoch": authority.workflow_lease.runtime_lease_epoch,
                }
            ),
            "effect_id": authority.effect_id,
            "effect_handoff_attempt": authority.effect_handoff_attempt,
            "effect_request_hash": authority.effect_request_hash,
        }
        return hashlib.sha256(canonical_json(authority_payload).encode("utf-8")).hexdigest()

    async def issue(
        self,
        transaction: WorkflowTransaction,
        request: WorkflowLaunchRequest,
        issue_authority: WorkflowSpawnIssueAuthority,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowLaunchTicket:
        tx = self._assert_open_workflow_transaction(transaction)
        request_payload = _workflow_launch_request_json(request)
        canonical_request = canonical_json(request_payload)
        ticket_receipt_id = self._derived_id("workflow-launch/receipt/v1", request.request_key)
        existing = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE request_key=?",
            (request.request_key,),
        ).fetchone()
        if existing is not None:
            stored_payload = json.loads(str(existing["canonical_payload"]))
            stored_request = (
                stored_payload.get("request") if isinstance(stored_payload, dict) else None
            )
            canonical_payload = canonical_json(stored_payload)
            payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
            if (
                str(existing["ticket_receipt_id"]) != ticket_receipt_id
                or canonical_json(stored_request) != canonical_request
                or str(existing["canonical_payload"]) != canonical_payload
                or str(existing["payload_hash"]) != payload_hash
                or str(existing["ticket_id"])
                != self._derived_id("workflow-launch/ticket/v1", payload_hash)
            ):
                raise UnitOfWorkConflict(
                    "workflow launch request key reused with different payload"
                )
            return _workflow_launch_ticket(existing)

        issue_authority_hash = self._require_workflow_spawn_issue_authority(
            tx.connection,
            request=request,
            authority=issue_authority,
            now=now,
        )

        catalog = await self.read_catalog(transaction)
        if request.catalog_generation != catalog.generation:
            raise UnitOfWorkConflict("workflow launch catalog generation is stale")
        profile = catalog.require(request.profile_key)
        try:
            validate_arguments(
                _thaw(request.start_input),
                profile.start_input_schema.canonical_schema,
            )
        except (ArgumentsValidationError, SchemaDefinitionError) as exc:
            raise UnitOfWorkConflict("workflow start input violates durable schema") from exc
        request_fingerprint = hashlib.sha256(canonical_request.encode()).hexdigest()
        resolved_run_id = request.requested_run_id or self._derived_id(
            "workflow-launch/run/v1", request_fingerprint
        )
        resolved_trace_id = request.requested_trace_id or self._derived_id(
            "workflow-launch/trace/v1", request_fingerprint
        )
        resolved_thread_id = request.requested_thread_id or self._derived_id(
            "workflow-launch/thread/v1", request_fingerprint
        )
        objective_hash = hashlib.sha256(request.objective.encode()).hexdigest()
        start_input_hash = hashlib.sha256(
            canonical_json(_thaw(request.start_input)).encode()
        ).hexdigest()
        durable_payload: dict[str, JsonValue] = {
            "request": request_payload,
            "catalog_authority_version": catalog.version,
            "catalog_hash": catalog.catalog_hash,
            "profile_binding": _workflow_catalog_profile_json(profile),
            "profile_fingerprint": profile.profile_fingerprint,
            "workflow_name": profile.workflow_name,
            "workflow_version": profile.workflow_version,
            "implementation_fingerprint": profile.implementation_fingerprint,
            "resolved_run_id": resolved_run_id,
            "resolved_trace_id": resolved_trace_id,
            "resolved_thread_id": resolved_thread_id,
            "objective_hash": objective_hash,
            "start_input_hash": start_input_hash,
            "issue_authority_hash": issue_authority_hash,
        }
        canonical_payload = canonical_json(durable_payload)
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        ticket_id = self._derived_id("workflow-launch/ticket/v1", payload_hash)
        _fault(fault, "workflow:launch_ticket:before_receipt_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_launch_ticket_receipts(
                ticket_receipt_id,request_key,ticket_id,canonical_payload,payload_hash,
                candidate_id,profile_key,catalog_generation,catalog_authority_version,
                catalog_hash,profile_fingerprint,workflow_name,workflow_version,
                implementation_fingerprint,session_id,request_id,turn_id,
                requested_run_id,requested_trace_id,requested_thread_id,
                resolved_run_id,resolved_trace_id,resolved_thread_id,
                tool_catalog_generation,objective,objective_hash,start_input_hash,
                spawn_origin_json,parent_run_id,root_run_id,attachment_policy,
                child_command_id,issue_authority_hash,issued_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticket_receipt_id,
                request.request_key,
                ticket_id,
                canonical_payload,
                payload_hash,
                request.candidate_id,
                request.profile_key,
                request.catalog_generation,
                catalog.version,
                catalog.catalog_hash,
                profile.profile_fingerprint,
                profile.workflow_name,
                profile.workflow_version,
                profile.implementation_fingerprint,
                request.session_id,
                request.request_id,
                request.turn_id,
                request.requested_run_id,
                request.requested_trace_id,
                request.requested_thread_id,
                resolved_run_id,
                resolved_trace_id,
                resolved_thread_id,
                request.tool_catalog_generation,
                request.objective,
                objective_hash,
                start_input_hash,
                canonical_json(request.spawn_origin.to_json()),
                request.spawn_origin.parent_run_id,
                request.root_run_id,
                request.attachment_policy.value,
                request.child_command_id,
                issue_authority_hash,
                now,
            ),
        )
        _fault(fault, "workflow:launch_ticket:after_receipt_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_continuations(
                operation_id,ticket_receipt_id,parent_run_id,state,
                owner_id,runtime_lease_epoch,run_fence_epoch,workflow_lease_epoch,
                claim_epoch,expires_at,version,completion_receipt_id,
                completion_path_kind,effect_id,handoff_attempt,
                effect_request_hash,issue_authority_hash,created_at,updated_at
            ) VALUES(?,?,?,'pending',NULL,NULL,NULL,NULL,0,NULL,0,NULL,NULL,?,?,?,?,?,?)
            """,
            (
                request.request_key,
                ticket_receipt_id,
                request.spawn_origin.parent_run_id,
                issue_authority.effect_id,
                issue_authority.effect_handoff_attempt,
                issue_authority.effect_request_hash,
                issue_authority_hash,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:launch_ticket:after_continuation_write")
        tx.register_after_commit_fault("workflow:launch_ticket:after_commit")
        row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket_receipt_id,),
        ).fetchone()
        assert row is not None
        return _workflow_launch_ticket(row)

    async def read_issued(
        self, transaction: WorkflowTransaction, request_key: str
    ) -> tuple[WorkflowLaunchTicket, WorkflowLaunchRequest] | None:
        tx = self._assert_open_workflow_transaction(transaction)
        request_key = _required(request_key, "request_key")
        row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE request_key=?",
            (request_key,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["canonical_payload"]))
        if not isinstance(payload, dict):
            raise UnitOfWorkConflict("stored workflow launch ticket is malformed")
        canonical_payload = canonical_json(payload)
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        if (
            str(row["canonical_payload"]) != canonical_payload
            or str(row["payload_hash"]) != payload_hash
            or str(row["ticket_id"]) != self._derived_id("workflow-launch/ticket/v1", payload_hash)
        ):
            raise UnitOfWorkConflict("stored workflow launch ticket self-hash differs")
        request = _workflow_launch_request_from_json(payload.get("request"))
        if request.request_key != request_key:
            raise UnitOfWorkConflict("stored workflow launch request key differs")
        return _workflow_launch_ticket(row), request

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
    ) -> WorkflowSpawnContinuationClaim:
        tx = self._assert_open_workflow_transaction(transaction)
        now = _time(now)
        ttl_seconds = _positive_ttl(ttl_seconds)
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if ticket_row is None or _workflow_launch_ticket(ticket_row) != ticket:
            raise UnitOfWorkConflict("workflow spawn ticket is forged or stale")
        payload = json.loads(str(ticket_row["canonical_payload"]))
        if not isinstance(payload, dict):
            raise UnitOfWorkConflict("workflow spawn ticket payload is malformed")
        request = _workflow_launch_request_from_json(payload.get("request"))
        row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (request.request_key,),
        ).fetchone()
        if (
            row is None
            or str(row["ticket_receipt_id"]) != ticket.ticket_receipt_id
            or str(row["effect_id"]) != issue_authority.effect_id
            or int(row["handoff_attempt"]) != issue_authority.effect_handoff_attempt
            or str(row["effect_request_hash"]) != issue_authority.effect_request_hash
        ):
            raise UnitOfWorkConflict("workflow spawn continuation identity differs")
        state = str(row["state"])
        workflow_epoch = (
            None if issue_authority.workflow_lease is None else issue_authority.workflow_lease.epoch
        )
        if state == "claimed" and float(row["expires_at"]) > now:
            authority_hash = self._require_workflow_spawn_issue_authority(
                tx.connection,
                request=request,
                authority=issue_authority,
                now=now,
            )
            if str(row["issue_authority_hash"]) != authority_hash:
                raise UnitOfWorkConflict("workflow spawn continuation issue authority differs")
            current = _workflow_spawn_continuation_claim(row)
            if (
                current.owner_id != issue_authority.execution_lease.owner_id
                or current.runtime_lease_epoch != issue_authority.execution_lease.epoch
                or current.run_fence_epoch != issue_authority.run_fence.epoch
                or current.workflow_lease_epoch != workflow_epoch
            ):
                raise UnitOfWorkConflict("workflow spawn continuation has a live foreign owner")
            return current
        if state == "completed":
            raise UnitOfWorkConflict("workflow spawn continuation is already completed")
        if state == "claimed":
            if ready is None:
                raise UnitOfWorkConflict(
                    "expired workflow spawn continuation requires ready evidence"
                )
            self._require_matching_spawn_ready(tx.connection, row, ready)
            self._require_workflow_spawn_issue_authority(
                tx.connection,
                request=request,
                authority=issue_authority,
                now=now,
                require_effect_fence=False,
            )
        elif state == "pending" and ready is not None:
            raise UnitOfWorkConflict(
                "initial workflow spawn continuation rejects recovery evidence"
            )
        elif state != "pending":
            raise UnitOfWorkConflict("workflow spawn continuation state is malformed")
        else:
            authority_hash = self._require_workflow_spawn_issue_authority(
                tx.connection,
                request=request,
                authority=issue_authority,
                now=now,
            )
            if str(row["issue_authority_hash"]) != authority_hash:
                raise UnitOfWorkConflict("workflow spawn continuation issue authority differs")
        next_epoch = int(row["claim_epoch"]) + 1
        expires_at = now + ttl_seconds
        _fault(fault, "workflow:spawn_continuation:before_claim_write")
        changed = tx.connection.execute(
            """
            UPDATE workflow_spawn_continuations
            SET state='claimed',owner_id=?,runtime_lease_epoch=?,run_fence_epoch=?,
                workflow_lease_epoch=?,claim_epoch=?,expires_at=?,version=version+1,
                updated_at=?
            WHERE operation_id=? AND version=? AND state=?
            """,
            (
                issue_authority.execution_lease.owner_id,
                issue_authority.execution_lease.epoch,
                issue_authority.run_fence.epoch,
                workflow_epoch,
                next_epoch,
                expires_at,
                now,
                request.request_key,
                int(row["version"]),
                state,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn continuation claim CAS failed")
        _fault(fault, "workflow:spawn_continuation:after_claim_write")
        tx.register_after_commit_fault("workflow:spawn_continuation:after_commit")
        claimed = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (request.request_key,),
        ).fetchone()
        assert claimed is not None
        return _workflow_spawn_continuation_claim(claimed)

    async def mark_spawn_continuation_ready(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        effect_snapshot: EffectRecord,
        evidence_ref: str,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnContinuationReady:
        tx = self._assert_open_workflow_transaction(transaction)
        evidence_ref = _required(evidence_ref, "evidence_ref")
        now = _time(now)
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if ticket_row is None or _workflow_launch_ticket(ticket_row) != ticket:
            raise UnitOfWorkConflict("workflow spawn ticket is forged or stale")
        continuation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if continuation is None or str(continuation["state"]) == "completed":
            raise UnitOfWorkConflict("workflow spawn continuation is unavailable")
        durable_effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (effect_snapshot.effect_id.value,),
        ).fetchone()
        if (
            durable_effect is None
            or _effect_record(durable_effect) != effect_snapshot
            or str(continuation["effect_id"]) != effect_snapshot.effect_id.value
            or int(continuation["handoff_attempt"]) != effect_snapshot.handoff_attempt
            or str(continuation["effect_request_hash"]) != effect_snapshot.request_hash
            or effect_snapshot.state not in {EffectState.HANDED_OFF, EffectState.UNKNOWN}
        ):
            raise UnitOfWorkConflict("workflow spawn ready Effect evidence differs")
        ready_receipt_id = self._derived_id(
            "workflow-spawn/ready/v1", str(continuation["operation_id"])
        )
        existing = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready WHERE operation_id=?",
            (str(continuation["operation_id"]),),
        ).fetchone()
        if existing is not None:
            ready = _workflow_spawn_continuation_ready(existing)
            if (
                ready.ready_receipt_id != ready_receipt_id
                or ready.ticket_receipt_id != ticket.ticket_receipt_id
                or ready.effect_id != effect_snapshot.effect_id.value
                or ready.handoff_attempt != effect_snapshot.handoff_attempt
                or ready.evidence_ref != evidence_ref
            ):
                raise UnitOfWorkConflict(
                    "workflow spawn ready identity reused with different evidence"
                )
            return ready
        _fault(fault, "workflow:spawn_ready:before_receipt_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_continuation_ready(
                ready_receipt_id,operation_id,ticket_receipt_id,effect_id,
                handoff_attempt,evidence_ref,version,created_at,consumed_at
            ) VALUES(?,?,?,?,?,?,0,?,NULL)
            """,
            (
                ready_receipt_id,
                str(continuation["operation_id"]),
                ticket.ticket_receipt_id,
                effect_snapshot.effect_id.value,
                effect_snapshot.handoff_attempt,
                evidence_ref,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_ready:after_receipt_write")
        tx.register_after_commit_fault("workflow:spawn_ready:after_commit")
        created = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
            (ready_receipt_id,),
        ).fetchone()
        assert created is not None
        return _workflow_spawn_continuation_ready(created)

    def list_ready_spawn_continuations(
        self, snapshot_cursor: str | None, *, limit: int
    ) -> tuple[tuple[WorkflowSpawnContinuationReady, ...], str | None]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("workflow spawn ready limit must be between 1 and 100")
        if snapshot_cursor is not None:
            snapshot_cursor = _required(snapshot_cursor, "snapshot_cursor")
        rows = self.database.connection.execute(
            """
            SELECT ready.* FROM workflow_spawn_continuation_ready AS ready
            JOIN workflow_spawn_continuations AS continuation
              ON continuation.operation_id=ready.operation_id
            WHERE ready.consumed_at IS NULL AND continuation.state<>'completed'
              AND (? IS NULL OR ready.ready_receipt_id>?)
            ORDER BY ready.ready_receipt_id LIMIT ?
            """,
            (snapshot_cursor, snapshot_cursor, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_cursor = None if len(rows) <= limit else str(page[-1]["ready_receipt_id"])
        return tuple(_workflow_spawn_continuation_ready(row) for row in page), next_cursor

    def read_spawn_ready_blocker(
        self, ready: WorkflowSpawnContinuationReady
    ) -> WaitBlockerRecord | None:
        from simple_harness.runtime.orchestration import (
            WorkflowSpawnContinuationReady,
        )

        if not isinstance(ready, WorkflowSpawnContinuationReady):
            raise TypeError("ready must be a WorkflowSpawnContinuationReady")
        durable = self.database.connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
            (ready.ready_receipt_id,),
        ).fetchone()
        if durable is None or _workflow_spawn_continuation_ready(durable) != ready:
            raise UnitOfWorkConflict("workflow spawn ready receipt differs")
        rows = self.database.connection.execute(
            """
            SELECT * FROM run_wait_blockers
            WHERE kind='tool' AND ledger_identity=? AND handoff_attempt=?
              AND wake_consumed=0 AND superseded_by IS NULL
            ORDER BY blocker_id LIMIT 2
            """,
            (ready.effect_id, ready.handoff_attempt),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise UnitOfWorkConflict("workflow spawn ready maps to multiple wait blockers")
        blocker = _wait_blocker_record(rows[0])
        if (
            blocker.kind is not RecoveryKind.TOOL
            or blocker.ledger_identity != ready.effect_id
            or blocker.handoff_attempt != ready.handoff_attempt
        ):
            raise UnitOfWorkConflict("workflow spawn ready blocker differs")
        return blocker

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
    ) -> WorkflowSpawnReadyActivation:
        from simple_harness.runtime.orchestration import (
            WorkflowSpawnReadyActivationState,
            _create_workflow_spawn_ready_activation,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        owner_id = _required(owner_id, "owner_id")
        now = _time(now)
        expires_at = now + _positive_ttl(ttl_seconds)
        continuation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (ready.spawn_operation_id,),
        ).fetchone()
        if continuation is None or str(continuation["state"]) == "completed":
            raise UnitOfWorkConflict("workflow spawn continuation is unavailable")
        self._require_matching_spawn_ready(tx.connection, continuation, ready)
        blocker = tx.connection.execute(
            "SELECT * FROM run_wait_blockers WHERE blocker_id=?",
            (blocker_snapshot.blocker_id,),
        ).fetchone()
        if (
            blocker is None
            or _wait_blocker_record(blocker) != blocker_snapshot
            or blocker_snapshot.run_id != str(continuation["parent_run_id"])
            or blocker_snapshot.kind is not RecoveryKind.TOOL
            or blocker_snapshot.ledger_identity != ready.effect_id
            or blocker_snapshot.handoff_attempt != ready.handoff_attempt
            or blocker_snapshot.wake_consumed
        ):
            raise UnitOfWorkConflict("workflow spawn blocker authority differs")
        run = tx.connection.execute(
            "SELECT * FROM runs WHERE run_id=?",
            (blocker_snapshot.run_id,),
        ).fetchone()
        if run is None or str(run["state"]) != RunState.WAITING.value:
            raise UnitOfWorkConflict("workflow spawn parent Run is not waiting")
        existing_active = tx.connection.execute(
            """
            SELECT * FROM workflow_spawn_ready_activations
            WHERE ready_receipt_id=? AND state='active'
            """,
            (ready.ready_receipt_id,),
        ).fetchone()
        if existing_active is not None:
            activation = _workflow_spawn_ready_activation(tx.connection, existing_active)
            if activation.execution_lease.owner_id != owner_id:
                raise UnitOfWorkConflict("workflow spawn ready has a live activation owner")
            return activation
        runtime_row = tx.connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (blocker_snapshot.run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        fence_row = tx.connection.execute(
            "SELECT owner_id,epoch,runtime_lease_epoch,state FROM run_fences WHERE run_id=?",
            (blocker_snapshot.run_id,),
        ).fetchone()
        if runtime_row is not None and float(runtime_row["expires_at"]) > now:
            if str(runtime_row["owner_id"]) != owner_id:
                raise UnitOfWorkConflict("workflow spawn parent has a live foreign Runtime owner")
            runtime_epoch = int(runtime_row["epoch"])
            expires_at = max(expires_at, float(runtime_row["expires_at"]))
        else:
            runtime_epoch = (
                max(
                    0 if runtime_row is None else int(runtime_row["epoch"]),
                    0 if fence_row is None else int(fence_row["runtime_lease_epoch"]),
                )
                + 1
            )
        _fault(fault, "workflow:spawn_activation:before_runtime_lease_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(run_id,namespace) DO UPDATE SET
                owner_id=excluded.owner_id,epoch=excluded.epoch,
                expires_at=excluded.expires_at
            """,
            (
                blocker_snapshot.run_id,
                RUNTIME_LEASE_NAMESPACE,
                owner_id,
                runtime_epoch,
                expires_at,
            ),
        )
        _fault(fault, "workflow:spawn_activation:after_runtime_lease_write")
        fence_epoch = 1 if fence_row is None else int(fence_row["epoch"])
        if fence_row is not None and (
            str(fence_row["state"]) != "active"
            or str(fence_row["owner_id"]) != owner_id
            or int(fence_row["runtime_lease_epoch"]) != runtime_epoch
        ):
            fence_epoch += 1
        _fault(fault, "workflow:spawn_activation:before_run_fence_write")
        tx.connection.execute(
            """
            INSERT INTO run_fences(
                run_id,owner_id,runtime_lease_epoch,epoch,state,acquired_at,released_at
            ) VALUES(?,?,?,?,'active',?,NULL)
            ON CONFLICT(run_id) DO UPDATE SET
                owner_id=excluded.owner_id,
                runtime_lease_epoch=excluded.runtime_lease_epoch,
                epoch=excluded.epoch,state='active',acquired_at=excluded.acquired_at,
                released_at=NULL
            """,
            (blocker_snapshot.run_id, owner_id, runtime_epoch, fence_epoch, now),
        )
        _fault(fault, "workflow:spawn_activation:after_run_fence_write")
        workflow_namespace: str | None = None
        admission = tx.connection.execute(
            "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
            (blocker_snapshot.run_id,),
        ).fetchone()
        if admission is not None:
            admission_payload = json.loads(str(admission["request_json"]))
            if not isinstance(admission_payload, dict):
                raise UnitOfWorkConflict("workflow spawn parent admission is malformed")
            workflow_namespace = _required(
                admission_payload.get("checkpoint_namespace"),
                "checkpoint_namespace",
            )
            _fault(fault, "workflow:spawn_activation:before_workflow_lease_write")
            tx.connection.execute(
                """
                INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(run_id,namespace) DO UPDATE SET
                    owner_id=excluded.owner_id,epoch=excluded.epoch,
                    expires_at=excluded.expires_at
                """,
                (
                    blocker_snapshot.run_id,
                    workflow_namespace,
                    owner_id,
                    runtime_epoch,
                    expires_at,
                ),
            )
            _fault(fault, "workflow:spawn_activation:after_workflow_lease_write")
        next_claim_epoch = int(continuation["claim_epoch"]) + 1
        _fault(fault, "workflow:spawn_activation:before_continuation_write")
        changed = tx.connection.execute(
            """
            UPDATE workflow_spawn_continuations
            SET state='claimed',owner_id=?,runtime_lease_epoch=?,run_fence_epoch=?,
                workflow_lease_epoch=?,claim_epoch=?,expires_at=?,version=version+1,
                updated_at=?
            WHERE operation_id=? AND version=? AND state<>'completed'
            """,
            (
                owner_id,
                runtime_epoch,
                fence_epoch,
                None if workflow_namespace is None else runtime_epoch,
                next_claim_epoch,
                expires_at,
                now,
                ready.spawn_operation_id,
                int(continuation["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn activation continuation CAS failed")
        _fault(fault, "workflow:spawn_activation:after_continuation_write")
        changed = tx.connection.execute(
            """
            UPDATE runs SET state='running',version=version+1,updated_at=?
            WHERE run_id=? AND state='waiting' AND version=?
            """,
            (now, blocker_snapshot.run_id, int(run["version"])),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn activation Run CAS failed")
        tx.connection.execute(
            "UPDATE run_wait_blockers SET wake_consumed=1,consumed_at=?,version=version+1 WHERE"
            " blocker_id=? AND wake_consumed=0 AND version=?",
            (now, blocker_snapshot.blocker_id, blocker_snapshot.version),
        )
        tx.connection.execute(
            "UPDATE workflow_spawn_continuation_ready SET consumed_at=? WHERE ready_receipt_id=?"
            " AND consumed_at IS NULL",
            (now, ready.ready_receipt_id),
        )
        activation_receipt_id = self._derived_id(
            "workflow-spawn/activation/v1",
            f"{ready.ready_receipt_id}:{runtime_epoch}:1",
        )
        canonical_hash = _workflow_spawn_activation_hash(
            activation_receipt_id=activation_receipt_id,
            ready_receipt_id=ready.ready_receipt_id,
            spawn_operation_id=ready.spawn_operation_id,
            parent_run_id=blocker_snapshot.run_id,
            effect_id=ready.effect_id,
            owner_id=owner_id,
            runtime_lease_epoch=runtime_epoch,
            run_fence_epoch=fence_epoch,
            workflow_lease_epoch=(None if workflow_namespace is None else runtime_epoch),
            continuation_claim_epoch=next_claim_epoch,
            predecessor_activation_receipt_id=None,
            version=1,
        )
        _fault(fault, "workflow:spawn_activation:before_receipt_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_ready_activations(
                activation_receipt_id,ready_receipt_id,spawn_operation_id,
                parent_run_id,effect_id,owner_id,runtime_lease_epoch,
                run_fence_epoch,workflow_lease_epoch,continuation_claim_epoch,
                predecessor_activation_receipt_id,state,version,canonical_hash,
                created_at,superseded_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'active',1,?,?,NULL,NULL)
            """,
            (
                activation_receipt_id,
                ready.ready_receipt_id,
                ready.spawn_operation_id,
                blocker_snapshot.run_id,
                ready.effect_id,
                owner_id,
                runtime_epoch,
                fence_epoch,
                None if workflow_namespace is None else runtime_epoch,
                next_claim_epoch,
                canonical_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_activation:after_receipt_write")
        self._insert_event(
            tx.connection,
            event_id=f"{blocker_snapshot.run_id}:spawn-ready:{activation_receipt_id}",
            run_id=blocker_snapshot.run_id,
            kind="run.recovered",
            payload={
                "owner_id": owner_id,
                "lease_epoch": runtime_epoch,
                "spawn_operation_id": ready.spawn_operation_id,
            },
            now=now,
        )
        tx.register_after_commit_fault("workflow:spawn_activation:after_commit")
        stored = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
            (activation_receipt_id,),
        ).fetchone()
        assert stored is not None
        activation = _workflow_spawn_ready_activation(tx.connection, stored)
        if activation.state is not WorkflowSpawnReadyActivationState.ACTIVE:
            raise AssertionError("new workflow spawn activation must be active")
        return _create_workflow_spawn_ready_activation(
            ready_receipt=activation.ready_receipt,
            continuation_claim=activation.continuation_claim,
            execution_lease=activation.execution_lease,
            run_fence=activation.run_fence,
            workflow_lease=activation.workflow_lease,
            blocker_id=activation.blocker_id,
            activation_receipt_id=activation.activation_receipt_id,
            activation_version=activation.activation_version,
            predecessor_activation_receipt_id=(activation.predecessor_activation_receipt_id),
            state=activation.state,
        )

    async def read_spawn_ready_activation(
        self,
        transaction: WorkflowTransaction,
        parent_run_id: str,
        activation_receipt_id: str | None = None,
    ) -> WorkflowSpawnReadyActivation | None:
        tx = self._assert_open_workflow_transaction(transaction)
        parent_run_id = _required(parent_run_id, "parent_run_id")
        if activation_receipt_id is None:
            row = tx.connection.execute(
                """
                SELECT * FROM workflow_spawn_ready_activations
                WHERE parent_run_id=? AND state='active'
                ORDER BY created_at DESC,activation_receipt_id DESC LIMIT 1
                """,
                (parent_run_id,),
            ).fetchone()
        else:
            activation_receipt_id = _required(activation_receipt_id, "activation_receipt_id")
            row = tx.connection.execute(
                """
                SELECT * FROM workflow_spawn_ready_activations
                WHERE parent_run_id=? AND activation_receipt_id=?
                """,
                (parent_run_id, activation_receipt_id),
            ).fetchone()
        if row is None:
            return None
        return _workflow_spawn_ready_activation(tx.connection, row)

    async def reclaim_spawn_ready_activation(
        self,
        transaction: WorkflowTransaction,
        prior: WorkflowSpawnReadyActivation,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowSpawnReadyActivation:
        from simple_harness.runtime.orchestration import (
            WorkflowSpawnReadyActivation,
            WorkflowSpawnReadyActivationState,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        if not isinstance(prior, WorkflowSpawnReadyActivation):
            raise TypeError("prior must be a WorkflowSpawnReadyActivation")
        owner_id = _required(owner_id, "owner_id")
        now = _time(now)
        expires_at = now + _positive_ttl(ttl_seconds)
        row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
            (prior.activation_receipt_id,),
        ).fetchone()
        if row is None:
            raise UnitOfWorkConflict("workflow spawn activation receipt is missing")
        if str(row["state"]) != WorkflowSpawnReadyActivationState.ACTIVE.value:
            raise UnitOfWorkConflict("workflow spawn activation is stale")
        stored = _workflow_spawn_ready_activation(tx.connection, row)
        if stored != prior or stored.state is not WorkflowSpawnReadyActivationState.ACTIVE:
            raise UnitOfWorkConflict("workflow spawn activation is stale")
        runtime = tx.connection.execute(
            "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
            (prior.execution_lease.run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        fence = tx.connection.execute(
            "SELECT * FROM run_fences WHERE run_id=?",
            (prior.execution_lease.run_id,),
        ).fetchone()
        run = tx.connection.execute(
            "SELECT * FROM runs WHERE run_id=?",
            (prior.execution_lease.run_id,),
        ).fetchone()
        if runtime is None or fence is None or run is None:
            raise UnitOfWorkConflict("workflow spawn activation authority disappeared")
        if str(run["state"]) != RunState.RUNNING.value:
            raise UnitOfWorkConflict("workflow spawn parent Run is not recoverable")
        if float(runtime["expires_at"]) > now:
            if str(runtime["owner_id"]) != owner_id:
                raise UnitOfWorkConflict(
                    "workflow spawn activation has a live foreign Runtime owner"
                )
            return stored
        if (
            str(runtime["owner_id"]) != prior.execution_lease.owner_id
            or int(runtime["epoch"]) != prior.execution_lease.epoch
            or str(fence["owner_id"]) != prior.run_fence.owner_id
            or int(fence["runtime_lease_epoch"]) != prior.run_fence.runtime_lease_epoch
            or int(fence["epoch"]) != prior.run_fence.epoch
            or str(fence["state"]) != "active"
        ):
            raise UnitOfWorkConflict("workflow spawn activation authority drifted")

        workflow_namespace: str | None = None
        workflow_epoch: int | None = None
        prior_workflow_lease = prior.workflow_lease
        if prior_workflow_lease is not None:
            workflow_namespace = prior_workflow_lease.namespace
            projection = tx.connection.execute(
                "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
                (prior.execution_lease.run_id, workflow_namespace),
            ).fetchone()
            if (
                projection is None
                or str(projection["owner_id"]) != prior_workflow_lease.owner_id
                or int(projection["epoch"]) != prior_workflow_lease.epoch
                or float(projection["expires_at"]) > now
            ):
                raise UnitOfWorkConflict(
                    "workflow spawn Workflow activation is still live or drifted"
                )
            workflow_epoch = int(projection["epoch"]) + 1

        runtime_epoch = max(int(runtime["epoch"]), int(fence["runtime_lease_epoch"])) + 1
        fence_epoch = int(fence["epoch"]) + 1
        continuation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (prior.ready_receipt.spawn_operation_id,),
        ).fetchone()
        if (
            continuation is None
            or str(continuation["state"]) != "claimed"
            or str(continuation["owner_id"]) != prior.continuation_claim.owner_id
            or int(continuation["runtime_lease_epoch"])
            != prior.continuation_claim.runtime_lease_epoch
            or int(continuation["run_fence_epoch"]) != prior.continuation_claim.run_fence_epoch
            or int(continuation["claim_epoch"]) != prior.continuation_claim.claim_epoch
            or int(continuation["version"]) != prior.continuation_claim.version
        ):
            raise UnitOfWorkConflict("workflow spawn continuation claim drifted")
        next_claim_epoch = int(continuation["claim_epoch"]) + 1
        activation_version = prior.activation_version + 1
        activation_receipt_id = self._derived_id(
            "workflow-spawn/activation/v1",
            f"{prior.ready_receipt.ready_receipt_id}:{runtime_epoch}:{activation_version}",
        )

        _fault(fault, "workflow:spawn_activation_reclaim:before_runtime_lease_write")
        changed = tx.connection.execute(
            """
            UPDATE workflow_leases SET owner_id=?,epoch=?,expires_at=?
            WHERE run_id=? AND namespace=? AND owner_id=? AND epoch=? AND expires_at<=?
            """,
            (
                owner_id,
                runtime_epoch,
                expires_at,
                prior.execution_lease.run_id,
                RUNTIME_LEASE_NAMESPACE,
                prior.execution_lease.owner_id,
                prior.execution_lease.epoch,
                now,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn Runtime takeover CAS failed")
        _fault(fault, "workflow:spawn_activation_reclaim:after_runtime_lease_write")
        changed = tx.connection.execute(
            """
            UPDATE run_fences
            SET owner_id=?,runtime_lease_epoch=?,epoch=?,state='active',
                acquired_at=?,released_at=NULL
            WHERE run_id=? AND owner_id=? AND runtime_lease_epoch=? AND epoch=?
              AND state='active'
            """,
            (
                owner_id,
                runtime_epoch,
                fence_epoch,
                now,
                prior.execution_lease.run_id,
                prior.run_fence.owner_id,
                prior.run_fence.runtime_lease_epoch,
                prior.run_fence.epoch,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn RunFence takeover CAS failed")
        _fault(fault, "workflow:spawn_activation_reclaim:after_run_fence_write")
        if (
            workflow_namespace is not None
            and workflow_epoch is not None
            and prior_workflow_lease is not None
        ):
            changed = tx.connection.execute(
                """
                UPDATE workflow_leases SET owner_id=?,epoch=?,expires_at=?
                WHERE run_id=? AND namespace=? AND owner_id=? AND epoch=?
                  AND expires_at<=?
                """,
                (
                    owner_id,
                    workflow_epoch,
                    expires_at,
                    prior.execution_lease.run_id,
                    workflow_namespace,
                    prior_workflow_lease.owner_id,
                    prior_workflow_lease.epoch,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn Workflow takeover CAS failed")
            _fault(fault, "workflow:spawn_activation_reclaim:after_workflow_lease_write")

        changed = tx.connection.execute(
            """
            UPDATE workflow_spawn_continuations
            SET owner_id=?,runtime_lease_epoch=?,run_fence_epoch=?,
                workflow_lease_epoch=?,claim_epoch=?,expires_at=?,
                version=version+1,updated_at=?
            WHERE operation_id=? AND state='claimed' AND version=?
            """,
            (
                owner_id,
                runtime_epoch,
                fence_epoch,
                workflow_epoch,
                next_claim_epoch,
                expires_at,
                now,
                prior.ready_receipt.spawn_operation_id,
                prior.continuation_claim.version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn continuation takeover CAS failed")
        _fault(fault, "workflow:spawn_activation_reclaim:after_continuation_write")

        superseded_version = prior.activation_version + 1
        superseded_hash = _workflow_spawn_activation_hash(
            activation_receipt_id=prior.activation_receipt_id,
            ready_receipt_id=prior.ready_receipt.ready_receipt_id,
            spawn_operation_id=prior.ready_receipt.spawn_operation_id,
            parent_run_id=prior.execution_lease.run_id,
            effect_id=prior.ready_receipt.effect_id,
            owner_id=prior.execution_lease.owner_id,
            runtime_lease_epoch=prior.execution_lease.epoch,
            run_fence_epoch=prior.run_fence.epoch,
            workflow_lease_epoch=(
                None if prior.workflow_lease is None else prior.workflow_lease.epoch
            ),
            continuation_claim_epoch=prior.continuation_claim.claim_epoch,
            predecessor_activation_receipt_id=(prior.predecessor_activation_receipt_id),
            version=superseded_version,
        )
        changed = tx.connection.execute(
            """
            UPDATE workflow_spawn_ready_activations
            SET state='superseded',version=?,canonical_hash=?,superseded_at=?
            WHERE activation_receipt_id=? AND state='active' AND version=?
            """,
            (
                superseded_version,
                superseded_hash,
                now,
                prior.activation_receipt_id,
                prior.activation_version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn predecessor supersede CAS failed")
        _fault(fault, "workflow:spawn_activation_reclaim:after_predecessor_write")

        successor_hash = _workflow_spawn_activation_hash(
            activation_receipt_id=activation_receipt_id,
            ready_receipt_id=prior.ready_receipt.ready_receipt_id,
            spawn_operation_id=prior.ready_receipt.spawn_operation_id,
            parent_run_id=prior.execution_lease.run_id,
            effect_id=prior.ready_receipt.effect_id,
            owner_id=owner_id,
            runtime_lease_epoch=runtime_epoch,
            run_fence_epoch=fence_epoch,
            workflow_lease_epoch=workflow_epoch,
            continuation_claim_epoch=next_claim_epoch,
            predecessor_activation_receipt_id=prior.activation_receipt_id,
            version=activation_version,
        )
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_ready_activations(
                activation_receipt_id,ready_receipt_id,spawn_operation_id,
                parent_run_id,effect_id,owner_id,runtime_lease_epoch,
                run_fence_epoch,workflow_lease_epoch,continuation_claim_epoch,
                predecessor_activation_receipt_id,state,version,canonical_hash,
                created_at,superseded_at,consumed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,NULL,NULL)
            """,
            (
                activation_receipt_id,
                prior.ready_receipt.ready_receipt_id,
                prior.ready_receipt.spawn_operation_id,
                prior.execution_lease.run_id,
                prior.ready_receipt.effect_id,
                owner_id,
                runtime_epoch,
                fence_epoch,
                workflow_epoch,
                next_claim_epoch,
                prior.activation_receipt_id,
                activation_version,
                successor_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_activation_reclaim:after_successor_write")
        tx.connection.execute(
            "UPDATE runs SET version=version+1,updated_at=? WHERE run_id=? AND state='running'",
            (now, prior.execution_lease.run_id),
        )
        self._insert_event(
            tx.connection,
            event_id=f"{prior.execution_lease.run_id}:spawn-ready:{activation_receipt_id}",
            run_id=prior.execution_lease.run_id,
            kind="run.recovered",
            payload={
                "owner_id": owner_id,
                "lease_epoch": runtime_epoch,
                "spawn_operation_id": prior.ready_receipt.spawn_operation_id,
                "predecessor_activation_receipt_id": prior.activation_receipt_id,
            },
            now=now,
        )
        tx.register_after_commit_fault("workflow:spawn_activation_reclaim:after_commit")
        successor = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
            (activation_receipt_id,),
        ).fetchone()
        assert successor is not None
        return _workflow_spawn_ready_activation(tx.connection, successor)

    @staticmethod
    def _require_matching_spawn_ready(
        connection: sqlite3.Connection,
        continuation: sqlite3.Row,
        ready: WorkflowSpawnContinuationReady,
    ) -> None:
        durable = connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
            (ready.ready_receipt_id,),
        ).fetchone()
        if (
            durable is None
            or _workflow_spawn_continuation_ready(durable) != ready
            or ready.spawn_operation_id != str(continuation["operation_id"])
            or ready.ticket_receipt_id != str(continuation["ticket_receipt_id"])
            or ready.effect_id != str(continuation["effect_id"])
            or ready.handoff_attempt != int(continuation["handoff_attempt"])
            or durable["consumed_at"] is not None
        ):
            raise UnitOfWorkConflict("workflow spawn ready evidence differs")

    async def read_spawn_continuation_outcome(
        self,
        transaction: WorkflowTransaction,
        spawn_operation_id: str,
    ) -> ToolResult | None:
        tx = self._assert_open_workflow_transaction(transaction)
        spawn_operation_id = _required(spawn_operation_id, "spawn_operation_id")
        continuation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (spawn_operation_id,),
        ).fetchone()
        completion = tx.connection.execute(
            "SELECT * FROM workflow_spawn_completion_receipts WHERE spawn_operation_id=?",
            (spawn_operation_id,),
        ).fetchone()
        if continuation is None:
            if completion is not None:
                raise UnitOfWorkConflict("workflow spawn completion lacks its continuation")
            return None
        pointer = continuation["completion_receipt_id"]
        if pointer is None and completion is None:
            return None
        if (
            pointer is None
            or completion is None
            or str(pointer) != str(completion["completion_receipt_id"])
            or str(continuation["state"]) != "completed"
            or str(continuation["completion_path_kind"]) != str(completion["path_kind"])
        ):
            raise UnitOfWorkConflict("workflow spawn completion pointer differs")

        ticket = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (str(continuation["ticket_receipt_id"]),),
        ).fetchone()
        if ticket is None:
            raise UnitOfWorkConflict("workflow spawn completion ticket is missing")
        payload = json.loads(str(ticket["canonical_payload"]))
        if not isinstance(payload, dict):
            raise UnitOfWorkConflict("workflow spawn completion ticket is malformed")
        canonical_payload = canonical_json(payload)
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        request = _workflow_launch_request_from_json(payload.get("request"))
        if (
            request.request_key != spawn_operation_id
            or str(ticket["request_key"]) != spawn_operation_id
            or str(ticket["canonical_payload"]) != canonical_payload
            or str(ticket["payload_hash"]) != payload_hash
            or str(ticket["ticket_id"])
            != self._derived_id("workflow-launch/ticket/v1", payload_hash)
        ):
            raise UnitOfWorkConflict("workflow spawn completion ticket hash differs")

        completion_hash = _workflow_spawn_completion_hash_from_row(completion)
        if completion_hash != str(completion["canonical_hash"]):
            raise UnitOfWorkConflict("workflow spawn completion hash differs")
        if (
            str(completion["ticket_receipt_id"]) != str(ticket["ticket_receipt_id"])
            or str(completion["parent_run_id"]) != str(continuation["parent_run_id"])
            or str(completion["effect_id"]) != str(continuation["effect_id"])
            or int(completion["handoff_attempt"]) != int(continuation["handoff_attempt"])
            or str(completion["effect_request_hash"]) != str(continuation["effect_request_hash"])
            or str(completion["issue_authority_hash"]) != str(continuation["issue_authority_hash"])
        ):
            raise UnitOfWorkConflict("workflow spawn completion identity differs")

        result = _tool_result(completion["tool_result_json"])
        effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (str(completion["effect_id"]),),
        ).fetchone()
        if result is None or effect is None:
            raise UnitOfWorkConflict("workflow spawn completion Effect is missing")
        result_json = _tool_result_json(result)
        if (
            str(completion["tool_result_json"]) != result_json
            or str(completion["tool_result_hash"])
            != hashlib.sha256(result_json.encode()).hexdigest()
            or str(effect["state"]) != result.outcome.value
            or str(effect["result_json"]) != result_json
            or str(effect["call_id"]) != result.call_id.value
            or str(effect["request_hash"]) != str(completion["effect_request_hash"])
            or int(effect["handoff_attempt"]) != int(completion["handoff_attempt"])
            or (
                completion["failure_evidence_id"] is not None
                and effect["evidence_ref"] != completion["failure_evidence_id"]
            )
        ):
            raise UnitOfWorkConflict("workflow spawn completion Effect differs")

        path_kind = str(completion["path_kind"])
        ready_count = int(
            tx.connection.execute(
                "SELECT COUNT(*) FROM workflow_spawn_continuation_ready WHERE operation_id=?",
                (spawn_operation_id,),
            ).fetchone()[0]
        )
        activation_count = int(
            tx.connection.execute(
                "SELECT COUNT(*) FROM workflow_spawn_ready_activations WHERE spawn_operation_id=?",
                (spawn_operation_id,),
            ).fetchone()[0]
        )
        if path_kind == "direct":
            if ready_count != 0 or activation_count != 0:
                raise UnitOfWorkConflict("workflow spawn direct completion has recovery rows")
        elif path_kind in {"ready_recovery", "parent_terminal_activated"}:
            ready_row = tx.connection.execute(
                "SELECT * FROM workflow_spawn_continuation_ready WHERE operation_id=?",
                (spawn_operation_id,),
            ).fetchone()
            activation_rows = tx.connection.execute(
                """
                SELECT * FROM workflow_spawn_ready_activations
                WHERE spawn_operation_id=? ORDER BY created_at,activation_receipt_id
                """,
                (spawn_operation_id,),
            ).fetchall()
            if (
                ready_count != 1
                or ready_row is None
                or ready_row["consumed_at"] is None
                or not activation_rows
                or str(ready_row["ticket_receipt_id"]) != str(completion["ticket_receipt_id"])
                or str(ready_row["effect_id"]) != str(completion["effect_id"])
                or int(ready_row["handoff_attempt"]) != int(completion["handoff_attempt"])
                or str(completion["activation_chain_head_id"])
                != str(activation_rows[-1]["activation_receipt_id"])
            ):
                raise UnitOfWorkConflict("workflow spawn recovery completion chain differs")
            predecessor: str | None = None
            for index, activation in enumerate(activation_rows):
                if (
                    str(activation["ready_receipt_id"]) != str(ready_row["ready_receipt_id"])
                    or str(activation["spawn_operation_id"]) != spawn_operation_id
                    or str(activation["parent_run_id"]) != str(completion["parent_run_id"])
                    or str(activation["effect_id"]) != str(completion["effect_id"])
                    or activation["predecessor_activation_receipt_id"] != predecessor
                    or str(activation["canonical_hash"])
                    != _workflow_spawn_activation_hash(
                        activation_receipt_id=str(activation["activation_receipt_id"]),
                        ready_receipt_id=str(activation["ready_receipt_id"]),
                        spawn_operation_id=str(activation["spawn_operation_id"]),
                        parent_run_id=str(activation["parent_run_id"]),
                        effect_id=str(activation["effect_id"]),
                        owner_id=str(activation["owner_id"]),
                        runtime_lease_epoch=int(activation["runtime_lease_epoch"]),
                        run_fence_epoch=int(activation["run_fence_epoch"]),
                        workflow_lease_epoch=(
                            None
                            if activation["workflow_lease_epoch"] is None
                            else int(activation["workflow_lease_epoch"])
                        ),
                        continuation_claim_epoch=int(activation["continuation_claim_epoch"]),
                        predecessor_activation_receipt_id=predecessor,
                        version=int(activation["version"]),
                    )
                    or (
                        index < len(activation_rows) - 1
                        and str(activation["state"]) != "superseded"
                    )
                    or (
                        index == len(activation_rows) - 1 and str(activation["state"]) != "consumed"
                    )
                ):
                    raise UnitOfWorkConflict("workflow spawn recovery activation chain differs")
                predecessor = str(activation["activation_receipt_id"])
        elif path_kind == "parent_terminal_ticket_only":
            if (
                ready_count != 0
                or activation_count != 0
                or completion["activation_chain_head_id"] is not None
            ):
                raise UnitOfWorkConflict("workflow spawn terminal ticket-only chain differs")
        elif path_kind == "parent_terminal_ready_unactivated":
            ready_row = tx.connection.execute(
                "SELECT * FROM workflow_spawn_continuation_ready WHERE operation_id=?",
                (spawn_operation_id,),
            ).fetchone()
            blocker = (
                None
                if ready_row is None
                else tx.connection.execute(
                    "SELECT * FROM run_wait_blockers WHERE run_id=? "
                    "AND kind='tool' AND ledger_identity=? AND handoff_attempt=?",
                    (
                        str(completion["parent_run_id"]),
                        str(ready_row["effect_id"]),
                        int(ready_row["handoff_attempt"]),
                    ),
                ).fetchone()
            )
            if (
                ready_count != 1
                or activation_count != 0
                or ready_row is None
                or ready_row["consumed_at"] is None
                or str(ready_row["ticket_receipt_id"]) != str(completion["ticket_receipt_id"])
                or str(ready_row["effect_id"]) != str(completion["effect_id"])
                or int(ready_row["handoff_attempt"]) != int(completion["handoff_attempt"])
                or completion["activation_chain_head_id"] is not None
                or blocker is None
                or int(blocker["wake_consumed"]) != 1
                or blocker["consumed_at"] is None
                or str(blocker["superseded_by"]) != str(ready_row["ready_receipt_id"])
            ):
                raise UnitOfWorkConflict("workflow spawn unactivated terminal chain differs")
        else:
            raise UnitOfWorkConflict("workflow spawn completion path is unsupported")

        child_start_receipt_id = completion["child_runtime_start_receipt_id"]
        child_wait_receipt_id = completion["child_wait_receipt_id"]
        if child_start_receipt_id is not None or child_wait_receipt_id is not None:
            if (
                child_start_receipt_id is None
                or child_wait_receipt_id is None
                or result.outcome is not ToolOutcome.SUCCEEDED
                or completion["failure_evidence_kind"] is not None
                or completion["failure_evidence_id"] is not None
                or completion["failure_evidence_json"] is not None
                or completion["failure_evidence_hash"] is not None
                or not isinstance(result.value, Mapping)
            ):
                raise UnitOfWorkConflict("workflow spawn success completion shape differs")
            wait = tx.connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts WHERE parent_wait_receipt_id=?",
                (str(child_wait_receipt_id),),
            ).fetchone()
            start_receipt = tx.connection.execute(
                "SELECT * FROM runtime_start_receipts WHERE ticket_receipt_id=?",
                (str(child_start_receipt_id),),
            ).fetchone()
            child = (
                None
                if start_receipt is None
                else tx.connection.execute(
                    "SELECT * FROM runs WHERE run_id=?",
                    (str(start_receipt["run_id"]),),
                ).fetchone()
            )
            command = tx.connection.execute(
                "SELECT * FROM child_commands WHERE workflow_ticket_receipt_id=?",
                (str(child_start_receipt_id),),
            ).fetchone()
            link = (
                None
                if child is None
                else tx.connection.execute(
                    "SELECT * FROM run_links WHERE parent_run_id=? AND child_run_id=?",
                    (str(completion["parent_run_id"]), str(child["run_id"])),
                ).fetchone()
            )
            parent = tx.connection.execute(
                "SELECT state,version FROM runs WHERE run_id=?",
                (str(completion["parent_run_id"]),),
            ).fetchone()
            if (
                wait is None
                or start_receipt is None
                or child is None
                or command is None
                or link is None
                or parent is None
                or str(wait["spawn_operation_id"]) != spawn_operation_id
                or str(wait["parent_run_id"]) != str(completion["parent_run_id"])
                or str(wait["child_run_id"]) != str(child["run_id"])
                or str(wait["child_command_id"]) != str(command["command_id"])
                or str(command["parent_run_id"]) != str(completion["parent_run_id"])
                or str(command["child_run_id"]) != str(child["run_id"])
                or str(command["state"]) not in {"pending", "scheduled", "acked"}
                or str(link["attachment_policy"]) != AttachmentPolicy.ATTACHED.value
                or str(child["parent_run_id"]) != str(completion["parent_run_id"])
                or str(child["root_run_id"]) != request.root_run_id
            ):
                raise UnitOfWorkConflict("workflow spawn success durable chain differs")
            wait_state = str(wait["state"])
            claimed_continuation: sqlite3.Row | None = None
            expected_lifecycle_hash: str | None
            if wait_state == "unconsumed":
                if (
                    wait["child_signal_id"] is not None
                    or wait["continuation_id"] is not None
                    or str(parent["state"]) != RunState.WAITING.value
                    or int(parent["version"]) != int(wait["parent_waiting_version"])
                    or tx.connection.execute(
                        "SELECT 1 FROM workflow_leases WHERE run_id=? LIMIT 1",
                        (str(completion["parent_run_id"]),),
                    ).fetchone()
                    is not None
                ):
                    raise UnitOfWorkConflict("workflow spawn unconsumed child-wait differs")
            elif wait_state == "woken":
                signal = tx.connection.execute(
                    "SELECT * FROM child_signals WHERE signal_id=?",
                    (wait["child_signal_id"],),
                ).fetchone()
                continuation = tx.connection.execute(
                    "SELECT * FROM continuations WHERE continuation_id=?",
                    (wait["continuation_id"],),
                ).fetchone()
                expected_lifecycle_hash = hashlib.sha256(
                    canonical_json(
                        {
                            "identity_hash": str(wait["identity_hash"]),
                            "state": "woken",
                            "version": int(wait["version"]),
                            "child_signal_id": wait["child_signal_id"],
                            "continuation_id": wait["continuation_id"],
                        }
                    ).encode()
                ).hexdigest()
                if (
                    signal is None
                    or continuation is None
                    or str(signal["state"]) != "acked"
                    or str(signal["parent_run_id"]) != str(completion["parent_run_id"])
                    or str(signal["child_run_id"]) != str(child["run_id"])
                    or str(continuation["run_id"]) != str(completion["parent_run_id"])
                    or str(continuation["state"]) != "pending"
                    or str(parent["state"]) != RunState.QUEUED.value
                    or str(wait["lifecycle_hash"]) != expected_lifecycle_hash
                ):
                    raise UnitOfWorkConflict("workflow spawn woken child-wait differs")
            elif wait_state == "claimed":
                signal = tx.connection.execute(
                    "SELECT * FROM child_signals WHERE signal_id=?",
                    (wait["child_signal_id"],),
                ).fetchone()
                continuation = tx.connection.execute(
                    "SELECT * FROM continuations WHERE continuation_id=?",
                    (wait["continuation_id"],),
                ).fetchone()
                claimed_continuation = continuation
                runtime_lease = tx.connection.execute(
                    "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
                    (str(completion["parent_run_id"]), RUNTIME_LEASE_NAMESPACE),
                ).fetchone()
                expected_lifecycle_hash = (
                    None
                    if continuation is None
                    else hashlib.sha256(
                        canonical_json(
                            {
                                "identity_hash": str(wait["identity_hash"]),
                                "state": "claimed",
                                "version": int(wait["version"]),
                                "child_signal_id": wait["child_signal_id"],
                                "continuation_id": wait["continuation_id"],
                                "claim_owner": continuation["claimed_by"],
                                "runtime_lease_epoch": continuation["runtime_lease_epoch"],
                            }
                        ).encode()
                    ).hexdigest()
                )
                if (
                    signal is None
                    or continuation is None
                    or runtime_lease is None
                    or str(signal["state"]) != "acked"
                    or str(continuation["state"]) != "claimed"
                    or str(continuation["run_id"]) != str(completion["parent_run_id"])
                    or str(runtime_lease["owner_id"]) != str(continuation["claimed_by"])
                    or int(runtime_lease["epoch"]) != int(continuation["runtime_lease_epoch"])
                    or str(parent["state"]) != RunState.RUNNING.value
                    or str(wait["lifecycle_hash"]) != expected_lifecycle_hash
                ):
                    raise UnitOfWorkConflict("workflow spawn claimed child-wait differs")
            elif wait_state in {"acked_completion_pending", "acked"}:
                signal = tx.connection.execute(
                    "SELECT * FROM child_signals WHERE signal_id=?",
                    (wait["child_signal_id"],),
                ).fetchone()
                continuation = tx.connection.execute(
                    "SELECT * FROM continuations WHERE continuation_id=?",
                    (wait["continuation_id"],),
                ).fetchone()
                progress = tx.connection.execute(
                    "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
                    (wait["progress_receipt_id"],),
                ).fetchone()
                claimed_continuation = continuation
                pending_json = wait["pending_child_completion_json"]
                pending_hash = wait["pending_child_completion_hash"]
                if (
                    signal is None
                    or continuation is None
                    or progress is None
                    or str(signal["state"]) != "acked"
                    or str(continuation["state"]) != "acked"
                    or continuation["ack_receipt_id"] != wait["progress_receipt_id"]
                    or str(progress["continuation_id"]) != str(continuation["continuation_id"])
                    or str(progress["owner_id"]) != str(continuation["claimed_by"])
                    or int(progress["runtime_lease_epoch"])
                    != int(continuation["runtime_lease_epoch"])
                    or int(progress["claim_epoch"]) != int(continuation["claim_epoch"])
                    or pending_json is None
                    or pending_hash is None
                    or hashlib.sha256(str(pending_json).encode()).hexdigest() != str(pending_hash)
                ):
                    raise UnitOfWorkConflict("workflow spawn acknowledged child-wait differs")
                if wait_state == "acked_completion_pending":
                    expected_lifecycle_hash = hashlib.sha256(
                        canonical_json(
                            {
                                "identity_hash": str(wait["identity_hash"]),
                                "state": "acked_completion_pending",
                                "version": int(wait["version"]),
                                "child_signal_id": wait["child_signal_id"],
                                "continuation_id": wait["continuation_id"],
                                "progress_receipt_id": wait["progress_receipt_id"],
                                "pending_child_completion_hash": pending_hash,
                            }
                        ).encode()
                    ).hexdigest()
                    if (
                        wait["child_completion_append_receipt_id"] is not None
                        or wait["child_completion_context_revision"] is not None
                        or str(parent["state"]) != RunState.RUNNING.value
                        or str(wait["lifecycle_hash"]) != expected_lifecycle_hash
                    ):
                        raise UnitOfWorkConflict("workflow spawn pending completion differs")
                else:
                    expected_lifecycle_hash = hashlib.sha256(
                        canonical_json(
                            {
                                "identity_hash": str(wait["identity_hash"]),
                                "state": "acked",
                                "version": int(wait["version"]),
                                "progress_receipt_id": wait["progress_receipt_id"],
                                "child_completion_append_id": wait["child_completion_append_id"],
                                "child_completion_context_revision": wait[
                                    "child_completion_context_revision"
                                ],
                            }
                        ).encode()
                    ).hexdigest()
                    appended_context = tx.connection.execute(
                        "SELECT checkpoint_json FROM workflow_checkpoints "
                        "WHERE run_id=? AND namespace='react.context.v1' "
                        "AND version=?",
                        (
                            str(completion["parent_run_id"]),
                            wait["child_completion_context_revision"],
                        ),
                    ).fetchone()
                    appended_payload = (
                        None
                        if appended_context is None
                        else json.loads(str(appended_context["checkpoint_json"]))
                    )
                    append_receipts = (
                        None
                        if not isinstance(appended_payload, dict)
                        else appended_payload.get("append_receipts")
                    )
                    if (
                        wait["child_completion_append_receipt_id"]
                        != wait["child_completion_append_id"]
                        or not isinstance(append_receipts, dict)
                        or wait["child_completion_append_id"] not in append_receipts
                        or str(wait["lifecycle_hash"]) != expected_lifecycle_hash
                    ):
                        raise UnitOfWorkConflict("workflow spawn completed Context append differs")
            elif wait_state == "acked_parent_terminal":
                signal = tx.connection.execute(
                    "SELECT * FROM child_signals WHERE signal_id=?",
                    (wait["child_signal_id"],),
                ).fetchone()
                continuation = tx.connection.execute(
                    "SELECT * FROM continuations WHERE continuation_id=?",
                    (wait["continuation_id"],),
                ).fetchone()
                progress = tx.connection.execute(
                    "SELECT * FROM continuation_progress_receipts WHERE receipt_id=?",
                    (wait["progress_receipt_id"],),
                ).fetchone()
                terminal_event = tx.connection.execute(
                    "SELECT kind FROM run_events WHERE event_id=? AND run_id=?",
                    (
                        wait["pending_completion_terminal_receipt_id"],
                        str(completion["parent_run_id"]),
                    ),
                ).fetchone()
                pending_json = wait["pending_child_completion_json"]
                pending_hash = wait["pending_child_completion_hash"]
                terminal_state = wait["pending_completion_terminal_state"]
                terminal_receipt_id = wait["pending_completion_terminal_receipt_id"]
                phase_kind = wait["parent_terminal_phase_kind"]
                claimed_ack = wait["claimed_continuation_terminal_ack_receipt_id"]
                if phase_kind not in {
                    "child_active",
                    "signal_pending",
                    "continuation_claimed",
                    "completion_pending",
                }:
                    raise UnitOfWorkConflict("workflow spawn parent-terminal phase differs")
                expected_terminal_hash = hashlib.sha256(
                    canonical_json(
                        {
                            "parent_wait_receipt_id": str(wait["parent_wait_receipt_id"]),
                            "spawn_operation_id": spawn_operation_id,
                            "phase_kind": phase_kind,
                            "terminal_receipt_id": terminal_receipt_id,
                            "terminal_state": terminal_state,
                            "child_signal_id": (
                                None if phase_kind == "child_active" else wait["child_signal_id"]
                            ),
                            "continuation_id": (
                                None if phase_kind == "child_active" else wait["continuation_id"]
                            ),
                            "pending_child_completion_hash": pending_hash,
                            "claimed_continuation_ack_receipt_id": claimed_ack,
                            "child_cancel_request_id": wait["child_cancel_request_id"],
                            "child_cancel_receipt_id": wait["child_cancel_receipt_id"],
                            "reused_child_cancel_receipt_id": wait[
                                "reused_child_cancel_receipt_id"
                            ],
                        }
                    ).encode()
                ).hexdigest()
                lifecycle_payload: dict[str, JsonValue] = {
                    "identity_hash": str(wait["identity_hash"]),
                    "state": "acked_parent_terminal",
                    "version": int(wait["version"]),
                    "phase_kind": phase_kind,
                    "terminal_receipt_id": terminal_receipt_id,
                    "terminal_state": terminal_state,
                    "terminal_hash": expected_terminal_hash,
                }
                if (
                    phase_kind == "child_active"
                    and wait["late_signal_quarantine_receipt_id"] is not None
                ):
                    lifecycle_payload["late_signal_quarantine_receipt_id"] = wait[
                        "late_signal_quarantine_receipt_id"
                    ]
                expected_lifecycle_hash = hashlib.sha256(
                    canonical_json(lifecycle_payload).encode()
                ).hexdigest()
                common_differs = (
                    terminal_state
                    not in {
                        RunState.COMPLETED.value,
                        RunState.FAILED.value,
                        RunState.CANCELLED.value,
                    }
                    or terminal_event is None
                    or str(parent["state"]) != str(terminal_state)
                    or str(terminal_event["kind"]) != f"run.{terminal_state}"
                    or str(wait["pending_completion_terminal_hash"]) != expected_terminal_hash
                    or str(wait["lifecycle_hash"]) != expected_lifecycle_hash
                )
                progress_differs = (
                    continuation is None
                    or progress is None
                    or str(continuation["state"]) != "acked"
                    or continuation["ack_receipt_id"] != wait["progress_receipt_id"]
                    or str(progress["continuation_id"]) != str(continuation["continuation_id"])
                )
                phase_differs = False
                if phase_kind == "child_active":
                    cancel_id = (
                        wait["child_cancel_receipt_id"]
                        if wait["child_cancel_receipt_id"] is not None
                        else wait["reused_child_cancel_receipt_id"]
                    )
                    cancel = tx.connection.execute(
                        "SELECT * FROM workflow_cancel_receipts WHERE cancel_id=?",
                        (cancel_id,),
                    ).fetchone()
                    late_receipt_id = wait["late_signal_quarantine_receipt_id"]
                    late_ack = (
                        None
                        if late_receipt_id is None
                        else tx.connection.execute(
                            "SELECT * FROM child_signal_ack_receipts WHERE receipt_id=?",
                            (late_receipt_id,),
                        ).fetchone()
                    )
                    late_event = (
                        None
                        if late_ack is None
                        else tx.connection.execute(
                            "SELECT kind FROM run_events WHERE event_id=?",
                            (late_ack["event_id"],),
                        ).fetchone()
                    )
                    cancel_is_terminal = cancel is not None and str(cancel["phase"]) == "terminal"
                    late_differs = (
                        signal is not None or continuation is not None
                        if late_receipt_id is None
                        else (
                            signal is None
                            or continuation is None
                            or late_ack is None
                            or late_event is None
                            or str(signal["state"]) != "acked"
                            or signal["ack_receipt_id"] != late_receipt_id
                            or str(continuation["state"]) != "quarantined"
                            or str(late_ack["signal_id"]) != str(signal["signal_id"])
                            or str(late_ack["continuation_id"])
                            != str(continuation["continuation_id"])
                            or str(late_event["kind"]) != "child.signal_quarantined"
                        )
                    )
                    phase_differs = (
                        late_differs
                        or progress is not None
                        or pending_json is not None
                        or pending_hash is not None
                        or cancel is None
                        or str(cancel["run_id"]) != str(child["run_id"])
                        or str(cancel["phase"])
                        not in {
                            "requested",
                            "cancelling",
                            "blocked",
                            "terminal",
                        }
                        or (cancel_is_terminal and str(child["state"]) != RunState.CANCELLED.value)
                        or (
                            not cancel_is_terminal
                            and str(child["state"]) != RunState.CANCEL_REQUESTED.value
                        )
                        or (
                            wait["child_cancel_receipt_id"] is not None
                            and wait["child_cancel_request_id"] != wait["child_cancel_receipt_id"]
                        )
                        or (
                            wait["child_cancel_receipt_id"] is None
                            and wait["reused_child_cancel_receipt_id"] is None
                        )
                        or (
                            wait["child_cancel_receipt_id"] is not None
                            and wait["reused_child_cancel_receipt_id"] is not None
                        )
                        or claimed_ack is not None
                    )
                elif phase_kind == "signal_pending":
                    phase_differs = (
                        signal is None
                        or continuation is None
                        or str(signal["state"]) != "acked"
                        or str(continuation["state"]) != "quarantined"
                        or progress is not None
                        or pending_json is not None
                        or pending_hash is not None
                        or wait["late_signal_quarantine_receipt_id"] != terminal_receipt_id
                        or claimed_ack is not None
                        or wait["child_cancel_request_id"] is not None
                        or wait["child_cancel_receipt_id"] is not None
                        or wait["reused_child_cancel_receipt_id"] is not None
                    )
                elif phase_kind == "continuation_claimed":
                    phase_differs = (
                        signal is None
                        or str(signal["state"]) != "acked"
                        or progress_differs
                        or pending_json is not None
                        or pending_hash is not None
                        or claimed_ack != wait["progress_receipt_id"]
                        or wait["late_signal_quarantine_receipt_id"] is not None
                        or wait["child_cancel_request_id"] is not None
                        or wait["child_cancel_receipt_id"] is not None
                        or wait["reused_child_cancel_receipt_id"] is not None
                    )
                else:
                    phase_differs = (
                        signal is None
                        or str(signal["state"]) != "acked"
                        or progress_differs
                        or pending_json is None
                        or pending_hash is None
                        or hashlib.sha256(str(pending_json).encode()).hexdigest()
                        != str(pending_hash)
                        or wait["child_completion_append_receipt_id"] is not None
                        or wait["child_completion_context_revision"] is not None
                        or wait["late_signal_quarantine_receipt_id"] is not None
                        or claimed_ack is not None
                        or wait["child_cancel_request_id"] is not None
                        or wait["child_cancel_receipt_id"] is not None
                        or wait["reused_child_cancel_receipt_id"] is not None
                    )
                if common_differs or phase_differs:
                    raise UnitOfWorkConflict("workflow spawn parent-terminal child-wait differs")
            else:
                raise UnitOfWorkConflict("workflow spawn child-wait lifecycle is unsupported")
            if str(command["state"]) == "acked":
                terminal = tx.connection.execute(
                    "SELECT * FROM child_terminal_receipts WHERE command_id=?",
                    (str(command["command_id"]),),
                ).fetchone()
                signal = (
                    None
                    if terminal is None or terminal["signal_id"] is None
                    else tx.connection.execute(
                        "SELECT * FROM child_signals WHERE signal_id=?",
                        (str(terminal["signal_id"]),),
                    ).fetchone()
                )
                if (
                    terminal is None
                    or signal is None
                    or str(terminal["child_run_id"]) != str(child["run_id"])
                    or str(terminal["terminal_state"]) != str(child["state"])
                    or str(signal["parent_run_id"]) != str(completion["parent_run_id"])
                    or str(signal["child_run_id"]) != str(child["run_id"])
                ):
                    raise UnitOfWorkConflict("workflow spawn child terminal successor differs")
            checkpoint = tx.connection.execute(
                "SELECT * FROM workflow_checkpoints WHERE run_id=? AND"
                " namespace='react.termination.v1' AND version=?",
                (
                    str(completion["parent_run_id"]),
                    int(wait["react_checkpoint_revision"]),
                ),
            ).fetchone()
            context = tx.connection.execute(
                "SELECT checkpoint_json FROM workflow_checkpoints WHERE run_id=? AND"
                " namespace='react.context.v1' AND version=?",
                (
                    str(completion["parent_run_id"]),
                    int(wait["context_post_revision"]),
                ),
            ).fetchone()
            fence = tx.connection.execute(
                "SELECT * FROM run_fences WHERE run_id=?",
                (str(completion["parent_run_id"]),),
            ).fetchone()
            historical_checkpoint_differs = (
                checkpoint is None
                or context is None
                or fence is None
                or str(checkpoint["checkpoint_hash"]) != str(wait["react_checkpoint_hash"])
                or hashlib.sha256(str(checkpoint["checkpoint_json"]).encode()).hexdigest()
                != str(checkpoint["checkpoint_hash"])
            )
            historical_fence_differs = wait_state in {"unconsumed", "woken"} and (
                fence is None
                or str(fence["state"]) != "released"
                or int(fence["runtime_lease_epoch"]) != int(wait["released_runtime_lease_epoch"])
                or int(fence["epoch"]) != int(wait["released_run_fence_epoch"])
            )
            current_fence_differs = wait_state in {"claimed", "acked_completion_pending"} and (
                fence is None
                or claimed_continuation is None
                or str(fence["state"]) != "active"
                or str(fence["owner_id"]) != str(claimed_continuation["claimed_by"])
                or int(fence["runtime_lease_epoch"])
                != int(claimed_continuation["runtime_lease_epoch"])
                or int(fence["runtime_lease_epoch"]) <= int(wait["released_runtime_lease_epoch"])
                or int(fence["epoch"]) <= int(wait["released_run_fence_epoch"])
            )
            if historical_checkpoint_differs or historical_fence_differs or current_fence_differs:
                raise UnitOfWorkConflict("workflow spawn success checkpoint differs")
            return result

        evidence = _workflow_spawn_failure_evidence(completion)
        evidence_hash = str(completion["failure_evidence_hash"])
        if str(completion["failure_evidence_id"]) != self._derived_id(
            "workflow-spawn/failure-evidence/v1", evidence_hash
        ):
            raise UnitOfWorkConflict("workflow spawn failure evidence identity differs")
        evidence_kind = evidence.get("kind")
        if evidence_kind == "catalog_stale":
            if (
                result.outcome is not ToolOutcome.FAILED
                or result.error_code != "workflow_catalog_stale"
                or evidence.get("ticket_catalog_generation") != int(ticket["catalog_generation"])
                or evidence.get("ticket_catalog_version")
                != int(ticket["catalog_authority_version"])
                or evidence.get("ticket_catalog_hash") != str(ticket["catalog_hash"])
                or (
                    evidence.get("observed_catalog_generation"),
                    evidence.get("observed_catalog_version"),
                    evidence.get("observed_catalog_hash"),
                )
                == (
                    int(ticket["catalog_generation"]),
                    int(ticket["catalog_authority_version"]),
                    str(ticket["catalog_hash"]),
                )
            ):
                raise UnitOfWorkConflict("workflow spawn catalog-stale evidence differs")
        elif evidence_kind == "graph_version_unavailable":
            activation = tx.connection.execute(
                "SELECT * FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
                (str(completion["activation_chain_head_id"]),),
            ).fetchone()
            observed_kind = evidence.get("observed_kind")
            observed_hash = evidence.get("observed_implementation_hash")
            if (
                result.outcome is not ToolOutcome.FAILED
                or result.error_code != "graph_version_unavailable"
                or activation is None
                or evidence.get("ticket_receipt_id") != str(ticket["ticket_receipt_id"])
                or evidence.get("profile_key") != str(ticket["profile_key"])
                or evidence.get("workflow_name") != str(ticket["workflow_name"])
                or evidence.get("workflow_version") != str(ticket["workflow_version"])
                or evidence.get("expected_implementation_hash")
                != str(ticket["implementation_fingerprint"])
                or evidence.get("activation_receipt_id") != str(activation["activation_receipt_id"])
                or evidence.get("parent_run_id") != str(activation["parent_run_id"])
                or evidence.get("owner_id") != str(activation["owner_id"])
                or evidence.get("runtime_lease_epoch") != int(activation["runtime_lease_epoch"])
                or evidence.get("run_fence_epoch") != int(activation["run_fence_epoch"])
                or evidence.get("workflow_lease_epoch")
                != (
                    None
                    if activation["workflow_lease_epoch"] is None
                    else int(activation["workflow_lease_epoch"])
                )
                or evidence.get("continuation_claim_epoch")
                != int(activation["continuation_claim_epoch"])
                or not isinstance(evidence.get("registry_content_digest"), str)
                or observed_kind not in {"missing", "drift"}
                or (observed_kind == "missing" and observed_hash is not None)
                or (
                    observed_kind == "drift"
                    and (
                        not isinstance(observed_hash, str)
                        or observed_hash == evidence.get("expected_implementation_hash")
                    )
                )
            ):
                raise UnitOfWorkConflict("workflow spawn graph-unavailable evidence differs")
        elif evidence_kind == "workflow_parent_terminal_before_spawn":
            from simple_harness.workflow.execution_ports import (
                WorkflowTerminalOutcome,
            )

            terminal = self._read_workflow_terminal_outcome(
                tx.connection, str(completion["parent_run_id"])
            )
            if (
                path_kind
                not in {
                    "parent_terminal_ticket_only",
                    "parent_terminal_ready_unactivated",
                    "parent_terminal_activated",
                }
                or result.outcome is not ToolOutcome.FAILED
                or result.error_code != "workflow_parent_terminal_before_spawn"
                or not isinstance(terminal, WorkflowTerminalOutcome)
                or not self._verify_workflow_terminal(tx.connection, terminal)
                or evidence.get("terminal_receipt_id") != terminal.receipt_id
                or evidence.get("terminal_state") != terminal.state
                or evidence.get("terminal_outcome_hash") != terminal.outcome_hash
            ):
                raise UnitOfWorkConflict("workflow spawn parent-terminal evidence differs")
        else:
            raise UnitOfWorkConflict("workflow spawn failure evidence is unsupported")
        return result

    async def read_spawn_admission_outcome(
        self,
        transaction: WorkflowTransaction,
        spawn_operation_id: str,
    ) -> WorkflowSpawnAdmissionOutcome | None:
        from simple_harness.runtime.workflow_spawn import (
            ChildStartDispatchRef,
            WorkflowChildWaitBinding,
            _create_workflow_spawn_admission_outcome,
            _create_workflow_spawn_result,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        result = await self.read_spawn_continuation_outcome(transaction, spawn_operation_id)
        if result is None:
            return None
        completion = tx.connection.execute(
            "SELECT * FROM workflow_spawn_completion_receipts WHERE spawn_operation_id=?",
            (spawn_operation_id,),
        ).fetchone()
        if (
            completion is None
            or completion["child_runtime_start_receipt_id"] is None
            or completion["child_wait_receipt_id"] is None
            or result.outcome is not ToolOutcome.SUCCEEDED
            or not isinstance(result.value, Mapping)
        ):
            return None
        wait = tx.connection.execute(
            "SELECT * FROM workflow_spawn_child_wait_receipts WHERE parent_wait_receipt_id=?",
            (str(completion["child_wait_receipt_id"]),),
        ).fetchone()
        if wait is None:
            raise UnitOfWorkConflict("workflow spawn child-wait receipt is missing")
        value = thaw_json(result.value)
        if not isinstance(value, dict):
            raise UnitOfWorkConflict("workflow spawn result payload is malformed")
        spawn_result = _create_workflow_spawn_result(
            schema_version=value.get("schema_version"),
            child_run_id=value.get("child_run_id"),
            child_request_id=value.get("child_request_id"),
            parent_run_id=value.get("parent_run_id"),
            root_run_id=value.get("root_run_id"),
            ticket_receipt_id=value.get("ticket_receipt_id"),
            runtime_start_receipt_id=value.get("runtime_start_receipt_id"),
            child_command_id=value.get("child_command_id"),
            attachment_policy=AttachmentPolicy(cast(str, value.get("attachment_policy"))),
        )
        if (
            spawn_result.child_run_id != str(wait["child_run_id"])
            or spawn_result.parent_run_id != str(wait["parent_run_id"])
            or spawn_result.child_command_id != str(wait["child_command_id"])
            or spawn_result.runtime_start_receipt_id
            != str(completion["child_runtime_start_receipt_id"])
        ):
            raise UnitOfWorkConflict("workflow spawn admission result differs")
        child_start_ref = ChildStartDispatchRef(
            child_start_receipt_id=str(wait["child_start_receipt_id"]),
            child_dispatch_claim_id=str(wait["child_dispatch_claim_id"]),
            child_run_id=str(wait["child_run_id"]),
        )
        suspension = WorkflowChildWaitBinding(
            parent_run_id=str(wait["parent_run_id"]),
            child_run_id=str(wait["child_run_id"]),
            child_command_id=str(wait["child_command_id"]),
            parent_wait_receipt_id=str(wait["parent_wait_receipt_id"]),
            expected_parent_version=int(wait["parent_waiting_version"]),
            react_checkpoint_revision=int(wait["react_checkpoint_revision"]),
            expected_signal_domain=str(wait["expected_signal_domain"]),
            source_phase=str(wait["source_phase"]),
            batch_digest=str(wait["batch_digest"]),
            spawn_ordinal=int(wait["spawn_ordinal"]),
            next_tool_ordinal=int(wait["next_tool_ordinal"]),
            spawn_result_append_receipt_id=str(wait["spawn_result_append_receipt_id"]),
            context_revision=int(wait["context_post_revision"]),
            termination_started_at=float(wait["termination_started_at"]),
            termination_last_observed_at=float(wait["termination_last_observed_at"]),
            wall_deadline=(None if wait["wall_deadline"] is None else float(wait["wall_deadline"])),
            termination_policy_snapshot_hash=str(wait["termination_policy_snapshot_hash"]),
        )
        return _create_workflow_spawn_admission_outcome(
            child_start_ref=child_start_ref,
            result=spawn_result,
            suspension=suspension,
        )

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
    ) -> WorkflowSpawnToolOutcome:
        from simple_harness.contracts.messages import Message, MessageRole
        from simple_harness.runtime.context import _append_context_in_transaction
        from simple_harness.runtime.orchestration import RuntimeStartDisposition
        from simple_harness.runtime.workflow_spawn import (
            ChildStartDispatchRef,
            WorkflowChildWaitBinding,
            WorkflowSpawnChildControlKind,
            _create_workflow_spawn_child_control,
            _create_workflow_spawn_result,
            _create_workflow_spawn_tool_outcome,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        if (
            await self.read_spawn_admission_outcome(transaction, continuation.spawn_operation_id)
            is not None
        ):
            raise UnitOfWorkConflict(
                "workflow spawn admission already committed; use receipt reader"
            )
        now = _time(now)
        continuation_row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (continuation.spawn_operation_id,),
        ).fetchone()
        if (
            continuation_row is None
            or str(continuation_row["state"]) != "claimed"
            or _workflow_spawn_continuation_claim(continuation_row) != continuation
            or continuation.expires_at <= now
            or continuation.ticket_receipt_id != ticket.ticket_receipt_id
        ):
            raise UnitOfWorkConflict("workflow spawn continuation claim differs")
        parent = tx.connection.execute(
            "SELECT * FROM runs WHERE run_id=?",
            (continuation.parent_run_id,),
        ).fetchone()
        runtime = tx.connection.execute(
            "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
            (continuation.parent_run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        fence = tx.connection.execute(
            "SELECT * FROM run_fences WHERE run_id=?",
            (continuation.parent_run_id,),
        ).fetchone()
        if (
            parent is None
            or str(parent["state"]) != RunState.RUNNING.value
            or runtime is None
            or fence is None
            or str(runtime["owner_id"]) != continuation.owner_id
            or int(runtime["epoch"]) != continuation.runtime_lease_epoch
            or float(runtime["expires_at"]) <= now
            or str(fence["state"]) != "active"
            or str(fence["owner_id"]) != continuation.owner_id
            or int(fence["runtime_lease_epoch"]) != continuation.runtime_lease_epoch
            or int(fence["epoch"]) != continuation.run_fence_epoch
        ):
            raise UnitOfWorkConflict("workflow spawn parent authority differs")
        active_activation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations "
            "WHERE spawn_operation_id=? AND state='active'",
            (continuation.spawn_operation_id,),
        ).fetchone()
        path_kind = "direct"
        activation_chain_head_id: str | None = None
        if active_activation is not None:
            durable_activation = _workflow_spawn_ready_activation(tx.connection, active_activation)
            if durable_activation.continuation_claim != continuation:
                raise UnitOfWorkConflict("workflow spawn recovery activation claim differs")
            ready = tx.connection.execute(
                "SELECT consumed_at FROM workflow_spawn_continuation_ready "
                "WHERE ready_receipt_id=?",
                (durable_activation.ready_receipt.ready_receipt_id,),
            ).fetchone()
            if ready is None or ready["consumed_at"] is None:
                raise UnitOfWorkConflict("workflow spawn recovery ready receipt is unconsumed")
            path_kind = "ready_recovery"
            activation_chain_head_id = durable_activation.activation_receipt_id

        admission = await self.admit_runtime_start(
            transaction,
            ticket,
            start,
            request,
            snapshot,
            claim,
            now=now,
            fault=fault,
        )
        if (
            admission.disposition
            not in {
                RuntimeStartDisposition.START_NEW,
                RuntimeStartDisposition.START_ORPHAN,
            }
            or admission.dispatch_claim is None
        ):
            raise UnitOfWorkConflict("direct workflow spawn requires a fresh child start authority")
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (str(continuation_row["effect_id"]),),
        ).fetchone()
        checkpoint = tx.connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE run_id=? AND namespace='react.termination.v1'"
            " ORDER BY version DESC LIMIT 1",
            (continuation.parent_run_id,),
        ).fetchone()
        if ticket_row is None or effect is None or checkpoint is None:
            raise UnitOfWorkConflict("workflow spawn success authority is missing")
        checkpoint_json = str(checkpoint["checkpoint_json"])
        checkpoint_value = json.loads(checkpoint_json)
        if (
            not isinstance(checkpoint_value, dict)
            or checkpoint_value.get("phase") != "tool_batch_reserved"
            or int(checkpoint["version"]) != 0
            or str(effect["state"]) not in {EffectState.HANDED_OFF.value, EffectState.UNKNOWN.value}
            or str(effect["run_id"]) != continuation.parent_run_id
        ):
            raise UnitOfWorkConflict("workflow spawn success phase differs")
        spawn_result = _create_workflow_spawn_result(
            schema_version="workflow_spawn.result.v1",
            child_run_id=admission.receipt.run_id,
            child_request_id=start.request_id.value,
            parent_run_id=continuation.parent_run_id,
            root_run_id=str(parent["root_run_id"]),
            ticket_receipt_id=ticket.ticket_receipt_id,
            runtime_start_receipt_id=admission.receipt.ticket_receipt_id,
            child_command_id=str(ticket_row["child_command_id"]),
            attachment_policy=AttachmentPolicy.ATTACHED,
        )
        tool_result = ToolResult.succeeded(CallId(str(effect["call_id"])), spawn_result.to_json())
        tool_result_json = _tool_result_json(tool_result)
        tool_result_hash = hashlib.sha256(tool_result_json.encode()).hexdigest()
        append_id = self._derived_id(
            "workflow-spawn/context-append/v1", continuation.spawn_operation_id
        )
        context_pre_revision = checkpoint_value.get("context_revision")
        if isinstance(context_pre_revision, bool) or not isinstance(context_pre_revision, int):
            raise UnitOfWorkConflict("workflow spawn Context revision is missing")
        parent_lease = ExecutionLease(
            continuation.parent_run_id,
            RUNTIME_LEASE_NAMESPACE,
            continuation.owner_id,
            continuation.runtime_lease_epoch,
            float(runtime["expires_at"]),
        )
        context = _append_context_in_transaction(
            tx.connection,
            RunId(continuation.parent_run_id),
            parent_lease,
            context_pre_revision,
            append_id,
            (
                Message(
                    MessageRole.TOOL,
                    canonical_json(spawn_result.to_json()),
                    name="workflow_spawn",
                    call_id=CallId(str(effect["raw_call_id"])),
                ),
            ),
            now=now,
        )
        _fault(fault, "workflow:spawn_admission:after_context_write")
        batch_digest = hashlib.sha256(checkpoint_json.encode()).hexdigest()
        next_checkpoint = dict(checkpoint_value)
        next_checkpoint.update(
            {
                "phase": "child_wait",
                "context_revision": context.revision,
                "tool_result_progress": int(effect["call_ordinal"]) + 1,
                "workflow_spawn_operation_id": continuation.spawn_operation_id,
                "workflow_spawn_child_run_id": admission.receipt.run_id,
            }
        )
        next_checkpoint_json = canonical_json(cast(JsonValue, next_checkpoint))
        next_checkpoint_hash = hashlib.sha256(next_checkpoint_json.encode()).hexdigest()
        next_checkpoint_version = int(checkpoint["version"]) + 1
        tx.connection.execute(
            "INSERT INTO"
            " workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                f"{continuation.parent_run_id}:react.termination.v1:{next_checkpoint_version}",
                continuation.parent_run_id,
                "react.termination.v1",
                next_checkpoint_json,
                next_checkpoint_hash,
                continuation.runtime_lease_epoch,
                next_checkpoint_version,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_admission:after_checkpoint_write")
        parent_waiting_version = int(parent["version"]) + 1
        changed = tx.connection.execute(
            "UPDATE runs SET state='waiting',version=version+1,updated_at=? WHERE run_id=? AND"
            " state='running' AND version=?",
            (now, continuation.parent_run_id, int(parent["version"])),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn parent WAITING CAS failed")
        _fault(fault, "workflow:spawn_admission:after_parent_write")
        runtime_deleted = tx.connection.execute(
            "DELETE FROM workflow_leases WHERE run_id=? AND namespace=? AND owner_id=? AND epoch=?",
            (
                continuation.parent_run_id,
                RUNTIME_LEASE_NAMESPACE,
                continuation.owner_id,
                continuation.runtime_lease_epoch,
            ),
        ).rowcount
        if runtime_deleted != 1:
            raise UnitOfWorkConflict("workflow spawn Runtime release CAS failed")
        if continuation.workflow_lease_epoch is not None:
            workflow_deleted = tx.connection.execute(
                "DELETE FROM workflow_leases WHERE run_id=? AND namespace<>? AND owner_id=? AND"
                " epoch=?",
                (
                    continuation.parent_run_id,
                    RUNTIME_LEASE_NAMESPACE,
                    continuation.owner_id,
                    continuation.workflow_lease_epoch,
                ),
            ).rowcount
            if workflow_deleted != 1:
                raise UnitOfWorkConflict("workflow spawn Workflow release CAS failed")
        changed = tx.connection.execute(
            "UPDATE run_fences SET state='released',released_at=? WHERE run_id=? AND owner_id=? AND"
            " runtime_lease_epoch=? AND epoch=? AND state='active'",
            (
                now,
                continuation.parent_run_id,
                continuation.owner_id,
                continuation.runtime_lease_epoch,
                continuation.run_fence_epoch,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn RunFence release CAS failed")
        _fault(fault, "workflow:spawn_admission:after_authority_release")
        if active_activation is not None:
            consumed_version = int(active_activation["version"]) + 1
            consumed_hash = _workflow_spawn_activation_hash(
                activation_receipt_id=str(active_activation["activation_receipt_id"]),
                ready_receipt_id=str(active_activation["ready_receipt_id"]),
                spawn_operation_id=str(active_activation["spawn_operation_id"]),
                parent_run_id=str(active_activation["parent_run_id"]),
                effect_id=str(active_activation["effect_id"]),
                owner_id=str(active_activation["owner_id"]),
                runtime_lease_epoch=int(active_activation["runtime_lease_epoch"]),
                run_fence_epoch=int(active_activation["run_fence_epoch"]),
                workflow_lease_epoch=(
                    None
                    if active_activation["workflow_lease_epoch"] is None
                    else int(active_activation["workflow_lease_epoch"])
                ),
                continuation_claim_epoch=int(active_activation["continuation_claim_epoch"]),
                predecessor_activation_receipt_id=(
                    None
                    if active_activation["predecessor_activation_receipt_id"] is None
                    else str(active_activation["predecessor_activation_receipt_id"])
                ),
                version=consumed_version,
            )
            consumed = tx.connection.execute(
                "UPDATE workflow_spawn_ready_activations "
                "SET state='consumed',version=?,canonical_hash=?,consumed_at=? "
                "WHERE activation_receipt_id=? AND state='active' AND version=?",
                (
                    consumed_version,
                    consumed_hash,
                    now,
                    str(active_activation["activation_receipt_id"]),
                    int(active_activation["version"]),
                ),
            ).rowcount
            if consumed != 1:
                raise UnitOfWorkConflict("workflow spawn recovery activation consume CAS failed")
            _fault(fault, "workflow:spawn_admission:after_activation_write")
        parent_wait_receipt_id = self._derived_id(
            "workflow-spawn/child-wait/v1", continuation.spawn_operation_id
        )
        termination_started_at = float(checkpoint_value["started_at"])
        termination_last_observed_at = float(checkpoint_value["last_observed_at"])
        termination_policy_snapshot_hash = hashlib.sha256(
            canonical_json(
                {
                    "started_at": termination_started_at,
                    "last_observed_at": termination_last_observed_at,
                }
            ).encode()
        ).hexdigest()
        wait_identity: dict[str, JsonValue] = {
            "spawn_operation_id": continuation.spawn_operation_id,
            "parent_run_id": continuation.parent_run_id,
            "child_run_id": admission.receipt.run_id,
            "child_command_id": str(ticket_row["child_command_id"]),
            "parent_wait_receipt_id": parent_wait_receipt_id,
        }
        identity_hash = hashlib.sha256(canonical_json(wait_identity).encode()).hexdigest()
        lifecycle_hash = hashlib.sha256(
            canonical_json(
                {
                    **wait_identity,
                    "state": "unconsumed",
                    "parent_waiting_version": parent_waiting_version,
                    "react_checkpoint_hash": next_checkpoint_hash,
                    "context_post_revision": context.revision,
                }
            ).encode()
        ).hexdigest()
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_child_wait_receipts(
                parent_wait_receipt_id,spawn_operation_id,parent_run_id,child_run_id,
                child_command_id,parent_pre_version,parent_waiting_version,
                react_checkpoint_revision,react_checkpoint_hash,expected_signal_domain,
                source_phase,batch_digest,spawn_ordinal,next_tool_ordinal,
                prior_result_append_receipts_json,raw_tool_call_id,
                spawn_result_append_id,spawn_result_append_receipt_id,
                spawn_tool_message_hash,context_pre_revision,context_post_revision,
                released_runtime_lease_epoch,released_workflow_lease_epoch,
                termination_started_at,termination_last_observed_at,wall_deadline,
                termination_policy_snapshot_hash,released_run_fence_epoch,
                child_start_receipt_id,child_dispatch_claim_id,
                child_runtime_lease_epoch,wake_activation_receipt_id,
                state,version,identity_hash,lifecycle_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'tool_batch_reserved',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unconsumed',0,?,?,?)
            """,  # noqa: E501
            (
                parent_wait_receipt_id,
                continuation.spawn_operation_id,
                continuation.parent_run_id,
                admission.receipt.run_id,
                str(ticket_row["child_command_id"]),
                int(parent["version"]),
                parent_waiting_version,
                next_checkpoint_version,
                next_checkpoint_hash,
                "child.terminal.v1",
                batch_digest,
                int(effect["call_ordinal"]),
                int(effect["call_ordinal"]) + 1,
                "[]",
                str(effect["raw_call_id"]),
                append_id,
                append_id,
                tool_result_hash,
                context_pre_revision,
                context.revision,
                continuation.runtime_lease_epoch,
                continuation.workflow_lease_epoch,
                termination_started_at,
                termination_last_observed_at,
                None,
                termination_policy_snapshot_hash,
                continuation.run_fence_epoch,
                admission.receipt.ticket_receipt_id,
                admission.dispatch_claim.claim_id,
                admission.activation.execution_lease.epoch,  # type: ignore[union-attr]
                activation_chain_head_id,
                identity_hash,
                lifecycle_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_admission:after_child_wait_write")
        changed = tx.connection.execute(
            "UPDATE execution_effects SET"
            " state='succeeded',result_json=?,settled_at=?,version=version+1 WHERE effect_id=? AND"
            " state IN ('handed_off','unknown') AND version=?",
            (tool_result_json, now, str(effect["effect_id"]), int(effect["version"])),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn Effect settlement CAS failed")
        completion_receipt_id = self._derived_id(
            "workflow-spawn/completion/v1", continuation.spawn_operation_id
        )
        completion_values: dict[str, object] = {
            "completion_receipt_id": completion_receipt_id,
            "spawn_operation_id": continuation.spawn_operation_id,
            "ticket_receipt_id": continuation.ticket_receipt_id,
            "parent_run_id": continuation.parent_run_id,
            "path_kind": path_kind,
            "effect_id": str(effect["effect_id"]),
            "handoff_attempt": int(effect["handoff_attempt"]),
            "effect_request_hash": str(effect["request_hash"]),
            "issue_authority_hash": str(continuation_row["issue_authority_hash"]),
            "tool_result_json": tool_result_json,
            "tool_result_hash": tool_result_hash,
            "child_runtime_start_receipt_id": admission.receipt.ticket_receipt_id,
            "failure_evidence_kind": None,
            "failure_evidence_id": None,
            "failure_evidence_json": None,
            "failure_evidence_hash": None,
            "activation_chain_head_id": activation_chain_head_id,
            "child_wait_receipt_id": parent_wait_receipt_id,
            "created_at": now,
        }
        completion_hash = _workflow_spawn_completion_hash(completion_values)
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_completion_receipts(
                completion_receipt_id,spawn_operation_id,ticket_receipt_id,parent_run_id,
                path_kind,effect_id,handoff_attempt,effect_request_hash,
                issue_authority_hash,tool_result_json,tool_result_hash,
                child_runtime_start_receipt_id,failure_evidence_kind,
                failure_evidence_id,failure_evidence_json,failure_evidence_hash,
                activation_chain_head_id,child_wait_receipt_id,canonical_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?)
            """,
            (
                completion_receipt_id,
                continuation.spawn_operation_id,
                continuation.ticket_receipt_id,
                continuation.parent_run_id,
                path_kind,
                str(effect["effect_id"]),
                int(effect["handoff_attempt"]),
                str(effect["request_hash"]),
                str(continuation_row["issue_authority_hash"]),
                tool_result_json,
                tool_result_hash,
                admission.receipt.ticket_receipt_id,
                activation_chain_head_id,
                parent_wait_receipt_id,
                completion_hash,
                now,
            ),
        )
        changed = tx.connection.execute(
            "UPDATE workflow_spawn_continuations SET"
            " state='completed',completion_receipt_id=?,completion_path_kind=?,version=version+1,updated_at=?"  # noqa: E501
            " WHERE operation_id=? AND state='claimed' AND version=?",
            (
                completion_receipt_id,
                path_kind,
                now,
                continuation.spawn_operation_id,
                continuation.version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn completion CAS failed")
        _fault(fault, "workflow:spawn_admission:after_completion_write")
        tx.register_after_commit_fault("workflow:spawn_admission:after_commit")
        child_start_ref = ChildStartDispatchRef(
            admission.receipt.ticket_receipt_id,
            admission.dispatch_claim.claim_id,
            admission.receipt.run_id,
        )
        suspension = WorkflowChildWaitBinding(
            parent_run_id=continuation.parent_run_id,
            child_run_id=admission.receipt.run_id,
            child_command_id=str(ticket_row["child_command_id"]),
            parent_wait_receipt_id=parent_wait_receipt_id,
            expected_parent_version=parent_waiting_version,
            react_checkpoint_revision=next_checkpoint_version,
            expected_signal_domain="child.terminal.v1",
            source_phase="tool_batch_reserved",
            batch_digest=batch_digest,
            spawn_ordinal=int(effect["call_ordinal"]),
            next_tool_ordinal=int(effect["call_ordinal"]) + 1,
            spawn_result_append_receipt_id=append_id,
            context_revision=context.revision,
            termination_started_at=termination_started_at,
            termination_last_observed_at=termination_last_observed_at,
            wall_deadline=None,
            termination_policy_snapshot_hash=termination_policy_snapshot_hash,
        )
        control_kind = {
            RuntimeStartDisposition.START_NEW: WorkflowSpawnChildControlKind.START,
            RuntimeStartDisposition.START_ORPHAN: WorkflowSpawnChildControlKind.START,
            RuntimeStartDisposition.RECOVER_START: WorkflowSpawnChildControlKind.RECOVER,
            RuntimeStartDisposition.RECOVER_RESUME: WorkflowSpawnChildControlKind.RECOVER,
            RuntimeStartDisposition.ATTACH_CURRENT: WorkflowSpawnChildControlKind.ATTACH,
            RuntimeStartDisposition.FOREIGN_ACTIVE: WorkflowSpawnChildControlKind.WAITING,
            RuntimeStartDisposition.WAITING: WorkflowSpawnChildControlKind.WAITING,
            RuntimeStartDisposition.CANCEL_PENDING: WorkflowSpawnChildControlKind.CANCEL,
            RuntimeStartDisposition.TERMINAL: WorkflowSpawnChildControlKind.TERMINAL,
        }[admission.disposition]
        child_control = _create_workflow_spawn_child_control(kind=control_kind, admission=admission)
        return _create_workflow_spawn_tool_outcome(
            tool_result=tool_result,
            child_control=child_control,
            child_start_ref=child_start_ref,
            suspension=suspension,
        )

    async def settle_spawn_continuation_catalog_stale(
        self,
        transaction: WorkflowTransaction,
        continuation: WorkflowSpawnContinuationClaim,
        ready: WorkflowSpawnContinuationReady | None,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult:
        tx = self._assert_open_workflow_transaction(transaction)
        existing = await self.read_spawn_continuation_outcome(
            transaction, continuation.spawn_operation_id
        )
        if existing is not None:
            return existing
        now = _time(now)
        row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (continuation.spawn_operation_id,),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != "claimed"
            or _workflow_spawn_continuation_claim(row) != continuation
        ):
            raise UnitOfWorkConflict("workflow spawn continuation claim differs")
        activation: sqlite3.Row | None = None
        path_kind = "direct"
        if ready is not None:
            ready_row = tx.connection.execute(
                "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
                (ready.ready_receipt_id,),
            ).fetchone()
            activation = tx.connection.execute(
                """
                SELECT * FROM workflow_spawn_ready_activations
                WHERE ready_receipt_id=? AND state='active'
                """,
                (ready.ready_receipt_id,),
            ).fetchone()
            if (
                ready_row is None
                or _workflow_spawn_continuation_ready(ready_row) != ready
                or ready_row["consumed_at"] is None
                or activation is None
                or _workflow_spawn_ready_activation(tx.connection, activation).continuation_claim
                != continuation
            ):
                raise UnitOfWorkConflict("workflow spawn recovery settlement authority differs")
            path_kind = "ready_recovery"
        ticket = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (continuation.ticket_receipt_id,),
        ).fetchone()
        catalog = tx.connection.execute(
            "SELECT * FROM workflow_catalog_authorities LIMIT 1"
        ).fetchone()
        effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (str(row["effect_id"]),),
        ).fetchone()
        if ticket is None or catalog is None or effect is None:
            raise UnitOfWorkConflict("workflow spawn settlement authority is missing")
        ticket_catalog = (
            int(ticket["catalog_generation"]),
            int(ticket["catalog_authority_version"]),
            str(ticket["catalog_hash"]),
        )
        observed_catalog = (
            int(catalog["generation"]),
            int(catalog["version"]),
            str(catalog["catalog_hash"]),
        )
        if ticket_catalog == observed_catalog:
            raise UnitOfWorkConflict("workflow launch catalog is not stale")
        if (
            str(effect["state"]) not in {EffectState.HANDED_OFF.value, EffectState.UNKNOWN.value}
            or str(effect["run_id"]) != continuation.parent_run_id
            or str(effect["request_hash"]) != str(row["effect_request_hash"])
            or int(effect["handoff_attempt"]) != int(row["handoff_attempt"])
        ):
            raise UnitOfWorkConflict("workflow spawn Effect settlement authority differs")
        result = ToolResult.failed(
            CallId(str(effect["call_id"])),
            "workflow_catalog_stale",
            "The workflow catalog changed before the child Run was admitted.",
        )
        result_json = _tool_result_json(result)
        result_hash = hashlib.sha256(result_json.encode()).hexdigest()
        evidence: dict[str, JsonValue] = {
            "kind": "catalog_stale",
            "ticket_catalog_generation": ticket_catalog[0],
            "ticket_catalog_version": ticket_catalog[1],
            "ticket_catalog_hash": ticket_catalog[2],
            "observed_catalog_authority_id": str(catalog["authority_id"]),
            "observed_catalog_generation": observed_catalog[0],
            "observed_catalog_version": observed_catalog[1],
            "observed_catalog_hash": observed_catalog[2],
        }
        evidence_json = canonical_json(evidence)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        completion_receipt_id = self._derived_id(
            "workflow-spawn/completion/v1", continuation.spawn_operation_id
        )
        completion_values: dict[str, object] = {
            "completion_receipt_id": completion_receipt_id,
            "spawn_operation_id": continuation.spawn_operation_id,
            "ticket_receipt_id": continuation.ticket_receipt_id,
            "parent_run_id": continuation.parent_run_id,
            "path_kind": path_kind,
            "effect_id": str(row["effect_id"]),
            "handoff_attempt": int(row["handoff_attempt"]),
            "effect_request_hash": str(row["effect_request_hash"]),
            "issue_authority_hash": str(row["issue_authority_hash"]),
            "tool_result_json": result_json,
            "tool_result_hash": result_hash,
            "child_runtime_start_receipt_id": None,
            "failure_evidence_kind": "catalog_stale",
            "failure_evidence_id": self._derived_id(
                "workflow-spawn/failure-evidence/v1", evidence_hash
            ),
            "failure_evidence_json": evidence_json,
            "failure_evidence_hash": evidence_hash,
            "activation_chain_head_id": (
                None if activation is None else str(activation["activation_receipt_id"])
            ),
            "child_wait_receipt_id": None,
            "created_at": now,
        }
        completion_hash = _workflow_spawn_completion_hash(completion_values)
        _fault(fault, "workflow:spawn_catalog_stale:before_effect_write")
        changed = tx.connection.execute(
            """
            UPDATE execution_effects
            SET state='failed',result_json=?,evidence_ref=?,settled_at=?,version=version+1
            WHERE effect_id=? AND state IN ('handed_off','unknown') AND version=?
            """,
            (
                result_json,
                str(completion_values["failure_evidence_id"]),
                now,
                str(row["effect_id"]),
                int(effect["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn Effect settlement CAS failed")
        _fault(fault, "workflow:spawn_catalog_stale:after_effect_write")
        if activation is not None:
            consumed_version = int(activation["version"]) + 1
            consumed_hash = _workflow_spawn_activation_hash(
                activation_receipt_id=str(activation["activation_receipt_id"]),
                ready_receipt_id=str(activation["ready_receipt_id"]),
                spawn_operation_id=str(activation["spawn_operation_id"]),
                parent_run_id=str(activation["parent_run_id"]),
                effect_id=str(activation["effect_id"]),
                owner_id=str(activation["owner_id"]),
                runtime_lease_epoch=int(activation["runtime_lease_epoch"]),
                run_fence_epoch=int(activation["run_fence_epoch"]),
                workflow_lease_epoch=(
                    None
                    if activation["workflow_lease_epoch"] is None
                    else int(activation["workflow_lease_epoch"])
                ),
                continuation_claim_epoch=int(activation["continuation_claim_epoch"]),
                predecessor_activation_receipt_id=(
                    None
                    if activation["predecessor_activation_receipt_id"] is None
                    else str(activation["predecessor_activation_receipt_id"])
                ),
                version=consumed_version,
            )
            changed = tx.connection.execute(
                """
                UPDATE workflow_spawn_ready_activations
                SET state='consumed',version=?,canonical_hash=?,consumed_at=?
                WHERE activation_receipt_id=? AND state='active' AND version=?
                """,
                (
                    consumed_version,
                    consumed_hash,
                    now,
                    str(activation["activation_receipt_id"]),
                    int(activation["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn recovery activation consume CAS failed")
            _fault(fault, "workflow:spawn_catalog_stale:after_activation_write")
        tx.connection.execute(
            """
            INSERT INTO workflow_spawn_completion_receipts(
                completion_receipt_id,spawn_operation_id,ticket_receipt_id,
                parent_run_id,path_kind,effect_id,handoff_attempt,
                effect_request_hash,issue_authority_hash,tool_result_json,
                tool_result_hash,child_runtime_start_receipt_id,
                failure_evidence_kind,failure_evidence_id,failure_evidence_json,
                failure_evidence_hash,activation_chain_head_id,
                child_wait_receipt_id,canonical_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,?,?)
            """,
            (
                completion_receipt_id,
                continuation.spawn_operation_id,
                continuation.ticket_receipt_id,
                continuation.parent_run_id,
                path_kind,
                str(row["effect_id"]),
                int(row["handoff_attempt"]),
                str(row["effect_request_hash"]),
                str(row["issue_authority_hash"]),
                result_json,
                result_hash,
                "catalog_stale",
                str(completion_values["failure_evidence_id"]),
                evidence_json,
                evidence_hash,
                (None if activation is None else str(activation["activation_receipt_id"])),
                completion_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_catalog_stale:after_completion_write")
        changed = tx.connection.execute(
            """
            UPDATE workflow_spawn_continuations
            SET state='completed',completion_receipt_id=?,
                completion_path_kind=?,version=version+1,updated_at=?
            WHERE operation_id=? AND state='claimed' AND version=?
            """,
            (
                completion_receipt_id,
                path_kind,
                now,
                continuation.spawn_operation_id,
                continuation.version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn completion CAS failed")
        _fault(fault, "workflow:spawn_catalog_stale:after_continuation_write")
        tx.register_after_commit_fault("workflow:spawn_catalog_stale:after_commit")
        return result

    async def settle_spawn_continuation_graph_unavailable(
        self,
        transaction: WorkflowTransaction,
        continuation: WorkflowSpawnContinuationClaim,
        ready: WorkflowSpawnContinuationReady | None,
        evidence: VerifiedWorkflowGraphUnavailable,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult:
        from simple_harness.runtime.orchestration import (
            VerifiedWorkflowGraphUnavailable,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        existing = await self.read_spawn_continuation_outcome(
            transaction, continuation.spawn_operation_id
        )
        if existing is not None:
            return existing
        if (
            type(evidence) is not VerifiedWorkflowGraphUnavailable
            or not evidence._is_sdk_verified()
        ):
            raise TypeError("graph-unavailable evidence is not SDK verified")
        if ready is None:
            raise UnitOfWorkConflict(
                "graph-unavailable settlement requires ready recovery authority"
            )
        now = _time(now)
        row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (continuation.spawn_operation_id,),
        ).fetchone()
        ready_row = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
            (ready.ready_receipt_id,),
        ).fetchone()
        activation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations "
            "WHERE ready_receipt_id=? AND state='active'",
            (ready.ready_receipt_id,),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != "claimed"
            or _workflow_spawn_continuation_claim(row) != continuation
            or ready_row is None
            or _workflow_spawn_continuation_ready(ready_row) != ready
            or ready_row["consumed_at"] is None
            or activation is None
            or _workflow_spawn_ready_activation(tx.connection, activation).continuation_claim
            != continuation
        ):
            raise UnitOfWorkConflict("workflow spawn graph settlement authority differs")
        ticket = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (continuation.ticket_receipt_id,),
        ).fetchone()
        effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (str(row["effect_id"]),),
        ).fetchone()
        if ticket is None or effect is None:
            raise UnitOfWorkConflict("workflow spawn graph settlement facts are missing")
        workflow_epoch = (
            None
            if activation["workflow_lease_epoch"] is None
            else int(activation["workflow_lease_epoch"])
        )
        proof_fields = (
            evidence.ticket_receipt_id,
            evidence.profile_key,
            evidence.workflow_name,
            evidence.workflow_version,
            evidence.expected_implementation_hash,
            evidence.activation_receipt_id,
            evidence.parent_run_id,
            evidence.owner_id,
            evidence.runtime_lease_epoch,
            evidence.run_fence_epoch,
            evidence.workflow_lease_epoch,
            evidence.continuation_claim_epoch,
        )
        durable_fields = (
            str(ticket["ticket_receipt_id"]),
            str(ticket["profile_key"]),
            str(ticket["workflow_name"]),
            str(ticket["workflow_version"]),
            str(ticket["implementation_fingerprint"]),
            str(activation["activation_receipt_id"]),
            str(activation["parent_run_id"]),
            str(activation["owner_id"]),
            int(activation["runtime_lease_epoch"]),
            int(activation["run_fence_epoch"]),
            workflow_epoch,
            int(activation["continuation_claim_epoch"]),
        )
        if (
            proof_fields != durable_fields
            or evidence.observed_kind not in {"missing", "drift"}
            or (
                evidence.observed_kind == "missing"
                and evidence.observed_implementation_hash is not None
            )
            or (
                evidence.observed_kind == "drift"
                and (
                    evidence.observed_implementation_hash is None
                    or evidence.observed_implementation_hash
                    == evidence.expected_implementation_hash
                )
            )
            or str(effect["state"]) not in {EffectState.HANDED_OFF.value, EffectState.UNKNOWN.value}
            or str(effect["run_id"]) != continuation.parent_run_id
            or str(effect["request_hash"]) != str(row["effect_request_hash"])
            or int(effect["handoff_attempt"]) != int(row["handoff_attempt"])
        ):
            raise UnitOfWorkConflict("workflow graph-unavailable evidence differs")

        result = ToolResult.failed(
            CallId(str(effect["call_id"])),
            "graph_version_unavailable",
            "The pinned workflow graph version is unavailable.",
        )
        result_json = _tool_result_json(result)
        result_hash = hashlib.sha256(result_json.encode()).hexdigest()
        evidence_value: dict[str, JsonValue] = {
            "kind": "graph_version_unavailable",
            "ticket_receipt_id": evidence.ticket_receipt_id,
            "profile_key": evidence.profile_key,
            "workflow_name": evidence.workflow_name,
            "workflow_version": evidence.workflow_version,
            "expected_implementation_hash": evidence.expected_implementation_hash,
            "registry_content_digest": evidence.registry_content_digest,
            "activation_receipt_id": evidence.activation_receipt_id,
            "parent_run_id": evidence.parent_run_id,
            "owner_id": evidence.owner_id,
            "runtime_lease_epoch": evidence.runtime_lease_epoch,
            "run_fence_epoch": evidence.run_fence_epoch,
            "workflow_lease_epoch": evidence.workflow_lease_epoch,
            "continuation_claim_epoch": evidence.continuation_claim_epoch,
            "observed_kind": evidence.observed_kind,
            "observed_implementation_hash": evidence.observed_implementation_hash,
        }
        evidence_json = canonical_json(evidence_value)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        completion_receipt_id = self._derived_id(
            "workflow-spawn/completion/v1", continuation.spawn_operation_id
        )
        completion_values: dict[str, object] = {
            "completion_receipt_id": completion_receipt_id,
            "spawn_operation_id": continuation.spawn_operation_id,
            "ticket_receipt_id": continuation.ticket_receipt_id,
            "parent_run_id": continuation.parent_run_id,
            "path_kind": "ready_recovery",
            "effect_id": str(row["effect_id"]),
            "handoff_attempt": int(row["handoff_attempt"]),
            "effect_request_hash": str(row["effect_request_hash"]),
            "issue_authority_hash": str(row["issue_authority_hash"]),
            "tool_result_json": result_json,
            "tool_result_hash": result_hash,
            "child_runtime_start_receipt_id": None,
            "failure_evidence_kind": "graph_version_unavailable",
            "failure_evidence_id": self._derived_id(
                "workflow-spawn/failure-evidence/v1", evidence_hash
            ),
            "failure_evidence_json": evidence_json,
            "failure_evidence_hash": evidence_hash,
            "activation_chain_head_id": str(activation["activation_receipt_id"]),
            "child_wait_receipt_id": None,
            "created_at": now,
        }
        completion_hash = _workflow_spawn_completion_hash(completion_values)
        _fault(fault, "workflow:spawn_graph_unavailable:before_effect_write")
        changed = tx.connection.execute(
            "UPDATE execution_effects SET state='failed',result_json=?,evidence_ref=?,"
            "settled_at=?,version=version+1 WHERE effect_id=? "
            "AND state IN ('handed_off','unknown') AND version=?",
            (
                result_json,
                str(completion_values["failure_evidence_id"]),
                now,
                str(row["effect_id"]),
                int(effect["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn Effect settlement CAS failed")
        _fault(fault, "workflow:spawn_graph_unavailable:after_effect_write")
        consumed_version = int(activation["version"]) + 1
        consumed_hash = _workflow_spawn_activation_hash(
            activation_receipt_id=str(activation["activation_receipt_id"]),
            ready_receipt_id=str(activation["ready_receipt_id"]),
            spawn_operation_id=str(activation["spawn_operation_id"]),
            parent_run_id=str(activation["parent_run_id"]),
            effect_id=str(activation["effect_id"]),
            owner_id=str(activation["owner_id"]),
            runtime_lease_epoch=int(activation["runtime_lease_epoch"]),
            run_fence_epoch=int(activation["run_fence_epoch"]),
            workflow_lease_epoch=workflow_epoch,
            continuation_claim_epoch=int(activation["continuation_claim_epoch"]),
            predecessor_activation_receipt_id=(
                None
                if activation["predecessor_activation_receipt_id"] is None
                else str(activation["predecessor_activation_receipt_id"])
            ),
            version=consumed_version,
        )
        changed = tx.connection.execute(
            "UPDATE workflow_spawn_ready_activations SET state='consumed',version=?,"
            "canonical_hash=?,consumed_at=? WHERE activation_receipt_id=? "
            "AND state='active' AND version=?",
            (
                consumed_version,
                consumed_hash,
                now,
                str(activation["activation_receipt_id"]),
                int(activation["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn recovery activation consume CAS failed")
        _fault(fault, "workflow:spawn_graph_unavailable:after_activation_write")
        tx.connection.execute(
            "INSERT INTO workflow_spawn_completion_receipts("
            "completion_receipt_id,spawn_operation_id,ticket_receipt_id,parent_run_id,"
            "path_kind,effect_id,handoff_attempt,effect_request_hash,"
            "issue_authority_hash,tool_result_json,tool_result_hash,"
            "child_runtime_start_receipt_id,failure_evidence_kind,"
            "failure_evidence_id,failure_evidence_json,failure_evidence_hash,"
            "activation_chain_head_id,child_wait_receipt_id,canonical_hash,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,?,?)",
            (
                completion_receipt_id,
                continuation.spawn_operation_id,
                continuation.ticket_receipt_id,
                continuation.parent_run_id,
                "ready_recovery",
                str(row["effect_id"]),
                int(row["handoff_attempt"]),
                str(row["effect_request_hash"]),
                str(row["issue_authority_hash"]),
                result_json,
                result_hash,
                "graph_version_unavailable",
                str(completion_values["failure_evidence_id"]),
                evidence_json,
                evidence_hash,
                str(activation["activation_receipt_id"]),
                completion_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_graph_unavailable:after_completion_write")
        changed = tx.connection.execute(
            "UPDATE workflow_spawn_continuations SET state='completed',"
            "completion_receipt_id=?,completion_path_kind='ready_recovery',"
            "version=version+1,updated_at=? WHERE operation_id=? "
            "AND state='claimed' AND version=?",
            (
                completion_receipt_id,
                now,
                continuation.spawn_operation_id,
                continuation.version,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn completion CAS failed")
        _fault(fault, "workflow:spawn_graph_unavailable:after_continuation_write")
        tx.register_after_commit_fault("workflow:spawn_graph_unavailable:after_commit")
        return result

    async def settle_spawn_continuation_for_parent_terminal(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        ready_or_continuation: WorkflowSpawnContinuationReady | WorkflowSpawnContinuationClaim,
        parent_terminal_snapshot: WorkflowTerminalOutcome,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ToolResult:
        from simple_harness.runtime.orchestration import (
            WorkflowSpawnContinuationClaim,
            WorkflowSpawnContinuationReady,
        )
        from simple_harness.workflow.execution_ports import WorkflowTerminalOutcome

        tx = self._assert_open_workflow_transaction(transaction)
        if not isinstance(
            ready_or_continuation,
            (WorkflowSpawnContinuationClaim, WorkflowSpawnContinuationReady),
        ):
            raise TypeError("ready_or_continuation must be durable workflow spawn evidence")
        if not isinstance(parent_terminal_snapshot, WorkflowTerminalOutcome):
            raise TypeError("parent_terminal_snapshot must be a WorkflowTerminalOutcome")
        operation_id = ready_or_continuation.spawn_operation_id
        existing = await self.read_spawn_continuation_outcome(transaction, operation_id)
        if existing is not None:
            completion = tx.connection.execute(
                "SELECT * FROM workflow_spawn_completion_receipts WHERE spawn_operation_id=?",
                (operation_id,),
            ).fetchone()
            if completion is None:
                raise UnitOfWorkConflict("workflow spawn terminal completion disappeared")
            path_kind = str(completion["path_kind"])
            if path_kind.startswith("parent_terminal_"):
                evidence = _workflow_spawn_failure_evidence(completion)
                replay_identity = (
                    evidence.get("terminal_receipt_id"),
                    evidence.get("terminal_state"),
                    evidence.get("terminal_outcome_hash"),
                )
                requested_identity = (
                    parent_terminal_snapshot.receipt_id,
                    parent_terminal_snapshot.state,
                    parent_terminal_snapshot.outcome_hash,
                )
                durable_ticket = tx.connection.execute(
                    "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
                    (ticket.ticket_receipt_id,),
                ).fetchone()
                if (
                    replay_identity != requested_identity
                    or durable_ticket is None
                    or _workflow_launch_ticket(durable_ticket) != ticket
                    or ticket.ticket_receipt_id != str(completion["ticket_receipt_id"])
                ):
                    raise UnitOfWorkConflict(
                        "workflow spawn parent-terminal replay evidence differs"
                    )
                replay_ready_row = tx.connection.execute(
                    "SELECT * FROM workflow_spawn_continuation_ready WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if path_kind == "parent_terminal_ticket_only":
                    continuation_row = tx.connection.execute(
                        "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if (
                        not isinstance(
                            ready_or_continuation,
                            WorkflowSpawnContinuationClaim,
                        )
                        or continuation_row is None
                        or ready_or_continuation.version + 1 != int(continuation_row["version"])
                        or (
                            ready_or_continuation.spawn_operation_id,
                            ready_or_continuation.ticket_receipt_id,
                            ready_or_continuation.parent_run_id,
                            ready_or_continuation.owner_id,
                            ready_or_continuation.runtime_lease_epoch,
                            ready_or_continuation.run_fence_epoch,
                            ready_or_continuation.workflow_lease_epoch,
                            ready_or_continuation.claim_epoch,
                            ready_or_continuation.expires_at,
                        )
                        != (
                            str(continuation_row["operation_id"]),
                            str(continuation_row["ticket_receipt_id"]),
                            str(continuation_row["parent_run_id"]),
                            str(continuation_row["owner_id"]),
                            int(continuation_row["runtime_lease_epoch"]),
                            int(continuation_row["run_fence_epoch"]),
                            (
                                None
                                if continuation_row["workflow_lease_epoch"] is None
                                else int(continuation_row["workflow_lease_epoch"])
                            ),
                            int(continuation_row["claim_epoch"]),
                            float(continuation_row["expires_at"]),
                        )
                    ):
                        raise UnitOfWorkConflict(
                            "workflow spawn parent-terminal replay continuation differs"
                        )
                elif (
                    not isinstance(
                        ready_or_continuation,
                        WorkflowSpawnContinuationReady,
                    )
                    or replay_ready_row is None
                    or _workflow_spawn_continuation_ready(replay_ready_row) != ready_or_continuation
                ):
                    raise UnitOfWorkConflict("workflow spawn parent-terminal replay ready differs")
            return existing

        now = _time(now)
        if not self._verify_workflow_terminal(tx.connection, parent_terminal_snapshot):
            raise UnitOfWorkConflict("workflow spawn parent terminal receipt is forged or stale")
        continuation = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        issued = await self.read_issued(transaction, operation_id)
        if continuation is None or issued is None or issued[0] != ticket:
            raise UnitOfWorkConflict("workflow spawn parent-terminal authority is missing")
        request = issued[1]
        if (
            str(continuation["state"]) not in {"pending", "claimed"}
            or str(continuation["ticket_receipt_id"]) != ticket.ticket_receipt_id
            or str(continuation["parent_run_id"]) != parent_terminal_snapshot.run_id
            or request.spawn_origin.parent_run_id != parent_terminal_snapshot.run_id
            or request.request_key != operation_id
        ):
            raise UnitOfWorkConflict("workflow spawn parent-terminal identity differs")

        ready_rows = tx.connection.execute(
            "SELECT * FROM workflow_spawn_continuation_ready "
            "WHERE operation_id=? ORDER BY ready_receipt_id",
            (operation_id,),
        ).fetchall()
        activation_rows = tx.connection.execute(
            "SELECT * FROM workflow_spawn_ready_activations "
            "WHERE spawn_operation_id=? ORDER BY created_at,activation_receipt_id",
            (operation_id,),
        ).fetchall()
        active_rows = [row for row in activation_rows if str(row["state"]) == "active"]
        ready_row: sqlite3.Row | None = None
        blocker: sqlite3.Row | None = None
        activation: sqlite3.Row | None = None
        if isinstance(ready_or_continuation, WorkflowSpawnContinuationClaim):
            if (
                str(continuation["state"]) != "claimed"
                or _workflow_spawn_continuation_claim(continuation) != ready_or_continuation
                or ready_rows
                or activation_rows
            ):
                raise UnitOfWorkConflict("workflow spawn ticket-only terminal authority differs")
            path_kind = "parent_terminal_ticket_only"
        else:
            if len(ready_rows) != 1:
                raise UnitOfWorkConflict("workflow spawn parent-terminal ready shape differs")
            ready_row = ready_rows[0]
            assert ready_row is not None
            if (
                _workflow_spawn_continuation_ready(ready_row) != ready_or_continuation
                or str(ready_row["ticket_receipt_id"]) != ticket.ticket_receipt_id
                or str(ready_row["effect_id"]) != str(continuation["effect_id"])
                or int(ready_row["handoff_attempt"]) != int(continuation["handoff_attempt"])
            ):
                raise UnitOfWorkConflict("workflow spawn parent-terminal ready evidence differs")
            if not activation_rows:
                if ready_row["consumed_at"] is not None or active_rows:
                    raise UnitOfWorkConflict("workflow spawn unactivated terminal shape differs")
                blockers = tx.connection.execute(
                    "SELECT * FROM run_wait_blockers WHERE run_id=? "
                    "AND kind='tool' AND ledger_identity=? AND handoff_attempt=? "
                    "AND wake_consumed=0 AND superseded_by IS NULL "
                    "ORDER BY blocker_id",
                    (
                        parent_terminal_snapshot.run_id,
                        str(ready_row["effect_id"]),
                        int(ready_row["handoff_attempt"]),
                    ),
                ).fetchall()
                if len(blockers) != 1:
                    raise UnitOfWorkConflict("workflow spawn parent-terminal blocker differs")
                blocker = blockers[0]
                path_kind = "parent_terminal_ready_unactivated"
            else:
                if (
                    ready_row["consumed_at"] is None
                    or len(active_rows) != 1
                    or active_rows[0] is not activation_rows[-1]
                    or str(continuation["state"]) != "claimed"
                ):
                    raise UnitOfWorkConflict("workflow spawn activated terminal shape differs")
                predecessor: str | None = None
                for index, row in enumerate(activation_rows):
                    expected_state = "active" if index == len(activation_rows) - 1 else "superseded"
                    if (
                        str(row["ready_receipt_id"]) != str(ready_row["ready_receipt_id"])
                        or str(row["parent_run_id"]) != parent_terminal_snapshot.run_id
                        or str(row["effect_id"]) != str(continuation["effect_id"])
                        or row["predecessor_activation_receipt_id"] != predecessor
                        or str(row["state"]) != expected_state
                        or str(row["canonical_hash"])
                        != _workflow_spawn_activation_hash(
                            activation_receipt_id=str(row["activation_receipt_id"]),
                            ready_receipt_id=str(row["ready_receipt_id"]),
                            spawn_operation_id=str(row["spawn_operation_id"]),
                            parent_run_id=str(row["parent_run_id"]),
                            effect_id=str(row["effect_id"]),
                            owner_id=str(row["owner_id"]),
                            runtime_lease_epoch=int(row["runtime_lease_epoch"]),
                            run_fence_epoch=int(row["run_fence_epoch"]),
                            workflow_lease_epoch=(
                                None
                                if row["workflow_lease_epoch"] is None
                                else int(row["workflow_lease_epoch"])
                            ),
                            continuation_claim_epoch=int(row["continuation_claim_epoch"]),
                            predecessor_activation_receipt_id=predecessor,
                            version=int(row["version"]),
                        )
                    ):
                        raise UnitOfWorkConflict(
                            "workflow spawn parent-terminal activation chain differs"
                        )
                    predecessor = str(row["activation_receipt_id"])
                activation = active_rows[0]
                assert activation is not None
                if (
                    str(activation["owner_id"]) != str(continuation["owner_id"])
                    or int(activation["runtime_lease_epoch"])
                    != int(continuation["runtime_lease_epoch"])
                    or int(activation["run_fence_epoch"]) != int(continuation["run_fence_epoch"])
                    or activation["workflow_lease_epoch"] != continuation["workflow_lease_epoch"]
                    or int(activation["continuation_claim_epoch"])
                    != int(continuation["claim_epoch"])
                ):
                    raise UnitOfWorkConflict("workflow spawn active terminal claim differs")
                path_kind = "parent_terminal_activated"

        effect = tx.connection.execute(
            "SELECT * FROM execution_effects WHERE effect_id=?",
            (str(continuation["effect_id"]),),
        ).fetchone()
        if (
            effect is None
            or str(effect["state"]) not in {EffectState.HANDED_OFF.value, EffectState.UNKNOWN.value}
            or str(effect["run_id"]) != parent_terminal_snapshot.run_id
            or str(effect["request_hash"]) != str(continuation["effect_request_hash"])
            or int(effect["handoff_attempt"]) != int(continuation["handoff_attempt"])
        ):
            raise UnitOfWorkConflict("workflow spawn parent-terminal Effect authority differs")

        result = ToolResult.failed(
            CallId(str(effect["call_id"])),
            "workflow_parent_terminal_before_spawn",
            "The parent Run became terminal before the child Run was admitted.",
        )
        result_json = _tool_result_json(result)
        result_hash = hashlib.sha256(result_json.encode()).hexdigest()
        failure_evidence: dict[str, JsonValue] = {
            "kind": "workflow_parent_terminal_before_spawn",
            "terminal_receipt_id": parent_terminal_snapshot.receipt_id,
            "terminal_state": parent_terminal_snapshot.state,
            "terminal_outcome_hash": parent_terminal_snapshot.outcome_hash,
        }
        evidence_json = canonical_json(failure_evidence)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        evidence_id = self._derived_id("workflow-spawn/failure-evidence/v1", evidence_hash)
        completion_receipt_id = self._derived_id("workflow-spawn/completion/v1", operation_id)
        completion_values: dict[str, object] = {
            "completion_receipt_id": completion_receipt_id,
            "spawn_operation_id": operation_id,
            "ticket_receipt_id": ticket.ticket_receipt_id,
            "parent_run_id": parent_terminal_snapshot.run_id,
            "path_kind": path_kind,
            "effect_id": str(continuation["effect_id"]),
            "handoff_attempt": int(continuation["handoff_attempt"]),
            "effect_request_hash": str(continuation["effect_request_hash"]),
            "issue_authority_hash": str(continuation["issue_authority_hash"]),
            "tool_result_json": result_json,
            "tool_result_hash": result_hash,
            "child_runtime_start_receipt_id": None,
            "failure_evidence_kind": failure_evidence["kind"],
            "failure_evidence_id": evidence_id,
            "failure_evidence_json": evidence_json,
            "failure_evidence_hash": evidence_hash,
            "activation_chain_head_id": (
                None if activation is None else str(activation["activation_receipt_id"])
            ),
            "child_wait_receipt_id": None,
            "created_at": now,
        }
        completion_hash = _workflow_spawn_completion_hash(completion_values)

        _fault(fault, "workflow:spawn_parent_terminal:before_effect_write")
        changed = tx.connection.execute(
            "UPDATE execution_effects SET state='failed',result_json=?,"
            "evidence_ref=?,settled_at=?,version=version+1 WHERE effect_id=? "
            "AND state IN ('handed_off','unknown') AND version=?",
            (
                result_json,
                evidence_id,
                now,
                str(continuation["effect_id"]),
                int(effect["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn parent-terminal Effect CAS failed")
        _fault(fault, "workflow:spawn_parent_terminal:after_effect_write")

        if activation is not None:
            _fault(fault, "workflow:spawn_parent_terminal:before_activation_write")
            consumed_version = int(activation["version"]) + 1
            consumed_hash = _workflow_spawn_activation_hash(
                activation_receipt_id=str(activation["activation_receipt_id"]),
                ready_receipt_id=str(activation["ready_receipt_id"]),
                spawn_operation_id=str(activation["spawn_operation_id"]),
                parent_run_id=str(activation["parent_run_id"]),
                effect_id=str(activation["effect_id"]),
                owner_id=str(activation["owner_id"]),
                runtime_lease_epoch=int(activation["runtime_lease_epoch"]),
                run_fence_epoch=int(activation["run_fence_epoch"]),
                workflow_lease_epoch=(
                    None
                    if activation["workflow_lease_epoch"] is None
                    else int(activation["workflow_lease_epoch"])
                ),
                continuation_claim_epoch=int(activation["continuation_claim_epoch"]),
                predecessor_activation_receipt_id=(
                    None
                    if activation["predecessor_activation_receipt_id"] is None
                    else str(activation["predecessor_activation_receipt_id"])
                ),
                version=consumed_version,
            )
            changed = tx.connection.execute(
                "UPDATE workflow_spawn_ready_activations SET state='consumed',"
                "version=?,canonical_hash=?,consumed_at=? "
                "WHERE activation_receipt_id=? AND state='active' AND version=?",
                (
                    consumed_version,
                    consumed_hash,
                    now,
                    str(activation["activation_receipt_id"]),
                    int(activation["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn parent-terminal activation CAS failed")
            _fault(fault, "workflow:spawn_parent_terminal:after_activation_write")

        if ready_row is not None and activation is None:
            _fault(fault, "workflow:spawn_parent_terminal:before_ready_write")
            changed = tx.connection.execute(
                "UPDATE workflow_spawn_continuation_ready SET consumed_at=? "
                "WHERE ready_receipt_id=? AND consumed_at IS NULL",
                (now, str(ready_row["ready_receipt_id"])),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn parent-terminal ready CAS failed")
            _fault(fault, "workflow:spawn_parent_terminal:after_ready_write")
        if blocker is not None:
            assert ready_row is not None
            _fault(fault, "workflow:spawn_parent_terminal:before_blocker_write")
            changed = tx.connection.execute(
                "UPDATE run_wait_blockers SET wake_consumed=1,consumed_at=?,"
                "superseded_by=?,version=version+1 WHERE blocker_id=? "
                "AND wake_consumed=0 AND superseded_by IS NULL AND version=?",
                (
                    now,
                    str(ready_row["ready_receipt_id"]),
                    str(blocker["blocker_id"]),
                    int(blocker["version"]),
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("workflow spawn parent-terminal blocker CAS failed")
            _fault(fault, "workflow:spawn_parent_terminal:after_blocker_write")

        _fault(fault, "workflow:spawn_parent_terminal:before_completion_write")
        tx.connection.execute(
            "INSERT INTO workflow_spawn_completion_receipts("
            "completion_receipt_id,spawn_operation_id,ticket_receipt_id,parent_run_id,"
            "path_kind,effect_id,handoff_attempt,effect_request_hash,"
            "issue_authority_hash,tool_result_json,tool_result_hash,"
            "child_runtime_start_receipt_id,failure_evidence_kind,"
            "failure_evidence_id,failure_evidence_json,failure_evidence_hash,"
            "activation_chain_head_id,child_wait_receipt_id,canonical_hash,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,?,?)",
            (
                completion_receipt_id,
                operation_id,
                ticket.ticket_receipt_id,
                parent_terminal_snapshot.run_id,
                path_kind,
                str(continuation["effect_id"]),
                int(continuation["handoff_attempt"]),
                str(continuation["effect_request_hash"]),
                str(continuation["issue_authority_hash"]),
                result_json,
                result_hash,
                str(failure_evidence["kind"]),
                evidence_id,
                evidence_json,
                evidence_hash,
                completion_values["activation_chain_head_id"],
                completion_hash,
                now,
            ),
        )
        _fault(fault, "workflow:spawn_parent_terminal:after_completion_write")
        _fault(fault, "workflow:spawn_parent_terminal:before_continuation_write")
        changed = tx.connection.execute(
            "UPDATE workflow_spawn_continuations SET state='completed',"
            "completion_receipt_id=?,completion_path_kind=?,version=version+1,"
            "updated_at=? WHERE operation_id=? AND state IN ('pending','claimed') "
            "AND version=?",
            (
                completion_receipt_id,
                path_kind,
                now,
                operation_id,
                int(continuation["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("workflow spawn parent-terminal continuation CAS failed")
        _fault(fault, "workflow:spawn_parent_terminal:after_continuation_write")
        tx.register_after_commit_fault("workflow:spawn_parent_terminal:after_commit")
        return result

    async def verify(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
    ) -> VerifiedWorkflowLaunchTicket:
        tx = self._assert_open_workflow_transaction(transaction)
        row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if row is None or _workflow_launch_ticket(row) != ticket:
            raise UnitOfWorkConflict("workflow launch ticket is forged or stale")
        catalog = await self.read_catalog(transaction)
        if (
            catalog.generation != int(row["catalog_generation"])
            or catalog.version != int(row["catalog_authority_version"])
            or catalog.catalog_hash != str(row["catalog_hash"])
        ):
            raise UnitOfWorkConflict("workflow launch catalog authority changed")
        profile = catalog.require(str(row["profile_key"]))
        payload = json.loads(str(row["canonical_payload"]))
        stored_binding = payload.get("profile_binding") if isinstance(payload, dict) else None
        if (
            canonical_json(stored_binding)
            != canonical_json(_workflow_catalog_profile_json(profile))
            or profile.profile_fingerprint != str(row["profile_fingerprint"])
            or profile.workflow_name != str(row["workflow_name"])
            or profile.workflow_version != str(row["workflow_version"])
            or profile.implementation_fingerprint != str(row["implementation_fingerprint"])
        ):
            raise UnitOfWorkConflict("workflow launch profile binding changed")
        return _verified_workflow_launch_ticket(row)

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
    ) -> RuntimeStartAdmission:
        from simple_harness.runtime.orchestration import (
            RuntimeStartActivation,
            RuntimeStartAdmission,
            RuntimeStartDispatchClaim,
            RuntimeStartDisposition,
        )

        tx = self._assert_open_workflow_transaction(transaction)
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if ticket_row is None or _workflow_launch_ticket(ticket_row) != ticket:
            raise UnitOfWorkConflict("workflow launch ticket is forged or stale")
        verified = _verified_workflow_launch_ticket(ticket_row)
        _validate_runtime_start_binding(verified, start, request, snapshot)
        snapshot_json = canonical_json(snapshot.to_json())
        snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
        request_json = canonical_json(self._workflow_request_payload(request))
        workflow_request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        existing_receipt = tx.connection.execute(
            "SELECT * FROM runtime_start_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if existing_receipt is not None:
            _require_runtime_start_receipt_identity(
                existing_receipt,
                verified=verified,
                snapshot_hash=snapshot_hash,
                workflow_request_hash=workflow_request_hash,
            )
            return self._classify_runtime_start_admission(
                tx.connection,
                existing_receipt,
                claim=claim,
                now=now,
                fault=fault,
            )
        # A new generic Run may only be created while the ticket's durable
        # catalog/profile binding is still current.  Existing admissions above
        # are immutable facts and intentionally replay before this current-view
        # check.
        verified = await self.verify(transaction, ticket)
        expires_at = now + float(claim.lease_ttl_seconds)
        _fault(fault, "workflow:runtime_start:before_session_write")
        tx.connection.execute(
            "INSERT OR IGNORE INTO execution_sessions(session_id,user_id,created_at) "
            "VALUES(?,'harness-system',?)",
            (verified.session_id, now),
        )
        _fault(fault, "workflow:runtime_start:after_session_write")
        _fault(fault, "workflow:runtime_start:before_run_write")
        tx.connection.execute(
            "INSERT INTO"
            " runs(run_id,execution_session_id,request_id,root_run_id,parent_run_id,profile_key,driver_kind,state,version,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,?,?,'workflow','running',1,?,?)",
            (
                verified.resolved_run_id,
                verified.session_id,
                verified.request_id,
                verified.root_run_id,
                verified.parent_run_id,
                verified.profile_key,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_run_write")
        _fault(fault, "workflow:runtime_start:before_snapshot_write")
        tx.connection.execute(
            "INSERT INTO run_start_snapshots(run_id,snapshot_json,snapshot_hash,created_at)"
            " VALUES(?,?,?,?)",
            (verified.resolved_run_id, snapshot_json, snapshot_hash, now),
        )
        _fault(fault, "workflow:runtime_start:after_snapshot_write")
        _fault(fault, "workflow:runtime_start:before_child_link_write")
        tx.connection.execute(
            "INSERT INTO run_links(parent_run_id,child_run_id,attachment_policy,created_at)"
            " VALUES(?,?,?,?)",
            (
                verified.parent_run_id,
                verified.resolved_run_id,
                verified.attachment_policy.value,
                now,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_child_link_write")
        _fault(fault, "workflow:runtime_start:before_child_command_write")
        tx.connection.execute(
            "INSERT INTO"
            " child_commands(command_id,parent_run_id,child_run_id,ticket_id,workflow_ticket_receipt_id,state,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,NULL,?,'pending',?,?)",
            (
                verified.child_command_id,
                verified.parent_run_id,
                verified.resolved_run_id,
                verified.ticket_receipt_id,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_child_command_write")
        event_id = f"{verified.resolved_run_id}:workflow-start-created"
        _fault(fault, "workflow:runtime_start:before_event_write")
        self._insert_event(
            tx.connection,
            event_id=event_id,
            run_id=verified.resolved_run_id,
            kind="run.created",
            payload={"profile_key": verified.profile_key, "driver_kind": "workflow"},
            now=now,
        )
        _fault(fault, "workflow:runtime_start:after_event_write")
        _fault(fault, "workflow:runtime_start:before_runtime_lease_write")
        tx.connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?)",
            (
                verified.resolved_run_id,
                RUNTIME_LEASE_NAMESPACE,
                claim.owner_id,
                1,
                expires_at,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_runtime_lease_write")
        _fault(fault, "workflow:runtime_start:before_run_fence_write")
        tx.connection.execute(
            "INSERT INTO"
            " run_fences(run_id,owner_id,runtime_lease_epoch,epoch,state,acquired_at,released_at)"
            " VALUES(?,?,?,?, 'active',?,NULL)",
            (verified.resolved_run_id, claim.owner_id, 1, 1, now),
        )
        _fault(fault, "workflow:runtime_start:after_run_fence_write")
        _fault(fault, "workflow:runtime_start:before_receipt_write")
        tx.connection.execute(
            "INSERT INTO"
            " runtime_start_receipts(ticket_receipt_id,run_id,trace_id,thread_id,committed_run_version,start_snapshot_hash,workflow_request_hash,created_at)"  # noqa: E501
            " VALUES(?,?,?,?,1,?,?,?)",
            (
                ticket.ticket_receipt_id,
                verified.resolved_run_id,
                verified.resolved_trace_id,
                verified.resolved_thread_id,
                snapshot_hash,
                workflow_request_hash,
                now,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_receipt_write")
        claim_id = self._derived_id("runtime-start-dispatch/v1", ticket.ticket_receipt_id)
        _fault(fault, "workflow:runtime_start:before_dispatch_claim_write")
        tx.connection.execute(
            "INSERT INTO"
            " runtime_start_dispatch_claims(claim_id,ticket_receipt_id,run_id,owner_id,runtime_lease_epoch,claim_epoch,expires_at,version,state,created_at,updated_at)"  # noqa: E501
            " VALUES(?,?,?,?,1,1,?,0,'claimed',?,?)",
            (
                claim_id,
                ticket.ticket_receipt_id,
                verified.resolved_run_id,
                claim.owner_id,
                expires_at,
                now,
                now,
            ),
        )
        _fault(fault, "workflow:runtime_start:after_dispatch_claim_write")
        tx.register_after_commit_fault("workflow:runtime_start:after_commit")
        receipt_row = tx.connection.execute(
            "SELECT * FROM runtime_start_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        assert receipt_row is not None
        execution_lease = ExecutionLease(
            verified.resolved_run_id,
            RUNTIME_LEASE_NAMESPACE,
            claim.owner_id,
            1,
            expires_at,
        )
        fence = RunFenceLease(
            run_id=RunId(verified.resolved_run_id),
            epoch=1,
            owner_id=claim.owner_id,
            runtime_lease_epoch=1,
        )
        return RuntimeStartAdmission(
            receipt=_runtime_start_receipt(receipt_row),
            disposition=RuntimeStartDisposition.START_NEW,
            activation=RuntimeStartActivation(execution_lease, fence),
            dispatch_claim=RuntimeStartDispatchClaim(
                claim_id, verified.resolved_run_id, claim.owner_id, 1, 1
            ),
        )

    async def resume_admitted_runtime_start(
        self,
        transaction: WorkflowTransaction,
        ticket: WorkflowLaunchTicket,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RuntimeStartAdmission:
        tx = self._assert_open_workflow_transaction(transaction)
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        receipt_row = tx.connection.execute(
            "SELECT * FROM runtime_start_receipts WHERE ticket_receipt_id=?",
            (ticket.ticket_receipt_id,),
        ).fetchone()
        if (
            ticket_row is None
            or _workflow_launch_ticket(ticket_row) != ticket
            or receipt_row is None
        ):
            raise UnitOfWorkConflict("workflow child Runtime admission receipt is missing")
        return self._classify_runtime_start_admission(
            tx.connection,
            receipt_row,
            claim=claim,
            now=_time(now),
            fault=fault,
        )

    async def resume_spawn_child_start(
        self,
        transaction: WorkflowTransaction,
        child_run_id: str,
        claim: RuntimeActivationClaim,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RuntimeStartAdmission:
        tx = self._assert_open_workflow_transaction(transaction)
        child_run_id = _required(child_run_id, "child_run_id")
        command = tx.connection.execute(
            "SELECT workflow_ticket_receipt_id FROM child_commands WHERE child_run_id=?",
            (child_run_id,),
        ).fetchone()
        if command is None or command["workflow_ticket_receipt_id"] is None:
            raise UnitOfWorkConflict("workflow child has no durable workflow launch ticket")
        ticket_row = tx.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (str(command["workflow_ticket_receipt_id"]),),
        ).fetchone()
        if ticket_row is None:
            raise UnitOfWorkConflict("workflow child launch ticket disappeared")
        return await self.resume_admitted_runtime_start(
            transaction,
            _workflow_launch_ticket(ticket_row),
            claim,
            now=now,
            fault=fault,
        )

    def _runtime_retry_wake(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        run_version: int,
        resume_row: sqlite3.Row | None,
    ):
        from simple_harness.workflow.execution_ports import WorkflowRetryWake

        if resume_row is None or str(resume_row["phase"]) != "retry_wait":
            return None
        receipt = self._resume_receipt(resume_row, connection=connection, include_activation=False)
        if receipt.next_attempt_at is None:
            raise UnitOfWorkConflict("retry wait receipt has no due time")
        try:
            outcome = _json_object(
                json.loads(str(resume_row["outcome_json"])), "retry wait outcome"
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise UnitOfWorkConflict("retry wait outcome is invalid") from exc
        receipt_id = receipt.request.receipt_id
        receipt_version = receipt.version
        mode = receipt.request.mode
        due_at = receipt.next_attempt_at
        try:
            wait_event_id = _required(outcome.get("wait_event_id"), "wait_event_id")
        except ValueError as exc:
            raise UnitOfWorkConflict("retry wait outcome is invalid") from exc
        start = connection.execute(
            "SELECT request_json FROM workflow_start_admissions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if start is None:
            raise UnitOfWorkConflict("retry wait lacks pinned start authority")
        start_request = start_admission_request_from_json(
            _json_object(json.loads(str(start["request_json"])), "start request")
        )
        head = connection.execute(
            "SELECT checkpoint_id,checkpoint_json,checkpoint_hash FROM "
            "workflow_checkpoints WHERE run_id=? AND namespace=? "
            "ORDER BY version DESC LIMIT 1",
            (run_id, start_request.checkpoint_namespace),
        ).fetchone()
        if (
            head is None
            or str(head["checkpoint_id"]) != receipt.request.expected_checkpoint_head
            or hashlib.sha256(str(head["checkpoint_json"]).encode()).hexdigest()
            != str(head["checkpoint_hash"])
        ):
            raise UnitOfWorkConflict("retry wait checkpoint authority changed")
        durable_pending = tuple(
            sorted(
                (
                    str(item["decision_id"]),
                    hashlib.sha256(str(item["request_json"]).encode()).hexdigest(),
                )
                for item in connection.execute(
                    "SELECT decision_id,request_json FROM decisions "
                    "WHERE run_id=? AND state!='open'",
                    (run_id,),
                ).fetchall()
            )
        )
        if durable_pending != receipt.request.pending_interrupts:
            raise UnitOfWorkConflict("retry wait interrupt authority changed")
        core: dict[str, JsonValue] = {
            "run_id": run_id,
            "receipt_id": receipt_id,
            "receipt_version": receipt_version,
            "mode": mode.value,
            "due_at": due_at,
            "wait_event_id": wait_event_id,
            "generic_run_version": run_version,
            "request_fingerprint": receipt.request_fingerprint,
            "responses_hash": receipt.request.responses_hash,
            "expected_checkpoint_head": receipt.request.expected_checkpoint_head,
        }
        outcome_hash = hashlib.sha256(canonical_json(core).encode()).hexdigest()
        if (
            outcome.get("status") != "retryable"
            or outcome.get("generic_run_version") != run_version
            or outcome.get("outcome_hash") != outcome_hash
        ):
            raise UnitOfWorkConflict("retry wait outcome binding changed")
        event = connection.execute(
            "SELECT kind,payload_json FROM run_events WHERE run_id=? AND event_id=?",
            (run_id, wait_event_id),
        ).fetchone()
        if (
            event is None
            or str(event["kind"]) != "workflow.retry_waiting"
            or canonical_json(json.loads(str(event["payload_json"]))) != canonical_json(core)
        ):
            raise UnitOfWorkConflict("retry wait event binding changed")
        if (
            connection.execute(
                "SELECT 1 FROM workflow_leases WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            is not None
        ):
            raise UnitOfWorkConflict("retry wait retained an activation lease")
        fence = connection.execute(
            "SELECT state FROM run_fences WHERE run_id=?", (run_id,)
        ).fetchone()
        if fence is None or str(fence["state"]) != "released":
            raise UnitOfWorkConflict("retry wait retained an active Run fence")
        return WorkflowRetryWake(
            run_id,
            receipt_id,
            receipt_version,
            mode,
            due_at,
            wait_event_id,
            run_version,
            outcome_hash,
        )

    @staticmethod
    def _read_workflow_terminal_outcome(connection: sqlite3.Connection, run_id: str):
        from simple_harness.workflow.execution_ports import WorkflowTerminalOutcome

        row = connection.execute(
            "SELECT * FROM workflow_terminal_receipts WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        delivery_ids_raw = json.loads(str(row["delivery_ids_json"]))
        if not isinstance(delivery_ids_raw, list) or not all(
            isinstance(item, str) for item in delivery_ids_raw
        ):
            raise UnitOfWorkConflict("terminal delivery identity is invalid")
        return WorkflowTerminalOutcome(
            str(row["receipt_id"]),
            run_id,
            str(row["checkpoint_id"]),
            str(row["state"]),
            str(row["event_id"]),
            tuple(delivery_ids_raw),
            str(row["outcome_hash"]),
        )

    def read_workflow_terminal_outcome(self, run_id: str):
        return self._read_workflow_terminal_outcome(self.database.connection, run_id)

    @staticmethod
    def _verify_workflow_terminal_unchecked(
        connection: sqlite3.Connection, outcome: object
    ) -> bool:
        from simple_harness.workflow.execution_ports import WorkflowTerminalOutcome

        if not isinstance(outcome, WorkflowTerminalOutcome):
            return False
        row = connection.execute(
            "SELECT * FROM workflow_terminal_receipts WHERE receipt_id=? AND run_id=?",
            (outcome.receipt_id, outcome.run_id),
        ).fetchone()
        if row is None:
            return False
        delivery_ids = tuple(json.loads(str(row["delivery_ids_json"])))
        durable = WorkflowTerminalOutcome(
            str(row["receipt_id"]),
            str(row["run_id"]),
            str(row["checkpoint_id"]),
            str(row["state"]),
            str(row["event_id"]),
            delivery_ids,
            str(row["outcome_hash"]),
        )
        if durable != outcome:
            return False
        terminal_payload = json.loads(str(row["terminal_payload_json"]))
        delivery_facts = json.loads(str(row["delivery_facts_json"]))
        if not isinstance(delivery_facts, list):
            return False
        canonical_outcome: dict[str, JsonValue] = {
            "receipt_id": outcome.receipt_id,
            "run_id": outcome.run_id,
            "checkpoint_id": outcome.checkpoint_id,
            "state": outcome.state,
            "event_id": outcome.event_id,
            "delivery_ids": list(outcome.delivery_ids),
            "terminal_payload": terminal_payload,
            "delivery_facts": delivery_facts,
        }
        if (
            hashlib.sha256(canonical_json(canonical_outcome).encode()).hexdigest()
            != outcome.outcome_hash
        ):
            return False
        run = connection.execute(
            "SELECT state,version FROM runs WHERE run_id=?", (outcome.run_id,)
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT checkpoint_id,checkpoint_json,checkpoint_hash,namespace,version "
            "FROM workflow_checkpoints WHERE run_id=? AND namespace=? "
            "ORDER BY version DESC LIMIT 1",
            (outcome.run_id, str(row["checkpoint_namespace"])),
        ).fetchone()
        event = connection.execute(
            "SELECT kind,payload_json FROM run_events WHERE run_id=? AND event_id=?",
            (outcome.run_id, outcome.event_id),
        ).fetchone()
        fence_receipt = connection.execute(
            "SELECT owner_id,runtime_lease_epoch,run_fence_epoch "
            "FROM workflow_terminal_fence_receipts "
            "WHERE receipt_id=? AND run_id=?",
            (str(row["terminal_fence_receipt_id"]), outcome.run_id),
        ).fetchone()
        fence = connection.execute(
            "SELECT owner_id,runtime_lease_epoch,epoch,state FROM run_fences WHERE run_id=?",
            (outcome.run_id,),
        ).fetchone()
        start = connection.execute(
            "SELECT json_extract(request_json,'$.checkpoint_namespace') AS namespace "
            "FROM workflow_start_admissions WHERE run_id=?",
            (outcome.run_id,),
        ).fetchone()
        if (
            run is None
            or str(run["state"]) != outcome.state
            or int(run["version"]) != int(row["run_version"])
            or checkpoint is None
            or str(checkpoint["checkpoint_id"]) != outcome.checkpoint_id
            or str(checkpoint["namespace"]) != str(row["checkpoint_namespace"])
            or int(checkpoint["version"]) != int(row["checkpoint_version"])
            or str(checkpoint["checkpoint_hash"]) != str(row["checkpoint_hash"])
            or hashlib.sha256(str(checkpoint["checkpoint_json"]).encode()).hexdigest()
            != str(checkpoint["checkpoint_hash"])
            or not isinstance(
                checkpoint_payload := json.loads(str(checkpoint["checkpoint_json"])),
                dict,
            )
            or checkpoint_payload.get("checkpoint_id") != outcome.checkpoint_id
            or event is None
            or str(event["kind"])
            != {
                "completed": "run.completed",
                "failed": "run.failed",
                "cancelled": "run.cancelled",
            }.get(outcome.state)
            or hashlib.sha256(str(event["payload_json"]).encode()).hexdigest()
            != str(row["event_payload_hash"])
            or fence_receipt is None
            or fence is None
            or str(fence["state"]) != "released"
            or start is None
            or str(start["namespace"]) != str(row["checkpoint_namespace"])
            or tuple(fence_receipt)
            != (
                str(fence["owner_id"]),
                int(fence["runtime_lease_epoch"]),
                int(fence["epoch"]),
            )
            or connection.execute(
                "SELECT 1 FROM workflow_leases WHERE run_id=? LIMIT 1",
                (outcome.run_id,),
            ).fetchone()
            is not None
        ):
            return False
        if tuple(sorted(outcome.delivery_ids)) != outcome.delivery_ids:
            return False
        actual_delivery_ids = tuple(
            str(item["delivery_id"])
            for item in connection.execute(
                "SELECT delivery_id FROM delivery_outbox WHERE run_id=? ORDER BY delivery_id",
                (outcome.run_id,),
            ).fetchall()
        )
        if actual_delivery_ids != outcome.delivery_ids:
            return False
        actual_facts: list[dict[str, JsonValue]] = []
        for delivery_id in outcome.delivery_ids:
            delivery = connection.execute(
                "SELECT delivery_id,sink_kind,idempotency_key,payload_json,state,created_at "
                "FROM delivery_outbox WHERE run_id=? AND delivery_id=?",
                (outcome.run_id, delivery_id),
            ).fetchone()
            if delivery is None or str(delivery["state"]) not in {
                "pending",
                "claimed",
                "delivered",
                "failed",
                "released",
            }:
                return False
            actual_facts.append(
                {
                    "delivery_id": str(delivery["delivery_id"]),
                    "sink_kind": str(delivery["sink_kind"]),
                    "idempotency_key": str(delivery["idempotency_key"]),
                    "payload": json.loads(str(delivery["payload_json"])),
                    "created_at": float(delivery["created_at"]),
                }
            )
        return canonical_json(cast(JsonValue, actual_facts)) == canonical_json(
            cast(JsonValue, delivery_facts)
        )

    @classmethod
    def _verify_workflow_terminal(cls, connection: sqlite3.Connection, outcome: object) -> bool:
        try:
            return cls._verify_workflow_terminal_unchecked(connection, outcome)
        except (
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return False

    def verify_workflow_terminal(self, outcome: object) -> bool:
        return self._verify_workflow_terminal(self.database.connection, outcome)

    def _require_runtime_start_shape(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_row: sqlite3.Row,
        run: sqlite3.Row,
        dispatch: sqlite3.Row,
        start_receipt: sqlite3.Row | None,
        resume_receipt: sqlite3.Row | None,
    ) -> None:
        """Validate immutable generic/workflow admission cross-authority."""

        run_id = str(receipt_row["run_id"])
        ticket_row = connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts WHERE ticket_receipt_id=?",
            (str(receipt_row["ticket_receipt_id"]),),
        ).fetchone()
        if ticket_row is None:
            raise UnitOfWorkConflict("generic Run identity lacks launch ticket")
        verified = _verified_workflow_launch_ticket(ticket_row)
        expected_run = (
            verified.resolved_run_id,
            verified.session_id,
            verified.request_id,
            verified.root_run_id,
            verified.parent_run_id,
            verified.profile_key,
            "workflow",
        )
        actual_run = (
            str(run["run_id"]),
            str(run["execution_session_id"]),
            str(run["request_id"]),
            str(run["root_run_id"]),
            None if run["parent_run_id"] is None else str(run["parent_run_id"]),
            str(run["profile_key"]),
            str(run["driver_kind"]),
        )
        if actual_run != expected_run or (
            str(receipt_row["trace_id"]),
            str(receipt_row["thread_id"]),
        ) != (verified.resolved_trace_id, verified.resolved_thread_id):
            raise UnitOfWorkConflict("generic Run identity differs from launch ticket")
        expected_claim_id = self._derived_id(
            "runtime-start-dispatch/v1", verified.ticket_receipt_id
        )
        if (
            str(dispatch["claim_id"]) != expected_claim_id
            or str(dispatch["ticket_receipt_id"]) != verified.ticket_receipt_id
            or str(dispatch["run_id"]) != run_id
            or str(dispatch["state"]) not in {"claimed", "consumed"}
        ):
            raise UnitOfWorkConflict("Runtime start dispatch identity differs")

        run_state = str(run["state"])
        if run_state not in {
            RunState.RUNNING.value,
            RunState.WAITING.value,
            RunState.CANCEL_REQUESTED.value,
            RunState.COMPLETED.value,
            RunState.FAILED.value,
            RunState.CANCELLED.value,
        }:
            raise UnitOfWorkConflict("workflow Runtime admission requires RUNNING state algebra")
        if start_receipt is None:
            if resume_receipt is not None or str(dispatch["state"]) != "claimed":
                raise UnitOfWorkConflict("Runtime start phase matrix differs")
            if run_state not in {
                RunState.RUNNING.value,
                RunState.CANCEL_REQUESTED.value,
            }:
                raise UnitOfWorkConflict("orphan Runtime start must remain RUNNING")
            return

        if str(dispatch["state"]) != "consumed":
            raise UnitOfWorkConflict("bound workflow retained a claimed dispatch")
        start_request_value = json.loads(str(start_receipt["request_json"]))
        if not isinstance(start_request_value, dict):
            raise UnitOfWorkConflict("stored workflow start request is invalid")
        start_request = start_admission_request_from_json(start_request_value)
        payload_json, fingerprint, start_run, trace_id, thread_id = self._start_identity(
            start_request
        )
        if (
            start_request.mode is not StartMode.PRECREATED
            or str(start_receipt["request_key"]) != verified.ticket_receipt_id
            or str(start_receipt["request_id"]) != verified.request_id
            or str(start_receipt["run_id"]) != run_id
            or start_run != run_id
            or str(start_receipt["trace_id"]) != trace_id
            or str(start_receipt["thread_id"]) != thread_id
            or str(start_receipt["request_fingerprint"]) != fingerprint
            or str(start_receipt["request_json"]) != payload_json
        ):
            raise UnitOfWorkConflict("workflow start receipt identity differs")
        start_phase = str(start_receipt["phase"])
        resume_phase = None if resume_receipt is None else str(resume_receipt["phase"])
        if run_state == RunState.RUNNING.value:
            if start_phase not in {StartPhase.CLAIMED.value, StartPhase.RUNNING.value}:
                raise UnitOfWorkConflict("RUNNING workflow start phase differs")
            if resume_phase not in {None, ResumePhase.ADMITTED.value, ResumePhase.CLAIMED.value}:
                raise UnitOfWorkConflict("RUNNING workflow resume phase differs")
        elif run_state == RunState.WAITING.value:
            if start_phase != StartPhase.RUNNING.value or resume_phase not in {
                None,
                ResumePhase.RETRY_WAIT.value,
            }:
                raise UnitOfWorkConflict("WAITING workflow phase differs")
        elif run_state == RunState.CANCEL_REQUESTED.value:
            if start_phase not in {
                StartPhase.CLAIMED.value,
                StartPhase.RUNNING.value,
                StartPhase.SETTLED.value,
            } or resume_phase not in {
                None,
                ResumePhase.ADMITTED.value,
                ResumePhase.CLAIMED.value,
                ResumePhase.RETRY_WAIT.value,
                ResumePhase.SETTLED.value,
            }:
                raise UnitOfWorkConflict("cancel workflow phase differs")
        elif start_phase not in {StartPhase.RUNNING.value, StartPhase.SETTLED.value}:
            raise UnitOfWorkConflict("terminal/cancel workflow phase differs")

    @staticmethod
    def _require_live_runtime_start_cofence(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        runtime: sqlite3.Row,
        fence: sqlite3.Row,
        dispatch: sqlite3.Row,
        start_receipt: sqlite3.Row | None,
        resume_receipt: sqlite3.Row | None,
        now: float,
    ) -> None:
        owner_id = str(runtime["owner_id"])
        runtime_epoch = int(runtime["epoch"])
        expires_at = float(runtime["expires_at"])
        if (
            expires_at <= now
            or str(fence["state"]) != "active"
            or str(fence["owner_id"]) != owner_id
            or int(fence["runtime_lease_epoch"]) != runtime_epoch
        ):
            raise UnitOfWorkConflict("live Runtime start authorities are split-brain")
        if start_receipt is None:
            if (
                str(dispatch["state"]) != "claimed"
                or str(dispatch["owner_id"]) != owner_id
                or int(dispatch["runtime_lease_epoch"]) != runtime_epoch
                or float(dispatch["expires_at"]) <= now
                or float(dispatch["expires_at"]) != expires_at
            ):
                raise UnitOfWorkConflict("live Runtime dispatch is not claimable")
            return
        if str(dispatch["state"]) != "consumed":
            raise UnitOfWorkConflict("bound workflow retained a claimed dispatch")
        request_payload = json.loads(str(start_receipt["request_json"]))
        namespace = _required(request_payload.get("checkpoint_namespace"), "checkpoint_namespace")
        projection = connection.execute(
            "SELECT owner_id,epoch,expires_at FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, namespace),
        ).fetchone()
        receipt = resume_receipt if resume_receipt is not None else start_receipt
        if (
            projection is None
            or str(projection["owner_id"]) != owner_id
            or int(projection["epoch"]) != runtime_epoch
            or float(projection["expires_at"]) != expires_at
            or receipt["claim_owner"] is None
            or str(receipt["claim_owner"]) != owner_id
            or receipt["claim_epoch"] is None
            or int(receipt["claim_epoch"]) != runtime_epoch
        ):
            raise UnitOfWorkConflict("live workflow projection is split-brain")

    @classmethod
    def _require_waiting_authority_shape(
        cls,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        runtime: sqlite3.Row | None,
        fence: sqlite3.Row | None,
        dispatch: sqlite3.Row,
        start_receipt: sqlite3.Row,
        resume_receipt: sqlite3.Row | None,
        now: float,
    ) -> None:
        request_payload = json.loads(str(start_receipt["request_json"]))
        namespace = _required(request_payload.get("checkpoint_namespace"), "checkpoint_namespace")
        workflow = connection.execute(
            "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, namespace),
        ).fetchone()
        if runtime is None and workflow is None:
            if fence is None or str(fence["state"]) != "released":
                raise UnitOfWorkConflict("released WAITING authority is incomplete")
            return
        if runtime is None or workflow is None or fence is None:
            raise UnitOfWorkConflict("WAITING authority is partially released")
        cls._require_live_runtime_start_cofence(
            connection,
            run_id=run_id,
            runtime=runtime,
            fence=fence,
            dispatch=dispatch,
            start_receipt=start_receipt,
            resume_receipt=resume_receipt,
            now=now,
        )

    def _classify_runtime_start_admission(
        self,
        connection: sqlite3.Connection,
        receipt_row: sqlite3.Row,
        *,
        claim: RuntimeActivationClaim,
        now: float,
        fault: FaultHook | None,
    ) -> RuntimeStartAdmission:
        from simple_harness.runtime.orchestration import (
            RuntimeStartActivation,
            RuntimeStartAdmission,
            RuntimeStartDisposition,
        )
        from simple_harness.workflow.execution_ports import (
            WorkflowRecoveryReceiptKind,
            WorkflowRecoveryWork,
        )

        run_id = str(receipt_row["run_id"])
        receipt = _runtime_start_receipt(receipt_row)
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        dispatch = connection.execute(
            "SELECT * FROM runtime_start_dispatch_claims WHERE run_id=?", (run_id,)
        ).fetchone()
        runtime = connection.execute(
            "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
            (run_id, RUNTIME_LEASE_NAMESPACE),
        ).fetchone()
        fence = connection.execute("SELECT * FROM run_fences WHERE run_id=?", (run_id,)).fetchone()
        start_receipt_row = connection.execute(
            "SELECT * FROM workflow_start_admissions WHERE run_id=?", (run_id,)
        ).fetchone()
        resume_rows = connection.execute(
            "SELECT * FROM workflow_resume_admissions "
            "WHERE run_id=? AND phase IN ('admitted','claimed','retry_wait') "
            "ORDER BY created_at,receipt_id LIMIT 2",
            (run_id,),
        ).fetchall()
        if len(resume_rows) > 1:
            raise UnitOfWorkConflict("workflow has multiple unsettled resume receipts")
        resume_receipt_row = None if not resume_rows else resume_rows[0]
        if run is None or dispatch is None:
            raise UnitOfWorkConflict("runtime start authority is incomplete")
        self._require_runtime_start_shape(
            connection,
            receipt_row=receipt_row,
            run=run,
            dispatch=dispatch,
            start_receipt=start_receipt_row,
            resume_receipt=resume_receipt_row,
        )
        run_state = str(run["state"])
        if run_state == RunState.WAITING.value:
            retry_wake = self._runtime_retry_wake(
                connection,
                run_id=run_id,
                run_version=int(run["version"]),
                resume_row=resume_receipt_row,
            )
            if retry_wake is None:
                if start_receipt_row is None:
                    raise UnitOfWorkConflict("generic WAITING Run lacks pinned workflow admission")
                request_payload = json.loads(str(start_receipt_row["request_json"]))
                namespace = _required(
                    request_payload.get("checkpoint_namespace"),
                    "checkpoint_namespace",
                )
                head = connection.execute(
                    "SELECT checkpoint_id,checkpoint_json,checkpoint_hash "
                    "FROM workflow_checkpoints "
                    "WHERE run_id=? AND namespace=? ORDER BY version DESC LIMIT 1",
                    (run_id, namespace),
                ).fetchone()
                open_decision = None
                blocker = None
                if head is not None:
                    if hashlib.sha256(str(head["checkpoint_json"]).encode()).hexdigest() != str(
                        head["checkpoint_hash"]
                    ):
                        raise UnitOfWorkConflict("pinned workflow checkpoint self-hash changed")
                    checkpoint_payload = json.loads(str(head["checkpoint_json"]))
                    interrupt = (
                        checkpoint_payload.get("interrupt")
                        if isinstance(checkpoint_payload, dict)
                        else None
                    )
                    if interrupt is None:
                        interrupt_operation = connection.execute(
                            "SELECT payload_json FROM workflow_native_operations "
                            "WHERE run_id=? AND namespace=? AND base_checkpoint_id=? "
                            "AND operation_kind='interrupt' "
                            "ORDER BY created_at DESC,operation_id DESC LIMIT 1",
                            (run_id, namespace, str(head["checkpoint_id"])),
                        ).fetchone()
                        if interrupt_operation is not None:
                            operation_payload = json.loads(str(interrupt_operation["payload_json"]))
                            interrupt = (
                                operation_payload.get("interrupt")
                                if isinstance(operation_payload, dict)
                                else None
                            )
                    interrupt_id = (
                        interrupt.get("interrupt_id") if isinstance(interrupt, dict) else None
                    )
                    if isinstance(interrupt_id, str) and interrupt_id:
                        open_decision = connection.execute(
                            "SELECT 1 FROM decisions WHERE run_id=? "
                            "AND decision_id=? AND state='open' LIMIT 1",
                            (run_id, interrupt_id),
                        ).fetchone()
                    if open_decision is None:
                        open_decision = connection.execute(
                            "SELECT 1 FROM decisions WHERE run_id=? AND state='open' "
                            "AND json_extract(request_json,'$.checkpoint_id')=? "
                            "AND json_extract(request_json,'$.checkpoint_namespace')=? LIMIT 1",
                            (run_id, str(head["checkpoint_id"]), namespace),
                        ).fetchone()
                    linked_blockers = (
                        checkpoint_payload.get("wait_blocker_ids", [])
                        if isinstance(checkpoint_payload, dict)
                        else []
                    )
                    if isinstance(linked_blockers, list) and linked_blockers:
                        placeholders = ",".join("?" for _ in linked_blockers)
                        blocker = connection.execute(
                            "SELECT 1 FROM run_wait_blockers WHERE run_id=? "
                            "AND resolution_id IS NULL AND blocker_id IN ("
                            + placeholders
                            + ") LIMIT 1",
                            (run_id, *linked_blockers),
                        ).fetchone()
                if head is None or (open_decision is None and blocker is None):
                    raise UnitOfWorkConflict(
                        "generic WAITING Run lacks matching workflow authority"
                    )
                self._require_waiting_authority_shape(
                    connection,
                    run_id=run_id,
                    runtime=runtime,
                    fence=fence,
                    dispatch=dispatch,
                    start_receipt=start_receipt_row,
                    resume_receipt=resume_receipt_row,
                    now=now,
                )
            return RuntimeStartAdmission(
                receipt, RuntimeStartDisposition.WAITING, retry_wake=retry_wake
            )
        if run_state == RunState.CANCEL_REQUESTED.value:
            cancel = connection.execute(
                "SELECT phase FROM workflow_cancel_receipts WHERE run_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if cancel is None or str(cancel["phase"]) not in {
                "requested",
                "cancelling",
                "blocked",
            }:
                raise UnitOfWorkConflict("generic cancel state lacks workflow cancel authority")
            return RuntimeStartAdmission(receipt, RuntimeStartDisposition.CANCEL_PENDING)
        if run_state in {"completed", "failed", "cancelled"}:
            terminal = self._read_workflow_terminal_outcome(connection, run_id)
            if terminal is None or not self._verify_workflow_terminal(connection, terminal):
                raise UnitOfWorkConflict("terminal workflow outcome is incomplete")
            return RuntimeStartAdmission(
                receipt,
                RuntimeStartDisposition.TERMINAL,
                workflow_terminal=terminal,
            )
        live = (
            runtime is not None
            and fence is not None
            and str(fence["state"]) == "active"
            and float(runtime["expires_at"]) > now
        )
        if live:
            assert runtime is not None and fence is not None
            self._require_live_runtime_start_cofence(
                connection,
                run_id=run_id,
                runtime=runtime,
                fence=fence,
                dispatch=dispatch,
                start_receipt=start_receipt_row,
                resume_receipt=resume_receipt_row,
                now=now,
            )
            if str(runtime["owner_id"]) != claim.owner_id:
                return RuntimeStartAdmission(receipt, RuntimeStartDisposition.FOREIGN_ACTIVE)
            if start_receipt_row is not None or resume_receipt_row is not None:
                return RuntimeStartAdmission(receipt, RuntimeStartDisposition.ATTACH_CURRENT)
            if str(dispatch["state"]) != "claimed":
                raise UnitOfWorkConflict("consumed dispatch has no workflow receipt")
            activation = _runtime_start_activation(runtime, fence)
            return RuntimeStartAdmission(
                receipt,
                RuntimeStartDisposition.START_ORPHAN,
                activation=activation,
                dispatch_claim=_runtime_start_dispatch_claim(dispatch),
            )
        prior_start_receipt = (
            None
            if start_receipt_row is None or resume_receipt_row is not None
            else self._start_receipt(
                start_receipt_row,
                connection=connection,
            )
        )
        old_runtime_epoch = 0 if runtime is None else int(runtime["epoch"])
        old_fence_runtime_epoch = 0 if fence is None else int(fence["runtime_lease_epoch"])
        workflow_epoch = 0
        workflow_namespace: str | None = None
        if start_receipt_row is not None:
            workflow_namespace = json.loads(str(start_receipt_row["request_json"]))[
                "checkpoint_namespace"
            ]
            projection = connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id=? AND namespace=?",
                (run_id, workflow_namespace),
            ).fetchone()
            if projection is not None and float(projection["expires_at"]) > now:
                raise UnitOfWorkConflict("live foreign workflow projection blocks Runtime takeover")
            workflow_epoch = 0 if projection is None else int(projection["epoch"])
        next_epoch = max(old_runtime_epoch, old_fence_runtime_epoch, workflow_epoch) + 1
        expires_at = now + float(claim.lease_ttl_seconds)
        _fault(fault, "workflow:runtime_start:before_runtime_takeover_write")
        connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(run_id,namespace) DO UPDATE SET"
            " owner_id=excluded.owner_id,epoch=excluded.epoch,expires_at=excluded.expires_at",
            (run_id, RUNTIME_LEASE_NAMESPACE, claim.owner_id, next_epoch, expires_at),
        )
        _fault(fault, "workflow:runtime_start:after_runtime_takeover_write")
        if workflow_namespace is not None and resume_receipt_row is not None:
            _fault(
                fault,
                "workflow:runtime_start:before_workflow_takeover_write",
            )
            connection.execute(
                "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id,namespace) DO UPDATE SET "
                "owner_id=excluded.owner_id,epoch=excluded.epoch,"
                "expires_at=excluded.expires_at",
                (
                    run_id,
                    workflow_namespace,
                    claim.owner_id,
                    next_epoch,
                    expires_at,
                ),
            )
            _fault(
                fault,
                "workflow:runtime_start:after_workflow_takeover_write",
            )
        fence_epoch = 1 if fence is None else int(fence["epoch"]) + 1
        _fault(fault, "workflow:runtime_start:before_fence_takeover_write")
        connection.execute(
            "INSERT INTO"
            " run_fences(run_id,owner_id,runtime_lease_epoch,epoch,state,acquired_at,released_at)"
            " VALUES(?,?,?,?, 'active',?,NULL) ON CONFLICT(run_id) DO UPDATE SET"
            " owner_id=excluded.owner_id,runtime_lease_epoch=excluded.runtime_lease_epoch,epoch=excluded.epoch,state='active',acquired_at=excluded.acquired_at,released_at=NULL",  # noqa: E501
            (run_id, claim.owner_id, next_epoch, fence_epoch, now),
        )
        _fault(fault, "workflow:runtime_start:after_fence_takeover_write")
        activation = RuntimeStartActivation(
            ExecutionLease(
                run_id,
                RUNTIME_LEASE_NAMESPACE,
                claim.owner_id,
                next_epoch,
                expires_at,
            ),
            RunFenceLease(
                run_id=RunId(run_id),
                epoch=fence_epoch,
                owner_id=claim.owner_id,
                runtime_lease_epoch=next_epoch,
            ),
        )
        if start_receipt_row is None:
            if str(dispatch["state"]) != "claimed":
                raise UnitOfWorkConflict("consumed dispatch has no workflow receipt")
            _fault(fault, "workflow:runtime_start:before_dispatch_takeover_write")
            connection.execute(
                "UPDATE runtime_start_dispatch_claims SET"
                " owner_id=?,runtime_lease_epoch=?,claim_epoch=claim_epoch+1,expires_at=?,version=version+1,updated_at=?"  # noqa: E501
                " WHERE claim_id=?",
                (claim.owner_id, next_epoch, expires_at, now, str(dispatch["claim_id"])),
            )
            _fault(fault, "workflow:runtime_start:after_dispatch_takeover_write")
            updated_dispatch = connection.execute(
                "SELECT * FROM runtime_start_dispatch_claims WHERE claim_id=?",
                (str(dispatch["claim_id"]),),
            ).fetchone()
            assert updated_dispatch is not None
            return RuntimeStartAdmission(
                receipt,
                RuntimeStartDisposition.START_ORPHAN,
                activation=activation,
                dispatch_claim=_runtime_start_dispatch_claim(updated_dispatch),
            )
        if resume_receipt_row is not None:
            resume_receipt = self._resume_receipt(
                resume_receipt_row,
                connection=connection,
                include_activation=False,
            )
            work = WorkflowRecoveryWork(
                run_id=run_id,
                receipt_kind=WorkflowRecoveryReceiptKind.RESUME,
                receipt_id=resume_receipt.request.receipt_id,
                receipt_version=resume_receipt.version,
                mode=resume_receipt.request.mode,
                due_at=(
                    None
                    if resume_receipt_row["next_attempt_at"] is None
                    else float(resume_receipt_row["next_attempt_at"])
                ),
                request_fingerprint=resume_receipt.request_fingerprint,
                receipt_snapshot=resume_receipt,
            )
            return RuntimeStartAdmission(
                receipt,
                RuntimeStartDisposition.RECOVER_RESUME,
                activation=activation,
                recovery_work=work,
            )
        assert prior_start_receipt is not None
        start_receipt = prior_start_receipt
        if start_receipt.request.mode is not StartMode.PRECREATED:
            raise UnitOfWorkConflict("standalone start cannot be Runtime-recovered")
        work = WorkflowRecoveryWork(
            run_id=run_id,
            receipt_kind=WorkflowRecoveryReceiptKind.START,
            receipt_id=start_receipt.request_key,
            receipt_version=start_receipt.version,
            mode=start_receipt.request.mode,
            due_at=None,
            request_fingerprint=start_receipt.request_fingerprint,
            receipt_snapshot=start_receipt,
        )
        return RuntimeStartAdmission(
            receipt,
            RuntimeStartDisposition.RECOVER_START,
            activation=activation,
            recovery_work=work,
        )

    def list_orphaned_forks(
        self, snapshot_cursor: str | None, *, now: float
    ) -> tuple[tuple[ForkReceipt, ...], str | None]:
        rows = self.database.connection.execute(
            "SELECT * FROM workflow_fork_receipts WHERE phase IN"
            " ('prepared','claimed','checkpointed') AND (? IS NULL OR fork_id>?) AND"
            " (claim_expires_at IS NULL OR claim_expires_at<=?) ORDER BY fork_id LIMIT 101",
            (snapshot_cursor, snapshot_cursor, now),
        ).fetchall()
        return tuple(self._fork_receipt(row) for row in rows[:100]), (
            None if len(rows) <= 100 else str(rows[99]["fork_id"])
        )


def _workflow_catalog_profiles_json(authority: WorkflowCatalogAuthority) -> str:
    return canonical_json([_workflow_catalog_profile_json(item) for item in authority.profiles])


def _workflow_catalog_profile_json(item: object) -> dict[str, JsonValue]:
    from simple_harness.runtime.orchestration import WorkflowCatalogProfileBinding

    if not isinstance(item, WorkflowCatalogProfileBinding):
        raise TypeError("workflow catalog profile binding must be typed")
    return {
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
            else _thaw(item.terminal_projection_descriptor)
        ),
        "terminal_request_factory_hash": item.terminal_request_factory_hash,
        "capability_snapshot": _thaw(item.capability_snapshot),
    }


def _validate_workflow_page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("workflow page limit must be between 1 and 50")


def _workflow_catalog_authority(row: sqlite3.Row) -> WorkflowCatalogAuthority:
    from simple_harness.runtime.orchestration import (
        WorkflowCatalogAuthority,
        WorkflowCatalogProfileBinding,
    )

    raw_profiles = json.loads(str(row["canonical_profiles"]))
    if not isinstance(raw_profiles, list):
        raise UnitOfWorkConflict("stored workflow catalog profiles are invalid")
    profiles = []
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            raise UnitOfWorkConflict("stored workflow catalog profile is invalid")
        profiles.append(
            WorkflowCatalogProfileBinding(
                profile_key=_required(raw.get("profile_key"), "profile.profile_key"),
                description=_required(raw.get("description"), "profile.description"),
                use_when=_required(raw.get("use_when"), "profile.use_when"),
                avoid_when=_required(raw.get("avoid_when"), "profile.avoid_when"),
                input_schema_ref=_required(raw.get("input_schema_ref"), "profile.input_schema_ref"),
                profile_fingerprint=_required(
                    raw.get("profile_fingerprint"), "profile.profile_fingerprint"
                ),
                workflow_name=_required(raw.get("workflow_name"), "profile.workflow_name"),
                workflow_version=_required(raw.get("workflow_version"), "profile.workflow_version"),
                implementation_fingerprint=_required(
                    raw.get("implementation_fingerprint"),
                    "profile.implementation_fingerprint",
                ),
                checkpoint_namespace=_required(
                    raw.get("checkpoint_namespace"), "profile.checkpoint_namespace"
                ),
                manifest_hash=_required(raw.get("manifest_hash"), "profile.manifest_hash"),
                state_schema_version=int(raw.get("state_schema_version", 0)),
                start_input_schema=_start_input_schema_from_json(raw.get("start_input_schema")),
                terminal_projection_descriptor=_optional_json_object(
                    raw.get("terminal_projection_descriptor"),
                    "profile.terminal_projection_descriptor",
                ),
                terminal_request_factory_hash=(
                    None
                    if raw.get("terminal_request_factory_hash") is None
                    else _required(
                        raw.get("terminal_request_factory_hash"),
                        "profile.terminal_request_factory_hash",
                    )
                ),
                capability_snapshot=_json_object(
                    raw.get("capability_snapshot"), "profile.capability_snapshot"
                ),
            )
        )
    return WorkflowCatalogAuthority(
        authority_id=str(row["authority_id"]),
        generation=int(row["generation"]),
        version=int(row["version"]),
        catalog_hash=str(row["catalog_hash"]),
        profiles=tuple(profiles),
    )


def _start_input_schema_from_json(value: object):  # type: ignore[no-untyped-def]
    from simple_harness.runtime.orchestration import StartInputSchema

    raw = _json_object(value, "start_input_schema")
    return StartInputSchema(
        schema_ref=_required(raw.get("schema_ref"), "start_input_schema.schema_ref"),
        canonical_schema=_json_object(
            raw.get("canonical_schema"), "start_input_schema.canonical_schema"
        ),
        schema_hash=_required(raw.get("schema_hash"), "start_input_schema.schema_hash"),
    )


def _json_object(value: object, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise UnitOfWorkConflict(f"stored {name} is not a JSON object")
    return cast(dict[str, JsonValue], value)


def _optional_json_object(value: object, name: str) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return _json_object(value, name)


def _workflow_launch_request_json(
    request: WorkflowLaunchRequest,
) -> dict[str, JsonValue]:
    return {
        "request_key": request.request_key,
        "candidate_id": request.candidate_id,
        "profile_key": request.profile_key,
        "catalog_generation": request.catalog_generation,
        "session_id": request.session_id,
        "request_id": request.request_id,
        "turn_id": request.turn_id,
        "requested_run_id": request.requested_run_id,
        "requested_trace_id": request.requested_trace_id,
        "requested_thread_id": request.requested_thread_id,
        "tool_catalog_generation": request.tool_catalog_generation,
        "objective": request.objective,
        "start_input": _thaw(request.start_input),
        "spawn_origin": request.spawn_origin.to_json(),
        "root_run_id": request.root_run_id,
        "attachment_policy": request.attachment_policy.value,
        "child_command_id": request.child_command_id,
    }


def _workflow_launch_request_from_json(value: object) -> WorkflowLaunchRequest:
    from simple_harness.runtime.orchestration import (
        WorkflowLaunchRequest,
        WorkflowSpawnOrigin,
    )

    raw = _json_object(value, "workflow launch request")
    expected_fields = {
        "request_key",
        "candidate_id",
        "profile_key",
        "catalog_generation",
        "session_id",
        "request_id",
        "turn_id",
        "requested_run_id",
        "requested_trace_id",
        "requested_thread_id",
        "tool_catalog_generation",
        "objective",
        "start_input",
        "spawn_origin",
        "root_run_id",
        "attachment_policy",
        "child_command_id",
    }
    if set(raw) != expected_fields:
        raise UnitOfWorkConflict("stored workflow launch request fields differ")
    origin = _json_object(raw["spawn_origin"], "workflow launch spawn_origin")
    if set(origin) != {
        "parent_run_id",
        "parent_request_id",
        "turn_id",
        "internal_tool_call_id",
    }:
        raise UnitOfWorkConflict("stored workflow launch origin fields differ")
    candidate = raw["candidate_id"]
    if candidate is not None and not isinstance(candidate, str):
        raise UnitOfWorkConflict("stored workflow launch candidate_id is malformed")
    optional_identifiers: dict[str, str | None] = {}
    for field_name in (
        "requested_run_id",
        "requested_trace_id",
        "requested_thread_id",
    ):
        item = raw[field_name]
        if item is not None and not isinstance(item, str):
            raise UnitOfWorkConflict(f"stored workflow launch {field_name} is malformed")
        optional_identifiers[field_name] = item
    catalog_generation = raw["catalog_generation"]
    tool_catalog_generation = raw["tool_catalog_generation"]
    if (
        isinstance(catalog_generation, bool)
        or not isinstance(catalog_generation, int)
        or isinstance(tool_catalog_generation, bool)
        or not isinstance(tool_catalog_generation, int)
    ):
        raise UnitOfWorkConflict("stored workflow launch generations are malformed")
    try:
        return WorkflowLaunchRequest(
            request_key=_required(raw["request_key"], "request_key"),
            candidate_id=candidate,
            profile_key=_required(raw["profile_key"], "profile_key"),
            catalog_generation=catalog_generation,
            session_id=_required(raw["session_id"], "session_id"),
            request_id=_required(raw["request_id"], "request_id"),
            turn_id=_required(raw["turn_id"], "turn_id"),
            requested_run_id=optional_identifiers["requested_run_id"],
            requested_trace_id=optional_identifiers["requested_trace_id"],
            requested_thread_id=optional_identifiers["requested_thread_id"],
            tool_catalog_generation=tool_catalog_generation,
            objective=_required(raw["objective"], "objective"),
            start_input=_json_object(raw["start_input"], "workflow launch start_input"),
            spawn_origin=WorkflowSpawnOrigin(
                parent_run_id=_required(origin["parent_run_id"], "parent_run_id"),
                parent_request_id=_required(origin["parent_request_id"], "parent_request_id"),
                turn_id=_required(origin["turn_id"], "turn_id"),
                internal_tool_call_id=_required(
                    origin["internal_tool_call_id"], "internal_tool_call_id"
                ),
            ),
            root_run_id=_required(raw["root_run_id"], "root_run_id"),
            attachment_policy=AttachmentPolicy(
                _required(raw["attachment_policy"], "attachment_policy")
            ),
            child_command_id=_required(raw["child_command_id"], "child_command_id"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise UnitOfWorkConflict("stored workflow launch request is malformed") from exc


def _workflow_launch_ticket(row: sqlite3.Row) -> WorkflowLaunchTicket:
    from simple_harness.runtime.orchestration import WorkflowLaunchTicket

    return WorkflowLaunchTicket(
        ticket_receipt_id=str(row["ticket_receipt_id"]),
        payload_hash=str(row["payload_hash"]),
        candidate_id=(None if row["candidate_id"] is None else str(row["candidate_id"])),
        profile_key=str(row["profile_key"]),
        catalog_generation=int(row["catalog_generation"]),
    )


def _workflow_spawn_continuation_claim(
    row: sqlite3.Row,
) -> WorkflowSpawnContinuationClaim:
    from simple_harness.runtime.orchestration import WorkflowSpawnContinuationClaim

    return WorkflowSpawnContinuationClaim(
        spawn_operation_id=str(row["operation_id"]),
        ticket_receipt_id=str(row["ticket_receipt_id"]),
        parent_run_id=str(row["parent_run_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        run_fence_epoch=int(row["run_fence_epoch"]),
        workflow_lease_epoch=(
            None if row["workflow_lease_epoch"] is None else int(row["workflow_lease_epoch"])
        ),
        claim_epoch=int(row["claim_epoch"]),
        expires_at=float(row["expires_at"]),
        version=int(row["version"]),
    )


def _workflow_spawn_continuation_ready(
    row: sqlite3.Row,
) -> WorkflowSpawnContinuationReady:
    from simple_harness.runtime.orchestration import WorkflowSpawnContinuationReady

    return WorkflowSpawnContinuationReady(
        ready_receipt_id=str(row["ready_receipt_id"]),
        spawn_operation_id=str(row["operation_id"]),
        ticket_receipt_id=str(row["ticket_receipt_id"]),
        effect_id=str(row["effect_id"]),
        handoff_attempt=int(row["handoff_attempt"]),
        evidence_ref=str(row["evidence_ref"]),
        version=int(row["version"]),
        created_at=float(row["created_at"]),
    )


def _workflow_spawn_activation_hash(
    *,
    activation_receipt_id: str,
    ready_receipt_id: str,
    spawn_operation_id: str,
    parent_run_id: str,
    effect_id: str,
    owner_id: str,
    runtime_lease_epoch: int,
    run_fence_epoch: int,
    workflow_lease_epoch: int | None,
    continuation_claim_epoch: int,
    predecessor_activation_receipt_id: str | None,
    version: int,
) -> str:
    payload: dict[str, JsonValue] = {
        "activation_receipt_id": activation_receipt_id,
        "ready_receipt_id": ready_receipt_id,
        "spawn_operation_id": spawn_operation_id,
        "parent_run_id": parent_run_id,
        "effect_id": effect_id,
        "owner_id": owner_id,
        "runtime_lease_epoch": runtime_lease_epoch,
        "run_fence_epoch": run_fence_epoch,
        "workflow_lease_epoch": workflow_lease_epoch,
        "continuation_claim_epoch": continuation_claim_epoch,
        "predecessor_activation_receipt_id": predecessor_activation_receipt_id,
        "version": version,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


_WORKFLOW_SPAWN_COMPLETION_FIELDS = (
    "completion_receipt_id",
    "spawn_operation_id",
    "ticket_receipt_id",
    "parent_run_id",
    "path_kind",
    "effect_id",
    "handoff_attempt",
    "effect_request_hash",
    "issue_authority_hash",
    "tool_result_json",
    "tool_result_hash",
    "child_runtime_start_receipt_id",
    "failure_evidence_kind",
    "failure_evidence_id",
    "failure_evidence_json",
    "failure_evidence_hash",
    "activation_chain_head_id",
    "child_wait_receipt_id",
    "created_at",
)


def _workflow_spawn_completion_hash(values: Mapping[str, object]) -> str:
    payload: dict[str, JsonValue] = {
        name: cast(JsonValue, values[name]) for name in _WORKFLOW_SPAWN_COMPLETION_FIELDS
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _workflow_spawn_completion_hash_from_row(row: sqlite3.Row) -> str:
    return _workflow_spawn_completion_hash(
        {name: row[name] for name in _WORKFLOW_SPAWN_COMPLETION_FIELDS}
    )


def _workflow_spawn_failure_evidence(row: sqlite3.Row) -> dict[str, JsonValue]:
    raw = row["failure_evidence_json"]
    if raw is None:
        raise UnitOfWorkConflict("workflow spawn failure evidence is missing")
    try:
        evidence = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise UnitOfWorkConflict("workflow spawn failure evidence is malformed") from exc
    if not isinstance(evidence, dict):
        raise UnitOfWorkConflict("workflow spawn failure evidence is malformed")
    canonical = canonical_json(evidence)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    if (
        str(raw) != canonical
        or row["failure_evidence_hash"] is None
        or str(row["failure_evidence_hash"]) != digest
        or row["failure_evidence_kind"] != evidence.get("kind")
        or row["failure_evidence_id"] is None
    ):
        raise UnitOfWorkConflict("workflow spawn failure evidence hash differs")
    return cast(dict[str, JsonValue], evidence)


def _workflow_spawn_ready_activation(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> WorkflowSpawnReadyActivation:
    from simple_harness.runtime.orchestration import (
        WorkflowSpawnReadyActivationState,
        _create_workflow_spawn_ready_activation,
    )

    canonical_hash = _workflow_spawn_activation_hash(
        activation_receipt_id=str(row["activation_receipt_id"]),
        ready_receipt_id=str(row["ready_receipt_id"]),
        spawn_operation_id=str(row["spawn_operation_id"]),
        parent_run_id=str(row["parent_run_id"]),
        effect_id=str(row["effect_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        run_fence_epoch=int(row["run_fence_epoch"]),
        workflow_lease_epoch=(
            None if row["workflow_lease_epoch"] is None else int(row["workflow_lease_epoch"])
        ),
        continuation_claim_epoch=int(row["continuation_claim_epoch"]),
        predecessor_activation_receipt_id=(
            None
            if row["predecessor_activation_receipt_id"] is None
            else str(row["predecessor_activation_receipt_id"])
        ),
        version=int(row["version"]),
    )
    if canonical_hash != str(row["canonical_hash"]):
        raise UnitOfWorkConflict("workflow spawn activation self-hash differs")
    ready_row = connection.execute(
        "SELECT * FROM workflow_spawn_continuation_ready WHERE ready_receipt_id=?",
        (str(row["ready_receipt_id"]),),
    ).fetchone()
    continuation_row = connection.execute(
        "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
        (str(row["spawn_operation_id"]),),
    ).fetchone()
    runtime = connection.execute(
        "SELECT * FROM workflow_leases WHERE run_id=? AND namespace=?",
        (str(row["parent_run_id"]), RUNTIME_LEASE_NAMESPACE),
    ).fetchone()
    fence = connection.execute(
        "SELECT * FROM run_fences WHERE run_id=?", (str(row["parent_run_id"]),)
    ).fetchone()
    blocker = connection.execute(
        """
        SELECT blocker_id FROM run_wait_blockers
        WHERE run_id=? AND kind='tool' AND ledger_identity=?
        ORDER BY created_at DESC LIMIT 1
        """,
        (str(row["parent_run_id"]), str(row["effect_id"])),
    ).fetchone()
    if (
        ready_row is None
        or continuation_row is None
        or runtime is None
        or fence is None
        or blocker is None
        or str(runtime["owner_id"]) != str(row["owner_id"])
        or int(runtime["epoch"]) != int(row["runtime_lease_epoch"])
        or str(fence["owner_id"]) != str(row["owner_id"])
        or int(fence["runtime_lease_epoch"]) != int(row["runtime_lease_epoch"])
        or int(fence["epoch"]) != int(row["run_fence_epoch"])
        or str(fence["state"]) != "active"
        or int(continuation_row["claim_epoch"]) != int(row["continuation_claim_epoch"])
        or str(continuation_row["owner_id"]) != str(row["owner_id"])
    ):
        raise UnitOfWorkConflict("workflow spawn activation authority disappeared")
    execution_lease = ExecutionLease(
        str(row["parent_run_id"]),
        RUNTIME_LEASE_NAMESPACE,
        str(row["owner_id"]),
        int(row["runtime_lease_epoch"]),
        float(runtime["expires_at"]),
    )
    run_fence = RunFenceLease(
        RunId(str(row["parent_run_id"])),
        int(row["run_fence_epoch"]),
        str(row["owner_id"]),
        int(row["runtime_lease_epoch"]),
    )
    workflow_lease: WorkflowLease | None = None
    if row["workflow_lease_epoch"] is not None:
        projection = connection.execute(
            "SELECT * FROM workflow_leases WHERE run_id=? AND epoch=? AND owner_id=? AND"
            " namespace<>?",
            (
                str(row["parent_run_id"]),
                int(row["workflow_lease_epoch"]),
                str(row["owner_id"]),
                RUNTIME_LEASE_NAMESPACE,
            ),
        ).fetchone()
        if projection is None:
            raise UnitOfWorkConflict("workflow spawn projection lease disappeared")
        workflow_lease = WorkflowLease(
            run_id=str(row["parent_run_id"]),
            owner_id=str(row["owner_id"]),
            epoch=int(row["workflow_lease_epoch"]),
            expires_at=float(projection["expires_at"]),
            runtime_lease_epoch=int(row["runtime_lease_epoch"]),
            namespace=str(projection["namespace"]),
        )
    return _create_workflow_spawn_ready_activation(
        ready_receipt=_workflow_spawn_continuation_ready(ready_row),
        continuation_claim=_workflow_spawn_continuation_claim(continuation_row),
        execution_lease=execution_lease,
        run_fence=run_fence,
        workflow_lease=workflow_lease,
        blocker_id=str(blocker["blocker_id"]),
        activation_receipt_id=str(row["activation_receipt_id"]),
        activation_version=int(row["version"]),
        predecessor_activation_receipt_id=(
            None
            if row["predecessor_activation_receipt_id"] is None
            else str(row["predecessor_activation_receipt_id"])
        ),
        state=WorkflowSpawnReadyActivationState(str(row["state"])),
    )


def _verified_workflow_launch_ticket(
    row: sqlite3.Row,
) -> VerifiedWorkflowLaunchTicket:
    from simple_harness.runtime.orchestration import (
        _create_verified_workflow_launch_ticket,
    )

    fields = (
        "ticket_receipt_id",
        "ticket_id",
        "candidate_id",
        "profile_key",
        "catalog_generation",
        "catalog_authority_version",
        "catalog_hash",
        "profile_fingerprint",
        "workflow_name",
        "workflow_version",
        "implementation_fingerprint",
        "session_id",
        "request_id",
        "turn_id",
        "requested_run_id",
        "requested_trace_id",
        "requested_thread_id",
        "resolved_run_id",
        "resolved_trace_id",
        "resolved_thread_id",
        "tool_catalog_generation",
        "objective_hash",
        "start_input_hash",
    )
    values: dict[str, object] = {}
    integer_fields = {
        "catalog_generation",
        "catalog_authority_version",
        "tool_catalog_generation",
    }
    nullable_fields = {
        "candidate_id",
        "requested_run_id",
        "requested_trace_id",
        "requested_thread_id",
    }
    for field_name in fields:
        value = row[field_name]
        if field_name in integer_fields:
            values[field_name] = int(value)
        elif field_name in nullable_fields and value is None:
            values[field_name] = None
        else:
            values[field_name] = str(value)
    payload = json.loads(str(row["canonical_payload"]))
    binding = payload.get("profile_binding") if isinstance(payload, dict) else None
    if not isinstance(binding, dict):
        raise UnitOfWorkConflict("workflow launch ticket profile binding is missing")
    schema = _start_input_schema_from_json(binding.get("start_input_schema"))
    binding_columns = (
        _required(binding.get("profile_key"), "ticket.profile_key"),
        _required(binding.get("profile_fingerprint"), "ticket.profile_fingerprint"),
        _required(binding.get("workflow_name"), "ticket.workflow_name"),
        _required(binding.get("workflow_version"), "ticket.workflow_version"),
        _required(
            binding.get("implementation_fingerprint"),
            "ticket.implementation_fingerprint",
        ),
    )
    durable_columns = (
        str(row["profile_key"]),
        str(row["profile_fingerprint"]),
        str(row["workflow_name"]),
        str(row["workflow_version"]),
        str(row["implementation_fingerprint"]),
    )
    payload_binding_columns = (
        _required(payload.get("profile_fingerprint"), "ticket.profile_fingerprint"),
        _required(payload.get("workflow_name"), "ticket.workflow_name"),
        _required(payload.get("workflow_version"), "ticket.workflow_version"),
        _required(
            payload.get("implementation_fingerprint"),
            "ticket.implementation_fingerprint",
        ),
    )
    if binding_columns != durable_columns or payload_binding_columns != durable_columns[1:]:
        raise UnitOfWorkConflict("workflow launch ticket binding columns differ")
    values.update(
        {
            "description": _required(binding.get("description"), "ticket.description"),
            "use_when": _required(binding.get("use_when"), "ticket.use_when"),
            "avoid_when": _required(binding.get("avoid_when"), "ticket.avoid_when"),
            "input_schema_ref": _required(
                binding.get("input_schema_ref"), "ticket.input_schema_ref"
            ),
            "checkpoint_namespace": _required(
                binding.get("checkpoint_namespace"), "ticket.checkpoint_namespace"
            ),
            "manifest_hash": _required(binding.get("manifest_hash"), "ticket.manifest_hash"),
            "state_schema_version": int(binding.get("state_schema_version", 0)),
            "start_input_schema": schema,
            "terminal_projection_descriptor": _optional_json_object(
                binding.get("terminal_projection_descriptor"),
                "ticket.terminal_projection_descriptor",
            ),
            "terminal_request_factory_hash": (
                None
                if binding.get("terminal_request_factory_hash") is None
                else _required(
                    binding.get("terminal_request_factory_hash"),
                    "ticket.terminal_request_factory_hash",
                )
            ),
            "capability_snapshot": _json_object(
                binding.get("capability_snapshot"), "ticket.capability_snapshot"
            ),
        }
    )
    request_payload = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(request_payload, dict):
        raise UnitOfWorkConflict("workflow launch ticket request is missing")
    launch_request = _workflow_launch_request_from_json(request_payload)
    request_key = _required(request_payload.get("request_key"), "ticket.request_key")
    profile_key = _required(request_payload.get("profile_key"), "ticket.profile_key")
    session_id = _required(request_payload.get("session_id"), "ticket.session_id")
    request_id = _required(request_payload.get("request_id"), "ticket.request_id")
    turn_id = _required(request_payload.get("turn_id"), "ticket.turn_id")
    objective = _required(request_payload.get("objective"), "ticket.objective")
    start_input = _json_object(request_payload.get("start_input"), "ticket.start_input")
    candidate_id = request_payload.get("candidate_id")
    requested_run_id = request_payload.get("requested_run_id")
    requested_trace_id = request_payload.get("requested_trace_id")
    requested_thread_id = request_payload.get("requested_thread_id")
    for field_name, field_value in (
        ("candidate_id", candidate_id),
        ("requested_run_id", requested_run_id),
        ("requested_trace_id", requested_trace_id),
        ("requested_thread_id", requested_thread_id),
    ):
        if field_value is not None and not isinstance(field_value, str):
            raise UnitOfWorkConflict(f"ticket {field_name} is not a string")
    catalog_generation = request_payload.get("catalog_generation")
    tool_catalog_generation = request_payload.get("tool_catalog_generation")
    if (
        not isinstance(catalog_generation, int)
        or isinstance(catalog_generation, bool)
        or not isinstance(tool_catalog_generation, int)
        or isinstance(tool_catalog_generation, bool)
    ):
        raise UnitOfWorkConflict("workflow launch ticket generations are invalid")
    canonical_request = canonical_json(request_payload)
    request_fingerprint = hashlib.sha256(canonical_request.encode()).hexdigest()

    def derived_id(domain: str) -> str:
        return hashlib.sha256(
            f"simple-harness.workflow.{domain}|{request_fingerprint}".encode()
        ).hexdigest()

    resolved_run_id = requested_run_id or derived_id("workflow-launch/run/v1")
    resolved_trace_id = requested_trace_id or derived_id("workflow-launch/trace/v1")
    resolved_thread_id = requested_thread_id or derived_id("workflow-launch/thread/v1")
    request_columns = (
        request_key,
        candidate_id,
        profile_key,
        catalog_generation,
        session_id,
        request_id,
        turn_id,
        requested_run_id,
        requested_trace_id,
        requested_thread_id,
        resolved_run_id,
        resolved_trace_id,
        resolved_thread_id,
        tool_catalog_generation,
        hashlib.sha256(objective.encode()).hexdigest(),
        hashlib.sha256(canonical_json(start_input).encode()).hexdigest(),
    )
    durable_request_columns = (
        str(row["request_key"]),
        None if row["candidate_id"] is None else str(row["candidate_id"]),
        str(row["profile_key"]),
        int(row["catalog_generation"]),
        str(row["session_id"]),
        str(row["request_id"]),
        str(row["turn_id"]),
        None if row["requested_run_id"] is None else str(row["requested_run_id"]),
        None if row["requested_trace_id"] is None else str(row["requested_trace_id"]),
        None if row["requested_thread_id"] is None else str(row["requested_thread_id"]),
        str(row["resolved_run_id"]),
        str(row["resolved_trace_id"]),
        str(row["resolved_thread_id"]),
        int(row["tool_catalog_generation"]),
        str(row["objective_hash"]),
        str(row["start_input_hash"]),
    )
    payload_catalog_columns = (
        payload.get("catalog_authority_version"),
        payload.get("catalog_hash"),
    )
    durable_catalog_columns = (
        int(row["catalog_authority_version"]),
        str(row["catalog_hash"]),
    )
    payload_derived_columns = (
        payload.get("resolved_run_id"),
        payload.get("resolved_trace_id"),
        payload.get("resolved_thread_id"),
        payload.get("objective_hash"),
        payload.get("start_input_hash"),
    )
    durable_derived_columns = (
        resolved_run_id,
        resolved_trace_id,
        resolved_thread_id,
        hashlib.sha256(objective.encode()).hexdigest(),
        hashlib.sha256(canonical_json(start_input).encode()).hexdigest(),
    )
    canonical_payload = canonical_json(payload)
    payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
    ticket_id = hashlib.sha256(
        f"simple-harness.workflow.workflow-launch/ticket/v1|{payload_hash}".encode()
    ).hexdigest()
    if (
        request_columns != durable_request_columns
        or payload_catalog_columns != durable_catalog_columns
        or payload_derived_columns != durable_derived_columns
        or str(row["canonical_payload"]) != canonical_payload
        or str(row["payload_hash"]) != payload_hash
        or str(row["ticket_id"]) != ticket_id
        or str(row["ticket_receipt_id"])
        != hashlib.sha256(
            f"simple-harness.workflow.workflow-launch/receipt/v1|{request_key}".encode()
        ).hexdigest()
    ):
        raise UnitOfWorkConflict("workflow launch ticket self-hash differs")
    issue_authority_hash = payload.get("issue_authority_hash")
    if (
        not isinstance(issue_authority_hash, str)
        or issue_authority_hash != str(row["issue_authority_hash"])
        or objective != str(row["objective"])
        or canonical_json(launch_request.spawn_origin.to_json()) != str(row["spawn_origin_json"])
        or launch_request.spawn_origin.parent_run_id != str(row["parent_run_id"])
        or launch_request.root_run_id != str(row["root_run_id"])
        or launch_request.attachment_policy.value != str(row["attachment_policy"])
        or launch_request.child_command_id != str(row["child_command_id"])
    ):
        raise UnitOfWorkConflict("workflow launch spawn authority binding differs")
    values.update(
        {
            "objective": objective,
            "spawn_origin": launch_request.spawn_origin,
            "parent_run_id": launch_request.spawn_origin.parent_run_id,
            "root_run_id": launch_request.root_run_id,
            "attachment_policy": launch_request.attachment_policy,
            "child_command_id": launch_request.child_command_id,
        }
    )
    return _create_verified_workflow_launch_ticket(values)


def _runtime_start_receipt(row: sqlite3.Row) -> RuntimeStartReceipt:
    from simple_harness.runtime.orchestration import RuntimeStartReceipt

    return RuntimeStartReceipt(
        ticket_receipt_id=str(row["ticket_receipt_id"]),
        run_id=str(row["run_id"]),
        trace_id=str(row["trace_id"]),
        thread_id=str(row["thread_id"]),
        committed_run_version=int(row["committed_run_version"]),
        start_snapshot_hash=str(row["start_snapshot_hash"]),
        workflow_request_hash=str(row["workflow_request_hash"]),
        created_at=float(row["created_at"]),
    )


def _runtime_start_dispatch_claim(
    row: sqlite3.Row,
) -> RuntimeStartDispatchClaim:
    from simple_harness.runtime.orchestration import RuntimeStartDispatchClaim

    return RuntimeStartDispatchClaim(
        claim_id=str(row["claim_id"]),
        run_id=str(row["run_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        claim_epoch=int(row["claim_epoch"]),
    )


def _runtime_start_dispatch_record(
    row: sqlite3.Row,
) -> RuntimeStartDispatchRecord:
    from simple_harness.runtime.orchestration import (
        RuntimeStartDispatchRecord,
        RuntimeStartDispatchState,
    )

    return RuntimeStartDispatchRecord(
        claim_id=str(row["claim_id"]),
        run_id=str(row["run_id"]),
        owner_id=str(row["owner_id"]),
        runtime_lease_epoch=int(row["runtime_lease_epoch"]),
        claim_epoch=int(row["claim_epoch"]),
        expires_at=float(row["expires_at"]),
        version=int(row["version"]),
        state=RuntimeStartDispatchState(str(row["state"])),
    )


def _runtime_start_activation(runtime: sqlite3.Row, fence: sqlite3.Row) -> RuntimeStartActivation:
    from simple_harness.runtime.orchestration import RuntimeStartActivation

    run_id = str(runtime["run_id"])
    return RuntimeStartActivation(
        execution_lease=ExecutionLease(
            run_id=run_id,
            namespace=str(runtime["namespace"]),
            owner_id=str(runtime["owner_id"]),
            epoch=int(runtime["epoch"]),
            expires_at=float(runtime["expires_at"]),
        ),
        run_fence=RunFenceLease(
            run_id=RunId(run_id),
            epoch=int(fence["epoch"]),
            owner_id=str(fence["owner_id"]),
            runtime_lease_epoch=int(fence["runtime_lease_epoch"]),
        ),
    )


def _require_runtime_start_receipt_identity(
    row: sqlite3.Row,
    *,
    verified: VerifiedWorkflowLaunchTicket,
    snapshot_hash: str,
    workflow_request_hash: str,
) -> None:
    expected = (
        verified.ticket_receipt_id,
        verified.resolved_run_id,
        verified.resolved_trace_id,
        verified.resolved_thread_id,
        snapshot_hash,
        workflow_request_hash,
    )
    actual = (
        str(row["ticket_receipt_id"]),
        str(row["run_id"]),
        str(row["trace_id"]),
        str(row["thread_id"]),
        str(row["start_snapshot_hash"]),
        str(row["workflow_request_hash"]),
    )
    if actual != expected:
        raise UnitOfWorkConflict("runtime start receipt differs")


def _validate_runtime_start_binding(
    verified: VerifiedWorkflowLaunchTicket,
    start: RunStart,
    request: StartAdmissionRequest,
    snapshot: StartSnapshot,
) -> None:
    from simple_harness.runtime.start_snapshot import StartSnapshot

    if not verified._is_sdk_verified():
        raise UnitOfWorkConflict("workflow launch ticket verification was forged")
    if not isinstance(snapshot, StartSnapshot):
        raise TypeError("snapshot must be a StartSnapshot")
    start_input_hash = hashlib.sha256(canonical_json(_thaw(start.input)).encode()).hexdigest()
    try:
        validate_arguments(_thaw(start.input), verified.start_input_schema.canonical_schema)
    except (ArgumentsValidationError, SchemaDefinitionError) as exc:
        raise UnitOfWorkConflict("workflow start input violates durable schema") from exc
    expected = (
        verified.session_id,
        verified.request_id,
        verified.resolved_run_id,
        verified.tool_catalog_generation,
        verified.ticket_receipt_id,
        StartMode.PRECREATED,
        verified.session_id,
        verified.request_id,
        verified.turn_id,
        verified.profile_key,
        "workflow",
        verified.tool_catalog_generation,
        verified.workflow_name,
        verified.workflow_version,
        verified.requested_run_id,
        verified.requested_trace_id,
        verified.requested_thread_id,
        verified.resolved_run_id,
        verified.resolved_trace_id,
        verified.resolved_thread_id,
        verified.checkpoint_namespace,
        verified.manifest_hash,
        verified.implementation_fingerprint,
        verified.state_schema_version,
        verified.start_input_schema.schema_ref,
        verified.start_input_schema.schema_hash,
        (
            None
            if verified.terminal_projection_descriptor is None
            else _thaw(verified.terminal_projection_descriptor)
        ),
        verified.terminal_request_factory_hash,
        start_input_hash,
        _thaw(verified.capability_snapshot),
    )
    actual = (
        start.execution_session_id.value,
        start.request_id.value,
        start.run_id.value,
        start.tool_catalog_generation,
        request.request_key,
        request.mode,
        request.session_id,
        request.request_id,
        request.turn_id,
        request.profile_key,
        request.driver_kind,
        request.tool_catalog_generation,
        request.workflow_name,
        request.workflow_version,
        request.requested_run_id,
        request.requested_trace_id,
        request.requested_thread_id,
        request.resolved_run_id,
        request.resolved_trace_id,
        request.resolved_thread_id,
        request.checkpoint_namespace,
        request.manifest_hash,
        request.implementation_hash,
        request.state_schema_version,
        request.start_input_schema_ref,
        request.start_input_schema_hash,
        (
            None
            if request.terminal_projection_descriptor is None
            else _thaw(request.terminal_projection_descriptor)
        ),
        request.terminal_request_factory_hash,
        hashlib.sha256(canonical_json(_thaw(request.start_input)).encode()).hexdigest(),
        _thaw(request.capability_snapshot),
    )
    if actual != expected or start_input_hash != verified.start_input_hash:
        raise UnitOfWorkConflict("Runtime start differs from verified launch ticket")
    if snapshot.workflow_admission != request:
        raise UnitOfWorkConflict("start snapshot workflow admission differs")
    if (
        snapshot.profile_key != verified.profile_key
        or snapshot.driver_kind != "workflow"
        or snapshot.tool_catalog_generation != verified.tool_catalog_generation
        or _thaw_json(snapshot.input) != _thaw(start.input)
    ):
        raise UnitOfWorkConflict("start snapshot differs from verified launch ticket")


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


def _recovery_candidate_json(value: RecoveryCandidate) -> dict[str, JsonValue]:
    return {
        "run_id": value.run_id,
        "run_version": value.run_version,
        "status": value.status,
        "runtime_lease_owner": value.runtime_lease_owner,
        "runtime_lease_epoch": value.runtime_lease_epoch,
        "runtime_lease_expires_at": value.runtime_lease_expires_at,
        "workflow_lease_namespace": value.workflow_lease_namespace,
        "workflow_lease_owner": value.workflow_lease_owner,
        "workflow_lease_epoch": value.workflow_lease_epoch,
        "workflow_lease_expires_at": value.workflow_lease_expires_at,
        "run_fence_owner": value.run_fence_owner,
        "run_fence_runtime_lease_epoch": value.run_fence_runtime_lease_epoch,
        "run_fence_epoch": value.run_fence_epoch,
        "run_fence_state": value.run_fence_state,
        "checkpoint_head": value.checkpoint_head,
    }


def _recovery_snapshot_json(value: RecoverySnapshot) -> dict[str, JsonValue]:
    return {
        "candidate": _recovery_candidate_json(value.candidate),
        "manifest_hash": value.manifest_hash,
        "implementation_hash": value.implementation_hash,
        "checkpoint_hash": value.checkpoint_hash,
        "unresolved_blocker_ids": list(value.unresolved_blocker_ids),
    }


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        execution_session_id=str(row["execution_session_id"]),
        request_id=str(row["request_id"]),
        root_run_id=str(row["root_run_id"]),
        parent_run_id=None if row["parent_run_id"] is None else str(row["parent_run_id"]),
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
        None if row["response_json"] is None else freeze_json(json.loads(str(row["response_json"])))
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
            None if row["runtime_lease_epoch"] is None else int(row["runtime_lease_epoch"])
        ),
        claim_epoch=int(row["claim_epoch"]),
        ack_receipt_id=(None if row["ack_receipt_id"] is None else str(row["ack_receipt_id"])),
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
        child_run_id=(None if row["child_run_id"] is None else str(row["child_run_id"])),
    )


def _child_command_record(row: sqlite3.Row) -> ChildCommandRecord:
    ticket_id = (
        row["ticket_id"] if row["ticket_id"] is not None else row["workflow_ticket_receipt_id"]
    )
    return ChildCommandRecord(
        command_id=str(row["command_id"]),
        parent_run_id=str(row["parent_run_id"]),
        child_run_id=str(row["child_run_id"]),
        ticket_id=str(ticket_id),
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
        ack_receipt_id=(None if row["ack_receipt_id"] is None else str(row["ack_receipt_id"])),
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
            None if row["handoff_receipt_ref"] is None else str(row["handoff_receipt_ref"])
        ),
        evidence_ref=(None if row["evidence_ref"] is None else str(row["evidence_ref"])),
        result=_tool_result(row["result_json"]),
        raw_call_id=(None if row["raw_call_id"] is None else str(row["raw_call_id"])),
        turn_ordinal=int(row["turn_ordinal"]),
        call_ordinal=int(row["call_ordinal"]),
        handoff_attempt=int(row["handoff_attempt"]),
        rehandoff_count=int(row["rehandoff_count"]),
    )


def _insert_provider_projection_receipt(
    connection: sqlite3.Connection, record: ProviderInvocationRecord
) -> None:
    session_row = connection.execute(
        "SELECT execution_session_id FROM runs WHERE run_id=?", (record.run_id.value,)
    ).fetchone()
    if session_row is None:
        raise UnitOfWorkNotFound(record.run_id.value)
    payload: JsonValue = {
        "invocation_id": record.invocation_id,
        "invocation_version": record.version,
        "run_id": record.run_id.value,
        "execution_session_id": str(session_row["execution_session_id"]),
        "request_id": record.request_id.value,
        "handoff_attempt": record.handoff_attempt,
        "state": record.state.value,
        "target": {
            "provider_id": record.target.provider_id,
            "model": record.target.model,
            "pricing_key": record.target.pricing_key,
            "endpoint_identity": record.target.endpoint_identity,
            "adapter_key": record.target.adapter_key,
        },
        "request_fingerprint": record.request_fingerprint,
        "usage": None if record.usage_json is None else _thaw_json(record.usage_json),
        "error_code": record.error_code,
        "settled_at": record.settled_at,
    }
    encoded = canonical_json(payload)
    payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
    connection.execute(
        "INSERT INTO provider_projection_outbox("
        "invocation_id,invocation_version,run_id,execution_session_id,request_id,"
        "payload_json,payload_hash,created_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(invocation_id,invocation_version) DO NOTHING",
        (
            record.invocation_id,
            record.version,
            record.run_id.value,
            str(session_row["execution_session_id"]),
            record.request_id.value,
            encoded,
            payload_hash,
            record.settled_at if record.settled_at is not None else record.claimed_at,
        ),
    )


def _provider_projection_receipt(row: sqlite3.Row) -> ProviderProjectionReceipt:
    payload = json.loads(str(row["payload_json"]))
    return ProviderProjectionReceipt(
        int(row["sequence"]),
        str(row["invocation_id"]),
        int(row["invocation_version"]),
        str(row["run_id"]),
        str(row["execution_session_id"]),
        str(row["request_id"]),
        frozen_payload(payload),
        str(row["payload_hash"]),
        float(row["created_at"]),
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
            None if row["estimator_json"] is None else json.loads(str(row["estimator_json"]))
        ),
        estimator_digest=(
            None if row["estimator_digest"] is None else str(row["estimator_digest"])
        ),
        budget_charge=BudgetCharge.from_json(usage["budget"]),
        response_json=(
            None if row["response_json"] is None else json.loads(str(row["response_json"]))
        ),
        usage_json=usage,
        error_code=(None if row["error_code"] is None else str(row["error_code"])),
        claimed_at=float(row["claimed_at"]),
        handed_off_at=(None if row["handed_off_at"] is None else float(row["handed_off_at"])),
        settled_at=(None if row["settled_at"] is None else float(row["settled_at"])),
        version=int(row["version"]),
        request_json=(
            None if row["request_json"] is None else json.loads(str(row["request_json"]))
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
        resolution_id=(None if row["resolution_id"] is None else str(row["resolution_id"])),
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
