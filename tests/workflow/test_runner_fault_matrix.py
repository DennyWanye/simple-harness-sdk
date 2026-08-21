# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import (
    PersonalWorkflowHostServices,
    StatePatch,
    WorkflowContext,
    WorkflowHostServices,
)
from simple_harness.workflow.definition import Edge, NodeDefinition, WorkflowDefinition
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


def _compiled(calls: list[str]):  # type: ignore[no-untyped-def]
    async def node(state, context):  # type: ignore[no-untyped-def]
        del state, context
        calls.append("native.node")
        return StatePatch({})

    return compile_workflow(
        WorkflowDefinition(
            "delegate",
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


def _build(
    database,
    registry,
    owner,
    host_services=WorkflowHostServices(),
):  # type: ignore[no-untyped-def]
    uow = SqliteExecutionUnitOfWork(database)
    ports = WorkflowExecutionPorts(uow, CheckpointExecutionAdapter(database), uow, uow, uow)
    store = SqliteNativeCheckpointStore(ports, blob_references=NoBlobReferences())
    runner = WorkflowRunner(
        registry=registry,
        checkpoint=store,
        recovery=RecordingRecoveryPort(),
        trace=RecordingTracePort(),
        execution_ports=ports,
        terminal_projection_port=LegacyTerminalProjectionPort(),
        terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
        host_services=host_services,
        owner=owner,
        clock=lambda: 1.0,
    )
    return uow, ports, store, runner


@pytest.mark.anyio
async def test_runner_injects_frozen_profile_services(tmp_path: Path) -> None:
    observed: list[object] = []

    async def node(state, context):  # type: ignore[no-untyped-def]
        del state
        observed.append(context.port("personal_workflow_runtime"))
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "service_injection",
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

    class Service:
        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            return {}

    service = Service()
    services = WorkflowHostServices(personal_v1=PersonalWorkflowHostServices(runtime=service))
    with Database.open(tmp_path / "services.db") as database:
        _uow, _ports, _store, runner = _build(
            database,
            WorkflowRegistry((compiled,)),
            "runner",
            services,
        )
        run_id = await runner.start(
            session_id="session",
            request_id="request",
            turn_id="turn",
            profile_key="workflow.personal_v1",
            tool_catalog_generation=1,
            workflow_name="service_injection",
            workflow_version="1",
            start_input={},
            capability_snapshot={},
        )
        await runner.run(run_id, {}, WorkflowContext())
    assert observed == [service]


async def _start_and_run(runner: WorkflowRunner, request_id: str) -> str:
    run_id = await runner.start(
        session_id="session",
        request_id=request_id,
        turn_id="turn",
        profile_key="workflow.delegate",
        tool_catalog_generation=1,
        workflow_name="delegate",
        workflow_version="1",
        start_input={},
        capability_snapshot={},
    )
    result = await runner.run(run_id, {}, WorkflowContext())
    assert result.output == {}
    return run_id


def test_runner_constructor_has_no_hidden_authority_defaults() -> None:
    required = {
        name
        for name, value in inspect.signature(WorkflowRunner).parameters.items()
        if value.default is inspect.Parameter.empty
    }
    assert {
        "registry",
        "checkpoint",
        "recovery",
        "trace",
        "execution_ports",
        "terminal_projection_port",
        "terminal_commit_projection_port",
    } <= required
    assert "unit_of_work" not in inspect.signature(WorkflowRunner).parameters
    assert "native_store" not in inspect.signature(WorkflowRunner).parameters


def test_runner_materializes_real_native_and_has_no_second_state_machine(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    registry = WorkflowRegistry((_compiled(calls),))
    with Database.open(tmp_path / "delegate.db") as database:
        _uow, _ports, _store, runner = _build(database, registry, "runner")
        run_id = asyncio.run(_start_and_run(runner, "request"))
        assert calls == ["native.node"]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )


def test_shared_registry_never_caches_runner_bound_authorities(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    registry = WorkflowRegistry((_compiled(calls),))
    observed: list[tuple[str, int]] = []
    for name in ("first", "second"):
        with Database.open(tmp_path / f"{name}.db") as database:
            _uow, _ports, _store, runner = _build(database, registry, name)
            run_id = asyncio.run(_start_and_run(runner, name))
            count = database.connection.execute(
                "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            observed.append((run_id, int(count)))
    assert observed[0][0] != observed[1][0]
    assert [count for _run_id, count in observed] == [2, 2]
    assert calls == ["native.node", "native.node"]


def test_runner_rejects_split_transaction_authority_before_write(
    tmp_path: Path,
) -> None:
    registry = WorkflowRegistry((_compiled([]),))
    with (
        Database.open(tmp_path / "authority-a.db") as database_a,
        Database.open(tmp_path / "authority-b.db") as database_b,
    ):
        _uow_a, _ports_a, store_a, _runner_a = _build(database_a, registry, "a")
        uow_b = SqliteExecutionUnitOfWork(database_b)
        ports_b = WorkflowExecutionPorts(
            uow_b, CheckpointExecutionAdapter(database_b), uow_b, uow_b, uow_b
        )
        with pytest.raises(ValueError, match="different transaction owners"):
            WorkflowRunner(
                registry=registry,
                checkpoint=store_a,
                recovery=RecordingRecoveryPort(),
                trace=RecordingTracePort(),
                execution_ports=ports_b,
                terminal_projection_port=LegacyTerminalProjectionPort(),
                terminal_commit_projection_port=NoTerminalCommitProjectionPort(),
            )
        assert database_b.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
