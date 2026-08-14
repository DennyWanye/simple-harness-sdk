# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from simple_harness.contracts import (
    CallId,
    EffectId,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import DecisionState
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationResult,
    CancellationToken,
    EffectExecutor,
    FunctionTool,
    ReconciliationObservation,
    ReconciliationState,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    RetryPolicy,
    StatePatch,
    WorkflowContext,
)
from simple_harness.workflow.control import WorkflowSuspended, workflow_interrupt
from simple_harness.workflow.definition import (
    ConditionalEdge,
    Edge,
    NodeDefinition,
    WorkflowDefinition,
    compile_workflow,
)
from simple_harness.workflow.errors import WorkflowErrorCode, WorkflowNodeError
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    ResumeAdmissionRequest,
    StartAdmissionRequest,
    StartMode,
    WorkflowExecutionPorts,
    WorkflowOperationConflict,
)
from simple_harness.workflow.native import (
    NativeWorkflowExecutable,
    TerminalProjectionDescriptor,
)


class _NoBlobReferences:
    async def validate_references(
        self, transaction, *, blob_refs, **identity  # type: ignore[no-untyped-def]
    ) -> None:
        del transaction, identity
        if blob_refs:
            raise WorkflowOperationConflict("unknown blob reference")


class _TerminalProjectionPort:
    def project_public(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs


class _TerminalCommitProjectionPort:
    def lookup(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs


def _channel(writer: str, value_type: JsonType = JsonType.STRING) -> ChannelSpec:
    return ChannelSpec(value_type, ReducerKind.SINGLE_WRITER, frozenset({writer}))


def _create_run(uow: SqliteExecutionUnitOfWork, run_id: str) -> dict[str, object]:
    request = StartAdmissionRequest(
        request_key=f"start-{run_id}",
        mode=StartMode.STANDALONE,
        session_id=f"session-{run_id}",
        request_id=f"request-{run_id}",
        turn_id=f"turn-{run_id}",
        profile_key="workflow.test",
        driver_kind="workflow",
        tool_catalog_generation=1,
        workflow_name="native-test",
        workflow_version="1",
        requested_run_id=run_id,
        requested_trace_id=f"trace-{run_id}",
        requested_thread_id=f"thread-{run_id}",
        resolved_run_id=run_id,
        resolved_trace_id=f"trace-{run_id}",
        resolved_thread_id=f"thread-{run_id}",
        checkpoint_namespace="native",
        manifest_hash="a" * 64,
        implementation_hash="b" * 64,
        state_schema_version=1,
        start_input_schema_ref="schema://workflow.test/v1",
        start_input_schema_hash=hashlib.sha256(b"{}").hexdigest(),
        terminal_projection_descriptor=None,
        terminal_request_factory_hash=None,
        start_input={},
        capability_snapshot={},
    )

    async def admit_and_claim():  # type: ignore[no-untyped-def]
        admitted = await uow.run_atomic(
            lambda transaction: uow.admit_start_standalone(
                transaction, request, now=0.0
            ),
            fault_label="test:admit_start",
        )
        return await uow.run_atomic(
            lambda transaction: uow.claim_activation(
                transaction,
                admitted.run_id,
                0,
                "workflow-owner",
                now=1.0,
                ttl_seconds=100.0,
            ),
            fault_label="test:claim_start",
        )

    activation = asyncio.run(admit_and_claim())
    config: dict[str, object] = {
        "run_id": run_id,
        "thread_id": f"thread-{run_id}",
        "checkpoint_ns": "native",
        "logical_timestamp": 2.0,
    }
    _apply_workflow_activation(config, activation)
    return config


def _apply_workflow_activation(config, activation):  # type: ignore[no-untyped-def]
    config.update(
        {
            "workflow_owner_id": activation.workflow_lease.owner_id,
            "workflow_lease_epoch": activation.workflow_lease.epoch,
            "runtime_lease_epoch": activation.execution_lease.epoch,
            "run_fence_epoch": activation.run_fence.epoch,
            "workflow_activation": {
                "run_id": activation.execution_lease.run_id,
                "owner_id": activation.execution_lease.owner_id,
                "runtime_namespace": activation.execution_lease.namespace,
                "runtime_epoch": activation.execution_lease.epoch,
                "expires_at": activation.execution_lease.expires_at,
                "run_fence_epoch": activation.run_fence.epoch,
                "workflow_namespace": activation.workflow_lease.namespace,
                "workflow_epoch": activation.workflow_lease.epoch,
            },
        }
    )


def _store(
    database: Database, *, fault=None  # type: ignore[no-untyped-def]
) -> tuple[SqliteExecutionUnitOfWork, SqliteNativeCheckpointStore]:
    uow = SqliteExecutionUnitOfWork(database, workflow_fault=fault)
    ports = WorkflowExecutionPorts(
        unit_of_work=uow,
        checkpoint=CheckpointExecutionAdapter(database),
        lifecycle=uow,
        recovery=uow,
        replay=uow,
    )
    return uow, SqliteNativeCheckpointStore(
        ports, blob_references=_NoBlobReferences()
    )


def _claim_resume(
    uow: SqliteExecutionUnitOfWork,
    store: SqliteNativeCheckpointStore,
    config: dict[str, object],
    *,
    decision_id: str,
    response: Mapping[str, JsonValue],
):  # type: ignore[no-untyped-def]
    run_id = str(config["run_id"])
    execution = asyncio.run(
        store.load_execution(
            run_id=run_id,
            thread_id=str(config["thread_id"]),
            checkpoint_ns=str(config["checkpoint_ns"]),
        )
    )
    interrupt = execution.snapshot.interrupt
    assert interrupt is not None
    assert interrupt["interrupt_id"] == decision_id
    run = uow.read_run(run_id)
    assert run is not None
    responses: dict[str, JsonValue] = {decision_id: dict(response)}
    request = ResumeAdmissionRequest(
        receipt_id=f"resume-{run_id}",
        run_id=run_id,
        expected_run_version=run.version,
        expected_checkpoint_head=execution.snapshot.checkpoint_id,
        pending_interrupts=(
            (
                decision_id,
                hashlib.sha256(canonical_json(dict(interrupt)).encode()).hexdigest(),
            ),
        ),
        responses=responses,
        responses_hash=hashlib.sha256(canonical_json(responses).encode()).hexdigest(),
        mode=StartMode.STANDALONE,
    )

    async def claim():  # type: ignore[no-untyped-def]
        admitted = await uow.run_atomic(
            lambda transaction: uow.admit_resume(transaction, request, now=4.0),
            fault_label="test:admit_resume",
        )
        return await uow.run_atomic(
            lambda transaction: uow.claim_resume_standalone(
                transaction,
                request.receipt_id,
                admitted.version,
                "workflow-owner",
                now=5.0,
                ttl_seconds=100.0,
            ),
            fault_label="test:claim_resume",
        )

    claimed = asyncio.run(claim())
    activation = claimed.activation
    assert activation is not None
    runtime_row = uow.database.connection.execute(
        "SELECT owner_id,epoch,expires_at FROM workflow_leases "
        "WHERE run_id=? AND namespace=?",
        (run_id, activation.execution_lease.namespace),
    ).fetchone()
    assert runtime_row is not None
    assert tuple(runtime_row) == (
        activation.execution_lease.owner_id,
        activation.execution_lease.epoch,
        activation.execution_lease.expires_at,
    )
    _apply_workflow_activation(config, activation)
    config.update(
        {
            "resume_binding": {
                "receipt_id": request.receipt_id,
                "expected_receipt_version": claimed.version,
                "target_run_revision": claimed.request.expected_run_version,
                "request_fingerprint": claimed.request_fingerprint,
            },
        }
    )
    return activation


def _native(workflow, store):  # type: ignore[no-untyped-def]
    return NativeWorkflowExecutable(
        workflow,
        store,
        terminal_projection_port=_TerminalProjectionPort(),
        terminal_commit_projection_port=_TerminalCommitProjectionPort(),
    )


def _invoke(executable, state, config):  # type: ignore[no-untyped-def]
    return asyncio.run(
        executable.ainvoke(
            state,
            WorkflowContext(),
            thread_id=config["thread_id"],
            run_id=config["run_id"],
            checkpoint_ns=config["checkpoint_ns"],
            configurable=config,
        )
    )


def test_real_sqlite_linear_close_reopen_preserves_terminal_checkpoint(
    tmp_path: Path,
) -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "durable"})

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-linear",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "linear.db"
    with Database.open(path) as database:
        uow, store = _store(database)
        config = _create_run(uow, "sqlite-linear")
        assert _invoke(_native(workflow, store), {}, config) == {"answer": "durable"}
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?",
            (config["run_id"],),
        ).fetchone()[0] == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.final'",
            (config["run_id"],),
        ).fetchone()[0] == 1
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        assert _invoke(_native(workflow, store), None, config) == {"answer": "durable"}
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.final'",
            (config["run_id"],),
        ).fetchone()[0] == 1


def test_real_sqlite_reopen_rejects_frozen_time_authority_drift(tmp_path: Path) -> None:
    calls = 0

    async def node(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return StatePatch({"answer": "durable"})

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-time-drift",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
        )
    )
    path = tmp_path / "time-drift.db"
    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if armed and point == "workflow_native:task_result.after_commit":
            armed = False
            raise ConnectionError(point)

    with Database.open(path) as database:
        uow, store = _store(database, fault=fault)
        config = _create_run(uow, "sqlite-time-drift")
        with pytest.raises(ConnectionError):
            _invoke(_native(workflow, store), {}, config)
    drifted = dict(config)
    drifted["logical_timestamp"] = 3.0
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        with pytest.raises(Exception, match="time|logical_timestamp"):
            _invoke(_native(workflow, store), None, drifted)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints WHERE run_id=?",
            (config["run_id"],),
        ).fetchone()[0] == 1
    assert calls == 1


@pytest.mark.parametrize("fault_label", ["task_result", "route"])
def test_real_sqlite_receipt_after_commit_reopens_without_repeating_pure_work(
    tmp_path: Path, fault_label: str
) -> None:
    calls = 0

    async def start(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return StatePatch({"choice": "done"})

    def route(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return "done"

    async def done(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "done"})

    workflow = compile_workflow(
        WorkflowDefinition(
            f"sqlite-{fault_label}",
            "1",
            1,
            "start",
            (NodeDefinition("start", start), NodeDefinition("done", done)),
            {"choice": _channel("start"), "answer": _channel("done")},
            16,
            8,
            edges=(Edge("done", "__end__"),),
            conditional_edges=(
                ConditionalEdge(
                    "start",
                    route,
                    {"done": "done"},
                    selector_effect_policy="pure",
                ),
            ),
        )
    )
    path = tmp_path / f"{fault_label}.db"
    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if armed and point == f"workflow_native:{fault_label}.after_commit":
            armed = False
            raise ConnectionError(point)

    with Database.open(path) as database:
        uow, store = _store(database, fault=fault)
        config = _create_run(uow, f"sqlite-{fault_label}")
        with pytest.raises(ConnectionError, match="after_commit"):
            _invoke(_native(workflow, store), {}, config)
    first_calls = calls
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        result = _invoke(_native(workflow, store), None, config)
        assert isinstance(result, Mapping) and result["answer"] == "done"
    assert calls == (2 if fault_label == "task_result" else first_calls)


def test_real_sqlite_retry_and_deterministic_join_survive_reopen(tmp_path: Path) -> None:
    attempts = 0

    async def flaky(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkflowNodeError(
                code=WorkflowErrorCode.RETRYABLE_TOOL, message_ref="retry"
            )
        return StatePatch({})

    async def branch(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    async def joined(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "joined"})

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-retry-join",
            "1",
            1,
            "flaky",
            (
                NodeDefinition(
                    "flaky",
                    flaky,
                    retry_policy=RetryPolicy(
                        max_attempts=2,
                        initial_delay_seconds=0,
                        max_delay_seconds=0,
                        retryable_codes=frozenset({"retryable_tool"}),
                    ),
                ),
                NodeDefinition("left", branch),
                NodeDefinition("right", branch),
                NodeDefinition("join", joined),
            ),
            {"answer": _channel("join")},
            20,
            10,
            edges=(
                Edge("flaky", "left"),
                Edge("flaky", "right"),
                Edge(("left", "right"), "join"),
                Edge("join", "__end__"),
            ),
        )
    )
    path = tmp_path / "retry-join.db"
    with Database.open(path) as database:
        uow, store = _store(database)
        config = _create_run(uow, "sqlite-retry-join")
        with pytest.raises(WorkflowNodeError, match="retry"):
            _invoke(_native(workflow, store), {}, config)
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        result = _invoke(_native(workflow, store), None, config)
        assert isinstance(result, Mapping) and result["answer"] == "joined"
        rows = reopened.connection.execute(
            "SELECT checkpoint_json FROM workflow_checkpoints WHERE run_id=? ORDER BY version DESC LIMIT 1",
            (config["run_id"],),
        ).fetchone()
        assert rows is not None and '"join_firings"' in str(rows[0])
    assert attempts == 2


def test_real_sqlite_bounded_cycle_and_parallel_frontier_are_deterministic(
    tmp_path: Path,
) -> None:
    async def loop(state, _context):  # type: ignore[no-untyped-def]
        counters = state.get("loop_counters", {})
        current = int(counters.get("loop", 0)) if isinstance(counters, Mapping) else 0
        return StatePatch({"loop_counters": {"loop": current + 1}})

    def select(state, _context):  # type: ignore[no-untyped-def]
        return "again" if state["loop_counters"]["loop"] < 2 else "done"

    cycle = compile_workflow(
        WorkflowDefinition(
            "sqlite-cycle",
            "1",
            1,
            "loop",
            (NodeDefinition("loop", loop),),
            {"loop_counters": _channel("loop", JsonType.OBJECT)},
            16,
            8,
            conditional_edges=(
                ConditionalEdge(
                    "loop",
                    select,
                    {"again": "loop", "done": "__end__"},
                    selector_effect_policy="pure",
                ),
            ),
            loop_budgets={"loop": 2},
            loop_budget_bindings={"loop->loop": "loop"},
        )
    )
    path = tmp_path / "cycle.db"
    with Database.open(path) as database:
        uow, store = _store(database)
        config = _create_run(uow, "sqlite-cycle")
        result = _invoke(
            _native(cycle, store), {"loop_counters": {"loop": 0}}, config
        )
        assert isinstance(result, Mapping)
        assert result["loop_counters"] == {"loop": 2}
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        assert _invoke(_native(cycle, store), None, config) == result

    order: list[str] = []

    async def root(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    async def left(_state, _context):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        order.append("left")
        return StatePatch({"left": "L"})

    async def right(_state, _context):  # type: ignore[no-untyped-def]
        order.append("right")
        return StatePatch({"right": "R"})

    parallel = compile_workflow(
        WorkflowDefinition(
            "sqlite-parallel",
            "1",
            1,
            "root",
            (
                NodeDefinition("root", root),
                NodeDefinition("left", left, dispatch="parallel"),
                NodeDefinition("right", right, dispatch="parallel"),
            ),
            {"left": _channel("left"), "right": _channel("right")},
            16,
            8,
            edges=(
                Edge("root", "left"),
                Edge("root", "right"),
                Edge("left", "__end__"),
                Edge("right", "__end__"),
            ),
        )
    )
    parallel_path = tmp_path / "parallel.db"
    with Database.open(parallel_path) as database:
        uow, store = _store(database)
        config = _create_run(uow, "sqlite-parallel")
        context = WorkflowContext(ports={"native_execution_policy": {"max_parallel_tasks": 2}})
        result = asyncio.run(
            _native(parallel, store).ainvoke(
                {},
                context,
                thread_id=config["thread_id"],
                run_id=config["run_id"],
                checkpoint_ns=config["checkpoint_ns"],
                configurable=config,
            )
        )
        assert result == {"left": "L", "right": "R"}
        assert sorted(order) == ["left", "right"]


def test_real_sqlite_terminal_delivery_frontier_after_commit_does_not_replay(
    tmp_path: Path,
) -> None:
    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch(
            {
                "values": {
                    "delivery_intents": [
                    {
                        "intent_id": "answer",
                        "kind": "assistant",
                        "channel": "chat",
                        "payload": {"answer": "once"},
                    }
                    ]
                }
            }
        )

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-delivery",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"values": _channel("node", JsonType.OBJECT)},
            16,
            8,
            edges=(Edge("node", "__end__"),),
        )
    )
    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if armed and point == "workflow_native:frontier.after_commit":
            armed = False
            raise ConnectionError(point)

    path = tmp_path / "delivery.db"
    with Database.open(path) as database:
        uow, store = _store(database, fault=fault)
        config = _create_run(uow, "sqlite-delivery")
        with pytest.raises(ConnectionError, match="frontier.after_commit"):
            _invoke(_native(workflow, store), {}, config)
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        result = _invoke(_native(workflow, store), None, config)
        assert isinstance(result, Mapping)
        events = reopened.connection.execute(
            "SELECT kind,COUNT(*) AS total FROM run_events WHERE run_id=? "
            "AND kind IN ('workflow.assistant','workflow.final') GROUP BY kind",
            (config["run_id"],),
        ).fetchall()
        assert {str(row["kind"]): int(row["total"]) for row in events} == {
            "workflow.assistant": 1,
            "workflow.final": 1,
        }


def test_real_sqlite_projection_prepare_reopens_without_projector_replay(
    tmp_path: Path,
) -> None:
    calls = 0

    async def node(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({"answer": "ok"})

    async def projector(_request, context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert context.logical_timestamp == 2.0
        return {
            "intents": [
                {
                    "intent_id": "answer",
                    "kind": "assistant",
                    "channel": "chat",
                    "payload": {"answer": "ok"},
                }
            ],
            "blob_refs": [],
        }

    descriptor = TerminalProjectionDescriptor.create(
        capability_id="terminal.commit",
        version="1",
        projector_fingerprint="a" * 64,
    )
    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-projection",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {"answer": _channel("node")},
            16,
            8,
            edges=(Edge("node", "__end__"),),
            terminal_projection_descriptor=descriptor,
        )
    )

    class CommitPort:
        def lookup(self, _name, _version, selected):  # type: ignore[no-untyped-def]
            return projector if selected == descriptor else None

    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if armed and point == "workflow_native:projection_prepare.after_commit":
            armed = False
            raise ConnectionError(point)

    path = tmp_path / "projection.db"
    with Database.open(path) as database:
        uow, store = _store(database, fault=fault)
        config = _create_run(uow, "sqlite-projection")
        executable = NativeWorkflowExecutable(
            workflow,
            store,
            terminal_projection_port=_TerminalProjectionPort(),
            terminal_commit_projection_port=CommitPort(),
        )
        with pytest.raises(ConnectionError, match="projection_prepare.after_commit"):
            _invoke(executable, {}, config)
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        executable = NativeWorkflowExecutable(
            workflow,
            store,
            terminal_projection_port=_TerminalProjectionPort(),
            terminal_commit_projection_port=CommitPort(),
        )
        result = _invoke(executable, None, config)
        assert isinstance(result, Mapping)
        receipt = reopened.connection.execute(
            "SELECT consumed_at FROM terminal_projection_prepares WHERE run_id=?",
            (config["run_id"],),
        ).fetchone()
        assert receipt is not None and receipt["consumed_at"] == 2.0
    assert calls == 1


@pytest.mark.parametrize("mode", ["permanent", "max_step", "engine_validation"])
def test_real_sqlite_failures_are_durable_and_reopen_without_node_replay(
    tmp_path: Path, mode: str
) -> None:
    calls = 0

    async def node(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if mode == "permanent":
            raise WorkflowNodeError(
                code=WorkflowErrorCode.PERMANENT, message_ref="permanent"
            )
        if mode == "engine_validation":
            return StatePatch({"answer": "valid"})
        return StatePatch({})

    if mode == "max_step":
        definition = WorkflowDefinition(
            "sqlite-max-step",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            {},
            5,
            4,
            edges=(Edge("node", "node"),),
            loop_budgets={"loop": 3},
            loop_budget_bindings={"node->node": "loop"},
        )
    else:
        definition = WorkflowDefinition(
            f"sqlite-{mode}",
            "1",
            1,
            "node",
            (NodeDefinition("node", node),),
            ({"answer": _channel("node", JsonType.INTEGER)} if mode == "engine_validation" else {}),
            16,
            8,
            edges=(Edge("node", "__end__"),),
        )
    workflow = compile_workflow(definition)
    path = tmp_path / f"{mode}.db"
    with Database.open(path) as database:
        uow, store = _store(database)
        config = _create_run(uow, f"sqlite-{mode}")
        with pytest.raises(WorkflowNodeError):
            _invoke(_native(workflow, store), {}, config)
        failure_count = database.connection.execute(
            "SELECT COUNT(*) FROM workflow_native_operations WHERE run_id=? "
            "AND operation_kind IN ('failure','engine_failure')",
            (config["run_id"],),
        ).fetchone()[0]
        assert failure_count == 1
    first_calls = calls
    with Database.open(path) as reopened:
        _uow, store = _store(reopened)
        with pytest.raises(WorkflowNodeError):
            _invoke(_native(workflow, store), None, config)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_native_operations WHERE run_id=? "
            "AND operation_kind IN ('failure','engine_failure')",
            (config["run_id"],),
        ).fetchone()[0] == 1
    assert calls == first_calls


def test_real_sqlite_interrupt_suspend_resolve_reopen_resume_exactly_once(
    tmp_path: Path,
) -> None:
    calls = 0

    async def approval(_state, context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert not context.ports
        response = workflow_interrupt({"question": "continue?"})
        assert isinstance(response, Mapping)
        return StatePatch({"approved": bool(response["approved"])})

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-interrupt",
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
            {"approved": _channel("approval", JsonType.BOOLEAN)},
            16,
            8,
            edges=(Edge("approval", "__end__"),),
        )
    )
    path = tmp_path / "interrupt.db"
    with Database.open(path) as database:
        uow, store = _store(database)
        config = _create_run(uow, "sqlite-interrupt")
        with pytest.raises(WorkflowSuspended):
            _invoke(_native(workflow, store), {}, config)
        row = database.connection.execute(
            "SELECT decision_id FROM decisions WHERE run_id=? AND state='open'",
            (config["run_id"],),
        ).fetchone()
        assert row is not None
        decision_id = str(row["decision_id"])
        decision = uow.read_decision(decision_id)
        assert decision is not None
        resolved = uow.commit_decision(
            decision_id=decision_id,
            run_id=str(config["run_id"]),
            kind=decision.kind,
            state=DecisionState.ALLOWED,
            request=thaw_json(decision.request),
            response={"approved": True},
            event_id=f"resolve-{decision_id}",
            now=3.0,
        )
        assert resolved.state is DecisionState.ALLOWED
    with Database.open(path) as reopened:
        reopened_uow, store = _store(reopened)
        _claim_resume(
            reopened_uow,
            store,
            config,
            decision_id=decision_id,
            response={"approved": True},
        )
        result = asyncio.run(
            _native(workflow, store).resume(
                {decision_id: {"approved": True}},
                WorkflowContext(ports={"clock": lambda: 999.0}),
                thread_id=config["thread_id"],
                run_id=config["run_id"],
                checkpoint_ns=config["checkpoint_ns"],
                configurable=config,
            )
        )
        assert isinstance(result, Mapping) and result["approved"] is True
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_decision_consumptions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()[0] == 1
    assert calls == 2


def test_real_effect_interrupt_effect_graph_reopens_without_physical_replay(
    tmp_path: Path,
) -> None:
    physical_calls: list[str] = []

    def handler(arguments, _context):  # type: ignore[no-untyped-def]
        phase = str(arguments["phase"])
        physical_calls.append(phase)
        return ToolResult.succeeded(CallId(f"call-{phase}"), {"phase": phase})

    class Allow:
        async def authorize(self, prepared):  # type: ignore[no-untyped-def]
            return AuthorizationResult(
                AuthorizationDecision.ALLOW,
                receipt_ref=f"auth:{prepared.effect_id.value}",
            )

    class Observe:
        async def observe(self, prepared):  # type: ignore[no-untyped-def]
            return ReconciliationObservation(
                ReconciliationState.STILL_UNKNOWN,
                f"unknown:{prepared.effect_id.value}",
            )

    path = tmp_path / "effect-interrupt-effect.db"
    database = Database.open(path)
    uow, store = _store(database)
    config = _create_run(uow, "sqlite-effect-interrupt-effect")
    _run, runtime_lease = uow.claim_runtime_activation(
        run_id=str(config["run_id"]),
        owner_id="workflow-owner",
        namespace="runtime.kernel",
        now=1.0,
        lease_ttl_seconds=100.0,
    )
    run_fence = asyncio.run(
        uow.acquire(RunId(str(config["run_id"])), runtime_lease, now=2.0)
    )
    current_run = uow.read_run(str(config["run_id"]))
    assert current_run is not None
    activation = asyncio.run(
        uow.run_atomic(
            lambda transaction: uow.bind_activation(
                transaction,
                str(config["run_id"]),
                current_run.version,
                runtime_lease,
                run_fence,
                now=2.0,
                ttl_seconds=100.0,
            ),
            fault_label="test:bind_effect_activation",
        )
    )
    registry = ToolRegistry(
        [
            FunctionTool(
                ToolSpec(
                    "phase_effect",
                    "Record one phase",
                    {
                        "type": "object",
                        "properties": {"phase": {"type": "string"}},
                        "required": ["phase"],
                        "additionalProperties": False,
                    },
                ),
                handler,
            )
        ]
    )
    executor = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=Allow(),
        reconciliation=Observe(),
        clock=lambda: 2.0,
    )

    async def execute_phase(phase: str):
        execution = await executor.execute(
            effect_id=EffectId(f"effect-{phase}"),
            call=ToolCall(
                CallId(f"call-{phase}"), "phase_effect", {"phase": phase}
            ),
            context=ToolContext(
                RunId(str(config["run_id"])),
                RequestId(f"request-{phase}"),
                CancellationToken(),
            ),
            execution_lease=activation.execution_lease,
            run_fence=activation.run_fence,
            workflow_lease=activation.workflow_lease,
        )
        assert execution.result.value is not None
        return StatePatch({phase: "done"})

    async def pre(_state, _context):  # type: ignore[no-untyped-def]
        return await execute_phase("pre")

    async def approval(_state, context):  # type: ignore[no-untyped-def]
        assert not context.ports
        response = workflow_interrupt({"question": "continue effects?"})
        assert isinstance(response, Mapping) and response["approved"] is True
        return StatePatch({"approved": True})

    async def post(_state, _context):  # type: ignore[no-untyped-def]
        return await execute_phase("post")

    workflow = compile_workflow(
        WorkflowDefinition(
            "sqlite-effect-interrupt-effect",
            "1",
            1,
            "pre",
            (
                NodeDefinition("pre", pre),
                NodeDefinition(
                    "approval",
                    approval,
                    interrupt_capable=True,
                    barrier=True,
                    exclusive_superstep=True,
                    pre_interrupt_effect_policy="pure",
                ),
                NodeDefinition("post", post),
            ),
            {
                "pre": _channel("pre"),
                "approved": _channel("approval", JsonType.BOOLEAN),
                "post": _channel("post"),
            },
            20,
            10,
            edges=(
                Edge("pre", "approval"),
                Edge("approval", "post"),
                Edge("post", "__end__"),
            ),
        )
    )
    with pytest.raises(WorkflowSuspended):
        _invoke(_native(workflow, store), {}, config)
    assert physical_calls == ["pre"]
    decision = database.connection.execute(
        "SELECT decision_id FROM decisions WHERE run_id=? AND state='open'",
        (config["run_id"],),
    ).fetchone()
    assert decision is not None
    decision_id = str(decision["decision_id"])
    record = uow.read_decision(decision_id)
    assert record is not None
    uow.commit_decision(
        decision_id=decision_id,
        run_id=str(config["run_id"]),
        kind=record.kind,
        state=DecisionState.ALLOWED,
        request=thaw_json(record.request),
        response={"approved": True},
        event_id=f"resolve-{decision_id}",
        now=3.0,
    )
    database.close()

    with Database.open(path) as reopened:
        reopened_uow, reopened_store = _store(reopened)
        activation = _claim_resume(
            reopened_uow,
            reopened_store,
            config,
            decision_id=decision_id,
            response={"approved": True},
        )
        reopened_executor = EffectExecutor(
            uow=reopened_uow,
            registry=registry,
            authorization=Allow(),
            reconciliation=Observe(),
            clock=lambda: 4.0,
        )
        executor = reopened_executor
        result = asyncio.run(
            _native(workflow, reopened_store).resume(
                {decision_id: {"approved": True}},
                WorkflowContext(),
                thread_id=config["thread_id"],
                run_id=config["run_id"],
                checkpoint_ns=config["checkpoint_ns"],
                configurable=config,
            )
        )
        assert isinstance(result, Mapping)
        assert result == {"pre": "done", "approved": True, "post": "done"}
        effects = reopened.connection.execute(
            "SELECT effect_id,state FROM execution_effects WHERE run_id=? ORDER BY effect_id",
            (config["run_id"],),
        ).fetchall()
        assert [(str(row["effect_id"]), str(row["state"])) for row in effects] == [
            ("effect-post", "succeeded"),
            ("effect-pre", "succeeded"),
        ]
    assert physical_calls == ["pre", "post"]
