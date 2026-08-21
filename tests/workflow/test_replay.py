# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from simple_harness.contracts import canonical_json
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    StatePatch,
)
from simple_harness.workflow.definition import (
    Edge,
    NodeDefinition,
    WorkflowDefinition,
)
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    DangerousEffectObservation,
    StartAdmissionRequest,
    StartMode,
    WorkflowExecutionPorts,
)
from simple_harness.workflow.native import NativeSnapshotEnvelope
from simple_harness.workflow.replay import (
    WorkflowReplay,
    WorkflowReplayError,
    confirm_dangerous_effects,
    deterministic_fork_key,
)
from simple_harness.workflow.runner import WorkflowRegistry, manifest_hash


class NoBlobReferences:
    async def validate_references(self, transaction, **values):  # type: ignore[no-untyped-def]
        del transaction, values


def _workflow():  # type: ignore[no-untyped-def]
    async def node(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return StatePatch({})

    return compile_workflow(
        WorkflowDefinition(
            "fork-demo",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {
                "value": ChannelSpec(
                    JsonType.INTEGER,
                    ReducerKind.SINGLE_WRITER,
                    frozenset({"node"}),
                )
            },
            5,
            4,
            edges=(Edge("node", "__end__"),),
        )
    )


def _seed(path: Path):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    workflow = _workflow()
    registry = WorkflowRegistry((workflow,))
    request = StartAdmissionRequest(
        request_key="fork-source-request",
        mode=StartMode.STANDALONE,
        session_id="session",
        request_id="request",
        turn_id="turn",
        profile_key="workflow.fork",
        driver_kind="workflow",
        tool_catalog_generation=1,
        workflow_name="fork-demo",
        workflow_version="1",
        requested_run_id="source-run",
        requested_trace_id="source-trace",
        requested_thread_id="source-thread",
        resolved_run_id="source-run",
        resolved_trace_id="source-trace",
        resolved_thread_id="source-thread",
        checkpoint_namespace="native",
        manifest_hash=manifest_hash(workflow.manifest),
        implementation_hash=workflow.manifest.implementation_bundle_hash,
        state_schema_version=workflow.manifest.state_schema_version,
        start_input_schema_ref=None,
        start_input_schema_hash=None,
        terminal_projection_descriptor=None,
        terminal_request_factory_hash=None,
        start_input={"value": 1},
        capability_snapshot={},
    )

    async def admit(transaction):  # type: ignore[no-untyped-def]
        return await uow.admit_start_standalone(transaction, request, now=1.0)

    receipt = asyncio.run(uow.run_atomic(admit, fault_label="seed-fork-source"))
    snapshot = NativeSnapshotEnvelope(
        thread_id=receipt.thread_id,
        checkpoint_ns="native",
        checkpoint_id="source-head",
        parent_checkpoint_id=None,
        run_id=receipt.run_id,
        state_schema_version=1,
        step=0,
        state={"value": 1},
        frontier=(),
        metadata={
            "manifest": workflow.manifest.to_dict(),
            "manifest_hash": manifest_hash(workflow.manifest),
            "implementation_hash": workflow.manifest.implementation_bundle_hash,
        },
    )
    raw = canonical_json(snapshot.to_dict())
    database.connection.execute(
        "INSERT INTO workflow_checkpoints(checkpoint_id,run_id,namespace,"
        "checkpoint_json,checkpoint_hash,lease_epoch,version,created_at) "
        "VALUES(?,?,?,?,?,1,0,2)",
        (
            snapshot.checkpoint_id,
            snapshot.run_id,
            snapshot.checkpoint_ns,
            raw,
            hashlib.sha256(raw.encode()).hexdigest(),
        ),
    )
    database.connection.commit()
    ports = WorkflowExecutionPorts(uow, CheckpointExecutionAdapter(database), uow, uow, uow)
    store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
    replay = WorkflowReplay(
        execution_ports=ports,
        native_store=store,
        registry=registry,
        owner_id="fork-owner",
        clock=lambda: 3.0,
    )
    return database, uow, replay, receipt.run_id, snapshot


def test_fork_key_is_canonical_and_source_version_bound() -> None:
    first = deterministic_fork_key(
        source_run_id="source",
        source_checkpoint_ns="native",
        source_checkpoint_id="checkpoint",
        source_version=3,
        state_patch={"b": 2, "a": 1},
    )
    second = deterministic_fork_key(
        source_run_id="source",
        source_checkpoint_ns="native",
        source_checkpoint_id="checkpoint",
        source_version=3,
        state_patch={"a": 1, "b": 2},
    )
    assert first == second
    assert first != deterministic_fork_key(
        source_run_id="source",
        source_checkpoint_ns="native",
        source_checkpoint_id="checkpoint",
        source_version=4,
        state_patch={"a": 1, "b": 2},
    )


def test_confirmation_digest_is_sdk_computed_and_canonically_ordered() -> None:
    observations = (
        DangerousEffectObservation("b", "tool", "unknown", 2, "b" * 64, 1),
        DangerousEffectObservation("a", "tool", "handed_off", 1, "a" * 64, 1),
    )
    confirmation = confirm_dangerous_effects("ancestor", observations)
    assert tuple(item.effect_id for item in confirmation.observations) == ("a", "b")
    assert len(confirmation.digest) == 64


def test_public_replay_executes_real_prepare_claim_checkpoint_commit_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "public-replay.db"
    database, _uow, replay, run_id, source = _seed(path)
    history = asyncio.run(replay.history(run_id))
    assert history[0]["checkpoint_id"] == source.checkpoint_id
    assert history[0]["engine_kind"] == "simple-harness-native"
    first = asyncio.run(
        replay.fork_checkpoint(
            run_id=run_id,
            checkpoint_id=source.checkpoint_id,
            expected_version=0,
            state_patch={"value": 2},
        )
    )
    database.close()

    database, _uow, replay, reopened_run_id, reopened_source = _open_seeded(path, run_id, source)
    database.connection.execute("UPDATE runs SET version=version+1 WHERE run_id=?", (run_id,))
    database.connection.commit()
    repeated = asyncio.run(
        replay.fork_checkpoint(
            run_id=reopened_run_id,
            checkpoint_id=reopened_source.checkpoint_id,
            expected_version=0,
            state_patch={"value": 2},
        )
    )
    assert repeated["run_id"] == first["run_id"]
    assert repeated["idempotent"] is True
    row = database.connection.execute(
        "SELECT checkpoint_json FROM workflow_checkpoints WHERE run_id=?",
        (first["run_id"],),
    ).fetchone()
    assert row is not None
    assert json.loads(row[0])["state"] == {"value": 2}
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id=?", (run_id,)
        ).fetchone()[0]
        == 1
    )
    database.close()


def _open_seeded(path: Path, run_id: str, source: NativeSnapshotEnvelope):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    workflow = _workflow()
    ports = WorkflowExecutionPorts(uow, CheckpointExecutionAdapter(database), uow, uow, uow)
    replay = WorkflowReplay(
        execution_ports=ports,
        native_store=SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences()),
        registry=WorkflowRegistry((workflow,)),
        owner_id="fork-owner",
        clock=lambda: 4.0,
    )
    return database, uow, replay, run_id, source


def test_public_replay_rejects_stale_source_and_unsafe_checkpoint_shapes(
    tmp_path: Path,
) -> None:
    database, _uow, replay, run_id, source = _seed(tmp_path / "replay-reject.db")
    with pytest.raises(WorkflowReplayError, match="identity changed"):
        asyncio.run(
            replay.fork_checkpoint(
                run_id=run_id,
                checkpoint_id=source.checkpoint_id,
                expected_version=1,
                state_patch={"value": 2},
            )
        )
    with pytest.raises(WorkflowReplayError, match="type"):
        asyncio.run(
            replay.fork_checkpoint(
                run_id=run_id,
                checkpoint_id=source.checkpoint_id,
                expected_version=0,
                state_patch={"value": "wrong"},
            )
        )
    database.close()
