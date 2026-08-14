# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.workflow.execution_ports import (
    CancelWorkflowRequest,
    ForkRequest,
    RecoveryOutcome,
    ResumeAdmissionRequest,
    ResumeCommitBinding,
    StartAdmissionRequest,
    StartMode,
    StartPhase,
    WorkflowRecoveryReceiptKind,
    WorkflowRecoveryWork,
)


def _start_request(**changes: object) -> StartAdmissionRequest:
    values: dict[str, object] = {
        "request_key": "start-key",
        "mode": StartMode.STANDALONE,
        "session_id": "session",
        "request_id": "request",
        "turn_id": "turn",
        "profile_key": "workflow.demo",
        "driver_kind": "workflow",
        "tool_catalog_generation": 1,
        "workflow_name": "demo",
        "workflow_version": "1",
        "requested_run_id": None,
        "requested_trace_id": None,
        "requested_thread_id": None,
        "resolved_run_id": None,
        "resolved_trace_id": None,
        "resolved_thread_id": None,
        "checkpoint_namespace": "native",
        "manifest_hash": "a" * 64,
        "implementation_hash": "b" * 64,
        "state_schema_version": 1,
        "start_input_schema_ref": None,
        "start_input_schema_hash": None,
        "terminal_projection_descriptor": None,
        "terminal_request_factory_hash": None,
        "start_input": {"objective": "test"},
        "capability_snapshot": {"tools": []},
    }
    values.update(changes)
    return StartAdmissionRequest(**values)  # type: ignore[arg-type]


async def _atomic(uow, operation, label="test"):  # type: ignore[no-untyped-def]
    return await uow.run_atomic(operation, fault_label=label)


def test_start_exact_replay_conflict_and_fault_reopen(tmp_path: Path) -> None:
    path = tmp_path / "start.db"
    request = _start_request()

    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)

        async def admit(transaction):  # type: ignore[no-untyped-def]
            return await uow.admit_start_standalone(transaction, request, now=1.0)

        first = asyncio.run(_atomic(uow, admit))
        repeated = asyncio.run(_atomic(uow, admit))
        assert first == repeated
        assert first.phase is StartPhase.ADMITTED
        assert len(first.run_id) == len(first.trace_id) == len(first.thread_id) == 64
        with pytest.raises(UnitOfWorkConflict, match="different payload"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda transaction: uow.admit_start_standalone(
                        transaction,
                        _start_request(workflow_version="2"),
                        now=1.0,
                    ),
                )
            )
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda transaction: uow.admit_start_standalone(
                    transaction, request, now=9.0
                ),
            )
        )
        assert replay == first

    for point in (
        "workflow:admit_start_standalone:after_runs_write",
        "workflow:admit_start_standalone:after_run_start_snapshots_write",
        "workflow:admit_start_standalone:after_workflow_start_admissions_write",
    ):
        fault_path = tmp_path / f"{point.rsplit(':', 1)[-1]}.db"
        with Database.open(fault_path) as database:
            uow = SqliteExecutionUnitOfWork(database)

            def fault(label: str, *, expected: str = point) -> None:
                if label == expected:
                    raise RuntimeError(expected)

            async def admit_with_fault(transaction, *, authority=uow):  # type: ignore[no-untyped-def]
                return await authority.admit_start_standalone(
                    transaction, request, now=1.0, fault=fault
                )

            with pytest.raises(RuntimeError, match=point):
                asyncio.run(
                    _atomic(
                        uow,
                        admit_with_fault,
                    )
                )
        with Database.open(fault_path) as reopened:
            assert (
                reopened.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                == 0
            )
            assert (
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM workflow_start_admissions"
                ).fetchone()[0]
                == 0
            )


def test_start_recovery_pagination_uses_unique_request_key_after_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "start-pagination.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_start_standalone(
                    tx,
                    _start_request(request_key="a-key", request_id="shared-request"),
                    now=1.0,
                ),
            )
        )
        second = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_start_standalone(
                    tx,
                    _start_request(
                        request_key="b-key",
                        request_id="shared-request",
                        session_id="session-two",
                    ),
                    now=2.0,
                ),
            )
        )
        assert first.request_id == second.request_id == "shared-request"

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        page_one, cursor = uow.list_unsettled_start_admissions(None, limit=1)
        assert tuple(item.request_key for item in page_one) == ("a-key",)
        assert cursor == "a-key"
        page_two, final_cursor = uow.list_unsettled_start_admissions(cursor, limit=1)
        assert tuple(item.request_key for item in page_two) == ("b-key",)
        assert final_cursor is None
        works = tuple(
            WorkflowRecoveryWork(
                run_id=receipt.run_id,
                receipt_kind=WorkflowRecoveryReceiptKind.START,
                receipt_id=receipt.request_key,
                receipt_version=receipt.version,
                mode=receipt.request.mode,
                due_at=None,
                request_fingerprint=receipt.request_fingerprint,
                receipt_snapshot=receipt,
            )
            for receipt in (*page_one, *page_two)
        )
        assert tuple(work.receipt_id for work in works) == ("a-key", "b-key")


def _admit_and_claim(uow: SqliteExecutionUnitOfWork):
    request = _start_request()

    async def scenario():
        receipt = await _atomic(
            uow,
            lambda tx: uow.admit_start_standalone(tx, request, now=1.0),
        )
        activation = await _atomic(
            uow,
            lambda tx: uow.claim_activation(
                tx,
                receipt.run_id,
                0,
                "runner",
                now=2.0,
                ttl_seconds=30.0,
            ),
        )
        return receipt, activation

    return asyncio.run(scenario())


def test_activation_renews_both_leases_and_fault_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "activation.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        assert activation.execution_lease.epoch == activation.workflow_lease.epoch
        assert (
            activation.execution_lease.expires_at
            == activation.workflow_lease.expires_at
        )

        def fault(label: str) -> None:
            if label == "workflow:renew_activation:after_runtime_lease_write":
                raise RuntimeError("renew fault")

        with pytest.raises(RuntimeError, match="renew fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.renew_activation(
                        tx, activation, now=3.0, ttl_seconds=30.0, fault=fault
                    ),
                )
            )
        expiries = tuple(
            row[0]
            for row in database.connection.execute(
                "SELECT expires_at FROM workflow_leases WHERE run_id=? ORDER BY namespace",
                (receipt.run_id,),
            ).fetchall()
        )
        assert expiries == (32.0, 32.0)


def test_standalone_live_heartbeat_reentry_keeps_original_claim_and_current_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "standalone-live-reentry.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        request = _start_request()
        receipt = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_start_standalone(tx, request, now=1.0),
            )
        )
        acquired = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_activation(
                    tx,
                    receipt.run_id,
                    0,
                    "runner",
                    now=2.0,
                    ttl_seconds=11.0,
                ),
            )
        )
        assert acquired.execution_lease.expires_at == 13.0
        renewed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.renew_activation(
                    tx, acquired, now=3.0, ttl_seconds=30.0
                ),
            )
        )
        assert renewed.execution_lease.expires_at == 33.0
        before_receipt = tuple(
            database.connection.execute(
                "SELECT phase,version,claim_action,claim_owner,claim_epoch,"
                "claim_expires_at FROM workflow_start_admissions"
            ).fetchone()
        )
        before_leases = tuple(
            tuple(row)
            for row in database.connection.execute(
                "SELECT namespace,owner_id,epoch,expires_at FROM workflow_leases "
                "ORDER BY namespace"
            ).fetchall()
        )
        before_fence = tuple(
            database.connection.execute(
                "SELECT epoch,owner_id,runtime_lease_epoch,state FROM run_fences"
            ).fetchone()
        )

        with pytest.raises(UnitOfWorkConflict, match="active owner"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_activation(
                        tx,
                        receipt.run_id,
                        0,
                        "foreign-runner",
                        now=13.0,
                        ttl_seconds=5.0,
                    ),
                )
            )

        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_activation(
                    tx,
                    receipt.run_id,
                    0,
                    "runner",
                    now=13.0,
                    ttl_seconds=5.0,
                ),
            )
        )
        assert replay == renewed
        assert tuple(
            database.connection.execute(
                "SELECT phase,version,claim_action,claim_owner,claim_epoch,"
                "claim_expires_at FROM workflow_start_admissions"
            ).fetchone()
        ) == before_receipt
        assert tuple(
            tuple(row)
            for row in database.connection.execute(
                "SELECT namespace,owner_id,epoch,expires_at FROM workflow_leases "
                "ORDER BY namespace"
            ).fetchall()
        ) == before_leases
        assert tuple(
            database.connection.execute(
                "SELECT epoch,owner_id,runtime_lease_epoch,state FROM run_fences"
            ).fetchone()
        ) == before_fence

        takeover = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_activation(
                    tx,
                    receipt.run_id,
                    0,
                    "runner",
                    now=40.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert takeover.execution_lease.epoch == 2
        assert takeover.execution_lease.expires_at == 50.0
        assert takeover.run_fence.epoch == acquired.run_fence.epoch + 1
        assert tuple(
            database.connection.execute(
                "SELECT version,claim_action,claim_epoch,claim_expires_at "
                "FROM workflow_start_admissions"
            ).fetchone()
        ) == (2, "resume", 2, 50.0)


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_runtime_lease_lifecycle_updates_workflow_projection_atomically(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / f"runtime-{operation}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        lease = activation.execution_lease
        if operation == "renew":
            renewed = uow.renew_runtime_lease(
                lease, now=3.0, lease_ttl_seconds=40.0
            )
            expected_expiry = renewed.expires_at
        else:
            uow.release_runtime_lease(lease, now=3.0)
            expected_expiry = 3.0
        rows = database.connection.execute(
            "SELECT namespace,owner_id,epoch,expires_at FROM workflow_leases "
            "WHERE run_id=? ORDER BY namespace",
            (receipt.run_id,),
        ).fetchall()
        assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
            ("native", "runner", 1, expected_expiry),
            ("runtime.kernel", "runner", 1, expected_expiry),
        ]


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_runtime_lease_lifecycle_rejects_drifted_workflow_projection(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / f"runtime-drift-{operation}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=31.0 "
            "WHERE run_id=? AND namespace='native'",
            (receipt.run_id,),
        )
        with pytest.raises(UnitOfWorkConflict, match="not co-fenced"):
            if operation == "renew":
                uow.renew_runtime_lease(
                    activation.execution_lease,
                    now=3.0,
                    lease_ttl_seconds=40.0,
                )
            else:
                uow.release_runtime_lease(activation.execution_lease, now=3.0)
        expiries = tuple(
            row[0]
            for row in database.connection.execute(
                "SELECT expires_at FROM workflow_leases WHERE run_id=? "
                "ORDER BY namespace",
                (receipt.run_id,),
            ).fetchall()
        )
        assert expiries == (31.0, 32.0)


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_runtime_lease_projection_fault_rolls_back_runtime_write(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / f"runtime-fault-{operation}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        point = f"runtime_lease_{operation}.native.before_projection_write"

        def fault(label: str) -> None:
            if label == point:
                raise RuntimeError(point)

        with pytest.raises(RuntimeError, match=point):
            if operation == "renew":
                uow.renew_runtime_lease(
                    activation.execution_lease,
                    now=3.0,
                    lease_ttl_seconds=40.0,
                    fault=fault,
                )
            else:
                uow.release_runtime_lease(
                    activation.execution_lease, now=3.0, fault=fault
                )
        expiries = tuple(
            row[0]
            for row in database.connection.execute(
                "SELECT expires_at FROM workflow_leases WHERE run_id=? "
                "ORDER BY namespace",
                (receipt.run_id,),
            ).fetchall()
        )
        assert expiries == (32.0, 32.0)


def test_resume_receipt_claim_and_settle_share_checkpoint_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "resume.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id=?",
            (receipt.run_id,),
        )
        database.connection.execute(
            "INSERT INTO workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at) VALUES('head',?,'native','{}',?,1,0,3)",
            (receipt.run_id, hashlib.sha256(b"{}").hexdigest()),
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,response_json,version,created_at,resolved_at) VALUES('decision',?,'workflow_interrupt','allowed','{}','{}',1,3,3)",
            (receipt.run_id,),
        )
        database.connection.commit()
        pending = (("decision", hashlib.sha256(b"{}").hexdigest()),)
        responses: dict[str, JsonValue] = {"decision": {"approved": True}}
        responses_hash = hashlib.sha256(canonical_json(responses).encode()).hexdigest()
        request = ResumeAdmissionRequest(
            "resume", receipt.run_id, 2, "head", pending, responses, responses_hash
        )

        async def scenario():
            admitted = await _atomic(
                uow, lambda tx: uow.admit_resume(tx, request, now=4.0)
            )
            claimed = await _atomic(
                uow,
                lambda tx: uow.claim_resume_standalone(
                    tx, "resume", admitted.version, "runner", now=5.0, ttl_seconds=30.0
                ),
            )
            binding = ResumeCommitBinding(
                "resume", claimed.version, 2, claimed.request_fingerprint
            )
            settled = await _atomic(
                uow,
                lambda tx: uow.settle_resume(
                    tx,
                    binding,
                    claimed.activation,
                    "head",
                    {"status": "waiting"},
                    now=6.0,
                ),
            )
            return settled

        settled = asyncio.run(scenario())
        assert settled.phase.value == "settled"


def test_resume_claim_enforces_mode_and_retry_due_time(tmp_path: Path) -> None:
    path = tmp_path / "resume-mode-due.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id=?",
            (receipt.run_id,),
        )
        database.connection.execute(
            "INSERT INTO workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at) VALUES('head',?,'native','{}',?,1,0,3)",
            (receipt.run_id, hashlib.sha256(b"{}").hexdigest()),
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,response_json,version,created_at,resolved_at) VALUES('decision',?,'workflow_interrupt','allowed','{}','{}',1,3,3)",
            (receipt.run_id,),
        )
        database.connection.commit()
        pending = (("decision", hashlib.sha256(b"{}").hexdigest()),)
        responses: dict[str, JsonValue] = {"decision": {"approved": True}}
        response_hash = hashlib.sha256(canonical_json(responses).encode()).hexdigest()

        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_resume(
                    tx,
                    ResumeAdmissionRequest(
                        "resume-mode",
                        receipt.run_id,
                        2,
                        "head",
                        pending,
                        responses,
                        response_hash,
                        StartMode.PRECREATED,
                    ),
                    now=4.0,
                ),
            )
        )
        with pytest.raises(UnitOfWorkConflict, match="not claimable"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_resume_standalone(
                        tx,
                        admitted.request.receipt_id,
                        admitted.version,
                        "runner",
                        now=5.0,
                        ttl_seconds=30.0,
                    ),
                )
            )
        row = database.connection.execute(
            "SELECT request_json FROM workflow_resume_admissions WHERE receipt_id=?",
            (admitted.request.receipt_id,),
        ).fetchone()
        payload = json.loads(str(row["request_json"]))
        payload["mode"] = "standalone"
        request_json = canonical_json(payload)
        database.connection.execute(
            "UPDATE workflow_resume_admissions SET mode='standalone',"
            "request_json=?,request_fingerprint=?,phase='retry_wait',"
            "next_attempt_at=10.0 WHERE receipt_id=?",
            (
                request_json,
                hashlib.sha256(request_json.encode()).hexdigest(),
                admitted.request.receipt_id,
            ),
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="not due"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_resume_standalone(
                        tx,
                        admitted.request.receipt_id,
                        admitted.version,
                        "runner",
                        now=9.0,
                        ttl_seconds=30.0,
                    ),
                )
            )
        claimed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_resume_standalone(
                    tx,
                    admitted.request.receipt_id,
                    admitted.version,
                    "runner",
                    now=10.0,
                    ttl_seconds=30.0,
                ),
            )
        )
        assert claimed.phase.value == "claimed"


def test_cancel_terminal_checkpoint_event_and_delivery_are_atomic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cancel.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)

        async def scenario():
            requested = await _atomic(
                uow,
                lambda tx: uow.request_cancel(
                    tx,
                    CancelWorkflowRequest("cancel", receipt.run_id, "user", 0),
                    0,
                    activation,
                    now=3.0,
                ),
            )
            lease = await _atomic(
                uow,
                lambda tx: uow.claim_cancel_convergence(
                    tx,
                    "cancel",
                    requested.generation,
                    "reconciler",
                    now=4.0,
                    ttl_seconds=30.0,
                ),
            )
            return await _atomic(
                uow,
                lambda tx: uow.settle_cancel_convergence(
                    tx,
                    lease,
                    {},
                    {"checkpoint_id": "cancel-head", "namespace": "native"},
                    {"event_id": "cancel-event", "generation": 0},
                    (
                        {
                            "delivery_id": "delivery",
                            "sink_kind": "test",
                            "idempotency_key": "cancel-delivery",
                            "payload": {"cancelled": True},
                        },
                    ),
                    now=5.0,
                ),
            )

        outcome = asyncio.run(scenario())
        assert outcome.terminal is True
        assert (
            database.connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (receipt.run_id,)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE checkpoint_id='cancel-head'"
            ).fetchone()[0]
            == 1
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE event_id='cancel-event'"
            ).fetchone()[0]
            == 1
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE delivery_id='delivery'"
            ).fetchone()[0]
            == 1
        )


def test_cancel_convergence_uses_final_resolution_not_wake_consumption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cancel-resolution.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        outcome_hash = hashlib.sha256(b"resolution").hexdigest()
        database.connection.execute(
            "INSERT INTO reconciliation_resolutions(resolution_id,kind,ledger_identity,handoff_attempt,outcome,outcome_hash,evidence_ref,payload_json,created_at) VALUES('resolution','tool','effect',1,'completed',?,'evidence','{}',2)",
            (outcome_hash,),
        )
        database.connection.execute(
            "INSERT INTO run_wait_blockers(blocker_id,run_id,kind,ledger_identity,handoff_attempt,observed_version,resolution_id,wake_consumed,created_at,resolved_at,version) VALUES('blocker',?,'tool','effect',1,1,'resolution',0,2,2,2)",
            (receipt.run_id,),
        )
        database.connection.commit()

        async def scenario():  # type: ignore[no-untyped-def]
            requested = await _atomic(
                uow,
                lambda tx: uow.request_cancel(
                    tx,
                    CancelWorkflowRequest(
                        "cancel-resolution", receipt.run_id, "user", 0
                    ),
                    0,
                    activation,
                    now=3.0,
                ),
            )
            assert requested.blocker_ids == ("blocker",)
            lease = await _atomic(
                uow,
                lambda tx: uow.claim_cancel_convergence(
                    tx,
                    requested.cancel_id,
                    requested.generation,
                    "reconciler",
                    now=4.0,
                    ttl_seconds=30.0,
                ),
            )
            resolution_snapshot = {
                "blocker": {
                    "blocker_version": 2,
                    "resolution_id": "resolution",
                    "outcome": "completed",
                    "outcome_hash": outcome_hash,
                }
            }
            return await _atomic(
                uow,
                lambda tx: uow.settle_cancel_convergence(
                    tx,
                    lease,
                    resolution_snapshot,
                    {"checkpoint_id": "cancel-resolution-head", "namespace": "native"},
                    {"event_id": "cancel-resolution-event", "generation": 0},
                    (),
                    now=5.0,
                ),
            )

        outcome = asyncio.run(scenario())
        assert outcome.terminal is True
        assert database.connection.execute(
            "SELECT wake_consumed FROM run_wait_blockers WHERE blocker_id='blocker'"
        ).fetchone()[0] == 0


def test_recovery_snapshot_cas_rejects_live_advance(tmp_path: Path) -> None:
    with Database.open(tmp_path / "recovery.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        snapshot = uow.read_recovery_snapshot(receipt.run_id)
        candidate = snapshot.candidate
        database.connection.execute(
            "UPDATE runs SET version=version+1 WHERE run_id=?", (receipt.run_id,)
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="snapshot changed"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.commit_recovery_outcome(
                        tx,
                        candidate,
                        snapshot,
                        RecoveryOutcome(
                            candidate.status,
                            "queued",
                            "recover",
                            "lease_expired",
                            "recovery-receipt",
                        ),
                        now=5.0,
                    ),
                )
            )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM workflow_recovery_receipts"
            ).fetchone()[0]
            == 0
        )


def test_recovery_candidates_use_pinned_namespace_and_full_authority_snapshot(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "recovery-candidates.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)
        database.connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) "
            "VALUES(?,?,?,?,?)",
            (receipt.run_id, "aaa-rogue", "rogue", 99, 999.0),
        )
        database.connection.commit()

        candidates, cursor = uow.list_candidates(None)

        assert cursor is None
        candidate = next(item for item in candidates if item.run_id == receipt.run_id)
        assert candidate.runtime_lease_owner == activation.execution_lease.owner_id
        assert candidate.runtime_lease_epoch == activation.execution_lease.epoch
        assert (
            candidate.runtime_lease_expires_at
            == activation.execution_lease.expires_at
        )
        assert candidate.workflow_lease_namespace == "native"
        assert candidate.workflow_lease_owner == activation.workflow_lease.owner_id
        assert candidate.workflow_lease_epoch == activation.workflow_lease.epoch
        assert (
            candidate.workflow_lease_expires_at
            == activation.workflow_lease.expires_at
        )
        assert candidate.run_fence_owner == activation.run_fence.owner_id
        assert (
            candidate.run_fence_runtime_lease_epoch
            == activation.run_fence.runtime_lease_epoch
        )
        assert candidate.run_fence_epoch == activation.run_fence.epoch
        assert candidate.run_fence_state == "active"
        assert candidate.checkpoint_head is None


@pytest.mark.parametrize(
    "advance_sql",
    (
        (
            "UPDATE workflow_leases SET owner_id='new-runtime-owner' "
            "WHERE run_id=? AND namespace='runtime.kernel'"
        ),
        (
            "UPDATE workflow_leases SET expires_at=expires_at+1 "
            "WHERE run_id=? AND namespace='native'"
        ),
        (
            "UPDATE run_fences SET runtime_lease_epoch=runtime_lease_epoch+1 "
            "WHERE run_id=?"
        ),
        "UPDATE run_fences SET state='released',released_at=5 WHERE run_id=?",
    ),
)
def test_recovery_snapshot_cas_binds_every_authority_field(
    tmp_path: Path, advance_sql: str
) -> None:
    with Database.open(tmp_path / "recovery-authority-cas.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        snapshot = uow.read_recovery_snapshot(receipt.run_id)
        database.connection.execute(advance_sql, (receipt.run_id,))
        database.connection.commit()

        with pytest.raises(UnitOfWorkConflict, match="snapshot changed"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.commit_recovery_outcome(
                        tx,
                        snapshot.candidate,
                        snapshot,
                        RecoveryOutcome(
                            snapshot.candidate.status,
                            "queued",
                            "recover",
                            "lease_expired",
                            "authority-cas-receipt",
                        ),
                        now=6.0,
                    ),
                )
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_recovery_receipts"
        ).fetchone()[0] == 0


def test_recovery_enumeration_is_workflow_only_stable_keyset_and_nullable(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "recovery-pagination.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        for index in range(102):
            run_id = f"workflow-{index:03d}"
            database.connection.execute(
                "INSERT OR IGNORE INTO execution_sessions(session_id,created_at) "
                "VALUES('recovery-page',0)"
            )
            database.connection.execute(
                "INSERT INTO runs(run_id,execution_session_id,request_id,root_run_id,"
                "profile_key,driver_kind,state,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'workflow','queued',0,0,0)",
                (run_id, "recovery-page", f"request-{index}", run_id, "agent.general"),
            )
        database.connection.execute(
            "INSERT INTO runs(run_id,execution_session_id,request_id,root_run_id,"
            "profile_key,driver_kind,state,version,created_at,updated_at) "
            "VALUES('non-workflow','recovery-page','other','non-workflow',"
            "'agent.general','react','queued',0,0,0)"
        )
        database.connection.execute(
            "UPDATE runs SET state='reserved_fork' WHERE run_id='workflow-101'"
        )
        database.connection.commit()

        first, cursor = uow.list_candidates(None)
        second, final_cursor = uow.list_candidates(cursor)

        assert len(first) == 100
        assert cursor == "workflow-099"
        assert tuple(item.run_id for item in second) == ("workflow-100",)
        assert final_cursor is None
        assert all(item.workflow_lease_namespace is None for item in first + second)
        assert all(item.runtime_lease_owner is None for item in first + second)


def test_resume_retry_wait_releases_activation_and_reclaims_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retry.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id=?",
            (receipt.run_id,),
        )
        database.connection.execute(
            "INSERT INTO workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at) VALUES('head',?,'native','{}',?,1,0,3)",
            (receipt.run_id, hashlib.sha256(b"{}").hexdigest()),
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,response_json,version,created_at,resolved_at) VALUES('decision',?,'workflow_interrupt','allowed','{}','{}',1,3,3)",
            (receipt.run_id,),
        )
        database.connection.commit()
        responses: dict[str, JsonValue] = {"decision": {"approved": True}}
        request = ResumeAdmissionRequest(
            "retry",
            receipt.run_id,
            2,
            "head",
            (("decision", hashlib.sha256(b"{}").hexdigest()),),
            responses,
            hashlib.sha256(canonical_json(responses).encode()).hexdigest(),
        )

        async def scenario():
            admitted = await _atomic(
                uow, lambda tx: uow.admit_resume(tx, request, now=4.0)
            )
            claimed = await _atomic(
                uow,
                lambda tx: uow.claim_resume_standalone(
                    tx,
                    "retry",
                    admitted.version,
                    "runner",
                    now=5.0,
                    ttl_seconds=30.0,
                ),
            )
            binding = ResumeCommitBinding(
                "retry", claimed.version, 2, claimed.request_fingerprint
            )
            waiting = await _atomic(
                uow,
                lambda tx: uow.defer_resume_retry(
                    tx, binding, claimed.activation, "retry-op", 1, 20.0, now=6.0
                ),
            )
            reclaimed = await _atomic(
                uow,
                lambda tx: uow.claim_resume_standalone(
                    tx,
                    "retry",
                    waiting.version,
                    "next",
                    now=20.0,
                    ttl_seconds=30.0,
                ),
            )
            return waiting, reclaimed

        waiting, reclaimed = asyncio.run(scenario())
        assert waiting.phase.value == "retry_wait"
        assert waiting.activation is None
        assert reclaimed.phase.value == "claimed"
        assert reclaimed.claim_owner == "next"


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:settle_cancel_convergence:after_runs_write",
        "workflow:settle_cancel_convergence:after_workflow_checkpoints_write",
        "workflow:settle_cancel_convergence:after_run_events_write",
        "workflow:settle_cancel_convergence:after_delivery_outbox_write",
        "workflow:settle_cancel_convergence:after_workflow_cancel_receipts_write",
    ),
)
def test_cancel_each_authority_write_fault_rolls_back_after_reopen(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"cancel-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)

        async def prepare():  # type: ignore[no-untyped-def]
            requested = await _atomic(
                uow,
                lambda tx: uow.request_cancel(
                    tx,
                    CancelWorkflowRequest("cancel-fault", receipt.run_id, "user", 0),
                    0,
                    activation,
                    now=3.0,
                ),
            )
            return await _atomic(
                uow,
                lambda tx: uow.claim_cancel_convergence(
                    tx,
                    "cancel-fault",
                    requested.generation,
                    "reconciler",
                    now=4.0,
                    ttl_seconds=30.0,
                ),
            )

        lease = asyncio.run(prepare())

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("cancel fault")

        with pytest.raises(RuntimeError, match="cancel fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.settle_cancel_convergence(
                        tx,
                        lease,
                        {},
                        {"checkpoint_id": "cancel-fault-head", "namespace": "native"},
                        {"event_id": "cancel-fault-event"},
                        (
                            {
                                "delivery_id": "cancel-fault-delivery",
                                "sink_kind": "test",
                                "idempotency_key": "cancel-fault-key",
                                "payload": {"cancelled": True},
                            },
                        ),
                        now=5.0,
                        fault=fault,
                    ),
                )
            )
    with Database.open(path) as reopened:
        row = reopened.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (receipt.run_id,)
        ).fetchone()
        assert row is not None and row[0] == "cancel_requested"
        for query, identity in (
            (
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE checkpoint_id=?",
                "cancel-fault-head",
            ),
            (
                "SELECT COUNT(*) FROM run_events WHERE event_id=?",
                "cancel-fault-event",
            ),
            (
                "SELECT COUNT(*) FROM delivery_outbox WHERE delivery_id=?",
                "cancel-fault-delivery",
            ),
        ):
            assert reopened.connection.execute(
                query,
                (identity,),
            ).fetchone()[0] == 0
        assert reopened.connection.execute(
            "SELECT phase FROM workflow_cancel_receipts WHERE cancel_id='cancel-fault'"
        ).fetchone()[0] == "cancelling"


def test_recovery_receipt_replays_after_state_advance_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "recovery-replay.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        snapshot = uow.read_recovery_snapshot(receipt.run_id)
        outcome = RecoveryOutcome(
            snapshot.candidate.status,
            "queued",
            "recover",
            "expired",
            "recovery-exact",
        )
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.commit_recovery_outcome(
                    tx, snapshot.candidate, snapshot, outcome, now=4.0
                ),
            )
        )
        assert first == outcome
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        repeated = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.commit_recovery_outcome(
                    tx, snapshot.candidate, snapshot, outcome, now=9.0
                ),
            )
        )
        assert repeated == outcome
        with pytest.raises(UnitOfWorkConflict, match="receipt changed"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.commit_recovery_outcome(
                        tx,
                        snapshot.candidate,
                        snapshot,
                        RecoveryOutcome(
                            outcome.previous_status,
                            outcome.status,
                            "quarantine",
                            outcome.reason,
                            outcome.receipt_id,
                        ),
                        now=9.0,
                    ),
                )
            )


def _fork_request(snapshot, *, fork_id: str = "fork") -> ForkRequest:  # type: ignore[no-untyped-def]
    payload: dict[str, JsonValue] = {
        "fork_id": fork_id,
        "source_run_id": snapshot.candidate.run_id,
        "source_namespace": "native",
        "source_checkpoint_id": "source-head",
        "source_run_version": snapshot.candidate.run_version,
        "source_head": "source-head",
        "engine_hash": "c" * 64,
        "manifest_hash": "a" * 64,
        "implementation_hash": "b" * 64,
        "schema_hash": "d" * 64,
        "patch": {"value": 2},
        "dangerous_confirmation": None,
    }
    fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return ForkRequest(fingerprint=fingerprint, **payload)  # type: ignore[arg-type]


def _seed_fork_source(uow, database):  # type: ignore[no-untyped-def]
    receipt, activation = _admit_and_claim(uow)
    checkpoint_json = canonical_json(
        {
            "engine_kind": "simple-harness-native",
            "snapshot_version": 1,
            "state_schema_version": 1,
            "thread_id": receipt.thread_id,
            "checkpoint_ns": "native",
            "checkpoint_id": "source-head",
            "parent_checkpoint_id": None,
            "run_id": receipt.run_id,
            "step": 0,
            "state": {"value": 1},
            "frontier": [],
            "completed_activations": {},
            "join_firings": [],
            "node_writes": {},
            "interrupt": None,
            "metadata": {},
        }
    )
    database.connection.execute(
        "INSERT INTO workflow_checkpoints(checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,lease_epoch,version,created_at) VALUES('source-head',?,'native',?,?,?,0,3)",
        (
            receipt.run_id,
            checkpoint_json,
            hashlib.sha256(checkpoint_json.encode()).hexdigest(),
            activation.workflow_lease.epoch,
        ),
    )
    database.connection.commit()
    snapshot = uow.read_recovery_snapshot(receipt.run_id)
    return receipt, snapshot, _fork_request(snapshot)


def test_fork_prepare_checkpoint_commit_and_commit_only_recovery(tmp_path: Path) -> None:
    path = tmp_path / "fork.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)

        async def prepare_and_checkpoint():  # type: ignore[no-untyped-def]
            prepared = await _atomic(
                uow,
                lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0),
            )
            lease = await _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, request.fork_id, prepared.version, "forker", now=5.0, ttl_seconds=1.0
                ),
            )
            checkpointed = await _atomic(
                uow,
                lambda tx: uow.checkpoint_fork(
                    tx, lease, None, "fork-checkpoint-op", {"state": {"value": 2}}, now=5.5
                ),
            )
            return checkpointed

        checkpointed = asyncio.run(prepare_and_checkpoint())
        assert checkpointed.phase.value == "checkpointed"
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)

        async def reclaim_and_commit():  # type: ignore[no-untyped-def]
            lease = await _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, "fork", checkpointed.version, "recovery", now=7.0, ttl_seconds=30.0
                ),
            )
            assert lease.mode == "commit_only"
            with pytest.raises(UnitOfWorkConflict, match="cannot rewrite"):
                await _atomic(
                    uow,
                    lambda tx: uow.checkpoint_fork(
                        tx, lease, checkpointed.target_checkpoint_id, "other", {}, now=8.0
                    ),
                )
            return await _atomic(
                uow,
                lambda tx: uow.commit_fork(
                    tx, lease, lease.expected_receipt_version, now=8.0
                ),
            )

        committed = asyncio.run(reclaim_and_commit())
        assert committed.phase.value == "committed"
        assert reopened.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (committed.target_run_id,)
        ).fetchone()[0] == "created"


def test_fork_checkpoint_and_commit_response_loss_replay_exactly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fork-response-loss.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)
        prepared = asyncio.run(
            _atomic(uow, lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0))
        )
        lease = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, request.fork_id, prepared.version, "forker", now=5.0,
                    ttl_seconds=30.0,
                ),
            )
        )
        checkpoint = {"state": {"value": 2}}
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.checkpoint_fork(
                    tx, lease, None, "checkpoint-op", checkpoint, now=5.5
                ),
            )
        )
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replayed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.checkpoint_fork(
                    tx, lease, None, "checkpoint-op", checkpoint, now=6.0
                ),
            )
        )
        assert replayed == first
        with pytest.raises(UnitOfWorkConflict, match="replay changed"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.checkpoint_fork(
                        tx, lease, None, "checkpoint-op", {"state": {}}, now=6.0
                    ),
                )
            )
        committed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.commit_fork(
                    tx, lease, first.version, now=6.5
                ),
            )
        )
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        exact = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.commit_fork(
                    tx, lease, first.version, now=40.0
                ),
            )
        )
        assert exact == committed
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id=?",
            (request.source_run_id,),
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?",
            (committed.target_run_id,),
        ).fetchone()[0] == 1


def test_fork_claim_response_loss_replays_same_active_write_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fork-claim-response-loss.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)
        prepared = asyncio.run(
            _atomic(uow, lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0))
        )
        claimed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx,
                    request.fork_id,
                    prepared.version,
                    "forker",
                    now=5.0,
                    ttl_seconds=30.0,
                ),
            )
        )
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replayed = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx,
                    request.fork_id,
                    prepared.version,
                    "forker",
                    now=6.0,
                    ttl_seconds=999.0,
                ),
            )
        )
        assert replayed == claimed
        with pytest.raises(UnitOfWorkConflict, match="active claimant"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_fork(
                        tx,
                        request.fork_id,
                        prepared.version,
                        "other",
                        now=6.0,
                        ttl_seconds=30.0,
                    ),
                )
            )


def test_fork_effect_snapshot_change_tombstones_reserved_child_atomically(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "fork-effect-change.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)
        prepared = asyncio.run(
            _atomic(uow, lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0))
        )
        lease = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, request.fork_id, prepared.version, "forker", now=5.0,
                    ttl_seconds=30.0,
                ),
            )
        )
        database.connection.execute(
            "INSERT INTO execution_effects(effect_id,run_id,call_id,tool_name,"
            "arguments_json,request_hash,authorization_receipt_ref,"
            "handoff_receipt_ref,fence_epoch,state,prepared_at,handed_off_at,"
            "version,handoff_attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "late-danger",
                request.source_run_id,
                "call",
                "write",
                "{}",
                "e" * 64,
                "auth",
                "handoff",
                1,
                "handed_off",
                5.1,
                5.2,
                2,
                1,
            ),
        )
        database.connection.commit()
        rolled_back = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.checkpoint_fork(
                    tx, lease, None, "checkpoint-op", {"state": {}}, now=6.0
                ),
            )
        )
        assert rolled_back.phase.value == "rolled_back"
        assert database.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (rolled_back.target_run_id,)
        ).fetchone()[0] == "reserved_fork"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?",
            (rolled_back.target_run_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:prepare_fork:after_runs_write",
        "workflow:prepare_fork:after_workflow_fork_receipts_write",
    ),
)
def test_fork_prepare_each_write_fault_rolls_back(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"fork-prepare-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("fork prepare fault")

        with pytest.raises(RuntimeError, match="fork prepare fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.prepare_fork(
                        tx, request, snapshot, now=4.0, fault=fault
                    ),
                )
            )
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_fork_receipts WHERE fork_id='fork'"
        ).fetchone()[0] == 0
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE state='reserved_fork'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:checkpoint_fork:after_workflow_checkpoints_write",
        "workflow:checkpoint_fork:after_workflow_fork_receipts_write",
    ),
)
def test_fork_checkpoint_each_write_fault_rolls_back(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"fork-checkpoint-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)

        async def prepare():  # type: ignore[no-untyped-def]
            prepared = await _atomic(
                uow, lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0)
            )
            return await _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, "fork", prepared.version, "forker", now=5.0, ttl_seconds=30.0
                ),
            )

        lease = asyncio.run(prepare())

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("fork checkpoint fault")

        with pytest.raises(RuntimeError, match="fork checkpoint fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.checkpoint_fork(
                        tx,
                        lease,
                        None,
                        "checkpoint-op",
                        {"state": {}},
                        now=6.0,
                        fault=fault,
                    ),
                )
            )
    with Database.open(path) as reopened:
        row = reopened.connection.execute(
            "SELECT phase,version,target_run_id FROM workflow_fork_receipts WHERE fork_id='fork'"
        ).fetchone()
        assert tuple(row[:2]) == ("claimed", 1)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (row[2],)
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:commit_fork:after_runs_write",
        "workflow:commit_fork:after_workflow_fork_receipts_write",
    ),
)
def test_fork_commit_each_write_fault_rolls_back(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"fork-commit-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _receipt, snapshot, request = _seed_fork_source(uow, database)

        async def checkpoint():  # type: ignore[no-untyped-def]
            prepared = await _atomic(
                uow, lambda tx: uow.prepare_fork(tx, request, snapshot, now=4.0)
            )
            lease = await _atomic(
                uow,
                lambda tx: uow.claim_fork(
                    tx, "fork", prepared.version, "forker", now=5.0, ttl_seconds=30.0
                ),
            )
            checkpointed = await _atomic(
                uow,
                lambda tx: uow.checkpoint_fork(
                    tx, lease, None, "checkpoint-op", {"state": {}}, now=6.0
                ),
            )
            return lease, checkpointed

        lease, checkpointed = asyncio.run(checkpoint())

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("fork commit fault")

        with pytest.raises(RuntimeError, match="fork commit fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.commit_fork(
                        tx, lease, checkpointed.version, now=7.0, fault=fault
                    ),
                )
            )
    with Database.open(path) as reopened:
        row = reopened.connection.execute(
            "SELECT phase,target_run_id FROM workflow_fork_receipts WHERE fork_id='fork'"
        ).fetchone()
        assert row[0] == "checkpointed"
        assert reopened.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (row[1],)
        ).fetchone()[0] == "reserved_fork"


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:request_cancel:after_runs_write",
        "workflow:request_cancel:after_workflow_leases_write",
        "workflow:request_cancel:after_run_fences_write",
        "workflow:request_cancel:after_workflow_cancel_receipts_write",
    ),
)
def test_cancel_request_fault_rolls_back_activation_and_receipt(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"cancel-request-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, activation = _admit_and_claim(uow)

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("cancel request fault")

        with pytest.raises(RuntimeError, match="cancel request fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.request_cancel(
                        tx,
                        CancelWorkflowRequest(
                            "cancel-request-fault", receipt.run_id, "user", 0
                        ),
                        0,
                        activation,
                        now=3.0,
                        fault=fault,
                    ),
                )
            )
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (receipt.run_id,)
        ).fetchone()[0] == "created"
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_leases WHERE run_id=?", (receipt.run_id,)
        ).fetchone()[0] == 2
        assert reopened.connection.execute(
            "SELECT state FROM run_fences WHERE run_id=?", (receipt.run_id,)
        ).fetchone()[0] == "active"
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_cancel_receipts WHERE cancel_id='cancel-request-fault'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_label",
    (
        "workflow:commit_recovery_outcome:after_runs_write",
        "workflow:commit_recovery_outcome:after_workflow_recovery_receipts_write",
    ),
)
def test_recovery_outcome_each_write_fault_rolls_back(
    tmp_path: Path, fault_label: str
) -> None:
    path = tmp_path / f"recovery-{fault_label.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        snapshot = uow.read_recovery_snapshot(receipt.run_id)
        outcome = RecoveryOutcome(
            snapshot.candidate.status,
            "queued",
            "recover",
            "expired",
            "recovery-fault",
        )

        def fault(label: str) -> None:
            if label == fault_label:
                raise RuntimeError("recovery fault")

        with pytest.raises(RuntimeError, match="recovery fault"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.commit_recovery_outcome(
                        tx,
                        snapshot.candidate,
                        snapshot,
                        outcome,
                        now=4.0,
                        fault=fault,
                    ),
                )
            )
    with Database.open(path) as reopened:
        assert tuple(
            reopened.connection.execute(
                "SELECT state,version FROM runs WHERE run_id=?", (receipt.run_id,)
            ).fetchone()
        ) == (snapshot.candidate.status, snapshot.candidate.run_version)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_recovery_receipts"
        ).fetchone()[0] == 0


def test_resolved_recovery_claim_is_dedicated_fenced_and_reclaimable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovery-claim.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        receipt, _activation = _admit_and_claim(uow)
        database.connection.execute(
            "INSERT INTO reconciliation_resolutions(resolution_id,kind,ledger_identity,handoff_attempt,outcome,outcome_hash,evidence_ref,payload_json,created_at) VALUES('resolution','provider','invocation',1,'completed',?,'evidence','{}',3)",
            ("e" * 64,),
        )
        database.connection.execute(
            "INSERT INTO run_wait_blockers(blocker_id,run_id,kind,ledger_identity,handoff_attempt,observed_version,resolution_id,wake_consumed,created_at,resolved_at,version) VALUES('blocker',?,'provider','invocation',1,1,'resolution',0,2,3,2)",
            (receipt.run_id,),
        )
        database.connection.commit()

        async def scenario():  # type: ignore[no-untyped-def]
            first = await _atomic(
                uow,
                lambda tx: uow.claim_resolved_recovery(
                    tx, "blocker", 2, "first", now=4.0, ttl_seconds=2.0
                ),
            )
            with pytest.raises(UnitOfWorkConflict, match="active claimant"):
                await _atomic(
                    uow,
                    lambda tx: uow.claim_resolved_recovery(
                        tx, "blocker", 2, "second", now=5.0, ttl_seconds=2.0
                    ),
                )
            reclaimed = await _atomic(
                uow,
                lambda tx: uow.claim_resolved_recovery(
                    tx, "blocker", 2, "second", now=6.0, ttl_seconds=2.0
                ),
            )
            return first, reclaimed

        first, reclaimed = asyncio.run(scenario())
        assert first.epoch == 1
        assert reclaimed.epoch == 2 and reclaimed.owner_id == "second"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_cancel_receipts"
        ).fetchone()[0] == 0
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,epoch FROM workflow_recovery_claims WHERE blocker_id='blocker'"
            ).fetchone()
        ) == ("second", 2)
