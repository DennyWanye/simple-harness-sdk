# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from simple_harness.contracts import RunId, thaw_json
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import DecisionState, UnitOfWorkConflict
from simple_harness.runtime.orchestration import RuntimeStartDispatchClaim
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    StatePatch,
    WorkflowContext,
)
from simple_harness.workflow.control import workflow_interrupt
from simple_harness.workflow.definition import Edge, NodeDefinition, WorkflowDefinition
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    StartAdmissionRequest,
    StartMode,
    WorkflowExecutionPorts,
)
from simple_harness.workflow.runner import (
    WorkflowRegistry,
    WorkflowRunner,
    manifest_hash,
)

from ._fakes import (
    LegacyTerminalProjectionPort,
    NoTerminalCommitProjectionPort,
    RecordingRecoveryPort,
    RecordingTracePort,
)


class NoBlobReferences:
    async def validate_references(self, transaction, *, blob_refs, **values):  # type: ignore[no-untyped-def]
        del transaction, blob_refs, values


class ObservedRenewUnitOfWork(SqliteExecutionUnitOfWork):
    def __init__(self, database, *, renewed: asyncio.Event, fail: bool = False):  # type: ignore[no-untyped-def]
        super().__init__(database)
        self.renewed = renewed
        self.fail = fail
        self.fail_resolve = False

    async def renew_activation(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail:
            self.renewed.set()
            raise UnitOfWorkConflict("simulated workflow lease loss")
        result = await super().renew_activation(*args, **kwargs)
        self.renewed.set()
        return result

    async def resolve_decision_and_admit_resume(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail_resolve:
            kwargs["fault"] = lambda point: (
                (_ for _ in ()).throw(RuntimeError("simulated resolve crash"))
                if point == "workflow:resolve_resume:before_admit"
                else None
            )
        return await super().resolve_decision_and_admit_resume(*args, **kwargs)


def test_runner_real_native_start_run_and_reopen(tmp_path: Path) -> None:
    calls: list[str] = []

    async def node(state, context):  # type: ignore[no-untyped-def]
        del state, context
        calls.append("node")
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "runner",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {},
            5,
            4,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "runner.db"

    def build(database):  # type: ignore[no-untyped-def]
        uow = SqliteExecutionUnitOfWork(database)
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(database), uow, uow, uow
        )
        store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
        return uow, WorkflowRunner(
            registry=WorkflowRegistry((compiled,)),
            checkpoint=store,
            recovery=RecordingRecoveryPort(),
            trace=RecordingTracePort(),
            execution_ports=ports,
            terminal_projection_port=LegacyTerminalProjectionPort(),
            terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            owner="runner",
            clock=lambda: 1.0,
        )

    with Database.open(path) as database:
        _uow, runner = build(database)

        async def scenario() -> str:
            run_id = await runner.start(
                session_id="session",
                request_id="request",
                turn_id="turn",
                profile_key="workflow.runner",
                tool_catalog_generation=1,
                workflow_name="runner",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )
            result = await runner.run(run_id, context=WorkflowContext())
            assert result.output == {}
            return run_id

        run_id = asyncio.run(scenario())
        assert calls == ["node"]

    with Database.open(path) as database:
        uow, _runner = build(database)
        assert uow.read_run(run_id).state.value == "completed"  # type: ignore[union-attr]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )


def test_runner_cancel_terminal_exact_replay_survives_reopen(tmp_path: Path) -> None:
    async def node(state, context):  # type: ignore[no-untyped-def]
        del state, context
        raise AssertionError("cancelled workflow must not execute a node")

    compiled = compile_workflow(
        WorkflowDefinition(
            "cancel-replay",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {},
            5,
            4,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "cancel-replay.db"

    def build(database):  # type: ignore[no-untyped-def]
        uow = SqliteExecutionUnitOfWork(database)
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(database), uow, uow, uow
        )
        return uow, WorkflowRunner(
            registry=WorkflowRegistry((compiled,)),
            checkpoint=SqliteNativeCheckpointStore(
                ports, blob_references=NoBlobReferences()
            ),
            recovery=RecordingRecoveryPort(),
            trace=RecordingTracePort(),
            execution_ports=ports,
            terminal_projection_port=LegacyTerminalProjectionPort(),
            terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            owner="runner",
            clock=lambda: 2.0,
        )

    with Database.open(path) as database:
        uow, runner = build(database)
        run_id = asyncio.run(
            runner.start(
                session_id="session",
                request_id="cancel",
                turn_id="turn",
                profile_key="workflow.cancel-replay",
                tool_catalog_generation=1,
                workflow_name="cancel-replay",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )
        )
        first = asyncio.run(runner.request_cancel(run_id, "user"))
        assert first.status.value == "cancelled"
        assert isinstance(first.output, Mapping)
        first_identity = (first.output["cancel_id"], first.output["generation"])
        assert uow.verify_workflow_cancel_terminal(
            run_id=run_id,
            cancel_id=str(first_identity[0]),
            generation=int(first_identity[1]),
        )

    with Database.open(path) as reopened:
        uow, runner = build(reopened)
        replay = asyncio.run(runner.request_cancel(run_id, "user"))
        assert isinstance(replay.output, Mapping)
        assert (replay.output["cancel_id"], replay.output["generation"]) == first_identity
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_cancel_receipts WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.cancelled'",
            (run_id,),
        ).fetchone()[0] == 1
        assert uow.verify_workflow_cancel_terminal(
            run_id=run_id,
            cancel_id=str(first_identity[0]),
            generation=int(first_identity[1]),
        )


def test_runner_resume_receipt_settles_with_terminal_checkpoint_same_transaction(
    tmp_path: Path,
) -> None:
    calls = 0
    renewed = asyncio.Event()
    resume_phase = False
    resume_sleep_calls = 0
    clock_value = 2.0

    async def approval(_state, context):  # type: ignore[no-untyped-def]
        nonlocal clock_value
        nonlocal calls
        calls += 1
        response = workflow_interrupt({"question": "continue?"})
        await renewed.wait()
        assert isinstance(response, Mapping)
        return StatePatch({"approved": bool(response["approved"])})

    async def sleep(_seconds: float) -> None:
        nonlocal clock_value, resume_phase, resume_sleep_calls
        if not resume_phase:
            await asyncio.Event().wait()
        resume_sleep_calls += 1
        if resume_sleep_calls > 1:
            await asyncio.Event().wait()
        clock_value = 20.0
        await asyncio.sleep(0)

    compiled = compile_workflow(
        WorkflowDefinition(
            "runner-resume",
            "1",
            1,
            "approval",
            (
                NodeDefinition(
                    "approval",
                    approval,
                    interrupt_capable=True,
                    barrier=True,
                    exclusive_superstep=True,
                    pre_interrupt_effect_policy="pure",
                ),
            ),
            {
                "approved": ChannelSpec(
                    JsonType.BOOLEAN,
                    ReducerKind.SINGLE_WRITER,
                    frozenset({"approval"}),
                )
            },
            5,
            4,
            edges=(Edge("approval", "__end__"),),
        )
    )
    path = tmp_path / "runner-resume.db"
    with Database.open(path) as database:
        uow = ObservedRenewUnitOfWork(database, renewed=renewed)
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(database), uow, uow, uow
        )
        store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
        runner = WorkflowRunner(
            registry=WorkflowRegistry((compiled,)),
            checkpoint=store,
            recovery=RecordingRecoveryPort(),
            trace=RecordingTracePort(),
            execution_ports=ports,
            terminal_projection_port=LegacyTerminalProjectionPort(),
            terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            owner="runner",
            clock=lambda: clock_value,
            sleep=sleep,
            heartbeat_interval_seconds=1.0,
        )

        async def suspend():  # type: ignore[no-untyped-def]
            run_id = await runner.start(
                session_id="session",
                request_id="resume",
                turn_id="turn",
                profile_key="workflow.runner-resume",
                tool_catalog_generation=1,
                workflow_name="runner-resume",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )
            observed = await runner.run(run_id, context=WorkflowContext())
            assert observed.status.value == "waiting"
            assert isinstance(observed.output, Mapping)
            assert "interrupt" in observed.output
            return run_id

        run_id = asyncio.run(suspend())
        decision = database.connection.execute(
            "SELECT decision_id FROM decisions WHERE run_id=? AND state='open'",
            (run_id,),
        ).fetchone()
        assert decision is not None
        decision_id = str(decision[0])
        durable = uow.read_decision(decision_id)
        assert durable is not None
        resume_phase = True
        with pytest.raises(ValueError, match="nonce"):
            asyncio.run(
                runner.resolve_and_resume(
                    run_id,
                    decision_id=decision_id,
                    nonce="foreign-nonce",
                    expected_version=durable.version,
                    response={"approved": True},
                )
            )
        with pytest.raises(ValueError, match="version"):
            asyncio.run(
                runner.resolve_and_resume(
                    run_id,
                    decision_id=decision_id,
                    nonce=decision_id,
                    expected_version=durable.version + 1,
                    response={"approved": True},
                )
            )
        uow.fail_resolve = True
        with pytest.raises(RuntimeError, match="simulated resolve crash"):
            asyncio.run(
                runner.resolve_and_resume(
                    run_id,
                    decision_id=decision_id,
                    nonce=decision_id,
                    expected_version=durable.version,
                    response={"approved": True},
                )
            )
        uow.fail_resolve = False
        assert uow.read_decision(decision_id).state is DecisionState.OPEN
        assert uow.read_run(run_id).state.value == "waiting"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_resume_admissions WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0
        result = asyncio.run(
            runner.resolve_and_resume(
                run_id,
                decision_id=decision_id,
                nonce=decision_id,
                expected_version=durable.version,
                response={"approved": True},
                context=WorkflowContext(),
            )
        )
        assert isinstance(result.output, Mapping)
        assert result.output["approved"] is True
        receipt = database.connection.execute(
            "SELECT phase,committed_checkpoint FROM workflow_resume_admissions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert receipt is not None and receipt[0] == "settled"
        assert database.connection.execute(
            "SELECT checkpoint_id FROM workflow_checkpoints WHERE run_id=? ORDER BY version DESC LIMIT 1",
            (run_id,),
        ).fetchone()[0] == receipt[1]
        assert calls == 2


def test_runner_heartbeat_renews_before_long_node_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat-renew.db"

    async def scenario() -> tuple[str, float]:
        renewed = asyncio.Event()
        clock_value = 1.0
        sleep_calls = 0

        async def sleep(_seconds: float) -> None:
            nonlocal clock_value, sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                await asyncio.Event().wait()
            clock_value = 20.0
            await asyncio.sleep(0)

        async def node(_state, _context):  # type: ignore[no-untyped-def]
            await renewed.wait()
            return StatePatch({})

        compiled = compile_workflow(
            WorkflowDefinition(
                "heartbeat-renew",
                "1",
                1,
                "node",
                (NodeDefinition("node", node),),
                {},
                5,
                4,
                edges=(Edge("node", "__end__"),),
            )
        )
        with Database.open(path) as database:
            uow = ObservedRenewUnitOfWork(database, renewed=renewed)
            ports = WorkflowExecutionPorts(
                uow, CheckpointExecutionAdapter(database), uow, uow, uow
            )
            runner = WorkflowRunner(
                registry=WorkflowRegistry((compiled,)),
                checkpoint=SqliteNativeCheckpointStore(
                    ports, blob_references=NoBlobReferences()
                ),
                recovery=RecordingRecoveryPort(),
                trace=RecordingTracePort(),
                execution_ports=ports,
                terminal_projection_port=LegacyTerminalProjectionPort(),
                terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
                owner="runner",
                clock=lambda: clock_value,
                sleep=sleep,
                heartbeat_interval_seconds=1.0,
                lease_ttl_seconds=30.0,
            )
            run_id = await runner.start(
                session_id="session",
                request_id="heartbeat-renew",
                turn_id="turn",
                profile_key="workflow.heartbeat-renew",
                tool_catalog_generation=1,
                workflow_name="heartbeat-renew",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )
            result = await runner.run(run_id, context=WorkflowContext())
            assert result.status.value == "completed"
            expires_at = float(
                database.connection.execute(
                    "SELECT claim_expires_at FROM workflow_start_admissions WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            return run_id, expires_at

    run_id, _receipt_expiry = asyncio.run(scenario())
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 2
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_leases WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0


def test_runner_heartbeat_loss_cancels_noncooperative_node_and_fences_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "heartbeat-loss.db"

    async def scenario() -> str:
        loss = asyncio.Event()
        cancelled = asyncio.Event()

        async def node(_state, _context):  # type: ignore[no-untyped-def]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return StatePatch({"late": True})

        compiled = compile_workflow(
            WorkflowDefinition(
                "heartbeat-loss",
                "1",
                1,
                "node",
                (NodeDefinition("node", node),),
                {},
                5,
                4,
                edges=(Edge("node", "__end__"),),
            )
        )
        with Database.open(path) as database:
            uow = ObservedRenewUnitOfWork(database, renewed=loss, fail=True)
            ports = WorkflowExecutionPorts(
                uow, CheckpointExecutionAdapter(database), uow, uow, uow
            )
            runner = WorkflowRunner(
                registry=WorkflowRegistry((compiled,)),
                checkpoint=SqliteNativeCheckpointStore(
                    ports, blob_references=NoBlobReferences()
                ),
                recovery=RecordingRecoveryPort(),
                trace=RecordingTracePort(),
                execution_ports=ports,
                terminal_projection_port=LegacyTerminalProjectionPort(),
                terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
                owner="runner",
                clock=lambda: 2.0,
                sleep=lambda _seconds: asyncio.sleep(0),
                heartbeat_interval_seconds=1.0,
            )
            run_id = await runner.start(
                session_id="session",
                request_id="heartbeat-loss",
                turn_id="turn",
                profile_key="workflow.heartbeat-loss",
                tool_catalog_generation=1,
                workflow_name="heartbeat-loss",
                workflow_version="1",
                start_input={},
                capability_snapshot={},
            )
            with pytest.raises(UnitOfWorkConflict, match="simulated workflow lease loss"):
                await runner.run(run_id, context=WorkflowContext())
            assert loss.is_set() and cancelled.is_set()
            return run_id

    run_id = asyncio.run(scenario())
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT state FROM run_fences WHERE run_id=?", (run_id,)
        ).fetchone()[0] == "released"


def test_runner_precreated_start_rejects_missing_runtime_dispatch_authority(
    tmp_path: Path,
) -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "precreated",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {},
            5,
            4,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "precreated.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        ports = WorkflowExecutionPorts(
            uow, CheckpointExecutionAdapter(database), uow, uow, uow
        )
        runner = WorkflowRunner(
            registry=WorkflowRegistry((compiled,)),
            checkpoint=SqliteNativeCheckpointStore(
                ports, blob_references=NoBlobReferences()
            ),
            recovery=RecordingRecoveryPort(),
            trace=RecordingTracePort(),
            execution_ports=ports,
            terminal_projection_port=LegacyTerminalProjectionPort(),
            terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            owner="runtime-owner",
            clock=lambda: 2.0,
        )
        request = StartAdmissionRequest(
                request_key="precreated-key",
                mode=StartMode.PRECREATED,
                session_id="session",
                request_id="request",
                turn_id="turn",
                profile_key="workflow.precreated",
                driver_kind="workflow",
                tool_catalog_generation=1,
                workflow_name="precreated",
                workflow_version="1",
                requested_run_id="precreated-run",
                requested_trace_id="precreated-trace",
                requested_thread_id="precreated-thread",
                resolved_run_id="precreated-run",
                resolved_trace_id="precreated-trace",
                resolved_thread_id="precreated-thread",
                checkpoint_namespace="native",
                manifest_hash=manifest_hash(compiled.manifest),
                implementation_hash=compiled.manifest.implementation_bundle_hash,
                state_schema_version=compiled.manifest.state_schema_version,
                start_input_schema_ref="schema://workflow.precreated/v1",
                start_input_schema_hash=hashlib.sha256(b"{}").hexdigest(),
                terminal_projection_descriptor=None,
                terminal_request_factory_hash=None,
                start_input={},
                capability_snapshot={},
            )
        payload_json, _fingerprint, run_id, trace_id, thread_id = uow._start_identity(
            request
        )
        start_snapshot = json.loads(payload_json)
        start_snapshot.update(
            {"resolved_run_id": run_id, "trace_id": trace_id, "thread_id": thread_id}
        )
        uow.create_with_start_snapshot(
            execution_session_id="session",
            run_id=run_id,
            request_id="request",
            profile_key="workflow.precreated",
            driver_kind="workflow",
            snapshot=start_snapshot,
            event_id="precreated-created",
            now=0.0,
        )
        _run, execution_lease = uow.claim_runtime_activation(
            run_id=run_id,
            owner_id="runtime-owner",
            namespace="runtime.kernel",
            now=1.0,
            lease_ttl_seconds=30.0,
        )
        run_fence = asyncio.run(uow.acquire(RunId(run_id), execution_lease, now=1.0))
        missing_dispatch = RuntimeStartDispatchClaim(
            "missing-dispatch-claim",
            run_id,
            execution_lease.owner_id,
            execution_lease.epoch,
            1,
        )
        with pytest.raises(UnitOfWorkConflict, match="dispatch claim is missing"):
            asyncio.run(
                runner.start_precreated(
                    request=request,
                    execution_lease=execution_lease,
                    run_fence=run_fence,
                    dispatch_claim=missing_dispatch,
                )
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_start_admissions"
        ).fetchone()[0] == 0
