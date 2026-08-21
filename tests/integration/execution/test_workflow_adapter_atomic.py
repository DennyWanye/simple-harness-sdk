# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowOperationConflict,
)


def _seed(path: Path, *, fault=None):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database, workflow_fault=fault)
    uow.create_with_start_snapshot(
        execution_session_id="session",
        run_id="run",
        request_id="request",
        profile_key="agent.general",
        driver_kind="workflow",
        snapshot={},
        event_id="created",
        now=0.0,
    )
    database.connection.execute("UPDATE runs SET state='running',version=1 WHERE run_id='run'")
    database.connection.execute(
        """
        INSERT INTO decisions(
            decision_id,run_id,kind,state,request_json,response_json,version,created_at,resolved_at
        ) VALUES('decision','run','approval','allowed','{}','{"ok":true}',1,0,1)
        """
    )
    database.connection.execute(
        """
        INSERT INTO execution_effects(
            effect_id,run_id,call_id,tool_name,arguments_json,request_hash,
            authorization_receipt_ref,fence_epoch,state,prepared_at
        ) VALUES('effect','run','call','tool','{}',?,'auth',1,'prepared',0)
        """,
        ("a" * 64,),
    )
    return database, uow, CheckpointExecutionAdapter(database)


async def _invoke(method: str, adapter, transaction, *, changed: bool = False):  # type: ignore[no-untyped-def]
    if method == "mark_running_on_claim":
        return await adapter.mark_running_on_claim(
            transaction,
            run_id="run",
            checkpoint_namespace="native",
            lease_epoch=1,
            claim_epoch=1,
            now=2.0 if changed else 1.0,
        )
    if method == "consume_decisions":
        return await adapter.consume_decisions(
            transaction,
            run_id="run",
            checkpoint_id="checkpoint",
            decision_ids=("decision",),
            responses={"decision": {"ok": not changed}},
            checkpoint_namespace="native",
            lease_epoch=1,
            now=1.0,
        )
    if method == "open_decision":
        return await adapter.open_decision(
            transaction,
            run_id="run",
            interrupt_id="interrupt",
            request={"kind": "interrupt", "prompt": "changed" if changed else "stable"},
            checkpoint_namespace="native",
            checkpoint_id="checkpoint",
            lease_epoch=1,
            now=1.0,
        )
    if method == "materialize_intent":
        return await adapter.materialize_intent(
            transaction,
            run_id="run",
            intent_id="intent",
            intent={"event_type": "workflow.test", "payload": {"changed": changed}},
            checkpoint_namespace="native",
            checkpoint_id="checkpoint",
            lease_epoch=1,
            now=1.0,
        )
    if method == "link_effects":
        return await adapter.link_effects(
            transaction,
            run_id="run",
            checkpoint_namespace="native",
            checkpoint_id="checkpoint",
            effect_ids=("effect",),
            lease_epoch=1,
            now=2.0 if changed else 1.0,
        )
    if method == "finalize_run":
        return await adapter.finalize_run(
            transaction,
            run_id="run",
            terminal_checkpoint_id="terminal",
            status="failed" if changed else "completed",
            outcome={"changed": changed},
            checkpoint_namespace="native",
            lease_epoch=1,
            now=1.0,
        )
    raise AssertionError(method)


METHODS = (
    "mark_running_on_claim",
    "consume_decisions",
    "open_decision",
    "materialize_intent",
    "link_effects",
    "finalize_run",
)


@pytest.mark.parametrize("method", METHODS)
def test_each_adapter_exact_replay_and_changed_payload_conflict(
    tmp_path: Path, method: str
) -> None:
    path = tmp_path / f"{method}.db"
    database, uow, adapter = _seed(path)
    try:

        async def first(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, adapter, transaction)

        outcome = asyncio.run(uow.run_atomic(first, fault_label="adapter"))
    finally:
        database.close()
    with Database.open(path) as reopened:
        replay_uow = SqliteExecutionUnitOfWork(reopened)
        replay_adapter = CheckpointExecutionAdapter(reopened)

        async def replay(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, replay_adapter, transaction)

        assert asyncio.run(replay_uow.run_atomic(replay, fault_label="adapter")) == outcome

        async def conflict(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, replay_adapter, transaction, changed=True)

        with pytest.raises(WorkflowOperationConflict, match="payload changed"):
            asyncio.run(replay_uow.run_atomic(conflict, fault_label="adapter"))


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("point", ("after_ledger", "after_receipt"))
def test_each_adapter_write_fault_rolls_back_ledger_and_receipt(
    tmp_path: Path, method: str, point: str
) -> None:
    path = tmp_path / f"{method}-{point}.db"

    def fault(label: str) -> None:
        if label == f"workflow_adapter.{method}.{point}":
            raise RuntimeError(label)

    database, uow, adapter = _seed(path, fault=fault)
    try:

        async def operation(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, adapter, transaction)

        with pytest.raises(RuntimeError, match=point):
            asyncio.run(uow.run_atomic(operation, fault_label="adapter"))
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM workflow_operation_receipts"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.close()
    with Database.open(path) as reopened:
        assert reopened.integrity_check() == ("ok",)
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM workflow_operation_receipts"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("method", METHODS)
def test_after_commit_response_loss_reopens_receipt_first(tmp_path: Path, method: str) -> None:
    path = tmp_path / f"{method}-response-loss.db"

    def fault(label: str) -> None:
        if label == "adapter.after_commit":
            raise RuntimeError(label)

    database, uow, adapter = _seed(path, fault=fault)
    try:

        async def operation(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, adapter, transaction)

        with pytest.raises(RuntimeError, match="after_commit"):
            asyncio.run(uow.run_atomic(operation, fault_label="adapter"))
    finally:
        database.close()
    with Database.open(path) as reopened:
        replay_uow = SqliteExecutionUnitOfWork(reopened)
        replay_adapter = CheckpointExecutionAdapter(reopened)

        async def replay(transaction):  # type: ignore[no-untyped-def]
            return await _invoke(method, replay_adapter, transaction)

        outcome = asyncio.run(replay_uow.run_atomic(replay, fault_label="adapter"))
        assert outcome is not None
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM workflow_operation_receipts"
            ).fetchone()[0]
            == 1
        )


def test_global_operation_id_rejects_cross_method_second_writer(tmp_path: Path) -> None:
    database, uow, adapter = _seed(tmp_path / "global.db")
    try:
        shared_id = hashlib.sha256(b'["run","shared"]').hexdigest()

        async def winner(transaction):  # type: ignore[no-untyped-def]
            return await adapter.open_decision(
                transaction,
                run_id="run",
                interrupt_id="shared",
                request={"kind": "interrupt"},
                operation_id=shared_id,
                checkpoint_namespace="native",
                checkpoint_id="checkpoint",
                lease_epoch=1,
                now=1.0,
            )

        asyncio.run(uow.run_atomic(winner, fault_label="winner"))

        async def loser(transaction):  # type: ignore[no-untyped-def]
            return await adapter.materialize_intent(
                transaction,
                run_id="run",
                intent_id="shared",
                intent={"event_type": "workflow.test", "payload": {}},
                operation_id=shared_id,
                checkpoint_namespace="native",
                checkpoint_id="checkpoint",
                lease_epoch=1,
                now=1.0,
            )

        with pytest.raises(WorkflowOperationConflict, match="adapter methods"):
            asyncio.run(uow.run_atomic(loser, fault_label="loser"))
        assert (
            database.connection.execute(
                "SELECT adapter_method FROM workflow_operation_receipts"
            ).fetchone()[0]
            == "open_decision"
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind='workflow.test'"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.close()
