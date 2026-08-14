# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from simple_harness.contracts import RunId, canonical_json
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowExecutionPorts,
    WorkflowOperationConflict,
)
from simple_harness.workflow.native import (
    TERMINAL_REQUEST_FACTORY_HASH,
    TERMINAL_REQUEST_SCHEMA_HASH,
    NativeSnapshotEnvelope,
    NativeWorkflowExecutable,
    TerminalProjectionDescriptor,
)


class NoBlobReferences:
    async def validate_references(self, transaction, *, blob_refs, **values):  # type: ignore[no-untyped-def]
        del transaction, values
        if blob_refs:
            raise WorkflowOperationConflict("unknown blob reference")


def _create(uow: SqliteExecutionUnitOfWork) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session", run_id="run", request_id="request",
        profile_key="agent.general", driver_kind="workflow", snapshot={},
        event_id="start", now=0.0,
    )


def _authorities(database: Database, *, fault=None):  # type: ignore[no-untyped-def]
    uow = SqliteExecutionUnitOfWork(database, workflow_fault=fault)
    ports = WorkflowExecutionPorts(
        unit_of_work=uow,
        checkpoint=CheckpointExecutionAdapter(database),
        lifecycle=uow,
        recovery=uow,
        replay=uow,
    )
    return uow, SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())


def _seed(store: SqliteNativeCheckpointStore, uow: SqliteExecutionUnitOfWork):
    _run, lease = uow.claim_runtime_activation(
        run_id="run", owner_id="owner", namespace="runtime.kernel", now=1.0,
        lease_ttl_seconds=30.0,
    )
    fence = asyncio.run(uow.acquire(RunId("run"), lease, now=1.0))
    uow.database.connection.execute(
        "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) VALUES('run','native','owner',?,?)",
        (lease.epoch, lease.expires_at),
    )
    uow.database.connection.commit()
    config = {
        "run_id": "run", "thread_id": "thread", "checkpoint_ns": "native",
        "workflow_name": "demo", "workflow_version": "1",
        "workflow_owner_id": "owner", "workflow_lease_epoch": lease.epoch,
        "logical_timestamp": 2.0,
        "workflow_activation": {
            "run_id": "run", "owner_id": "owner",
            "runtime_namespace": "runtime.kernel", "runtime_epoch": lease.epoch,
            "expires_at": lease.expires_at, "run_fence_epoch": fence.epoch,
            "workflow_namespace": "native", "workflow_epoch": lease.epoch,
        },
    }
    descriptor = {
        "capability_id": "terminal.demo", "version": "1",
        "projector_fingerprint": "1" * 64,
        "request_schema_hash": TERMINAL_REQUEST_SCHEMA_HASH,
        "request_factory_hash": TERMINAL_REQUEST_FACTORY_HASH,
    }
    descriptor["descriptor_digest"] = hashlib.sha256(
        "|".join((descriptor["capability_id"], descriptor["version"], descriptor["projector_fingerprint"], descriptor["request_schema_hash"], descriptor["request_factory_hash"])).encode()
    ).hexdigest()
    snapshot = NativeSnapshotEnvelope(
        thread_id="thread", checkpoint_ns="native", checkpoint_id="genesis",
        parent_checkpoint_id=None, run_id="run", state_schema_version=1, step=0,
        state={"schema_version": 1}, frontier=(),
        metadata={"terminal_projection_descriptor": descriptor, "logical_timestamp": 2.0},
    )
    asyncio.run(store.ensure_genesis(operation_id="genesis-op", snapshot=snapshot, configurable=config))
    return config, snapshot


def _prepare(store: SqliteNativeCheckpointStore, config, **changes):  # type: ignore[no-untyped-def]
    output = changes.pop("output", {"intents": [], "blob_refs": []})
    descriptor = TerminalProjectionDescriptor(
        "terminal.demo", "1", "1" * 64,
        TERMINAL_REQUEST_SCHEMA_HASH, TERMINAL_REQUEST_FACTORY_HASH,
    )
    request = NativeWorkflowExecutable._terminal_projection_request(
        descriptor=descriptor,state={"schema_version": 1},run_id="run",
        workflow_name="demo",workflow_version="1",status="completed",
        error=None,recovery_action=None,
    )
    values = {
        "operation_id": "projection-op", "expected_head": "genesis",
        "descriptor_digest": hashlib.sha256(
            "|".join(("terminal.demo", "1", "1" * 64, TERMINAL_REQUEST_SCHEMA_HASH, TERMINAL_REQUEST_FACTORY_HASH)).encode()
        ).hexdigest(),
        "input_hash": changes.pop(
            "input_hash", hashlib.sha256(canonical_json(request).encode()).hexdigest()
        ),
        "output": output,
        "output_hash": hashlib.sha256(canonical_json(output).encode()).hexdigest(),
        "blob_refs": (), "configurable": config,
    }
    values.update(changes)
    return asyncio.run(store.prepare_terminal_projection(**values))


def _frontier(store: SqliteNativeCheckpointStore, config, **changes):  # type: ignore[no-untyped-def]
    terminal = NativeWorkflowExecutable.terminal_intents(
        {"schema_version": 1, "values": {"delivery_intents": []}},
        run_id="run", status="completed", error=None, recovery_action=None,
    )
    mappings = tuple(
        {
            "intent_id": intent.intent_id or "run:run-terminal",
            "event_key": intent.event_key or "run:terminal",
            "event_type": intent.event_type,
            "channel": intent.channel or "final",
            "payload": dict(intent.payload),
        }
        for intent in terminal
    )
    values = {
        "operation_id": "frontier-op", "expected_head": "genesis",
        "state": {"schema_version": 1}, "frontier": (),
        "completed_activations": {}, "join_firings": (),
        "consumed_interrupt_ids": (), "intents": mappings, "blob_refs": (),
        "terminal_status": "completed", "terminal_error": None,
        "recovery_action": None,
        "terminal_projection_prepare_id": "projection-op", "configurable": config,
    }
    values.update(changes)
    return asyncio.run(store.commit_frontier(**values))


def test_prepare_reopen_exact_replay_and_payload_conflict(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path) as database:
        uow, store = _authorities(database)
        _create(uow)
        config, _ = _seed(store, uow)
        receipt = _prepare(store, config)
    with Database.open(path) as reopened:
        _uow, store = _authorities(reopened)
        assert _prepare(store, config) == receipt
        with pytest.raises(WorkflowOperationConflict, match="changed"):
            _prepare(store, config, output={"intents": [{"changed": True}]})


def test_stale_lease_sibling_head_and_terminal_reject_zero_write(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow, store = _authorities(database)
        _create(uow)
        config, _ = _seed(store, uow)
        expired = {**config, "logical_timestamp": 99.0}
        with pytest.raises(WorkflowOperationConflict, match="stale workflow lease"):
            _prepare(store, expired)
        sibling = {**config, "checkpoint_ns": "sibling"}
        with pytest.raises(WorkflowOperationConflict, match="stale workflow lease"):
            _prepare(store, sibling)
        with pytest.raises(WorkflowOperationConflict, match="head changed"):
            _prepare(store, config, expected_head="stale-head", operation_id="stale-op")
        assert database.connection.execute(
            "SELECT COUNT(*) FROM terminal_projection_prepares"
        ).fetchone()[0] == 0
        database.connection.execute("UPDATE runs SET state='cancelled' WHERE run_id='run'")
        with pytest.raises(WorkflowOperationConflict, match="terminal workflow Run"):
            _prepare(store, config, operation_id="terminal-op")


def test_prepare_and_terminal_consume_fault_reopen_exact_replay(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    fault_at = {"value": "workflow_native:projection_prepare.before_commit"}

    def fault(point: str) -> None:
        if point == fault_at["value"]:
            raise RuntimeError(point)

    with Database.open(path) as database:
        uow, store = _authorities(database, fault=fault)
        _create(uow)
        config, _ = _seed(store, uow)
        with pytest.raises(RuntimeError, match="projection_prepare.before_commit"):
            _prepare(store, config)
        assert database.connection.execute(
            "SELECT COUNT(*) FROM terminal_projection_prepares"
        ).fetchone()[0] == 0
        fault_at["value"] = ""
        _prepare(store, config)
        fault_at["value"] = "workflow_native:frontier.after_commit"
        with pytest.raises(RuntimeError, match="frontier.after_commit"):
            _frontier(store, config)
    with Database.open(path) as reopened:
        _uow, store = _authorities(reopened)
        replay = _frontier(store, config)
        assert replay.snapshot.parent_checkpoint_id == "genesis"
        row = reopened.connection.execute(
            "SELECT consumed_at FROM terminal_projection_prepares WHERE operation_id='projection-op'"
        ).fetchone()
        assert row is not None and row["consumed_at"] is not None
        with pytest.raises(WorkflowOperationConflict, match="replay changed"):
            _frontier(store, config, state={"schema_version": 1, "changed": True})
