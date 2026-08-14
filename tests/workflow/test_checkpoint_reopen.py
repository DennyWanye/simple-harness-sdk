# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

from simple_harness.contracts import RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import StatePatch, WorkflowContext
from simple_harness.workflow.definition import Edge, NodeDefinition, WorkflowDefinition
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowExecutionPorts,
)

from ._fakes import LegacyTerminalProjectionPort, NoTerminalCommitProjectionPort


class NoBlobReferences:
    async def validate_references(self, transaction, *, blob_refs, **values):  # type: ignore[no-untyped-def]
        del transaction, blob_refs, values


async def _node(state, context):  # type: ignore[no-untyped-def]
    del state, context
    return StatePatch({})


def _native(workflow, store):  # type: ignore[no-untyped-def]
    return workflow.bind(
        store=store,
        terminal_projection_port=LegacyTerminalProjectionPort(),
        terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
    )


def test_real_sqlite_native_checkpoint_reopens_without_copying_fake_state(
    tmp_path: Path,
) -> None:
    workflow = compile_workflow(
        WorkflowDefinition(
            "reopen", "1", 1, "node", (NodeDefinition("node", _node),), {}, 5, 4,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "execution.db"
    config = {
        "run_id": "run-reopen", "thread_id": "thread", "checkpoint_ns": "native",
        "workflow_name": "reopen", "workflow_version": "1",
        "workflow_owner_id": "owner", "workflow_lease_epoch": 1,
        "logical_timestamp": 2.0,
    }
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        uow.create_with_start_snapshot(
            execution_session_id="session", run_id="run-reopen", request_id="request",
            profile_key="agent.general", driver_kind="workflow", snapshot={},
            event_id="created", now=0.0,
        )
        _run, lease = uow.claim_runtime_activation(
            run_id="run-reopen", owner_id="owner", namespace="runtime.kernel", now=1.0,
            lease_ttl_seconds=30.0,
        )
        fence = asyncio.run(uow.acquire(RunId("run-reopen"), lease, now=1.0))
        database.connection.execute(
            "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) VALUES('run-reopen','native','owner',?,?)",
            (lease.epoch, lease.expires_at),
        )
        database.connection.commit()
        config["workflow_lease_epoch"] = lease.epoch
        config["workflow_activation"] = {
            "run_id": "run-reopen",
            "owner_id": "owner",
            "runtime_namespace": "runtime.kernel",
            "runtime_epoch": lease.epoch,
            "expires_at": lease.expires_at,
            "run_fence_epoch": fence.epoch,
            "workflow_namespace": "native",
            "workflow_epoch": lease.epoch,
        }
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(database), uow, uow, uow
        )
        store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
        result = asyncio.run(
            _native(workflow, store).ainvoke(
                {}, WorkflowContext(), thread_id="thread", run_id="run-reopen",
                checkpoint_ns="native", configurable=config,
            )
        )
        assert result == {}
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(reopened), uow, uow, uow
        )
        store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
        result = asyncio.run(
            _native(workflow, store).ainvoke(
                None, WorkflowContext(), thread_id="thread", run_id="run-reopen",
                checkpoint_ns="native", configurable=config,
            )
        )
        assert result == {}
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id='run-reopen'"
        ).fetchone()[0] == 2
