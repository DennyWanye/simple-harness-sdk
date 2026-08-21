# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import (
    StatePatch,
    WorkflowContext,
)
from simple_harness.workflow.definition import NodeDefinition, WorkflowDefinition
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowExecutionPorts,
)
from simple_harness.workflow.runner import WorkflowRegistry, WorkflowRunner

from ._fakes import (
    LegacyTerminalProjectionPort,
    NoTerminalCommitProjectionPort,
    RecordingRecoveryPort,
    RecordingTracePort,
)
from .test_runner_h16_sqlite import NoBlobReferences


def test_live_foreign_lease_prevents_second_owner_and_zero_writes(
    tmp_path: Path,
) -> None:
    async def node(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return StatePatch({})

    workflow = compile_workflow(
        WorkflowDefinition("lease", "1", 1, "node", (NodeDefinition("node", node),), {}, 3, 2)
    )

    async def scenario() -> None:
        with Database.open(tmp_path / "foreign-lease.db") as database:
            uow = SqliteExecutionUnitOfWork(database)
            execution_ports = WorkflowExecutionPorts(
                uow, CheckpointExecutionAdapter(database), uow, uow, uow
            )
            store = SqliteNativeCheckpointStore(execution_ports, blob_references=NoBlobReferences())
            first = WorkflowRunner(
                registry=WorkflowRegistry((workflow,)),
                checkpoint=store,
                recovery=RecordingRecoveryPort(),
                trace=RecordingTracePort(),
                execution_ports=execution_ports,
                owner="first",
                clock=lambda: 1.0,
                terminal_projection_port=LegacyTerminalProjectionPort(),
                terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            )
            run_id = await first.start(
                session_id="session",
                request_id="request",
                turn_id="turn",
                profile_key="workflow.lease",
                tool_catalog_generation=1,
                workflow_name="lease",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )

            async def claim(transaction):  # type: ignore[no-untyped-def]
                return await uow.claim_activation(
                    transaction,
                    run_id,
                    0,
                    "first",
                    now=1.0,
                    ttl_seconds=30.0,
                )

            await uow.run_atomic(claim, fault_label="claim-first")
            runner = WorkflowRunner(
                registry=WorkflowRegistry((workflow,)),
                checkpoint=store,
                recovery=RecordingRecoveryPort(),
                trace=RecordingTracePort(),
                execution_ports=execution_ports,
                owner="second",
                clock=lambda: 2.0,
                terminal_projection_port=LegacyTerminalProjectionPort(),
                terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            )
            with pytest.raises(RuntimeError, match="active owner"):
                await runner.run(run_id, context=WorkflowContext())
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                database.connection.execute(
                    "SELECT state FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                == "created"
            )

    asyncio.run(scenario())


def test_registry_rejects_same_version_with_different_manifest() -> None:
    async def one(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return StatePatch({})

    async def two(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return StatePatch({"changed": True})

    first = compile_workflow(
        WorkflowDefinition("immutable", "1", 1, "node", (NodeDefinition("node", one),), {}, 3, 2)
    )
    second = compile_workflow(
        WorkflowDefinition("immutable", "1", 1, "node", (NodeDefinition("node", two),), {}, 3, 2)
    )
    with pytest.raises(ValueError, match="different manifest"):
        WorkflowRegistry((first, second))
