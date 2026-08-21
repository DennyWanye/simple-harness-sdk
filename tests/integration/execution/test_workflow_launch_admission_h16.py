# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from simple_harness.contracts import (
    CallId,
    EffectId,
    ExecutionSessionId,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution import EffectState, effect_request_hash
from simple_harness.execution.budget import BudgetSnapshot
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildCommandState,
)
from simple_harness.execution.recovery import RecoveryKind, WaitBlockerSpec
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import DecisionState, RunState, UnitOfWorkConflict
from simple_harness.providers import CancelToken, ProviderResponse
from simple_harness.runtime.child_signal_runtime import ChildSignalRuntime
from simple_harness.runtime.context import SqliteContextPort
from simple_harness.runtime.drivers.react import ReActDriver
from simple_harness.runtime.kernel import (
    DriverInvocation,
    DriverResult,
    RuntimePorts,
    RuntimeProfile,
    _CanonicalWorkflowSpawnRuntimeCoordinator,
    build_runtime,
)
from simple_harness.runtime.orchestration import (
    RuntimeActivationClaim,
    RuntimeStartAdmission,
    RuntimeStartDispatchClaim,
    RuntimeStartDisposition,
    StartInputSchema,
    VerifiedWorkflowCatalogAuthority,
    WorkflowLaunchRequest,
    WorkflowProfileRegistration,
    WorkflowSpawnIssueAuthority,
    WorkflowSpawnOrigin,
    workflow_spawn_child_command_id,
    workflow_spawn_operation_id,
)
from simple_harness.runtime.profiles import (
    ProfileDescriptor,
    profile_descriptor_fingerprint,
)
from simple_harness.runtime.start_snapshot import (
    RunStart,
    StartSnapshot,
    bind_start_snapshot,
)
from simple_harness.runtime.workflow_spawn import (
    WorkflowSpawnChildControlKind,
    WorkflowSpawnFailed,
)
from simple_harness.tools.contracts import ToolOutcome
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import StatePatch
from simple_harness.workflow.control import workflow_interrupt
from simple_harness.workflow.definition import Edge, NodeDefinition, WorkflowDefinition
from simple_harness.workflow.errors import WorkflowDependencyUnavailable
from simple_harness.workflow.execution_ports import (
    CancelWorkflowRequest,
    CheckpointExecutionAdapter,
    PrecreatedStartAction,
    PrecreatedStartDispatch,
    ResumeAdmissionReceipt,
    ResumeAdmissionRequest,
    ResumePhase,
    StartClaimAction,
    StartMode,
    WorkflowExecutionPorts,
    WorkflowRecoveryReceiptKind,
    WorkflowRecoveryWork,
    start_admission_request_from_json,
    start_admission_request_to_json,
)
from simple_harness.workflow.runner import WorkflowRegistry, WorkflowRunner


async def _atomic(uow, operation, label="launch-test"):  # type: ignore[no-untyped-def]
    return await uow.run_atomic(operation, fault_label=label)


async def _catalog_node(state, context):  # type: ignore[no-untyped-def]
    del state, context
    return StatePatch({})


async def _catalog_replacement_node(state, context):  # type: ignore[no-untyped-def]
    del state, context
    return StatePatch({})


def _fresh_compiled():  # type: ignore[no-untyped-def]
    return compile_workflow(
        WorkflowDefinition(
            "durable_task",
            "1",
            1,
            "task",
            (NodeDefinition("task", _catalog_node),),
            {},
            5,
            4,
            edges=(Edge("task", "__end__"),),
        )
    )


_COMPILED = _fresh_compiled()

_COMPILED_REPLACEMENT = compile_workflow(
    WorkflowDefinition(
        "durable_task",
        "1",
        1,
        "task",
        (NodeDefinition("task", _catalog_replacement_node),),
        {},
        5,
        4,
        edges=(Edge("task", "__end__"),),
    )
)


def _runner(registry: WorkflowRegistry | None = None) -> WorkflowRunner:
    owner = object()

    class Authority:
        transaction_owner = owner

    checkpoint = Authority()
    ports = WorkflowExecutionPorts(
        Authority(),
        CheckpointExecutionAdapter(owner),
        Authority(),
        Authority(),
        Authority(),
    )
    return WorkflowRunner(
        registry=registry or WorkflowRegistry((_COMPILED,)),
        checkpoint=checkpoint,  # type: ignore[arg-type]
        recovery=Authority(),  # type: ignore[arg-type]
        trace=Authority(),  # type: ignore[arg-type]
        execution_ports=ports,
        terminal_projection_port=Authority(),  # type: ignore[arg-type]
        terminal_commit_projection_port=Authority(),  # type: ignore[arg-type]
    )


def _sqlite_runner(
    database: Database,
    uow: SqliteExecutionUnitOfWork,
    *,
    clock=lambda: 3.0,  # type: ignore[no-untyped-def]
    compiled=_COMPILED,  # type: ignore[no-untyped-def]
) -> WorkflowRunner:
    class NoBlobReferences:
        async def validate_references(
            self, transaction, *, blob_refs, **values  # type: ignore[no-untyped-def]
        ) -> None:
            del transaction, blob_refs, values

    class Trace:
        async def append(self, event, *, transaction):  # type: ignore[no-untyped-def]
            del event, transaction

    class LegacyProjection:
        def project_public(
            self, workflow_name, workflow_version, raw, engine_status  # type: ignore[no-untyped-def]
        ):
            del workflow_name, workflow_version, raw, engine_status

    class NoCommitProjection:
        def lookup(
            self, workflow_name, workflow_version, descriptor  # type: ignore[no-untyped-def]
        ):
            del workflow_name, workflow_version, descriptor

    execution_ports = WorkflowExecutionPorts(
        uow,
        CheckpointExecutionAdapter(database),
        uow,
        uow,
        uow,
    )
    checkpoint = SqliteNativeCheckpointStore(
        execution_ports,
        blob_references=NoBlobReferences(),
    )
    return WorkflowRunner(
        registry=WorkflowRegistry(() if compiled is None else (compiled,)),
        checkpoint=checkpoint,
        recovery=uow,
        trace=Trace(),
        execution_ports=execution_ports,
        terminal_projection_port=LegacyProjection(),
        terminal_commit_projection_port=NoCommitProjection(),
        owner="parent-worker",
        clock=clock,
    )


def _registration(*, generation: int = 1) -> WorkflowProfileRegistration:
    input_schema_ref = "schema://workflow.durable_task/v1"
    descriptor = ProfileDescriptor(
        key="workflow.durable_task",
        description="Complete a durable multi-step task.",
        use_when="The task needs durable progress and recovery.",
        avoid_when="A direct answer is sufficient.",
        input_schema_ref=input_schema_ref,
        generation=generation,
        fingerprint=profile_descriptor_fingerprint(
            "workflow.durable_task",
            "Complete a durable multi-step task.",
            "The task needs durable progress and recovery.",
            "A direct answer is sufficient.",
            input_schema_ref,
            generation,
        ),
    )
    schema_value = {
        "type": "object",
        "properties": {"objective": {"type": "string"}},
        "required": ["objective"],
        "additionalProperties": False,
    }
    schema = StartInputSchema(
        schema_ref=input_schema_ref,
        canonical_schema=schema_value,
        schema_hash=hashlib.sha256(canonical_json(schema_value).encode()).hexdigest(),
    )
    return WorkflowProfileRegistration(
        descriptor=descriptor,
        workflow_name="durable_task",
        workflow_version="1",
        start_input_schema=schema,
    )


def _catalog(
    *, generation: int = 1, version: int = 1
) -> VerifiedWorkflowCatalogAuthority:
    registration = _registration(generation=generation)
    return _runner().prepare_catalog_authority(generation, (registration,))


def _launch_request(**changes: object) -> WorkflowLaunchRequest:
    origin = WorkflowSpawnOrigin(
        parent_run_id="parent-run",
        parent_request_id="parent-request",
        turn_id="turn-1",
        internal_tool_call_id="raw-spawn-call-1",
    )
    request_key = workflow_spawn_operation_id(origin)
    values: dict[str, object] = {
        "request_key": request_key,
        "candidate_id": "candidate-1",
        "profile_key": "workflow.durable_task",
        "catalog_generation": 1,
        "session_id": "session-1",
        "request_id": "request-1",
        "turn_id": "turn-1",
        "requested_run_id": "run-1",
        "requested_trace_id": "trace-1",
        "requested_thread_id": "thread-1",
        "tool_catalog_generation": 7,
        "objective": "finish the durable task",
        "start_input": {"objective": "finish the durable task"},
        "spawn_origin": origin,
        "root_run_id": "parent-run",
        "attachment_policy": AttachmentPolicy.ATTACHED,
        "child_command_id": workflow_spawn_child_command_id(request_key),
    }
    values.update(changes)
    return WorkflowLaunchRequest(**values)  # type: ignore[arg-type]


async def _spawn_issue_authority(
    uow: SqliteExecutionUnitOfWork,
    *,
    raw_call_id: str = "raw-spawn-call-1",
    effect_id: str = "spawn-effect-1",
    call_id: str = "spawn-call-1",
    arguments: dict[str, JsonValue] | None = None,
) -> WorkflowSpawnIssueAuthority:
    arguments = arguments or {
        "profile_key": "workflow.durable_task",
        "objective": "finish the durable task",
        "start_input": {"objective": "finish the durable task"},
        "candidate_id": "candidate-1",
    }
    if uow.read_run("parent-run") is None:
        parent_start = RunStart(
            execution_session_id=ExecutionSessionId("session-1"),
            run_id=RunId("parent-run"),
            request_id=RequestId("parent-request"),
            turn_id="turn-1",
            input={
                "messages": [{"role": "user", "content": "parent"}],
                "capability_snapshot": {},
            },
            tool_catalog_generation=7,
        )
        parent_snapshot = bind_start_snapshot(
            parent_start,
            profile_key="agent.general",
            driver_kind="react",
        )
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="parent-run",
            request_id="parent-request",
            profile_key="agent.general",
            driver_kind="react",
            snapshot=parent_snapshot.to_json(),
            event_id="parent-run:created",
            now=0.1,
        )
    _, execution_lease = uow.claim_runtime_activation(
        run_id="parent-run",
        owner_id="parent-worker",
        namespace="runtime.kernel",
        now=0.2,
        lease_ttl_seconds=1000.0,
    )
    run_fence = await uow.acquire(RunId("parent-run"), execution_lease, now=0.2)
    context = SqliteContextPort(uow.database, clock=lambda: 0.3)
    context_snapshot = context.load(RunId("parent-run"))
    if context_snapshot.revision == 0:
        context_snapshot = context.append(
            RunId("parent-run"),
            execution_lease,
            0,
            "provider-request-1:assistant",
            (Message(MessageRole.ASSISTANT, ""),),
        )
    if uow.read_react_checkpoint("parent-run") is None:
        response_snapshot: dict[str, JsonValue] = {
            "request_id": "provider-request-1",
            "message": {
                "role": "assistant",
                "content": "",
                "name": None,
                "call_id": None,
                "metadata": {},
            },
            "tool_calls": [
                {
                    "call_id": raw_call_id,
                    "name": "workflow_spawn",
                    "arguments": arguments,
                }
            ],
            "usage": None,
            "model": None,
            "finish_reason": "tool_calls",
            "provider_request_id": None,
        }
        checkpoint = {
            "schema_version": 1,
            "started_at": 0.2,
            "last_observed_at": 0.2,
            "provider_turns_reserved_total": 1,
            "tool_calls_reserved_total": 1,
            "repeat_key": None,
            "repeat_streak": 1,
            "phase": "tool_batch_reserved",
            "provider_request_id": "provider-request-1",
            "tool_batch_id": "tool-batch:1",
            "context_revision": context_snapshot.revision,
            "provider_request_snapshot": {},
            "provider_request_fingerprint": "provider-fingerprint",
            "provider_response_snapshot": response_snapshot,
            "provider_response_digest": hashlib.sha256(
                canonical_json(response_snapshot).encode()
            ).hexdigest(),
            "tool_result_progress": 0,
        }
        checkpoint_hash = hashlib.sha256(canonical_json(checkpoint).encode()).hexdigest()
        uow.cas_react_checkpoint(
            run_id="parent-run",
            lease=execution_lease,
            expected_version=None,
            checkpoint=checkpoint,
            checkpoint_hash=checkpoint_hash,
            now=0.3,
        )
    request_hash = effect_request_hash(
        tool_name="workflow_spawn", arguments=arguments
    )
    effect = uow.read_effect(EffectId(effect_id))
    if effect is None:
        effect = uow.prepare_effect(
            effect_id=EffectId(effect_id),
            run_id=RunId("parent-run"),
            call_id=CallId(call_id),
            raw_call_id=raw_call_id,
            tool_name="workflow_spawn",
            arguments=arguments,
            request_hash=request_hash,
            authorization_receipt_ref=f"{effect_id}:authorization",
            run_fence=run_fence,
            execution_lease=execution_lease,
            now=0.4,
        )
    if effect.state is EffectState.PREPARED:
        effect = uow.mark_effect_handed_off(
            effect.effect_id,
            expected_version=effect.version,
            run_fence=run_fence,
            handoff_receipt_ref=f"{effect_id}:handoff",
            execution_lease=execution_lease,
            now=0.5,
        )
    assert effect.state is EffectState.HANDED_OFF
    return WorkflowSpawnIssueAuthority(
        react_checkpoint_revision=0,
        execution_lease=execution_lease,
        run_fence=run_fence,
        workflow_lease=None,
        effect_id=effect.effect_id.value,
        effect_handoff_attempt=effect.handoff_attempt,
        effect_request_hash=effect.request_hash,
    )


async def _publish_and_issue(
    uow: SqliteExecutionUnitOfWork,
    request: WorkflowLaunchRequest | None = None,
    catalog_authority: VerifiedWorkflowCatalogAuthority | None = None,
):
    authority = catalog_authority or _catalog()
    await _atomic(
        uow,
        lambda tx: uow.publish_catalog(tx, authority, 0, now=1.0),
        "publish-catalog",
    )
    launch = request or _launch_request()
    issue_authority = await _spawn_issue_authority(uow)
    ticket = await _atomic(
        uow,
        lambda tx: uow.issue(tx, launch, issue_authority, now=2.0),
        "issue-ticket",
    )
    verified = await _atomic(uow, lambda tx: uow.verify(tx, ticket), "verify-ticket")
    return launch, ticket, verified


async def _prepare_spawn_ready(
    uow: SqliteExecutionUnitOfWork,
    *,
    catalog_authority: VerifiedWorkflowCatalogAuthority | None = None,
):  # type: ignore[no-untyped-def]
    launch, ticket, _ = await _publish_and_issue(
        uow, catalog_authority=catalog_authority
    )
    issue_authority = await _spawn_issue_authority(uow)
    effect = uow.read_effect(EffectId(issue_authority.effect_id))
    assert effect is not None
    unknown = uow.mark_effect_unknown(
        effect.effect_id,
        expected_version=effect.version,
        expected_fence_epoch=issue_authority.run_fence.epoch,
        evidence_ref="spawn:unknown:1",
        now=2.1,
    )
    ready = await _atomic(
        uow,
        lambda tx: uow.mark_spawn_continuation_ready(
            tx,
            ticket,
            unknown,
            "spawn:ready:1",
            now=2.2,
        ),
    )
    parent = uow.read_run(launch.spawn_origin.parent_run_id)
    assert parent is not None
    _, blocker = uow.commit_runtime_wait_with_blocker(
        run_id=parent.run_id,
        expected_version=parent.version,
        event_id="parent-run:spawn-waiting",
        payload={"reason": "workflow_spawn_started_incomplete"},
        blocker=WaitBlockerSpec(
            RecoveryKind.TOOL,
            unknown.effect_id.value,
            unknown.handoff_attempt,
            unknown.version,
        ),
        lease=issue_authority.execution_lease,
        now=2.3,
    )
    return parent, ready, blocker


async def _prepare_spawn_ready_activation(
    uow: SqliteExecutionUnitOfWork,
    *,
    catalog_authority: VerifiedWorkflowCatalogAuthority | None = None,
):  # type: ignore[no-untyped-def]
    parent, ready, blocker = await _prepare_spawn_ready(
        uow, catalog_authority=catalog_authority
    )
    first = await _atomic(
        uow,
        lambda tx: uow.consume_spawn_ready_and_claim_activation(
            tx,
            ready,
            blocker,
            "parent-worker",
            now=2.4,
            ttl_seconds=10.0,
        ),
    )
    return parent, first


def _terminalize_spawn_parent(
    uow: SqliteExecutionUnitOfWork,
    terminal_state: RunState,
    *,
    now: float = 8.0,
):  # type: ignore[no-untyped-def]
    connection = uow.database.connection
    run = connection.execute(
        "SELECT state,version FROM runs WHERE run_id='parent-run'"
    ).fetchone()
    fence = connection.execute(
        "SELECT owner_id,runtime_lease_epoch,epoch FROM run_fences "
        "WHERE run_id='parent-run'"
    ).fetchone()
    assert run is not None and fence is not None
    namespace = "react.termination.v1"
    request_json = canonical_json({"checkpoint_namespace": namespace})
    connection.execute(
        "INSERT OR IGNORE INTO workflow_start_admissions("
        "request_key,request_id,request_fingerprint,request_json,mode,run_id,"
        "trace_id,thread_id,phase,version,created_at,updated_at) "
        "VALUES('parent-start','parent-request',?,?, 'standalone','parent-run',"
        "'parent-trace','parent-thread','admitted',0,0,0)",
        (hashlib.sha256(request_json.encode()).hexdigest(), request_json),
    )
    checkpoint_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version),-1)+1 FROM workflow_checkpoints "
            "WHERE run_id='parent-run' AND namespace=?",
            (namespace,),
        ).fetchone()[0]
    )
    checkpoint_id = f"parent-terminal-{terminal_state.value}"
    checkpoint_json = canonical_json(
        {"checkpoint_id": checkpoint_id, "status": terminal_state.value}
    )
    checkpoint_hash = hashlib.sha256(checkpoint_json.encode()).hexdigest()
    connection.execute(
        "INSERT INTO workflow_checkpoints("
        "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
        "lease_epoch,version,created_at) VALUES(?,'parent-run',?,?,?,?,?,?)",
        (
            checkpoint_id,
            namespace,
            checkpoint_json,
            checkpoint_hash,
            int(fence["runtime_lease_epoch"]),
            checkpoint_version,
            now,
        ),
    )
    event_id = f"parent-terminal-event-{terminal_state.value}"
    terminal_payload = {
        "status": "terminal",
        "terminal_status": terminal_state.value,
    }
    event_json = canonical_json(terminal_payload)
    durable_seq = int(
        connection.execute(
            "SELECT COALESCE(MAX(durable_seq),0)+1 FROM run_events "
            "WHERE run_id='parent-run'"
        ).fetchone()[0]
    )
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "VALUES(?,'parent-run',?,?,?,?)",
        (
            event_id,
            durable_seq,
            f"run.{terminal_state.value}",
            event_json,
            now,
        ),
    )
    fence_receipt_id = f"parent-terminal-fence-{terminal_state.value}"
    receipt_id = f"parent-terminal-receipt-{terminal_state.value}"
    next_run_version = int(run["version"]) + 1
    connection.execute(
        "INSERT INTO workflow_terminal_fence_receipts("
        "receipt_id,run_id,owner_id,runtime_lease_epoch,run_fence_epoch,created_at) "
        "VALUES(?,'parent-run',?,?,?,?)",
        (
            fence_receipt_id,
            str(fence["owner_id"]),
            int(fence["runtime_lease_epoch"]),
            int(fence["epoch"]),
            now,
        ),
    )
    terminal_fields = {
        "receipt_id": receipt_id,
        "run_id": "parent-run",
        "checkpoint_id": checkpoint_id,
        "state": terminal_state.value,
        "event_id": event_id,
        "delivery_ids": [],
        "terminal_payload": terminal_payload,
        "delivery_facts": [],
    }
    outcome_hash = hashlib.sha256(
        canonical_json(terminal_fields).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO workflow_terminal_receipts("
        "receipt_id,run_id,checkpoint_id,checkpoint_namespace,checkpoint_version,"
        "checkpoint_hash,state,run_version,event_id,event_payload_hash,"
        "delivery_ids_json,delivery_facts_json,terminal_payload_json,"
        "terminal_fence_receipt_id,outcome_hash,created_at) "
        "VALUES(?,'parent-run',?,?,?,?,?,?,?,?,'[]','[]',?,?,?,?)",
        (
            receipt_id,
            checkpoint_id,
            namespace,
            checkpoint_version,
            checkpoint_hash,
            terminal_state.value,
            next_run_version,
            event_id,
            hashlib.sha256(event_json.encode()).hexdigest(),
            event_json,
            fence_receipt_id,
            outcome_hash,
            now,
        ),
    )
    connection.execute("DELETE FROM workflow_leases WHERE run_id='parent-run'")
    connection.execute(
        "UPDATE run_fences SET state='released',released_at=? "
        "WHERE run_id='parent-run'",
        (now,),
    )
    connection.execute(
        "UPDATE runs SET state=?,version=?,updated_at=? WHERE run_id='parent-run'",
        (terminal_state.value, next_run_version, now),
    )
    connection.commit()
    outcome = uow.read_workflow_terminal_outcome("parent-run")
    assert outcome is not None and uow.verify_workflow_terminal(outcome)
    return outcome


def _start_inputs(ticket, verified, launch):  # type: ignore[no-untyped-def]
    start = RunStart(
        execution_session_id=ExecutionSessionId(verified.session_id),
        run_id=RunId(verified.resolved_run_id),
        request_id=RequestId(verified.request_id),
        turn_id=verified.turn_id,
        input=launch.start_input,
        tool_catalog_generation=verified.tool_catalog_generation,
    )
    request = _runner().prepare_start_admission(verified, start)
    snapshot = bind_start_snapshot(
        start,
        profile_key=verified.profile_key,
        driver_kind="workflow",
        workflow_admission=request,
    )
    return start, request, snapshot


def test_catalog_ticket_exact_replay_conflict_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "catalog-ticket.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        issue_authority = asyncio.run(_spawn_issue_authority(uow))
        repeated = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.issue(tx, launch, issue_authority, now=9.0),
            )
        )
        assert repeated == ticket
        assert verified.resolved_run_id == "run-1"
        with pytest.raises(UnitOfWorkConflict, match="different payload"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.issue(
                        tx,
                        _launch_request(objective="changed objective"),
                        issue_authority,
                        now=9.0,
                    ),
                )
            )
        with pytest.raises(UnitOfWorkConflict, match="version"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.publish_catalog(tx, _catalog(), 9, now=9.0),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.issue(tx, launch, issue_authority, now=20.0),
            )
        )
        assert replay == ticket
        assert asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, replay))) == verified


def test_attached_runtime_admission_materializes_child_link_and_command(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "attached-runtime-start.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("child-worker", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )

        child = database.connection.execute(
            "SELECT parent_run_id,root_run_id FROM runs WHERE run_id=?",
            (admitted.receipt.run_id,),
        ).fetchone()
        assert child is not None
        assert tuple(child) == ("parent-run", "parent-run")
        link = database.connection.execute(
            "SELECT attachment_policy FROM run_links WHERE parent_run_id=? AND child_run_id=?",
            ("parent-run", admitted.receipt.run_id),
        ).fetchone()
        assert link is not None and str(link["attachment_policy"]) == "attached"
        command = database.connection.execute(
            "SELECT parent_run_id,child_run_id,workflow_ticket_receipt_id,state "
            "FROM child_commands WHERE command_id=?",
            (launch.child_command_id,),
        ).fetchone()
        assert command is not None
        assert tuple(command) == (
            "parent-run",
            admitted.receipt.run_id,
            ticket.ticket_receipt_id,
            "pending",
        )


def test_direct_spawn_admission_atomically_settles_effect_context_and_parent_wait(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "direct-spawn-admission.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        authority = asyncio.run(_spawn_issue_authority(uow))
        continuation = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    authority,
                    None,
                    now=2.1,
                    ttl_seconds=10.0,
                ),
            )
        )
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.continue_spawn_admission(
                    tx,
                    ticket,
                    continuation,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("child-worker", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )

        assert outcome.child_control.kind is WorkflowSpawnChildControlKind.START
        result_payload = thaw_json(outcome.tool_result.value)
        assert isinstance(result_payload, dict)
        assert result_payload["child_run_id"] == "run-1"
        assert outcome.child_start_ref.child_run_id == "run-1"
        assert outcome.suspension.parent_run_id == "parent-run"
        assert uow.read_effect(EffectId(authority.effect_id)).state is EffectState.SUCCEEDED  # type: ignore[union-attr]
        parent = uow.read_run("parent-run")
        assert parent is not None and parent.state.value == "waiting"
        assert SqliteContextPort(database).load(RunId("parent-run")).revision == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_child_wait_receipts"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT state FROM run_fences WHERE run_id='parent-run'"
        ).fetchone()[0] == "released"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_leases WHERE run_id='parent-run'"
        ).fetchone()[0] == 0
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_admission_outcome(
                    tx, continuation.spawn_operation_id
                ),
            )
        )
        assert replay is not None
        assert replay.result.to_json() == result_payload
        assert replay.child_start_ref == outcome.child_start_ref
        assert replay.suspension == outcome.suspension

        child_activation = outcome.child_control.admission.activation
        assert child_activation is not None
        terminal = uow.finalize_child_and_enqueue_parent_signal(
            command_id=outcome.suspension.child_command_id,
            expected_child_version=1,
            terminal_state=RunState.COMPLETED,
            signal_id="spawn-child-signal-1",
            signal_payload={"status": "completed", "result": {"ok": True}},
            event_id="spawn-child-terminal-event-1",
            receipt_id="spawn-child-terminal-receipt-1",
            run_fence=child_activation.run_fence,
            execution_lease=child_activation.execution_lease,
            now=4.0,
        )
        assert terminal.signal is not None
        assert terminal.signal.parent_run_id == "parent-run"
        after_child_terminal = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_admission_outcome(
                    tx, continuation.spawn_operation_id
                ),
            )
        )
        assert after_child_terminal == replay


@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_admission:after_context_write", False),
        ("workflow:spawn_admission:after_checkpoint_write", False),
        ("workflow:spawn_admission:after_parent_write", False),
        ("workflow:spawn_admission:after_authority_release", False),
        ("workflow:spawn_admission:after_child_wait_write", False),
        ("workflow:spawn_admission:after_completion_write", False),
        ("workflow:spawn_admission:after_commit", True),
    ],
)
def test_direct_spawn_admission_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"direct-spawn-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(base))
        authority = asyncio.run(_spawn_issue_authority(base))
        continuation = asyncio.run(
            _atomic(
                base,
                lambda tx: base.claim_spawn_continuation(
                    tx,
                    ticket,
                    authority,
                    None,
                    now=2.1,
                    ttl_seconds=10.0,
                ),
            )
        )
        start, request, snapshot = _start_inputs(ticket, verified, launch)

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        failing = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if fault_point.endswith("after_commit")
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    failing,
                    lambda tx: failing.continue_spawn_admission(
                        tx,
                        ticket,
                        continuation,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim(
                            "child-worker", lease_ttl_seconds=10.0
                        ),
                        now=3.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runtime_start_receipts WHERE run_id='run-1'"
        ).fetchone()[0] == int(persisted)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_completion_receipts"
        ).fetchone()[0] == int(persisted)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_child_wait_receipts"
        ).fetchone()[0] == int(persisted)
        parent = uow.read_run("parent-run")
        assert parent is not None
        assert parent.state.value == ("waiting" if persisted else "running")
        effect = uow.read_effect(EffectId(authority.effect_id))
        assert effect is not None
        assert effect.state is (
            EffectState.SUCCEEDED if persisted else EffectState.HANDED_OFF
        )
        context = SqliteContextPort(reopened).load(RunId("parent-run"))
        assert context.revision == 1 + int(persisted)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_admission_outcome(
                    tx, continuation.spawn_operation_id
                ),
            )
        )
        assert (outcome is not None) is persisted


def test_ready_recovery_spawn_admission_consumes_activation_and_reopens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ready-recovery-spawn-admission.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(uow))
        issued = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_issued(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        assert issued is not None
        ticket, launch = issued
        verified = asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.continue_spawn_admission(
                    tx,
                    ticket,
                    activation.continuation_claim,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("child-worker", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        assert outcome.child_start_ref.child_run_id == "run-1"
        row = database.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations "
            "WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()
        assert row is not None and str(row["state"]) == "consumed"
        completion = database.connection.execute(
            "SELECT path_kind,activation_chain_head_id "
            "FROM workflow_spawn_completion_receipts"
        ).fetchone()
        assert completion is not None
        assert tuple(completion) == (
            "ready_recovery",
            activation.activation_receipt_id,
        )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_admission_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        assert replay is not None
        assert replay.child_start_ref.child_run_id == "run-1"


@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_admission:after_activation_write", False),
        ("workflow:spawn_admission:after_commit", True),
    ],
)
def test_ready_recovery_spawn_admission_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"ready-spawn-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(base))
        issued = asyncio.run(
            _atomic(
                base,
                lambda tx: base.read_issued(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        assert issued is not None
        ticket, launch = issued
        verified = asyncio.run(_atomic(base, lambda tx: base.verify(tx, ticket)))
        start, request, snapshot = _start_inputs(ticket, verified, launch)

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        failing = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if fault_point.endswith("after_commit")
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    failing,
                    lambda tx: failing.continue_spawn_admission(
                        tx,
                        ticket,
                        activation.continuation_claim,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim(
                            "child-worker", lease_ttl_seconds=10.0
                        ),
                        now=3.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        row = reopened.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations "
            "WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()
        assert row is not None
        assert str(row["state"]) == ("consumed" if persisted else "active")
        uow = SqliteExecutionUnitOfWork(reopened)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_admission_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        assert (outcome is not None) is persisted


def test_spawn_issue_claim_ready_and_read_are_one_durable_chain(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "spawn-chain.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _ = asyncio.run(_publish_and_issue(uow))
        issue_authority = asyncio.run(_spawn_issue_authority(uow))
        issued = asyncio.run(
            _atomic(uow, lambda tx: uow.read_issued(tx, launch.request_key))
        )
        assert issued == (ticket, launch)
        continuation = database.connection.execute(
            "SELECT * FROM workflow_spawn_continuations WHERE operation_id=?",
            (launch.request_key,),
        ).fetchone()
        assert continuation is not None
        assert tuple(
            continuation[key]
            for key in ("state", "effect_id", "handoff_attempt")
        ) == ("pending", issue_authority.effect_id, 1)

        effect = uow.read_effect(EffectId(issue_authority.effect_id))
        assert effect is not None
        ready = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.mark_spawn_continuation_ready(
                    tx,
                    ticket,
                    effect,
                    "reconcile:spawn-effect-1:attempt-1",
                    now=2.1,
                ),
            )
        )
        assert uow.list_ready_spawn_continuations(None, limit=10) == (
            (ready,),
            None,
        )
        with pytest.raises(UnitOfWorkConflict, match="initial.*rejects"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_spawn_continuation(
                        tx,
                        ticket,
                        issue_authority,
                        ready,
                        now=2.2,
                        ttl_seconds=10.0,
                    ),
                )
            )
        claim = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    issue_authority,
                    None,
                    now=2.2,
                    ttl_seconds=10.0,
                ),
            )
        )
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    issue_authority,
                    None,
                    now=3.0,
                    ttl_seconds=99.0,
                ),
            )
        )
        assert replay == claim
        assert claim.claim_epoch == 1


def test_spawn_ready_blocker_binding_is_readable_until_atomic_consume(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "spawn-ready-blocker.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, ready, blocker = asyncio.run(_prepare_spawn_ready(uow))
        assert uow.read_spawn_ready_blocker(ready) == blocker
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.consume_spawn_ready_and_claim_activation(
                    tx,
                    ready,
                    blocker,
                    "parent-worker",
                    now=2.4,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert uow.read_spawn_ready_blocker(ready) is None


def test_runtime_delivers_consumed_spawn_ready_activation_to_driver(
    tmp_path: Path,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    class CaptureDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.invocation = None

        async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
            del context, cancel
            self.invocation = invocation
            self.started.set()
            await asyncio.Event().wait()
            return DriverResult(RunState.WAITING)

    async def case() -> None:
        with Database.open(tmp_path / "spawn-ready-runtime.db") as database:
            uow = SqliteExecutionUnitOfWork(database)
            parent, ready, _blocker = await _prepare_spawn_ready(uow)
            noop = NoopPort()
            driver = CaptureDriver()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": driver},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 2.4),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="parent-worker",
                    clock=lambda: 2.4,
                    lease_ttl_seconds=10.0,
                ),
            )
            await runtime.start()
            await asyncio.wait_for(driver.started.wait(), timeout=1.0)
            invocation = driver.invocation
            assert invocation is not None
            activation = invocation.workflow_spawn_ready_activation
            assert activation is not None
            assert activation.ready_receipt == ready
            assert activation.execution_lease == invocation.execution_lease
            assert activation.run_fence == invocation.run_fence
            assert activation.execution_lease.run_id == parent.run_id
            await runtime.close()

    asyncio.run(case())


def test_runtime_reopens_consumed_spawn_ready_activation_after_owner_expiry(
    tmp_path: Path,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    class CaptureDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.invocation = None

        async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
            del context, cancel
            self.invocation = invocation
            self.started.set()
            await asyncio.Event().wait()
            return DriverResult(RunState.WAITING)

    async def case() -> None:
        path = tmp_path / "spawn-ready-runtime-reopen.db"
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            parent, first = await _prepare_spawn_ready_activation(uow)

        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            noop = NoopPort()
            driver = CaptureDriver()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": driver},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 2000.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="recovery-worker",
                    clock=lambda: 2000.0,
                    lease_ttl_seconds=10.0,
                ),
            )
            await runtime.start()
            await asyncio.wait_for(driver.started.wait(), timeout=1.0)
            invocation = driver.invocation
            assert invocation is not None
            successor = invocation.workflow_spawn_ready_activation
            assert successor is not None
            assert successor.execution_lease.run_id == parent.run_id
            assert successor.execution_lease.owner_id == "recovery-worker"
            assert successor.execution_lease.epoch == first.execution_lease.epoch + 1
            assert (
                successor.predecessor_activation_receipt_id
                == first.activation_receipt_id
            )
            await runtime.close()

    asyncio.run(case())


def test_react_ready_carrier_continues_spawn_without_provider_or_tool_replay(
    tmp_path: Path,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        with Database.open(tmp_path / "spawn-ready-react.db") as database:
            uow = SqliteExecutionUnitOfWork(database)
            parent, activation = await _prepare_spawn_ready_activation(uow)
            runner = _sqlite_runner(database, uow)
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver()},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="parent-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            raw_snapshot = uow.read_start_snapshot(parent.run_id)
            assert raw_snapshot is not None
            result = await ReActDriver().start(
                DriverInvocation(
                    run=uow.read_run(parent.run_id),  # type: ignore[arg-type]
                    start=StartSnapshot.from_json(raw_snapshot),
                    execution_lease=activation.execution_lease,
                    run_fence=activation.run_fence,
                    services=runtime._services,
                    workflow_spawn_ready_activation=activation,
                ),
                context=runtime._services.context,
                cancel=CancelToken(),
            )
            assert result.state is RunState.WAITING
            control = result.workflow_spawn_control
            assert control is not None
            assert control.child_start_ref.child_run_id == "run-1"
            assert control.child_control.kind is WorkflowSpawnChildControlKind.START
            assert uow.read_run(parent.run_id).state is RunState.WAITING  # type: ignore[union-attr]
            assert uow.read_effect(EffectId(activation.ready_receipt.effect_id)).state is EffectState.SUCCEEDED  # type: ignore[union-attr]
            scheduled: list[str] = []
            runtime._schedule = scheduled.append  # type: ignore[method-assign]
            await runtime._accept_workflow_spawn_control(parent.run_id, control)
            assert scheduled == ["run-1"]
            child_activation = control.child_control.admission.activation
            child_dispatch = control.child_control.admission.dispatch_claim
            assert child_activation is not None and child_dispatch is not None
            assert runtime._leases["run-1"] == child_activation.execution_lease
            assert runtime._fences["run-1"] == child_activation.run_fence
            assert runtime._workflow_start_dispatches["run-1"] == child_dispatch
            runtime._drop_local_authority("run-1")
            await asyncio.sleep(0)

    asyncio.run(case())


@pytest.mark.parametrize("lose_terminal_response", [False, True])
@pytest.mark.parametrize("bind_before_close", [False, True])
@pytest.mark.parametrize("recover_via_receipt", [False, True])
def test_runtime_reopens_committed_spawn_child_before_first_schedule(
    tmp_path: Path,
    lose_terminal_response: bool,
    bind_before_close: bool,
    recover_via_receipt: bool,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    def make_runtime(
        database: Database,
        uow: SqliteExecutionUnitOfWork,
        *,
        owner_id: str,
        now: float,
    ):
        noop = NoopPort()
        return build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "react")},
            {"react": ReActDriver()},
            RuntimePorts(
                provider=noop,
                tools=noop,
                authorization=noop,
                context=SqliteContextPort(database, clock=lambda: now),
                delivery=noop,
                tool_reconciliation=noop,
                reconciliation=noop,
                provider_reconciliation=noop,
                react_checkpoint=uow,
                tool_catalog=Catalog(),
                owner_id=owner_id,
                clock=lambda: now,
                lease_ttl_seconds=10.0,
            ),
                workflow_runner=_sqlite_runner(database, uow, clock=lambda: now),
        )

    async def case() -> None:
        path = tmp_path / (
            f"spawn-child-before-schedule-{lose_terminal_response}-"
            f"{bind_before_close}-{recover_via_receipt}.db"
        )
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            _parent, activation = await _prepare_spawn_ready_activation(uow)
            runtime = make_runtime(
                database, uow, owner_id="parent-worker", now=3.0
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            committed = await coordinator.continue_ready(activation)
            assert committed.child_start_ref.child_run_id == "run-1"
            replayed = await coordinator.continue_ready(activation)
            assert replayed.tool_result == committed.tool_result
            assert replayed.child_start_ref == committed.child_start_ref
            assert replayed.suspension == committed.suspension
            assert replayed.child_control.kind is WorkflowSpawnChildControlKind.START
            assert database.connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id='run-1'"
            ).fetchone()[0] == 1
            if bind_before_close:
                raw_snapshot = uow.read_start_snapshot("run-1")
                child_admission = committed.child_control.admission
                assert raw_snapshot is not None
                assert child_admission.activation is not None
                assert child_admission.dispatch_claim is not None
                request = StartSnapshot.from_json(raw_snapshot).workflow_admission
                assert request is not None
                runner = runtime._drivers["workflow"]._runner  # type: ignore[attr-defined]
                bound = await runner.start_precreated(
                    request=request,
                    execution_lease=child_admission.activation.execution_lease,
                    run_fence=child_admission.activation.run_fence,
                    dispatch_claim=child_admission.dispatch_claim,
                )
                assert bound.activation is not None
                attached = await coordinator.continue_ready(activation)
                assert (
                    attached.child_control.kind
                    is WorkflowSpawnChildControlKind.ATTACH
                )
                await runtime._accept_workflow_spawn_control(
                    "parent-run", attached
                )

        with Database.open(path) as database:
            lost = False

            def lose_after_commit(point: str) -> None:
                nonlocal lost
                if (
                    lose_terminal_response
                    and not lost
                    and point == "workflow_native:frontier.after_commit"
                ):
                    lost = True
                    raise RuntimeError("simulated terminal response loss")

            uow = SqliteExecutionUnitOfWork(
                database, workflow_fault=lose_after_commit
            )
            runtime = make_runtime(
                database, uow, owner_id="recovery-worker", now=2000.0
            )
            replayed = None
            if recover_via_receipt:
                coordinator = runtime._services.workflow_spawn
                assert coordinator is not None
                replayed = await coordinator.continue_ready(activation)
                assert replayed.child_start_ref == committed.child_start_ref
                assert replayed.child_control.kind is (
                    WorkflowSpawnChildControlKind.RECOVER
                    if bind_before_close
                    else WorkflowSpawnChildControlKind.START
                )
            await runtime.start()
            if replayed is not None:
                await runtime._accept_workflow_spawn_control(
                    "parent-run", replayed
                )
            await runtime.wait_idle(RunId("run-1"))
            child = uow.read_run("run-1")
            assert child is not None and child.state is RunState.COMPLETED
            terminal = uow.read_child_terminal_result_for_run("run-1")
            assert terminal is not None
            assert terminal.terminal_state == RunState.COMPLETED.value
            signal = database.connection.execute(
                "SELECT parent_run_id,child_run_id,state FROM child_signals "
                "WHERE child_run_id='run-1'"
            ).fetchone()
            assert signal is not None
            assert tuple(signal) == ("parent-run", "run-1", "pending")
            assert database.connection.execute(
                "SELECT COUNT(*) FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()[0] == 1
            assert lost is lose_terminal_response
            await runtime.close()

    asyncio.run(case())


def test_runtime_recovers_claimed_spawn_child_resume_after_crash(
    tmp_path: Path,
) -> None:
    calls = 0

    async def approval(_state, _context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        response = workflow_interrupt({"question": "continue?"})
        assert isinstance(response, dict)
        assert response["approved"] is True
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "durable_task",
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
            {},
            5,
            4,
            edges=(Edge("approval", "__end__"),),
        )
    )

    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    def make_runtime(
        database: Database,
        uow: SqliteExecutionUnitOfWork,
        *,
        owner_id: str,
        now: float,
    ):
        noop = NoopPort()
        return build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "react")},
            {"react": ReActDriver()},
            RuntimePorts(
                provider=noop,
                tools=noop,
                authorization=noop,
                context=SqliteContextPort(database, clock=lambda: now),
                delivery=noop,
                tool_reconciliation=noop,
                reconciliation=noop,
                provider_reconciliation=noop,
                react_checkpoint=uow,
                tool_catalog=Catalog(),
                owner_id=owner_id,
                clock=lambda: now,
                lease_ttl_seconds=10.0,
            ),
            workflow_runner=_sqlite_runner(
                database, uow, clock=lambda: now, compiled=compiled
            ),
        )

    async def case() -> None:
        path = tmp_path / "spawn-child-recover-resume.db"
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            runner = _sqlite_runner(database, uow, compiled=compiled)
            catalog_authority = runner.prepare_catalog_authority(
                1, (_registration(),)
            )
            _parent, ready = await _prepare_spawn_ready_activation(
                uow, catalog_authority=catalog_authority
            )
            runtime = make_runtime(
                database, uow, owner_id="parent-worker", now=3.0
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            await runtime._accept_workflow_spawn_control("parent-run", outcome)
            await runtime.wait_idle(RunId("run-1"))
            child = uow.read_run("run-1")
            assert child is not None and child.state is RunState.WAITING
            assert calls == 1
            waiting = await coordinator.continue_ready(ready)
            assert waiting.child_control.kind is WorkflowSpawnChildControlKind.WAITING
            await runtime._accept_workflow_spawn_control("parent-run", waiting)

            decision = database.connection.execute(
                "SELECT decision_id FROM decisions "
                "WHERE run_id='run-1' AND state='open'"
            ).fetchone()
            assert decision is not None
            decision_id = str(decision[0])
            durable = uow.read_decision(decision_id)
            assert durable is not None
            uow.commit_decision(
                decision_id=decision_id,
                run_id="run-1",
                kind=durable.kind,
                state=DecisionState.ALLOWED,
                request=thaw_json(durable.request),
                response={"approved": True},
                event_id=f"resolved-{decision_id}",
                now=4.0,
            )
            queued = uow.read_run("run-1")
            assert queued is not None and queued.state is RunState.QUEUED
            raw_snapshot = uow.read_start_snapshot("run-1")
            assert raw_snapshot is not None
            request = StartSnapshot.from_json(raw_snapshot).workflow_admission
            assert request is not None
            native = await runner.native_store.load_execution(
                run_id="run-1",
                thread_id=request.resolved_thread_id
                or request.requested_thread_id
                or "run-1",
                checkpoint_ns=request.checkpoint_namespace,
            )
            interrupt = native.snapshot.interrupt
            assert isinstance(interrupt, dict)
            request_hash = hashlib.sha256(
                canonical_json(interrupt).encode()
            ).hexdigest()
            responses = {decision_id: {"approved": True}}
            responses_hash = hashlib.sha256(
                canonical_json(responses).encode()
            ).hexdigest()
            resume_request = ResumeAdmissionRequest(
                receipt_id="resume-run-1",
                run_id="run-1",
                expected_run_version=queued.version,
                expected_checkpoint_head=native.snapshot.checkpoint_id,
                pending_interrupts=((decision_id, request_hash),),
                responses=responses,
                responses_hash=responses_hash,
                mode=StartMode.PRECREATED,
            )
            admitted = await _atomic(
                uow,
                lambda tx: uow.admit_resume(tx, resume_request, now=5.1),
            )
            _activated, execution_lease = uow.claim_runtime_activation(
                run_id="run-1",
                owner_id="resume-worker",
                namespace="runtime.kernel",
                now=5.2,
                lease_ttl_seconds=10.0,
            )
            run_fence = await uow.acquire(
                RunId("run-1"), execution_lease, now=5.2
            )
            claimed = await _atomic(
                uow,
                lambda tx: uow.claim_resume_precreated(
                    tx,
                    resume_request.receipt_id,
                    admitted.version,
                    execution_lease,
                    run_fence,
                    now=5.3,
                    ttl_seconds=10.0,
                ),
            )
            assert claimed.phase is ResumePhase.CLAIMED
            await runtime.close()

        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            runtime = make_runtime(
                database, uow, owner_id="recovery-worker", now=30.0
            )
            await runtime.start()
            await runtime.wait_idle(RunId("run-1"))
            child = uow.read_run("run-1")
            assert child is not None and child.state is RunState.COMPLETED
            receipt = database.connection.execute(
                "SELECT phase,committed_checkpoint FROM "
                "workflow_resume_admissions WHERE receipt_id='resume-run-1'"
            ).fetchone()
            assert receipt is not None and receipt[0] == "settled"
            assert receipt[1] is not None
            assert calls == 2
            assert database.connection.execute(
                "SELECT COUNT(*) FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()[0] == 1
            workflow_terminal = uow.read_workflow_terminal_outcome("run-1")
            assert workflow_terminal is not None
            assert uow.verify_workflow_terminal(workflow_terminal)
            assert database.connection.execute(
                "SELECT COUNT(*) FROM workflow_terminal_receipts "
                "WHERE run_id='run-1'"
            ).fetchone()[0] == 1
            assert database.connection.execute(
                "SELECT COUNT(*) FROM workflow_terminal_fence_receipts "
                "WHERE run_id='run-1'"
            ).fetchone()[0] == 1
            signal_row = database.connection.execute(
                "SELECT payload_json FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()
            assert signal_row is not None
            public_signal = json.loads(str(signal_row[0]))
            assert public_signal == {
                "status": "completed",
                "result": {
                    "kind": "workflow_terminal",
                    "status": "completed",
                    "error": None,
                    "recovery_action": None,
                    "card": None,
                },
            }
            assert "values" not in canonical_json(public_signal)
            claimed_signal = uow.claim_next_child_signal(
                parent_run_id="parent-run",
                owner_id="fault-worker",
                now=30.4,
                lease_seconds=0.05,
            )
            assert claimed_signal is not None
            signal_payload = thaw_json(claimed_signal.payload)
            assert isinstance(signal_payload, dict)
            continuation_id = (
                f"child-signal:{claimed_signal.signal_id}:continuation"
            )

            def fail_spawn_wait(point: str) -> None:
                if point == "child_signal_ack.spawn_wait.after_write":
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="spawn_wait.after_write"):
                uow.ack_child_signal_and_commit_parent_progress(
                    signal_id=claimed_signal.signal_id,
                    owner_id="fault-worker",
                    claim_epoch=claimed_signal.claim_epoch,
                    receipt_id=f"child-signal:{claimed_signal.signal_id}:receipt",
                    continuation_id=continuation_id,
                    continuation_payload={
                        "kind": "child_terminal",
                        "signal_id": claimed_signal.signal_id,
                        "child_run_id": claimed_signal.child_run_id,
                        "payload": signal_payload,
                    },
                    event_id=f"child-signal:{claimed_signal.signal_id}:acked",
                    event_payload={
                        "signal_id": claimed_signal.signal_id,
                        "continuation_id": continuation_id,
                        "receipt_id": (
                            f"child-signal:{claimed_signal.signal_id}:receipt"
                        ),
                    },
                    now=30.4,
                    fault=fail_spawn_wait,
                )
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "unconsumed"
            assert uow.read_run("parent-run").state is RunState.WAITING  # type: ignore[union-attr]
            assert uow.read_continuation(continuation_id) is None
            signal_result = ChildSignalRuntime(
                uow, owner_id="recovery-worker"
            ).receive_one(parent_run_id="parent-run", now=31.0)
            assert signal_result is not None
            wait = database.connection.execute(
                "SELECT state,child_signal_id,continuation_id FROM "
                "workflow_spawn_child_wait_receipts WHERE child_run_id='run-1'"
            ).fetchone()
            assert wait is not None
            assert tuple(wait) == (
                "woken",
                signal_result.signal.signal_id,
                signal_result.receipt.continuation_id,
            )
            replayed_outcome = await runtime._services.workflow_spawn.continue_ready(ready)  # type: ignore[union-attr]
            assert replayed_outcome.child_start_ref == outcome.child_start_ref
            _parent_running, parent_lease = uow.claim_runtime_activation(
                run_id="parent-run",
                owner_id="parent-resume-worker",
                namespace="runtime.kernel",
                now=31.1,
                lease_ttl_seconds=10.0,
            )
            parent_fence = await uow.acquire(
                RunId("parent-run"), parent_lease, now=31.1
            )

            def fail_claimed_wait(point: str) -> None:
                if point == "continuation_claim.spawn_wait.after_write":
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="spawn_wait.after_write"):
                uow.claim_continuation(
                    run_id="parent-run",
                    execution_lease=parent_lease,
                    now=31.2,
                    fault=fail_claimed_wait,
                )
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "woken"
            assert uow.read_continuation(
                signal_result.receipt.continuation_id
            ).state.value == "pending"  # type: ignore[union-attr]
            continuation = uow.claim_continuation(
                run_id="parent-run",
                execution_lease=parent_lease,
                now=31.3,
            )
            assert continuation is not None
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "claimed"

            def fail_child_continue(point: str) -> None:
                if point == "workflow:spawn_child_continue:after_wait_write":
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="after_wait_write"):
                uow.ack_spawn_child_continuation_and_continue_batch(
                    run_id="parent-run",
                    continuation_claim=continuation,
                    execution_lease=parent_lease,
                    run_fence=parent_fence,
                    now=31.4,
                    fault=fail_child_continue,
                )
            assert uow.read_continuation(
                continuation.continuation_id
            ).state.value == "claimed"  # type: ignore[union-attr]
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "claimed"
            resumed_checkpoint = (
                uow.ack_spawn_child_continuation_and_continue_batch(
                    run_id="parent-run",
                    continuation_claim=continuation,
                    execution_lease=parent_lease,
                    run_fence=parent_fence,
                    now=31.4,
                )
            )
            resumed_payload = thaw_json(resumed_checkpoint.checkpoint)
            assert isinstance(resumed_payload, dict)
            assert resumed_payload["phase"] == "tool_batch_reserved"
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "acked_completion_pending"

            def fail_child_completion(point: str) -> None:
                if point == "workflow:spawn_child_complete:after_context_write":
                    raise RuntimeError(point)

            context_before_completion = runtime._services.context.load(  # type: ignore[attr-defined]
                RunId("parent-run")
            )
            with pytest.raises(RuntimeError, match="after_context_write"):
                uow.commit_pending_spawn_child_completion_and_react_ready(
                    run_id="parent-run",
                    expected_checkpoint_version=resumed_checkpoint.version,
                    execution_lease=parent_lease,
                    run_fence=parent_fence,
                    now=31.5,
                    fault=fail_child_completion,
                )
            assert runtime._services.context.load(  # type: ignore[attr-defined]
                RunId("parent-run")
            ) == context_before_completion
            ready_checkpoint = (
                uow.commit_pending_spawn_child_completion_and_react_ready(
                    run_id="parent-run",
                    expected_checkpoint_version=resumed_checkpoint.version,
                    execution_lease=parent_lease,
                    run_fence=parent_fence,
                    now=31.5,
                )
            )
            ready_payload = thaw_json(ready_checkpoint.checkpoint)
            assert isinstance(ready_payload, dict)
            assert ready_payload["phase"] == "ready"
            context_after_completion = runtime._services.context.load(  # type: ignore[attr-defined]
                RunId("parent-run")
            )
            assert context_after_completion.revision == (
                context_before_completion.revision + 1
            )
            completion_message = context_after_completion.messages[-1]
            assert completion_message.role is MessageRole.USER
            assert completion_message.name == "workflow_child_completion"
            completion_payload = json.loads(completion_message.content)
            assert completion_payload["child_run_id"] == "run-1"
            assert completion_payload["terminal"] == public_signal
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()[0] == "acked"
            replayed_checkpoint = (
                uow.commit_pending_spawn_child_completion_and_react_ready(
                    run_id="parent-run",
                    expected_checkpoint_version=resumed_checkpoint.version,
                    execution_lease=parent_lease,
                    run_fence=parent_fence,
                    now=31.6,
                )
            )
            assert replayed_checkpoint == ready_checkpoint
            assert runtime._services.context.load(  # type: ignore[attr-defined]
                RunId("parent-run")
            ) == context_after_completion
            claimed_replay = await runtime._services.workflow_spawn.continue_ready(ready)  # type: ignore[union-attr]
            assert claimed_replay.child_start_ref == outcome.child_start_ref
            terminal = await runtime._services.workflow_spawn.continue_ready(ready)  # type: ignore[union-attr]
            assert (
                terminal.child_control.kind
                is WorkflowSpawnChildControlKind.TERMINAL
            )
            await runtime._accept_workflow_spawn_control(
                "parent-run", terminal
            )
            await runtime.close()

    asyncio.run(case())


def test_runtime_drains_child_signal_and_resumes_react_parent_once(
    tmp_path: Path,
) -> None:
    async def finish(_state, _context):  # type: ignore[no-untyped-def]
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "durable_task",
            "1",
            1,
            "finish",
            (NodeDefinition("finish", finish),),
            {},
            5,
            4,
            edges=(Edge("finish", "__end__"),),
        )
    )

    class FinalProvider:
        def __init__(self) -> None:
            self.calls = 0

        def read_provider_budget(self, _run_id):  # type: ignore[no-untyped-def]
            return BudgetSnapshot()

        async def invoke(
            self, _run_id, request, *, cancel, execution_lease
        ):  # type: ignore[no-untyped-def]
            del cancel, execution_lease
            self.calls += 1
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "parent resumed"),
            )

    class NoopPort:
        async def reconcile(self) -> None:
            return None

        def provider_tool_specs(self, _names=()):  # type: ignore[no-untyped-def]
            return ()

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        path = tmp_path / "spawn-child-kernel-drain.db"
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            runner = _sqlite_runner(database, uow, compiled=compiled)
            catalog_authority = runner.prepare_catalog_authority(
                1, (_registration(),)
            )
            _parent, ready = await _prepare_spawn_ready_activation(
                uow, catalog_authority=catalog_authority
            )
            provider = FinalProvider()
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver(clock=lambda: 3.0)},
                RuntimePorts(
                    provider=provider,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="kernel-parent-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            await runtime._accept_workflow_spawn_control("parent-run", outcome)
            await runtime.wait_idle(RunId("run-1"))
            child = uow.read_run("run-1")
            assert child is not None and child.state is RunState.COMPLETED
            assert database.connection.execute(
                "SELECT COUNT(*) FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()[0] == 1

            await runtime.start()
            await runtime.wait_idle(RunId("parent-run"))
            parent = uow.read_run("parent-run")
            assert parent is not None and parent.state is RunState.COMPLETED
            assert provider.calls == 1
            wait = database.connection.execute(
                "SELECT state,progress_receipt_id,"
                "child_completion_append_receipt_id "
                "FROM workflow_spawn_child_wait_receipts "
                "WHERE child_run_id='run-1'"
            ).fetchone()
            assert wait is not None
            assert wait["state"] == "acked"
            assert wait["progress_receipt_id"] is not None
            assert wait["child_completion_append_receipt_id"] is not None
            context = runtime._services.context.load(RunId("parent-run"))
            completion_messages = tuple(
                message
                for message in context.messages
                if message.name == "workflow_child_completion"
            )
            assert len(completion_messages) == 1
            assert json.loads(completion_messages[0].content)["child_run_id"] == (
                "run-1"
            )
            await runtime.close()

    asyncio.run(case())


def test_parent_terminal_atomically_closes_pending_child_completion(
    tmp_path: Path,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

        def provider_tool_specs(self, _names=()):  # type: ignore[no-untyped-def]
            return ()

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        path = tmp_path / "spawn-child-parent-terminal.db"
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            runner = _sqlite_runner(database, uow)
            catalog_authority = runner.prepare_catalog_authority(
                1, (_registration(),)
            )
            _parent, ready = await _prepare_spawn_ready_activation(
                uow, catalog_authority=catalog_authority
            )
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver(clock=lambda: 3.0)},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="parent-terminal-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            await runtime._accept_workflow_spawn_control("parent-run", outcome)
            await runtime.wait_idle(RunId("run-1"))

            signal_result = ChildSignalRuntime(
                uow, owner_id="parent-terminal-worker"
            ).receive_one(parent_run_id="parent-run", now=4.0)
            assert signal_result is not None
            _running, execution_lease = uow.claim_runtime_activation(
                run_id="parent-run",
                owner_id="parent-terminal-worker",
                namespace="runtime.kernel",
                now=4.1,
                lease_ttl_seconds=10.0,
            )
            run_fence = await uow.acquire(
                RunId("parent-run"), execution_lease, now=4.1
            )
            continuation = uow.claim_continuation(
                run_id="parent-run",
                execution_lease=execution_lease,
                now=4.2,
            )
            assert continuation is not None
            uow.ack_spawn_child_continuation_and_continue_batch(
                run_id="parent-run",
                continuation_claim=continuation,
                execution_lease=execution_lease,
                run_fence=run_fence,
                now=4.3,
            )
            wait_before = database.connection.execute(
                "SELECT state,pending_child_completion_hash FROM "
                "workflow_spawn_child_wait_receipts WHERE parent_run_id='parent-run'"
            ).fetchone()
            assert wait_before is not None
            assert wait_before["state"] == "acked_completion_pending"
            parent = uow.read_run("parent-run")
            assert parent is not None and parent.state is RunState.RUNNING

            def fail_after_wait(point: str) -> None:
                if point == "root_terminal.spawn_wait.after_write":
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="spawn_wait.after_write"):
                uow.commit_root_terminal_with_deliveries(
                    run_id="parent-run",
                    expected_version=parent.version,
                    terminal_state=RunState.CANCELLED,
                    event_id="parent-run:cancelled",
                    terminal_payload={"reason": "parent cancelled"},
                    deliveries=(),
                    fence=run_fence,
                    execution_lease=execution_lease,
                    terminal_fence_receipt_ref="parent-run:terminal-fence",
                    now=4.4,
                    fault=fail_after_wait,
                )
            assert uow.read_run("parent-run") == parent
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()[0] == "acked_completion_pending"
            assert database.connection.execute(
                "SELECT COUNT(*) FROM run_events "
                "WHERE event_id='parent-run:cancelled'"
            ).fetchone()[0] == 0

            terminal = uow.commit_root_terminal_with_deliveries(
                run_id="parent-run",
                expected_version=parent.version,
                terminal_state=RunState.CANCELLED,
                event_id="parent-run:cancelled",
                terminal_payload={"reason": "parent cancelled"},
                deliveries=(),
                fence=run_fence,
                execution_lease=execution_lease,
                terminal_fence_receipt_ref="parent-run:terminal-fence",
                now=4.5,
            )
            assert terminal.run.state is RunState.CANCELLED
            wait_after = database.connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()
            assert wait_after is not None
            assert wait_after["state"] == "acked_parent_terminal"
            assert wait_after["parent_terminal_phase_kind"] == "completion_pending"
            assert wait_after["pending_completion_terminal_receipt_id"] == (
                "parent-run:cancelled"
            )
            assert wait_after["child_completion_append_receipt_id"] is None
            durable = await _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            )
            assert durable == outcome.tool_result
            await runtime.close()

        with Database.open(path) as reopened:
            uow = SqliteExecutionUnitOfWork(reopened)
            durable = await _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            )
            assert durable == outcome.tool_result

    asyncio.run(case())


@pytest.mark.parametrize("phase", ["woken", "claimed"])
def test_parent_terminal_closes_child_signal_continuation(
    tmp_path: Path,
    phase: str,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

        def provider_tool_specs(self, _names=()):  # type: ignore[no-untyped-def]
            return ()

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        with Database.open(tmp_path / "spawn-child-signal-terminal.db") as database:
            uow = SqliteExecutionUnitOfWork(database)
            runner = _sqlite_runner(database, uow)
            catalog_authority = runner.prepare_catalog_authority(
                1, (_registration(),)
            )
            _parent, ready = await _prepare_spawn_ready_activation(
                uow, catalog_authority=catalog_authority
            )
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver(clock=lambda: 3.0)},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="signal-terminal-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            await runtime._accept_workflow_spawn_control("parent-run", outcome)
            await runtime.wait_idle(RunId("run-1"))
            signal_result = ChildSignalRuntime(
                uow, owner_id="signal-terminal-worker"
            ).receive_one(parent_run_id="parent-run", now=4.0)
            assert signal_result is not None
            assert uow.read_continuation(
                signal_result.receipt.continuation_id
            ).state.value == "pending"  # type: ignore[union-attr]
            running, execution_lease = uow.claim_runtime_activation(
                run_id="parent-run",
                owner_id="signal-terminal-worker",
                namespace="runtime.kernel",
                now=4.1,
                lease_ttl_seconds=10.0,
            )
            run_fence = await uow.acquire(
                RunId("parent-run"), execution_lease, now=4.1
            )
            continuation = None
            if phase == "claimed":
                continuation = uow.claim_continuation(
                    run_id="parent-run",
                    execution_lease=execution_lease,
                    now=4.15,
                )
                assert continuation is not None

            def fail_after_quarantine(point: str) -> None:
                expected = (
                    "root_terminal.spawn_wait.after_write"
                    if phase == "claimed"
                    else "root_terminal.spawn_continuation.after_write"
                )
                if point == expected:
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="after_write"):
                if continuation is None:
                    uow.commit_root_terminal_with_deliveries(
                        run_id="parent-run",
                        expected_version=running.version,
                        terminal_state=RunState.FAILED,
                        event_id="parent-run:failed",
                        terminal_payload={"error": "parent failed"},
                        deliveries=(),
                        fence=run_fence,
                        execution_lease=execution_lease,
                        terminal_fence_receipt_ref="parent-run:terminal-fence",
                        now=4.2,
                        fault=fail_after_quarantine,
                    )
                else:
                    uow.commit_root_terminal_with_deliveries_and_ack_continuation(
                        run_id="parent-run",
                        expected_version=running.version,
                        terminal_state=RunState.FAILED,
                        event_id="parent-run:failed",
                        terminal_payload={"error": "parent failed"},
                        deliveries=(),
                        continuation_claim=continuation,
                        run_fence=run_fence,
                        execution_lease=execution_lease,
                        receipt_id="parent-run:continuation-terminal",
                        terminal_fence_receipt_ref="parent-run:terminal-fence",
                        now=4.2,
                        fault=fail_after_quarantine,
                    )
            expected_continuation_state = "pending" if phase == "woken" else "claimed"
            assert uow.read_continuation(
                signal_result.receipt.continuation_id
            ).state.value == expected_continuation_state  # type: ignore[union-attr]
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()[0] == phase

            if continuation is None:
                committed_run = uow.commit_root_terminal_with_deliveries(
                    run_id="parent-run",
                    expected_version=running.version,
                    terminal_state=RunState.FAILED,
                    event_id="parent-run:failed",
                    terminal_payload={"error": "parent failed"},
                    deliveries=(),
                    fence=run_fence,
                    execution_lease=execution_lease,
                    terminal_fence_receipt_ref="parent-run:terminal-fence",
                    now=4.3,
                ).run
            else:
                committed_run = (
                    uow.commit_root_terminal_with_deliveries_and_ack_continuation(
                        run_id="parent-run",
                        expected_version=running.version,
                        terminal_state=RunState.FAILED,
                        event_id="parent-run:failed",
                        terminal_payload={"error": "parent failed"},
                        deliveries=(),
                        continuation_claim=continuation,
                        run_fence=run_fence,
                        execution_lease=execution_lease,
                        receipt_id="parent-run:continuation-terminal",
                        terminal_fence_receipt_ref="parent-run:terminal-fence",
                        now=4.3,
                    ).terminal.run
                )
            assert committed_run.state is RunState.FAILED
            continuation = uow.read_continuation(
                signal_result.receipt.continuation_id
            )
            assert continuation is not None
            assert continuation.state.value == (
                "quarantined" if phase == "woken" else "acked"
            )
            wait = database.connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()
            assert wait is not None
            assert wait["state"] == "acked_parent_terminal"
            assert wait["parent_terminal_phase_kind"] == (
                "signal_pending" if phase == "woken" else "continuation_claimed"
            )
            if phase == "woken":
                assert wait["late_signal_quarantine_receipt_id"] == (
                    "parent-run:failed"
                )
            else:
                assert wait[
                    "claimed_continuation_terminal_ack_receipt_id"
                ] == "parent-run:continuation-terminal"
            durable = await _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            )
            assert durable == outcome.tool_result
            await runtime.close()

    asyncio.run(case())


@pytest.mark.parametrize("pre_cancelled", [False, True])
def test_parent_terminal_requests_attached_child_cancel_before_child_can_finish(
    tmp_path: Path,
    pre_cancelled: bool,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_child(_state, _context):  # type: ignore[no-untyped-def]
        started.set()
        await release.wait()
        return StatePatch({})

    compiled = compile_workflow(
        WorkflowDefinition(
            "durable_task",
            "1",
            1,
            "blocked",
            (NodeDefinition("blocked", blocked_child),),
            {},
            5,
            4,
            edges=(Edge("blocked", "__end__"),),
        )
    )

    class NoopPort:
        async def reconcile(self) -> None:
            return None

        def provider_tool_specs(self, _names=()):  # type: ignore[no-untyped-def]
            return ()

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        with Database.open(
            tmp_path / f"spawn-child-active-terminal-{pre_cancelled}.db"
        ) as database:
            uow = SqliteExecutionUnitOfWork(database)
            runner = _sqlite_runner(database, uow, compiled=compiled)
            catalog_authority = runner.prepare_catalog_authority(
                1, (_registration(),)
            )
            _parent, ready = await _prepare_spawn_ready_activation(
                uow, catalog_authority=catalog_authority
            )
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver(clock=lambda: 3.0)},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="active-terminal-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            await runtime._accept_workflow_spawn_control("parent-run", outcome)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            child_before = uow.read_run("run-1")
            assert child_before is not None
            assert child_before.state is RunState.RUNNING
            prior_cancel = None
            if pre_cancelled:
                prior_cancel = await _atomic(
                    uow,
                    lambda tx: uow.request_cancel(
                        tx,
                        CancelWorkflowRequest(
                            "run-1:independent-cancel",
                            "run-1",
                            "independent cancel",
                            0,
                        ),
                        child_before.version,
                        None,
                        now=3.9,
                    ),
                )
            parent, execution_lease = uow.claim_runtime_activation(
                run_id="parent-run",
                owner_id="active-terminal-worker",
                namespace="runtime.kernel",
                now=4.0,
                lease_ttl_seconds=10.0,
            )
            run_fence = await uow.acquire(
                RunId("parent-run"), execution_lease, now=4.0
            )
            if not pre_cancelled:
                def fail_child_cancel(point: str) -> None:
                    if point == "root_terminal.child_cancel.receipt.after_write":
                        raise RuntimeError(point)

                with pytest.raises(RuntimeError, match="child_cancel.receipt"):
                    uow.commit_root_terminal_with_deliveries(
                        run_id="parent-run",
                        expected_version=parent.version,
                        terminal_state=RunState.CANCELLED,
                        event_id="parent-run:cancelled-active-child",
                        terminal_payload={"reason": "parent cancelled"},
                        deliveries=(),
                        fence=run_fence,
                        execution_lease=execution_lease,
                        terminal_fence_receipt_ref="parent-run:terminal-fence",
                        now=4.05,
                        fault=fail_child_cancel,
                    )
                assert uow.read_run("parent-run").state is RunState.WAITING  # type: ignore[union-attr]
                assert uow.read_run("run-1").state is RunState.RUNNING  # type: ignore[union-attr]
                assert database.connection.execute(
                    "SELECT COUNT(*) FROM workflow_cancel_receipts "
                    "WHERE run_id='run-1'"
                ).fetchone()[0] == 0
                assert database.connection.execute(
                    "SELECT state FROM workflow_spawn_child_wait_receipts "
                    "WHERE parent_run_id='parent-run'"
                ).fetchone()[0] == "unconsumed"
            terminal = uow.commit_root_terminal_with_deliveries(
                run_id="parent-run",
                expected_version=parent.version,
                terminal_state=RunState.CANCELLED,
                event_id="parent-run:cancelled-active-child",
                terminal_payload={"reason": "parent cancelled"},
                deliveries=(),
                fence=run_fence,
                execution_lease=execution_lease,
                terminal_fence_receipt_ref="parent-run:terminal-fence",
                now=4.1,
            )
            assert terminal.run.state is RunState.CANCELLED
            child_after = uow.read_run("run-1")
            assert child_after is not None
            assert child_after.state is RunState.CANCEL_REQUESTED
            cancel = database.connection.execute(
                "SELECT * FROM workflow_cancel_receipts WHERE run_id='run-1'"
            ).fetchone()
            assert cancel is not None
            assert cancel["phase"] == "requested"
            child_fence = database.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='run-1'"
            ).fetchone()
            assert child_fence is not None and child_fence["state"] == "cancelled"
            wait = database.connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()
            assert wait is not None
            assert wait["state"] == "acked_parent_terminal"
            assert wait["parent_terminal_phase_kind"] == "child_active"
            if prior_cancel is None:
                assert wait["child_cancel_request_id"] == cancel["cancel_id"]
                assert wait["child_cancel_receipt_id"] == cancel["cancel_id"]
                assert wait["reused_child_cancel_receipt_id"] is None
            else:
                assert wait["child_cancel_request_id"] is None
                assert wait["child_cancel_receipt_id"] is None
                assert wait["reused_child_cancel_receipt_id"] == (
                    prior_cancel.cancel_id
                )
            durable = await _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            )
            assert durable == outcome.tool_result

            runtime._drop_local_authority("run-1")
            await runtime._terminalize_cancelled(
                child_after, reason="attached_parent_terminal"
            )
            cancelled_child = uow.read_run("run-1")
            assert cancelled_child is not None
            assert cancelled_child.state is RunState.CANCELLED
            late_signal_row = database.connection.execute(
                "SELECT signal_id FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()
            assert late_signal_row is not None
            late_signal_id = str(late_signal_row["signal_id"])

            def fail_late_quarantine(point: str) -> None:
                if point == "child_signal_quarantine.spawn_wait.after_write":
                    raise RuntimeError(point)

            with pytest.raises(RuntimeError, match="spawn_wait.after_write"):
                uow.claim_next_child_signal(
                    parent_run_id="parent-run",
                    owner_id="late-signal-worker",
                    now=4.25,
                    lease_seconds=10.0,
                    fault=fail_late_quarantine,
                )
            assert uow.read_child_signal(late_signal_id).state.value == "pending"  # type: ignore[union-attr]
            assert database.connection.execute(
                "SELECT late_signal_quarantine_receipt_id FROM "
                "workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()[0] is None
            assert uow.claim_next_child_signal(
                parent_run_id="parent-run",
                owner_id="late-signal-worker",
                now=4.3,
                lease_seconds=10.0,
            ) is None
            late_signal = uow.read_child_signal(late_signal_id)
            assert late_signal is not None
            assert late_signal.state.value == "acked"
            wait = database.connection.execute(
                "SELECT * FROM workflow_spawn_child_wait_receipts "
                "WHERE parent_run_id='parent-run'"
            ).fetchone()
            assert wait is not None
            assert wait["late_signal_quarantine_receipt_id"] == (
                late_signal.ack_receipt_id
            )
            late_continuation = uow.read_continuation(
                str(wait["continuation_id"])
            )
            assert late_continuation is not None
            assert late_continuation.state.value == "quarantined"
            assert await _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            ) == outcome.tool_result

            release.set()
            await runtime.wait_idle(RunId("run-1"))
            assert uow.read_run("run-1").state is RunState.CANCELLED  # type: ignore[union-attr]
            assert database.connection.execute(
                "SELECT COUNT(*) FROM child_terminal_receipts WHERE child_run_id='run-1'"
            ).fetchone()[0] == 1
            await runtime.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:release_activation:before_child_command_write",
        "workflow:release_activation:after_child_command_write",
        "workflow:release_activation:before_child_signal_write",
        "workflow:release_activation:after_child_signal_write",
        "workflow:release_activation:before_child_event_write",
        "workflow:release_activation:after_child_event_write",
        "workflow:release_activation:before_child_receipt_write",
        "workflow:release_activation:after_child_receipt_write",
        "workflow:release_activation:before_terminal_event_write",
        "workflow:release_activation:after_terminal_event_write",
        "workflow:release_activation:before_terminal_fence_write",
        "workflow:release_activation:after_terminal_fence_write",
        "workflow:release_activation:before_terminal_receipt_write",
        "workflow:release_activation:after_terminal_receipt_write",
    ],
)
def test_native_child_terminal_fact_rolls_back_with_activation_release(
    tmp_path: Path, fault_point: str
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    async def case() -> None:
        path = tmp_path / f"native-child-terminal-{fault_point.rsplit(':', 1)[-1]}.db"
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            _parent, ready = await _prepare_spawn_ready_activation(uow)
            runner = _sqlite_runner(database, uow)
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver()},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 3.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="parent-worker",
                    clock=lambda: 3.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=runner,
            )
            coordinator = runtime._services.workflow_spawn
            assert coordinator is not None
            outcome = await coordinator.continue_ready(ready)
            child_admission = outcome.child_control.admission
            child_activation = child_admission.activation
            dispatch_claim = child_admission.dispatch_claim
            assert child_activation is not None and dispatch_claim is not None
            issued = await _atomic(
                uow,
                lambda tx: uow.read_issued(
                    tx, ready.ready_receipt.spawn_operation_id
                ),
            )
            assert issued is not None
            ticket, launch = issued
            verified = await _atomic(uow, lambda tx: uow.verify(tx, ticket))
            start = RunStart(
                execution_session_id=ExecutionSessionId(verified.session_id),
                run_id=RunId(verified.resolved_run_id),
                request_id=RequestId(verified.request_id),
                turn_id=verified.turn_id,
                input=launch.start_input,
                tool_catalog_generation=verified.tool_catalog_generation,
            )
            request = runner.prepare_start_admission(verified, start)
            bound = await _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    child_activation.execution_lease,
                    child_activation.run_fence,
                    dispatch_claim,
                    now=3.0,
                    ttl_seconds=10.0,
                ),
            )
            assert bound.activation is not None
            before = uow.read_run("run-1")
            assert before is not None and before.state is RunState.RUNNING

            def crash(point: str) -> None:
                if point == fault_point:
                    raise RuntimeError(point)

            uow.workflow_fault = crash

            async def terminal(tx):  # type: ignore[no-untyped-def]
                checkpoint_json = canonical_json(
                    {"checkpoint_id": "terminal-checkpoint"}
                )
                database.connection.execute(
                    "INSERT INTO workflow_checkpoints("
                    "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
                    "lease_epoch,version,created_at) VALUES(?,?,?,?,?,?,1,4)",
                    (
                        "terminal-checkpoint",
                        "run-1",
                        request.checkpoint_namespace,
                        checkpoint_json,
                        hashlib.sha256(checkpoint_json.encode()).hexdigest(),
                        bound.activation.workflow_lease.epoch,
                    ),
                )
                await runner.execution_ports.checkpoint.finalize_run(
                    tx,
                    run_id="run-1",
                    terminal_checkpoint_id="terminal-checkpoint",
                    status="completed",
                    outcome={},
                    checkpoint_namespace=request.checkpoint_namespace,
                    lease_epoch=bound.activation.workflow_lease.epoch,
                    now=4.0,
                )
                await uow.release_activation(
                    tx,
                    bound.activation,
                    before.version + 1,
                    {
                        "status": "terminal",
                        "terminal_status": "completed",
                        "output": {"ok": True},
                    },
                    now=4.0,
                )

            with pytest.raises(RuntimeError, match="release_activation"):
                await uow.run_atomic(terminal, fault_label="test:child-terminal")
            uow.workflow_fault = None
            assert uow.read_run("run-1") == before
            command = uow.read_child_command_for_run("run-1")
            assert command is not None and command.state is ChildCommandState.PENDING
            assert uow.read_child_terminal_result_for_run("run-1") is None
            assert database.connection.execute(
                "SELECT COUNT(*) FROM child_signals WHERE child_run_id='run-1'"
            ).fetchone()[0] == 0
            assert database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id='run-1' "
                "AND kind='child.completed'"
            ).fetchone()[0] == 0
            assert database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id='run-1' "
                "AND kind='run.completed'"
            ).fetchone()[0] == 0
            assert database.connection.execute(
                "SELECT COUNT(*) FROM workflow_terminal_fence_receipts "
                "WHERE run_id='run-1'"
            ).fetchone()[0] == 0
            assert database.connection.execute(
                "SELECT COUNT(*) FROM workflow_terminal_receipts "
                "WHERE run_id='run-1'"
            ).fetchone()[0] == 0
            assert database.connection.execute(
                "SELECT COUNT(*) FROM workflow_leases WHERE run_id='run-1'"
            ).fetchone()[0] == 2
            assert database.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='run-1'"
            ).fetchone()[0] == "active"

    asyncio.run(case())


def test_spawn_ready_activation_reopens_and_reclaims_one_successor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-ready-activation.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        parent, first = asyncio.run(_prepare_spawn_ready_activation(uow))
        reread = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_ready_activation(
                    tx,
                    parent.run_id,
                    first.activation_receipt_id,
                ),
            )
        )
        assert reread == first
        same_owner = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.reclaim_spawn_ready_activation(
                    tx,
                    first,
                    "parent-worker",
                    now=3.0,
                    ttl_seconds=99.0,
                ),
            )
        )
        assert same_owner == first
        with pytest.raises(UnitOfWorkConflict, match="live foreign"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.reclaim_spawn_ready_activation(
                        tx,
                        first,
                        "foreign-worker",
                        now=3.0,
                        ttl_seconds=10.0,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_ready_activation(
                    tx,
                    "parent-run",
                    first.activation_receipt_id,
                ),
            )
        )
        assert replay == first
        successor = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.reclaim_spawn_ready_activation(
                        tx,
                        replay,
                        "recovery-worker",
                        now=2000.0,
                        ttl_seconds=10.0,
                ),
            )
        )
        assert successor.predecessor_activation_receipt_id == first.activation_receipt_id
        assert successor.execution_lease.owner_id == "recovery-worker"
        assert successor.execution_lease.epoch == first.execution_lease.epoch + 1
        current = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_ready_activation(tx, "parent-run"),
            )
        )
        assert current == successor
        replayed_successor = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.reclaim_spawn_ready_activation(
                    tx,
                    successor,
                    "recovery-worker",
                    now=2001.0,
                    ttl_seconds=99.0,
                ),
            )
        )
        assert replayed_successor == successor
        with pytest.raises(UnitOfWorkConflict, match="stale"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.reclaim_spawn_ready_activation(
                        tx,
                        first,
                        "parent-worker",
                        now=2001.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        rows = reopened.connection.execute(
            "SELECT activation_receipt_id,state FROM workflow_spawn_ready_activations "
            "ORDER BY created_at,activation_receipt_id"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (first.activation_receipt_id, "superseded"),
            (successor.activation_receipt_id, "active"),
        ]


def test_spawn_direct_catalog_stale_settles_and_reopens_exact_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-catalog-stale.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _ = asyncio.run(_publish_and_issue(uow))
        issue_authority = asyncio.run(_spawn_issue_authority(uow))
        continuation = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    issue_authority,
                    None,
                    now=2.1,
                    ttl_seconds=10.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=2.2,
                ),
            )
        )
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_catalog_stale(
                    tx,
                    continuation,
                    None,
                    now=2.3,
                ),
            )
        )
        assert result.outcome is ToolOutcome.FAILED
        assert result.error_code == "workflow_catalog_stale"
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, launch.request_key
                ),
            )
        ) == result

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, launch.request_key
                ),
            )
        )
        assert replay == result
        replayed_settlement = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_catalog_stale(
                    tx,
                    continuation,
                    None,
                    now=20.0,
                ),
            )
        )
        assert replayed_settlement == result


def test_spawn_outcome_reader_rejects_corrupt_completion_hash(tmp_path: Path) -> None:
    path = tmp_path / "spawn-corrupt-outcome.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _ = asyncio.run(_publish_and_issue(uow))
        issue_authority = asyncio.run(_spawn_issue_authority(uow))
        continuation = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    issue_authority,
                    None,
                    now=2.1,
                    ttl_seconds=10.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=2.2,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_catalog_stale(
                    tx,
                    continuation,
                    None,
                    now=2.3,
                ),
            )
        )
        database.connection.execute(
            "UPDATE workflow_spawn_completion_receipts SET canonical_hash=?",
            ("0" * 64,),
        )
        with pytest.raises(UnitOfWorkConflict, match="completion.*hash"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.read_spawn_continuation_outcome(
                        tx, launch.request_key
                    ),
                )
            )


@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_catalog_stale:after_effect_write", False),
        ("workflow:spawn_catalog_stale:after_completion_write", False),
        ("workflow:spawn_catalog_stale:after_continuation_write", False),
        ("workflow:spawn_catalog_stale:after_commit", True),
    ],
)
def test_spawn_catalog_stale_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-catalog-stale-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        launch, ticket, _ = asyncio.run(_publish_and_issue(base))
        issue_authority = asyncio.run(_spawn_issue_authority(base))
        continuation = asyncio.run(
            _atomic(
                base,
                lambda tx: base.claim_spawn_continuation(
                    tx,
                    ticket,
                    issue_authority,
                    None,
                    now=2.1,
                    ttl_seconds=10.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                base,
                lambda tx: base.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=2.2,
                ),
            )
        )

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        uow = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if persisted
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.settle_spawn_continuation_catalog_stale(
                        tx,
                        continuation,
                        None,
                        now=2.3,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, launch.request_key
                ),
            )
        )
        effect = uow.read_effect(EffectId(issue_authority.effect_id))
        assert effect is not None
        if persisted:
            assert outcome is not None
            assert outcome.error_code == "workflow_catalog_stale"
            assert effect.state is EffectState.FAILED
        else:
            assert outcome is None
            assert effect.state is EffectState.HANDED_OFF


def test_spawn_ready_catalog_stale_consumes_activation_and_reopens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-ready-catalog-stale.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(uow))
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=2.5,
                ),
            )
        )
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_catalog_stale(
                    tx,
                    activation.continuation_claim,
                    activation.ready_receipt,
                    now=2.6,
                ),
            )
        )
        assert result.error_code == "workflow_catalog_stale"
        row = database.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()
        assert row is not None and row[0] == "consumed"
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        ) == result

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        ) == result

def test_spawn_ready_graph_unavailable_proof_settles_and_reopens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-graph-unavailable.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(uow))
        issued = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_issued(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        assert issued is not None
        ticket, _request = issued
        verified = asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        with pytest.raises(WorkflowDependencyUnavailable, match="available"):
            _sqlite_runner(database, uow)._prove_graph_unavailable(
                verified, activation
            )
        proof = _sqlite_runner(
            database, uow, compiled=None
        )._prove_graph_unavailable(verified, activation)
        assert proof.observed_kind == "missing"
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_graph_unavailable(
                    tx,
                    activation.continuation_claim,
                    activation.ready_receipt,
                    proof,
                    now=4.0,
                ),
            )
        )
        assert result.outcome is ToolOutcome.FAILED
        assert result.error_code == "graph_version_unavailable"
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        ) == result

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        ) == result


def test_spawn_ready_coordinator_returns_sealed_graph_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-graph-coordinator.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(uow))
        coordinator = _CanonicalWorkflowSpawnRuntimeCoordinator(
            uow=uow,
            runner=_sqlite_runner(database, uow, compiled=None),
            owner_id="parent-worker",
            lease_ttl_seconds=10.0,
            clock=lambda: 4.0,
        )
        outcome = asyncio.run(coordinator.continue_ready(activation))
        assert isinstance(outcome, WorkflowSpawnFailed)
        assert outcome.tool_result.error_code == "graph_version_unavailable"
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        ) == outcome.tool_result
        assert database.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations "
            "WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()[0] == "consumed"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id='parent-run'"
        ).fetchone()[0] == 0

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        coordinator = _CanonicalWorkflowSpawnRuntimeCoordinator(
            uow=uow,
            runner=_sqlite_runner(reopened, uow, compiled=None),
            owner_id="replacement-worker",
            lease_ttl_seconds=10.0,
            clock=lambda: 8.0,
        )
        replayed = asyncio.run(coordinator.continue_ready(activation))
        assert isinstance(replayed, WorkflowSpawnFailed)
        assert replayed == outcome


def test_spawn_ready_coordinator_returns_sealed_catalog_stale_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spawn-catalog-coordinator.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(uow))
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=3.0,
                ),
            )
        )
        coordinator = _CanonicalWorkflowSpawnRuntimeCoordinator(
            uow=uow,
            runner=_sqlite_runner(database, uow),
            owner_id="parent-worker",
            lease_ttl_seconds=10.0,
            clock=lambda: 4.0,
        )
        outcome = asyncio.run(coordinator.continue_ready(activation))
        assert isinstance(outcome, WorkflowSpawnFailed)
        assert outcome.tool_result.error_code == "workflow_catalog_stale"
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id='parent-run'"
        ).fetchone()[0] == 0

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        coordinator = _CanonicalWorkflowSpawnRuntimeCoordinator(
            uow=uow,
            runner=_sqlite_runner(reopened, uow),
            owner_id="replacement-worker",
            lease_ttl_seconds=10.0,
            clock=lambda: 8.0,
        )
        replayed = asyncio.run(coordinator.continue_ready(activation))
        assert isinstance(replayed, WorkflowSpawnFailed)
        assert replayed == outcome


@pytest.mark.parametrize(
    "terminal_state",
    [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED],
)
@pytest.mark.parametrize(
    ("shape", "expected_path"),
    [
        ("ticket_only", "parent_terminal_ticket_only"),
        ("ready_unactivated", "parent_terminal_ready_unactivated"),
        ("activated", "parent_terminal_activated"),
        ("activated_reclaimed", "parent_terminal_activated"),
    ],
)
def test_spawn_parent_terminal_settles_all_durable_shapes_and_reopens(
    tmp_path: Path,
    terminal_state: RunState,
    shape: str,
    expected_path: str,
) -> None:
    path = tmp_path / f"spawn-parent-terminal-{shape}-{terminal_state.value}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _ = asyncio.run(_publish_and_issue(uow))
        if shape == "ticket_only":
            issue_authority = asyncio.run(_spawn_issue_authority(uow))
            authority = asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_spawn_continuation(
                        tx,
                        ticket,
                        issue_authority,
                        None,
                        now=2.1,
                        ttl_seconds=10.0,
                    ),
                )
            )
        else:
            issue_authority = asyncio.run(_spawn_issue_authority(uow))
            effect = uow.read_effect(EffectId("spawn-effect-1"))
            assert effect is not None
            unknown = uow.mark_effect_unknown(
                effect.effect_id,
                expected_version=effect.version,
                expected_fence_epoch=1,
                evidence_ref="spawn:unknown:terminal",
                now=2.1,
            )
            ready = asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.mark_spawn_continuation_ready(
                        tx,
                        ticket,
                        unknown,
                        "spawn:ready:terminal",
                        now=2.2,
                    ),
                )
            )
            parent = uow.read_run("parent-run")
            assert parent is not None
            _, blocker = uow.commit_runtime_wait_with_blocker(
                run_id="parent-run",
                expected_version=parent.version,
                event_id="parent-run:spawn-terminal-waiting",
                payload={"reason": "workflow_spawn_started_incomplete"},
                blocker=WaitBlockerSpec(
                    RecoveryKind.TOOL,
                    unknown.effect_id.value,
                    unknown.handoff_attempt,
                    unknown.version,
                ),
                lease=issue_authority.execution_lease,
                now=2.3,
            )
            if shape == "ready_unactivated":
                authority = ready
            else:
                activation = asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.consume_spawn_ready_and_claim_activation(
                            tx,
                            ready,
                            blocker,
                            "parent-worker",
                            now=2.4,
                            ttl_seconds=10.0,
                        ),
                    )
                )
                if shape == "activated_reclaimed":
                    activation = asyncio.run(
                        _atomic(
                            uow,
                            lambda tx: uow.reclaim_spawn_ready_activation(
                                tx,
                                activation,
                                "replacement-worker",
                                now=2000.0,
                                ttl_seconds=10.0,
                            ),
                        )
                    )
                authority = activation.ready_receipt
        terminal = _terminalize_spawn_parent(
            uow,
            terminal_state,
            now=2010.0 if shape == "activated_reclaimed" else 8.0,
        )
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_for_parent_terminal(
                    tx,
                    ticket,
                    authority,
                    terminal,
                    now=9.0,
                ),
            )
        )
        assert result.outcome is ToolOutcome.FAILED
        assert result.error_code == "workflow_parent_terminal_before_spawn"
        completion = database.connection.execute(
            "SELECT path_kind FROM workflow_spawn_completion_receipts "
            "WHERE spawn_operation_id=?",
            (launch.request_key,),
        ).fetchone()
        assert completion is not None and completion[0] == expected_path
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_ready_activations "
            "WHERE spawn_operation_id=? AND state='active'",
            (launch.request_key,),
        ).fetchone()[0] == 0
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id='parent-run'"
        ).fetchone()[0] == 0
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.settle_spawn_continuation_for_parent_terminal(
                    tx,
                    ticket,
                    authority,
                    terminal,
                    now=10.0,
                ),
            )
        ) == result
        with pytest.raises(
            UnitOfWorkConflict, match="parent-terminal replay evidence differs"
        ):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.settle_spawn_continuation_for_parent_terminal(
                        tx,
                        ticket,
                        authority,
                        replace(terminal, outcome_hash="0" * 64),
                        now=10.1,
                    ),
                )
            )
        if shape in {"activated", "activated_reclaimed"}:
            with pytest.raises(UnitOfWorkConflict, match="activation is stale"):
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.reclaim_spawn_ready_activation(
                            tx,
                            activation,
                            "late-worker",
                            now=3000.0,
                            ttl_seconds=10.0,
                        ),
                    )
                )
            coordinator = _CanonicalWorkflowSpawnRuntimeCoordinator(
                uow=uow,
                runner=_sqlite_runner(database, uow),
                owner_id="late-worker",
                lease_ttl_seconds=10.0,
                clock=lambda: 3000.0,
            )
            replayed = asyncio.run(coordinator.continue_ready(activation))
            assert isinstance(replayed, WorkflowSpawnFailed)
            assert replayed.tool_result == result

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, launch.request_key
                ),
            )
        ) == result


@pytest.mark.parametrize(
    ("shape", "fault_point", "persisted"),
    [
        ("ready", "workflow:spawn_parent_terminal:before_effect_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_effect_write", False),
        ("ready", "workflow:spawn_parent_terminal:before_ready_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_ready_write", False),
        ("ready", "workflow:spawn_parent_terminal:before_blocker_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_blocker_write", False),
        ("activated", "workflow:spawn_parent_terminal:before_activation_write", False),
        ("activated", "workflow:spawn_parent_terminal:after_activation_write", False),
        ("ready", "workflow:spawn_parent_terminal:before_completion_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_completion_write", False),
        ("ready", "workflow:spawn_parent_terminal:before_continuation_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_continuation_write", False),
        ("ready", "workflow:spawn_parent_terminal:after_commit", True),
    ],
)
def test_spawn_parent_terminal_fault_reopen_is_atomic(
    tmp_path: Path,
    shape: str,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-parent-terminal-fault-{fault_point.rsplit(':', 1)[-1]}-{shape}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        parent, ready, blocker = asyncio.run(_prepare_spawn_ready(base))
        assert parent is not None
        issued = asyncio.run(
            _atomic(
                base,
                lambda tx: base.read_issued(tx, ready.spawn_operation_id),
            )
        )
        assert issued is not None
        ticket, _request = issued
        authority = ready
        if shape == "activated":
            activation = asyncio.run(
                _atomic(
                    base,
                    lambda tx: base.consume_spawn_ready_and_claim_activation(
                        tx,
                        ready,
                        blocker,
                        "parent-worker",
                        now=2.4,
                        ttl_seconds=10.0,
                    ),
                )
            )
            authority = activation.ready_receipt
        terminal = _terminalize_spawn_parent(base, RunState.CANCELLED)

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        crashing = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if persisted
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    crashing,
                    lambda tx: crashing.settle_spawn_continuation_for_parent_terminal(
                        tx,
                        ticket,
                        authority,
                        terminal,
                        now=9.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, ready.spawn_operation_id
                ),
            )
        )
        active = reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_ready_activations "
            "WHERE spawn_operation_id=? AND state='active'",
            (ready.spawn_operation_id,),
        ).fetchone()[0]
        if persisted:
            assert outcome is not None
            assert outcome.error_code == "workflow_parent_terminal_before_spawn"
            assert active == 0
        else:
            assert outcome is None
            assert active == (1 if shape == "activated" else 0)

def test_react_graph_failure_clears_ready_carrier_before_normal_loop(
    tmp_path: Path,
) -> None:
    class NoopPort:
        async def reconcile(self) -> None:
            return None

    class Catalog:
        def current_generation(self) -> int:
            return 7

    class RecordingLoop:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.calls += 1

            class Response:
                class MessageValue:
                    content = "continued after durable spawn failure"

                message = MessageValue()

            class Result:
                response = Response()

            return Result()

    async def case() -> None:
        with Database.open(tmp_path / "spawn-graph-react.db") as database:
            uow = SqliteExecutionUnitOfWork(database)
            parent, activation = await _prepare_spawn_ready_activation(uow)
            noop = NoopPort()
            runtime = build_runtime(
                uow,
                {"agent.general": RuntimeProfile("agent.general", "react")},
                {"react": ReActDriver()},
                RuntimePorts(
                    provider=noop,
                    tools=noop,
                    authorization=noop,
                    context=SqliteContextPort(database, clock=lambda: 4.0),
                    delivery=noop,
                    tool_reconciliation=noop,
                    reconciliation=noop,
                    provider_reconciliation=noop,
                    react_checkpoint=uow,
                    tool_catalog=Catalog(),
                    owner_id="parent-worker",
                    clock=lambda: 4.0,
                    lease_ttl_seconds=10.0,
                ),
                workflow_runner=_sqlite_runner(database, uow, compiled=None),
            )
            start = bind_start_snapshot(
                RunStart(
                    execution_session_id=ExecutionSessionId("session-1"),
                    run_id=RunId(parent.run_id),
                    request_id=RequestId(parent.request_id),
                    turn_id="turn-1",
                    input={"messages": [{"role": "user", "content": "continue"}]},
                    tool_catalog_generation=7,
                ),
                profile_key="agent.general",
                driver_kind="react",
            )
            driver = ReActDriver()
            loop = RecordingLoop()
            driver._loop = loop  # type: ignore[assignment]
            result = await driver.start(
                DriverInvocation(
                    run=uow.read_run(parent.run_id),  # type: ignore[arg-type]
                    start=start,
                    execution_lease=activation.execution_lease,
                    run_fence=activation.run_fence,
                    services=runtime._services,
                    workflow_spawn_ready_activation=activation,
                ),
                context=runtime._services.context,
                cancel=CancelToken(),
            )
            assert result.state is RunState.COMPLETED
            assert result.payload == {
                "response_present": True,
                "finish_reason": None,
            }
            assert loop.calls == 1
            effect = uow.read_effect(EffectId(activation.ready_receipt.effect_id))
            assert effect is not None and effect.state is EffectState.FAILED
            assert database.connection.execute(
                "SELECT state FROM workflow_spawn_ready_activations "
                "WHERE activation_receipt_id=?",
                (activation.activation_receipt_id,),
            ).fetchone()[0] == "consumed"

    asyncio.run(case())


@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_graph_unavailable:after_effect_write", False),
        ("workflow:spawn_graph_unavailable:after_activation_write", False),
        ("workflow:spawn_graph_unavailable:after_completion_write", False),
        ("workflow:spawn_graph_unavailable:after_continuation_write", False),
        ("workflow:spawn_graph_unavailable:after_commit", True),
    ],
)
def test_spawn_ready_graph_unavailable_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-graph-fault-{fault_point.rsplit(':', 1)[-1]}.db"
    operation_id: str
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(base))
        operation_id = activation.ready_receipt.spawn_operation_id
        issued = asyncio.run(
            _atomic(base, lambda tx: base.read_issued(tx, operation_id))
        )
        assert issued is not None
        ticket, _request = issued
        verified = asyncio.run(_atomic(base, lambda tx: base.verify(tx, ticket)))
        proof = _sqlite_runner(
            database, base, compiled=None
        )._prove_graph_unavailable(verified, activation)

        def crash(point: str) -> None:
            if point == fault_point:
                raise RuntimeError(point)

        crashing = (
            SqliteExecutionUnitOfWork(database, workflow_fault=crash)
            if persisted
            else base
        )
        with pytest.raises(RuntimeError, match="spawn_graph_unavailable"):
            asyncio.run(
                _atomic(
                    crashing,
                    lambda tx: crashing.settle_spawn_continuation_graph_unavailable(
                        tx,
                        activation.continuation_claim,
                        activation.ready_receipt,
                        proof,
                        now=4.0,
                        fault=crash,
                    ),
                )
            )

    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(tx, operation_id),
            )
        )
        effect = uow.read_effect(EffectId(activation.ready_receipt.effect_id))
        assert effect is not None
        activation_state = database.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations "
            "WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()[0]
        if persisted:
            assert outcome is not None
            assert outcome.error_code == "graph_version_unavailable"
            assert effect.state is EffectState.FAILED
            assert activation_state == "consumed"
        else:
            assert outcome is None
            assert effect.state is EffectState.UNKNOWN
            assert activation_state == "active"


@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_catalog_stale:after_effect_write", False),
        ("workflow:spawn_catalog_stale:after_activation_write", False),
        ("workflow:spawn_catalog_stale:after_completion_write", False),
        ("workflow:spawn_catalog_stale:after_continuation_write", False),
        ("workflow:spawn_catalog_stale:after_commit", True),
    ],
)
def test_spawn_ready_catalog_stale_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-ready-stale-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        _, activation = asyncio.run(_prepare_spawn_ready_activation(base))
        asyncio.run(
            _atomic(
                base,
                lambda tx: base.publish_catalog(
                    tx,
                    _catalog(generation=2),
                    1,
                    now=2.5,
                ),
            )
        )

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        uow = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if persisted
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.settle_spawn_continuation_catalog_stale(
                        tx,
                        activation.continuation_claim,
                        activation.ready_receipt,
                        now=2.6,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        outcome = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_continuation_outcome(
                    tx, activation.ready_receipt.spawn_operation_id
                ),
            )
        )
        row = reopened.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations WHERE activation_receipt_id=?",
            (activation.activation_receipt_id,),
        ).fetchone()
        assert row is not None
        if persisted:
            assert outcome is not None
            assert outcome.error_code == "workflow_catalog_stale"
            assert row[0] == "consumed"
        else:
            assert outcome is None
            assert row[0] == "active"

@pytest.mark.parametrize(
    ("fault_point", "persisted"),
    [
        ("workflow:spawn_activation_reclaim:after_runtime_lease_write", False),
        ("workflow:spawn_activation_reclaim:after_run_fence_write", False),
        ("workflow:spawn_activation_reclaim:after_continuation_write", False),
        ("workflow:spawn_activation_reclaim:after_predecessor_write", False),
        ("workflow:spawn_activation_reclaim:after_successor_write", False),
        ("workflow:spawn_activation_reclaim:after_commit", True),
    ],
)
def test_spawn_ready_reclaim_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-reclaim-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        _, first = asyncio.run(_prepare_spawn_ready_activation(base))

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        uow = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if persisted
            else base
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.reclaim_spawn_ready_activation(
                        tx,
                        first,
                        "recovery-worker",
                        now=2000.0,
                        ttl_seconds=10.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        current = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_spawn_ready_activation(tx, "parent-run"),
            )
        )
        assert current is not None
        rows = reopened.connection.execute(
            "SELECT state FROM workflow_spawn_ready_activations ORDER BY created_at"
        ).fetchall()
        if persisted:
            assert current.execution_lease.owner_id == "recovery-worker"
            assert [row[0] for row in rows] == ["superseded", "active"]
        else:
            assert current == first
            assert [row[0] for row in rows] == ["active"]


@pytest.mark.parametrize(
    "fault_point,persisted",
    [
        ("workflow:launch_ticket:before_receipt_write", False),
        ("workflow:launch_ticket:after_receipt_write", False),
        ("workflow:launch_ticket:after_continuation_write", False),
        ("workflow:launch_ticket:after_commit", True),
    ],
)
def test_spawn_issue_ticket_and_continuation_fault_reopen_is_atomic(
    tmp_path: Path,
    fault_point: str,
    persisted: bool,
) -> None:
    path = tmp_path / f"spawn-issue-{fault_point.rsplit(':', 1)[-1]}.db"
    launch = _launch_request()
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(tx, _catalog(), 0, now=1.0),
            )
        )
        issue_authority = asyncio.run(_spawn_issue_authority(uow))

        def fault(actual: str) -> None:
            if actual == fault_point:
                raise RuntimeError(actual)

        issue_uow = (
            SqliteExecutionUnitOfWork(database, workflow_fault=fault)
            if fault_point.endswith("after_commit")
            else uow
        )
        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    issue_uow,
                    lambda tx: issue_uow.issue(
                        tx,
                        launch,
                        issue_authority,
                        now=2.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == int(persisted)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_spawn_continuations"
        ).fetchone()[0] == int(persisted)
        if persisted:
            uow = SqliteExecutionUnitOfWork(reopened)
            replay = asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.issue(
                        tx,
                        launch,
                        issue_authority,
                        now=2000.0,
                    ),
                )
            )
            assert replay.ticket_receipt_id


def test_expired_spawn_claim_requires_ready_and_current_takeover_authority(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "spawn-takeover.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _, ticket, _ = asyncio.run(_publish_and_issue(uow))
        original = asyncio.run(_spawn_issue_authority(uow))
        effect = uow.read_effect(EffectId(original.effect_id))
        assert effect is not None
        ready = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.mark_spawn_continuation_ready(
                    tx, ticket, effect, "ready:takeover", now=2.0
                ),
            )
        )
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    original,
                    None,
                    now=2.0,
                    ttl_seconds=1.0,
                ),
            )
        )
        _, execution_lease = uow.claim_runtime_activation(
            run_id="parent-run",
            owner_id="takeover-worker",
            namespace="runtime.kernel",
            now=1001.0,
            lease_ttl_seconds=100.0,
        )
        run_fence = asyncio.run(
            uow.acquire(RunId("parent-run"), execution_lease, now=1001.0)
        )
        takeover = WorkflowSpawnIssueAuthority(
            react_checkpoint_revision=0,
            execution_lease=execution_lease,
            run_fence=run_fence,
            workflow_lease=None,
            effect_id=effect.effect_id.value,
            effect_handoff_attempt=effect.handoff_attempt,
            effect_request_hash=effect.request_hash,
        )
        with pytest.raises(UnitOfWorkConflict, match="requires ready evidence"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.claim_spawn_continuation(
                        tx,
                        ticket,
                        takeover,
                        None,
                        now=1001.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        successor = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.claim_spawn_continuation(
                    tx,
                    ticket,
                    takeover,
                    ready,
                    now=1001.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert successor.owner_id == "takeover-worker"
        assert successor.runtime_lease_epoch == execution_lease.epoch
        assert successor.claim_epoch == first.claim_epoch + 1


def test_concurrent_exact_ticket_issue_has_one_durable_winner(tmp_path: Path) -> None:
    path = tmp_path / "ticket-concurrent.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(tx, _catalog(), 0, now=1.0),
            )
        )
        issue_authority = asyncio.run(_spawn_issue_authority(uow))

    ready = threading.Barrier(2)
    tickets: list[object] = []
    failures: list[BaseException] = []

    def issue() -> None:
        database = Database.open(path, timeout=5.0)
        try:
            uow = SqliteExecutionUnitOfWork(database)
            ready.wait(5.0)
            tickets.append(
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.issue(
                            tx,
                            _launch_request(),
                            issue_authority,
                            now=2.0,
                        ),
                    )
                )
            )
        except BaseException as error:  # noqa: BLE001 - thread evidence
            failures.append(error)
        finally:
            database.close()

    threads = [threading.Thread(target=issue) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert not failures
    assert len(tickets) == 2
    assert tickets[0] == tickets[1]
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == 1


def test_concurrent_exact_runtime_admission_has_one_generic_run(tmp_path: Path) -> None:
    path = tmp_path / "admission-concurrent.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)

    ready = threading.Barrier(2)
    results: list[object] = []
    failures: list[BaseException] = []

    def admit() -> None:
        database = Database.open(path, timeout=5.0)
        try:
            uow = SqliteExecutionUnitOfWork(database)
            ready.wait(5.0)
            results.append(
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.admit_runtime_start(
                            tx,
                            ticket,
                            start,
                            request,
                            snapshot,
                            RuntimeActivationClaim(
                                "owner", lease_ttl_seconds=10.0
                            ),
                            now=3.0,
                        ),
                    )
                )
            )
        except BaseException as error:  # noqa: BLE001 - thread evidence
            failures.append(error)
        finally:
            database.close()

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert not failures
    assert {
        result.disposition for result in results  # type: ignore[union-attr]
    } == {
        RuntimeStartDisposition.START_NEW,
        RuntimeStartDisposition.START_ORPHAN,
    }
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runtime_start_receipts"
        ).fetchone()[0] == 1


def test_catalog_advance_rejects_old_ticket_before_generic_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-barrier.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        next_catalog = _catalog(generation=2, version=2)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(tx, next_catalog, 1, now=3.0),
            )
        )
        with pytest.raises(UnitOfWorkConflict, match="catalog"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                        now=4.0,
                    ),
                )
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:catalog:before_authority_write",
        "workflow:catalog:after_authority_write",
    ],
)
def test_catalog_publish_fault_rolls_back_after_reopen(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / f"catalog-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)

        def fault(label: str) -> None:
            if label == fault_point:
                raise RuntimeError(label)

        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.publish_catalog(
                        tx, _catalog(), 0, now=1.0, fault=fault
                    ),
                )
            )
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_catalog_authorities"
        ).fetchone()[0] == 0


def test_catalog_publish_after_commit_response_loss_replays_after_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-after-commit.db"

    def fault(label: str) -> None:
        if label == "workflow:catalog:after_commit":
            raise RuntimeError(label)

    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database, workflow_fault=fault)
        with pytest.raises(RuntimeError, match="workflow:catalog:after_commit"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.publish_catalog(tx, _catalog(), 0, now=1.0),
                )
            )
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(tx, _catalog(), 0, now=2.0),
            )
        )
        assert replay.generation == 1


def test_runtime_admission_dispatch_claim_and_recovery_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-admission.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)

        async def admit(owner: str, now: float):
            return await _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim(owner, lease_ttl_seconds=10.0),
                    now=now,
                ),
            )

        first = asyncio.run(admit("owner-a", 3.0))
        assert first.disposition is RuntimeStartDisposition.START_NEW
        same_owner = asyncio.run(admit("owner-a", 4.0))
        assert same_owner.disposition is RuntimeStartDisposition.START_ORPHAN
        assert same_owner.dispatch_claim == first.dispatch_claim
        foreign = asyncio.run(admit("owner-b", 4.0))
        assert foreign.disposition is RuntimeStartDisposition.FOREIGN_ACTIVE
        assert foreign.activation is None

        dispatch = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    first.activation.execution_lease,  # type: ignore[union-attr]
                    first.activation.run_fence,  # type: ignore[union-attr]
                    first.dispatch_claim,
                    now=5.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert dispatch.action is PrecreatedStartAction.NEW_CLAIMED
        assert dispatch.receipt.claim_action is StartClaimAction.NEW
        assert dispatch.receipt.version == 0
        assert dispatch.receipt.claim_epoch == 1
        assert dispatch.receipt.claim_expires_at == 13.0
        assert dispatch.activation == dispatch.receipt.activation
        assert database.connection.execute(
            "SELECT state FROM runtime_start_dispatch_claims"
        ).fetchone()[0] == "consumed"

        # Runtime heartbeat renews the durable record but the Driver keeps the
        # original stable claim capability; exact replay consumes/reads once.
        renewed = uow.renew_runtime_lease(
            first.activation.execution_lease,  # type: ignore[union-attr]
            now=6.0,
            lease_ttl_seconds=20.0,
        )
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    renewed,
                    first.activation.run_fence,  # type: ignore[union-attr]
                    first.dispatch_claim,
                    now=14.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert replay.receipt == dispatch.receipt
        assert replay.receipt.claim_action is StartClaimAction.NEW
        assert replay.receipt.claim_expires_at == 13.0
        assert renewed.expires_at == 26.0

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        database = reopened
        reopened_replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    renewed,
                    first.activation.run_fence,  # type: ignore[union-attr]
                    first.dispatch_claim,
                    now=15.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert reopened_replay.receipt == dispatch.receipt
        assert reopened_replay.receipt.claim_action is StartClaimAction.NEW
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=8 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET expires_at=8 WHERE run_id='run-1'"
        )
        database.connection.commit()
        launch_row = database.connection.execute(
            "SELECT * FROM workflow_launch_ticket_receipts"
        ).fetchone()
        assert launch_row is not None
        issued = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.read_issued(tx, _launch_request().request_key),
            )
        )
        assert issued is not None
        ticket = issued[0]
        verified = asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        start, request, snapshot = _start_inputs(ticket, verified, _launch_request())
        recovered = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )
        assert recovered.disposition is RuntimeStartDisposition.RECOVER_START
        assert recovered.activation.execution_lease.epoch == 2  # type: ignore[union-attr]
        rebound = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.recover_precreated_start(
                    tx,
                    recovered.recovery_work,
                    recovered.activation.execution_lease,  # type: ignore[union-attr]
                    recovered.activation.run_fence,  # type: ignore[union-attr]
                    now=20.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert rebound.action is PrecreatedStartAction.RESUME_CLAIMED
        assert rebound.receipt.claim_action is StartClaimAction.RESUME
        assert rebound.receipt.version == 1
        assert rebound.receipt.claim_epoch == 2
        assert rebound.receipt.claim_expires_at == 30.0
        assert rebound.activation == rebound.receipt.activation
        assert rebound.activation.execution_lease.owner_id == "owner-a"
        with pytest.raises(UnitOfWorkConflict):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        first.activation.execution_lease,  # type: ignore[union-attr]
                        first.activation.run_fence,  # type: ignore[union-attr]
                        first.dispatch_claim,
                        now=21.0,
                        ttl_seconds=10.0,
                    ),
                )
            )


def test_consumed_dispatch_is_audit_only_across_renew_and_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consumed-dispatch.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    admitted.activation.execution_lease,  # type: ignore[union-attr]
                    admitted.activation.run_fence,  # type: ignore[union-attr]
                    admitted.dispatch_claim,
                    now=4.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        before = database.connection.execute(
            "SELECT owner_id,runtime_lease_epoch,expires_at,version,state "
            "FROM runtime_start_dispatch_claims"
        ).fetchone()
        assert tuple(before) == ("owner-a", 1, 13.0, 1, "consumed")

        renewed = uow.renew_runtime_lease(
            admitted.activation.execution_lease,  # type: ignore[union-attr]
            now=5.0,
            lease_ttl_seconds=20.0,
        )
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at,version,state "
                "FROM runtime_start_dispatch_claims"
            ).fetchone()
        ) == tuple(before)
        uow.release_runtime_lease(renewed, now=6.0)
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at,version,state "
                "FROM runtime_start_dispatch_claims"
            ).fetchone()
        ) == tuple(before)

        recovered = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )
        assert recovered.disposition is RuntimeStartDisposition.RECOVER_START
        renewed_recovery = uow.renew_runtime_lease(
            recovered.activation.execution_lease,  # type: ignore[union-attr]
            now=21.0,
            lease_ttl_seconds=20.0,
        )
        assert renewed_recovery.owner_id == "owner-b"
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at,version,state "
                "FROM runtime_start_dispatch_claims"
            ).fetchone()
        ) == tuple(before)


def test_runtime_start_admission_rejects_cross_authority_objects(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "admission-invariants.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        assert admitted.activation is not None
        assert admitted.dispatch_claim is not None
        with pytest.raises(ValueError, match="dispatch claim is not co-fenced"):
            RuntimeStartAdmission(
                admitted.receipt,
                RuntimeStartDisposition.START_ORPHAN,
                activation=admitted.activation,
                dispatch_claim=RuntimeStartDispatchClaim(
                    admitted.dispatch_claim.claim_id,
                    admitted.receipt.run_id,
                    "foreign-owner",
                    admitted.activation.execution_lease.epoch,
                    admitted.dispatch_claim.claim_epoch,
                ),
            )


def _seed_started_precreated(
    uow: SqliteExecutionUnitOfWork,
    *,
    now: float = 3.0,
):
    launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
    start, request, snapshot = _start_inputs(ticket, verified, launch)
    admission = asyncio.run(
        _atomic(
            uow,
            lambda tx: uow.admit_runtime_start(
                tx,
                ticket,
                start,
                request,
                snapshot,
                RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                now=now,
            ),
        )
    )
    dispatch = asyncio.run(
        _atomic(
            uow,
            lambda tx: uow.ensure_and_bind_precreated_start(
                tx,
                request,
                admission.activation.execution_lease,  # type: ignore[union-attr]
                admission.activation.run_fence,  # type: ignore[union-attr]
                admission.dispatch_claim,
                now=now + 1,
                ttl_seconds=10.0,
            ),
        )
    )
    connection = uow.database.connection
    checkpoint_json = canonical_json(
        {
            "checkpoint_id": "checkpoint-1",
            "status": "running",
            "values": {},
        }
    )
    connection.execute(
        "INSERT INTO workflow_checkpoints("
        "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
        "lease_epoch,version,created_at) VALUES(?,?,?,?,?,?,0,?)",
        (
            "checkpoint-1",
            verified.resolved_run_id,
            request.checkpoint_namespace,
            checkpoint_json,
            hashlib.sha256(checkpoint_json.encode()).hexdigest(),
            1,
            now + 2,
        ),
    )
    connection.execute(
        "UPDATE workflow_start_admissions SET phase='running',version=1 "
        "WHERE run_id=?",
        (verified.resolved_run_id,),
    )
    connection.commit()
    return launch, ticket, verified, start, request, snapshot, admission, dispatch


def test_runtime_admission_recovers_unsettled_resume_not_start(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "recover-resume.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        (
            _,
            ticket,
            verified,
            start,
            request,
            snapshot,
            _,
            _,
        ) = _seed_started_precreated(uow)
        response_payload = {"decision-1": {"approved": True}}
        response_hash = hashlib.sha256(
            canonical_json(response_payload).encode()
        ).hexdigest()
        resume = ResumeAdmissionRequest(
            receipt_id="resume-1",
            run_id=verified.resolved_run_id,
            expected_run_version=1,
            expected_checkpoint_head="checkpoint-1",
            pending_interrupts=(),
            responses=response_payload,
            responses_hash=response_hash,
            mode=StartMode.PRECREATED,
        )
        resume_json = canonical_json(
            {
                "receipt_id": resume.receipt_id,
                "run_id": resume.run_id,
                "expected_run_version": resume.expected_run_version,
                "expected_checkpoint_head": resume.expected_checkpoint_head,
                "pending_interrupts": [],
                "responses": response_payload,
                "responses_hash": response_hash,
                "mode": resume.mode.value,
            }
        )
        database.connection.execute(
            "INSERT INTO workflow_resume_admissions("
            "receipt_id,run_id,request_fingerprint,request_json,mode,"
            "expected_run_version,expected_checkpoint_head,phase,version,"
            "claim_owner,claim_epoch,claim_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'claimed',1,'owner-a',1,5,6,6)",
            (
                resume.receipt_id,
                resume.run_id,
                hashlib.sha256(resume_json.encode()).hexdigest(),
                resume_json,
                resume.mode.value,
                resume.expected_run_version,
                resume.expected_checkpoint_head,
            ),
        )
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=5 WHERE run_id=?",
            (verified.resolved_run_id,),
        )
        database.connection.commit()
        recovered = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )
        assert recovered.disposition is RuntimeStartDisposition.RECOVER_RESUME
        assert recovered.recovery_work is not None
        assert (
            recovered.recovery_work.receipt_kind
            is WorkflowRecoveryReceiptKind.RESUME
        )
        assert recovered.recovery_work.receipt_id == "resume-1"


def test_runtime_admission_waiting_and_cancel_are_read_only(tmp_path: Path) -> None:
    waiting_path = tmp_path / "waiting.db"
    with Database.open(waiting_path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        (
            _,
            ticket,
            _,
            start,
            request,
            snapshot,
            _,
            _,
        ) = _seed_started_precreated(uow)
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO decisions("
            "decision_id,run_id,kind,state,request_json,version,created_at) "
            "VALUES('decision-open','run-1','approval','open',?,0,7)",
            (
                canonical_json(
                    {
                        "checkpoint_id": "checkpoint-1",
                        "checkpoint_namespace": request.checkpoint_namespace,
                    }
                ),
            ),
        )
        database.connection.commit()
        waiting = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=8.0,
                ),
            )
        )
        assert waiting.disposition is RuntimeStartDisposition.WAITING
        assert waiting.activation is None and waiting.retry_wake is None

    cancel_path = tmp_path / "cancel.db"
    with Database.open(cancel_path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        (
            _,
            ticket,
            _,
            start,
            request,
            snapshot,
            _,
            dispatch,
        ) = _seed_started_precreated(uow)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.request_cancel(
                    tx,
                    CancelWorkflowRequest("cancel-1", "run-1", "user", 0),
                    1,
                    dispatch.activation,
                    now=8.0,
                ),
            )
        )
        cancelled = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=9.0,
                ),
            )
        )
        assert cancelled.disposition is RuntimeStartDisposition.CANCEL_PENDING
        assert cancelled.activation is None


@pytest.mark.parametrize("now", [9.0, 20.0])
def test_runtime_admission_retry_wait_returns_same_durable_wake(
    tmp_path: Path, now: float
) -> None:
    with Database.open(tmp_path / f"retry-{now}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        (
            _,
            ticket,
            _,
            start,
            request,
            snapshot,
            _,
            _,
        ) = _seed_started_precreated(uow)
        response_payload: dict[str, object] = {}
        response_hash = hashlib.sha256(canonical_json(response_payload).encode()).hexdigest()
        resume_request = {
            "receipt_id": "resume-retry",
            "run_id": "run-1",
            "expected_run_version": 1,
            "expected_checkpoint_head": "checkpoint-1",
            "pending_interrupts": [],
            "responses": response_payload,
            "responses_hash": response_hash,
            "mode": "precreated",
        }
        resume_json = canonical_json(resume_request)
        receipt_version = 2
        due_at = 10.0
        wait_event_id = "retry-wait-event"
        wake_core = {
            "run_id": "run-1",
            "receipt_id": "resume-retry",
            "receipt_version": receipt_version,
            "mode": "precreated",
            "due_at": due_at,
            "wait_event_id": wait_event_id,
            "generic_run_version": 2,
            "request_fingerprint": hashlib.sha256(resume_json.encode()).hexdigest(),
            "responses_hash": response_hash,
            "expected_checkpoint_head": "checkpoint-1",
        }
        outcome_hash = hashlib.sha256(canonical_json(wake_core).encode()).hexdigest()
        database.connection.execute(
            "INSERT INTO workflow_resume_admissions("
            "receipt_id,run_id,request_fingerprint,request_json,mode,"
            "expected_run_version,expected_checkpoint_head,phase,version,"
            "retry_attempt,next_attempt_at,outcome_json,created_at,updated_at) "
            "VALUES('resume-retry','run-1',?,?, 'precreated',1,'checkpoint-1',"
            "'retry_wait',?,1,?,?,7,7)",
            (
                hashlib.sha256(resume_json.encode()).hexdigest(),
                resume_json,
                receipt_version,
                due_at,
                canonical_json(
                    {
                        "status": "retryable",
                        "wait_event_id": wait_event_id,
                        "generic_run_version": 2,
                        "outcome_hash": outcome_hash,
                    }
                ),
            ),
        )
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
        )
        database.connection.execute("DELETE FROM workflow_leases WHERE run_id='run-1'")
        database.connection.execute(
            "UPDATE run_fences SET state='released',released_at=7 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
            "VALUES(?,'run-1',2,'workflow.retry_waiting',?,7)",
            (wait_event_id, canonical_json(wake_core)),
        )
        database.connection.commit()
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=now,
                ),
            )
        )
        assert result.disposition is RuntimeStartDisposition.WAITING
        assert result.activation is None
        assert result.retry_wake is not None
        assert result.retry_wake.outcome_hash == outcome_hash


def test_runtime_admission_terminal_requires_full_durable_outcome(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "terminal.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        (
            _,
            ticket,
            _,
            start,
            request,
            snapshot,
            _,
            _,
        ) = _seed_started_precreated(uow)
        terminal_checkpoint = {"checkpoint_id": "checkpoint-terminal", "status": "completed"}
        checkpoint_json = canonical_json(terminal_checkpoint)
        checkpoint_hash = hashlib.sha256(checkpoint_json.encode()).hexdigest()
        event_payload = {"state": "completed", "checkpoint_id": "checkpoint-terminal"}
        event_json = canonical_json(event_payload)
        delivery_payload = {"type": "workflow.completed", "run_id": "run-1"}
        delivery_facts = [
            {
                "delivery_id": "delivery-terminal",
                "sink_kind": "test",
                "idempotency_key": "terminal:run-1",
                "payload": delivery_payload,
                "created_at": 8.0,
            }
        ]
        terminal_payload = {"result": "ok"}
        terminal_fields = {
            "receipt_id": "terminal-receipt",
            "run_id": "run-1",
            "checkpoint_id": "checkpoint-terminal",
            "state": "completed",
            "event_id": "terminal-event",
            "delivery_ids": ["delivery-terminal"],
            "terminal_payload": terminal_payload,
            "delivery_facts": delivery_facts,
        }
        outcome_hash = hashlib.sha256(
            canonical_json(terminal_fields).encode()
        ).hexdigest()
        database.connection.execute(
            "INSERT INTO workflow_checkpoints("
            "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
            "lease_epoch,version,created_at) VALUES(?,?,?,?,?,1,1,8)",
            (
                "checkpoint-terminal",
                "run-1",
                request.checkpoint_namespace,
                checkpoint_json,
                checkpoint_hash,
            ),
        )
        database.connection.execute(
            "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
            "VALUES('terminal-event','run-1',2,'run.completed',?,8)",
            (event_json,),
        )
        database.connection.execute(
            "INSERT INTO delivery_outbox("
            "delivery_id,run_id,sink_kind,idempotency_key,payload_json,state,version,created_at) "
            "VALUES('delivery-terminal','run-1','test','terminal:run-1',?,'pending',0,8)",
            (canonical_json(delivery_payload),),
        )
        database.connection.execute(
            "INSERT INTO workflow_terminal_fence_receipts("
            "receipt_id,run_id,owner_id,runtime_lease_epoch,run_fence_epoch,created_at) "
            "VALUES('terminal-fence','run-1','owner-a',1,1,8)"
        )
        database.connection.execute("DELETE FROM workflow_leases WHERE run_id='run-1'")
        database.connection.execute(
            "UPDATE run_fences SET state='released',released_at=8 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "UPDATE runs SET state='completed',version=2,updated_at=8 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO workflow_terminal_receipts("
            "receipt_id,run_id,checkpoint_id,checkpoint_namespace,checkpoint_version,"
            "checkpoint_hash,state,run_version,"
            "event_id,event_payload_hash,delivery_ids_json,delivery_facts_json,"
            "terminal_payload_json,terminal_fence_receipt_id,outcome_hash,created_at) "
            "VALUES('terminal-receipt','run-1','checkpoint-terminal',?,1,?,"
            "'completed',2,'terminal-event',?,?,?,?, 'terminal-fence',?,8)",
            (
                request.checkpoint_namespace,
                checkpoint_hash,
                hashlib.sha256(event_json.encode()).hexdigest(),
                canonical_json(["delivery-terminal"]),
                canonical_json(delivery_facts),
                canonical_json(terminal_payload),
                outcome_hash,
            ),
        )
        database.connection.commit()
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=9.0,
                ),
            )
        )
        assert result.disposition is RuntimeStartDisposition.TERMINAL
        assert result.activation is None
        assert result.workflow_terminal is not None
        assert result.workflow_terminal.outcome_hash == outcome_hash

        database.connection.execute(
            "INSERT INTO delivery_outbox("
            "delivery_id,run_id,sink_kind,idempotency_key,payload_json,state,version,created_at) "
            "VALUES('delivery-extra','run-1','test','terminal:extra','{}','pending',0,8)"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=9.0,
                    ),
                )
            )
        database.connection.execute(
            "DELETE FROM delivery_outbox WHERE delivery_id='delivery-extra'"
        )
        database.connection.execute(
            "UPDATE workflow_terminal_fence_receipts SET owner_id='forged' "
            "WHERE receipt_id='terminal-fence'"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=9.0,
                    ),
                )
            )
        database.connection.execute(
            "UPDATE workflow_terminal_fence_receipts SET owner_id='owner-a' "
            "WHERE receipt_id='terminal-fence'"
        )
        database.connection.execute(
            "INSERT INTO workflow_checkpoints("
            "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
            "lease_epoch,version,created_at) VALUES('checkpoint-later','run-1',?,"
            "'{}',?,1,2,9)",
            (request.checkpoint_namespace, hashlib.sha256(b"{}").hexdigest()),
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=9.0,
                    ),
                )
            )
        database.connection.execute(
            "DELETE FROM workflow_checkpoints WHERE checkpoint_id='checkpoint-later'"
        )

        for column, forged, original in (
            ("runtime_lease_epoch", 2, 1),
            ("run_fence_epoch", 2, 1),
        ):
            database.connection.execute(
                f"UPDATE workflow_terminal_fence_receipts SET {column}=? "
                "WHERE receipt_id='terminal-fence'",
                (forged,),
            )
            database.connection.commit()
            with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.admit_runtime_start(
                            tx,
                            ticket,
                            start,
                            request,
                            snapshot,
                            RuntimeActivationClaim(
                                "owner-b", lease_ttl_seconds=10.0
                            ),
                            now=9.0,
                        ),
                    )
                )
            database.connection.execute(
                f"UPDATE workflow_terminal_fence_receipts SET {column}=? "
                "WHERE receipt_id='terminal-fence'",
                (original,),
            )
        for column, forged, original in (
            ("checkpoint_namespace", "evil", request.checkpoint_namespace),
            ("checkpoint_version", 0, 1),
        ):
            database.connection.execute(
                f"UPDATE workflow_terminal_receipts SET {column}=? "
                "WHERE receipt_id='terminal-receipt'",
                (forged,),
            )
            database.connection.commit()
            with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.admit_runtime_start(
                            tx,
                            ticket,
                            start,
                            request,
                            snapshot,
                            RuntimeActivationClaim(
                                "owner-b", lease_ttl_seconds=10.0
                            ),
                            now=9.0,
                        ),
                    )
                )
            database.connection.execute(
                f"UPDATE workflow_terminal_receipts SET {column}=? "
                "WHERE receipt_id='terminal-receipt'",
                (original,),
            )

        database.connection.execute(
            "UPDATE workflow_checkpoints SET checkpoint_json='{}' "
            "WHERE checkpoint_id='checkpoint-terminal'"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=9.0,
                    ),
                )
            )
        database.connection.execute(
            "UPDATE workflow_checkpoints SET checkpoint_json=? "
            "WHERE checkpoint_id='checkpoint-terminal'",
            (checkpoint_json,),
        )
        database.connection.execute(
            "UPDATE run_events SET kind='run.failed' WHERE event_id='terminal-event'"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=9.0,
                    ),
                )
            )
        database.connection.execute(
            "UPDATE run_events SET kind='run.completed' WHERE event_id='terminal-event'"
        )

        database.connection.execute(
            "UPDATE run_events SET payload_json='{}' WHERE event_id='terminal-event'"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="terminal workflow outcome"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim(
                            "owner-b", lease_ttl_seconds=10.0
                        ),
                        now=10.0,
                    ),
                )
            )

        database.connection.execute(
            "UPDATE run_events SET payload_json=? WHERE event_id='terminal-event'",
            (event_json,),
        )
        database.connection.commit()
        terminal_outcome = result.workflow_terminal
        assert terminal_outcome is not None
        json_columns = (
            ("workflow_terminal_receipts", "delivery_ids_json", "{}"),
            ("workflow_terminal_receipts", "delivery_facts_json", "{}"),
            ("workflow_terminal_receipts", "terminal_payload_json", "[]"),
            ("workflow_checkpoints", "checkpoint_json", "[]"),
            ("run_events", "payload_json", "[]"),
            ("delivery_outbox", "payload_json", "[]"),
        )
        for table, column, wrong_container in json_columns:
            identity_column = {
                "workflow_terminal_receipts": "receipt_id",
                "workflow_checkpoints": "checkpoint_id",
                "run_events": "event_id",
                "delivery_outbox": "delivery_id",
            }[table]
            identity = {
                "workflow_terminal_receipts": "terminal-receipt",
                "workflow_checkpoints": "checkpoint-terminal",
                "run_events": "terminal-event",
                "delivery_outbox": "delivery-terminal",
            }[table]
            for invalid in ("{bad", "NaN", wrong_container):
                original = database.connection.execute(
                    f"SELECT {column} FROM {table} WHERE {identity_column}=?",
                    (identity,),
                ).fetchone()[column]
                database.connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {identity_column}=?",
                    (invalid, identity),
                )
                database.connection.commit()
                assert uow.verify_workflow_terminal(terminal_outcome) is False
                database.connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {identity_column}=?",
                    (original, identity),
                )
                database.connection.commit()


def test_admission_transaction_blocks_catalog_publish_until_pinned_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-admission-barrier.db"
    entered = threading.Event()
    release = threading.Event()
    admission_result: list[object] = []
    publisher_result: list[object] = []
    failures: list[BaseException] = []

    with Database.open(path, timeout=5.0) as admission_database:
        admission_uow = SqliteExecutionUnitOfWork(admission_database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(admission_uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        publisher_database = Database.open(path, timeout=5.0)
        publisher_uow = SqliteExecutionUnitOfWork(publisher_database)

        def barrier(label: str) -> None:
            if label == "workflow:runtime_start:before_run_write":
                entered.set()
                if not release.wait(5.0):
                    raise TimeoutError("catalog barrier was not released")

        def admit() -> None:
            try:
                admission_result.append(
                    asyncio.run(
                        _atomic(
                            admission_uow,
                            lambda tx: admission_uow.admit_runtime_start(
                                tx,
                                ticket,
                                start,
                                request,
                                snapshot,
                                RuntimeActivationClaim(
                                    "owner-a", lease_ttl_seconds=10.0
                                ),
                                now=3.0,
                                fault=barrier,
                            ),
                            "barrier-admission",
                        )
                    )
                )
            except BaseException as error:  # noqa: BLE001 - thread evidence
                failures.append(error)

        def publish() -> None:
            try:
                publisher_result.append(
                    asyncio.run(
                        _atomic(
                            publisher_uow,
                            lambda tx: publisher_uow.publish_catalog(
                                tx, _catalog(generation=2, version=2), 1, now=4.0
                            ),
                            "barrier-publish",
                        )
                    )
                )
            except BaseException as error:  # noqa: BLE001 - thread evidence
                failures.append(error)

        admission_thread = threading.Thread(target=admit)
        admission_thread.start()
        assert entered.wait(5.0)
        publisher_thread = threading.Thread(target=publish)
        publisher_thread.start()
        publisher_thread.join(0.05)
        assert publisher_thread.is_alive(), "publisher bypassed BEGIN IMMEDIATE"
        release.set()
        admission_thread.join(5.0)
        publisher_thread.join(5.0)
        publisher_database.close()

    assert not failures
    assert len(admission_result) == len(publisher_result) == 1
    assert admission_result[0].disposition is RuntimeStartDisposition.START_NEW  # type: ignore[union-attr]
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT generation FROM workflow_catalog_authorities"
        ).fetchone()[0] == 2
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runtime_start_receipts"
        ).fetchone()[0] == 1
        uow = SqliteExecutionUnitOfWork(reopened)
        # Receipt-first replays preserve the committed generation-1 authority;
        # catalog generation 2 only rejects previously unadmitted tickets.
        issued = asyncio.run(
            _atomic(uow, lambda tx: uow.read_issued(tx, launch.request_key))
        )
        assert issued is not None
        replayed_ticket = issued[0]
        assert replayed_ticket == ticket
        replayed_admission = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=5.0,
                ),
            )
        )
        assert replayed_admission.receipt == admission_result[0].receipt  # type: ignore[union-attr]


def test_after_commit_response_loss_replays_ticket_and_runtime_admission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "after-commit.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        authority = _catalog()
        asyncio.run(
            _atomic(base, lambda tx: base.publish_catalog(tx, authority, 0, now=1.0))
        )
        issue_authority = asyncio.run(_spawn_issue_authority(base))

        def issue_fault(label: str) -> None:
            if label == "workflow:launch_ticket:after_commit":
                raise RuntimeError(label)

        issue_uow = SqliteExecutionUnitOfWork(database, workflow_fault=issue_fault)
        with pytest.raises(RuntimeError, match="workflow:launch_ticket:after_commit"):
            asyncio.run(
                _atomic(
                    issue_uow,
                    lambda tx: issue_uow.issue(
                        tx,
                        _launch_request(),
                        issue_authority,
                        now=2.0,
                    ),
                    "issue-loss",
                )
            )

    with Database.open(path) as reopened:
        base = SqliteExecutionUnitOfWork(reopened)
        launch = _launch_request()
        issued = asyncio.run(
            _atomic(base, lambda tx: base.read_issued(tx, launch.request_key))
        )
        assert issued is not None
        ticket = issued[0]
        verified = asyncio.run(_atomic(base, lambda tx: base.verify(tx, ticket)))
        start, request, snapshot = _start_inputs(ticket, verified, launch)

        def admission_fault(label: str) -> None:
            if label == "workflow:runtime_start:after_commit":
                raise RuntimeError(label)

        admission_uow = SqliteExecutionUnitOfWork(
            reopened, workflow_fault=admission_fault
        )
        with pytest.raises(RuntimeError, match="workflow:runtime_start:after_commit"):
            asyncio.run(
                _atomic(
                    admission_uow,
                    lambda tx: admission_uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                        now=4.0,
                    ),
                    "admission-loss",
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        launch = _launch_request()
        issued = asyncio.run(
            _atomic(uow, lambda tx: uow.read_issued(tx, launch.request_key))
        )
        assert issued is not None
        ticket = issued[0]
        verified = asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=5.0,
                ),
            )
        )
        assert replay.disposition is RuntimeStartDisposition.START_ORPHAN
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()[0] == 1


def test_ensure_and_recover_after_commit_response_loss_replay_exactly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dispatch-after-commit.db"
    with Database.open(path) as database:
        base = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(base))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admission = asyncio.run(
            _atomic(
                base,
                lambda tx: base.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )

        def ensure_loss(label: str) -> None:
            if label == "workflow:ensure_precreated_start:after_commit":
                raise RuntimeError(label)

        losing = SqliteExecutionUnitOfWork(database, workflow_fault=ensure_loss)
        with pytest.raises(
            RuntimeError, match="workflow:ensure_precreated_start:after_commit"
        ):
            asyncio.run(
                _atomic(
                    losing,
                    lambda tx: losing.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        admission.activation.execution_lease,  # type: ignore[union-attr]
                        admission.activation.run_fence,  # type: ignore[union-attr]
                        admission.dispatch_claim,
                        now=4.0,
                        ttl_seconds=10.0,
                    ),
                    "ensure-loss",
                )
            )

    with Database.open(path) as reopened:
        base = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                base,
                lambda tx: base.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    admission.activation.execution_lease,  # type: ignore[union-attr]
                    admission.activation.run_fence,  # type: ignore[union-attr]
                    admission.dispatch_claim,
                    now=5.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert replay.action is PrecreatedStartAction.NEW_CLAIMED
        assert replay.receipt.claim_action is StartClaimAction.NEW
        assert replay.receipt.version == 0
        reopened.connection.execute(
            "UPDATE workflow_leases SET expires_at=6 WHERE run_id='run-1'"
        )
        reopened.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET expires_at=6 WHERE run_id='run-1'"
        )
        reopened.connection.commit()
        recovery = asyncio.run(
            _atomic(
                base,
                lambda tx: base.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )

        def recover_loss(label: str) -> None:
            if label == "workflow:recover_precreated_start:after_commit":
                raise RuntimeError(label)

        losing = SqliteExecutionUnitOfWork(reopened, workflow_fault=recover_loss)
        with pytest.raises(
            RuntimeError, match="workflow:recover_precreated_start:after_commit"
        ):
            asyncio.run(
                _atomic(
                    losing,
                    lambda tx: losing.recover_precreated_start(
                        tx,
                        recovery.recovery_work,
                        recovery.activation.execution_lease,  # type: ignore[union-attr]
                        recovery.activation.run_fence,  # type: ignore[union-attr]
                        now=20.0,
                        ttl_seconds=10.0,
                    ),
                    "recover-loss",
                )
            )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        renewed_recovery = uow.renew_runtime_lease(
            recovery.activation.execution_lease,  # type: ignore[union-attr]
            now=21.0,
            lease_ttl_seconds=20.0,
        )
        assert renewed_recovery.expires_at == 41.0
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.recover_precreated_start(
                    tx,
                    recovery.recovery_work,
                    recovery.activation.execution_lease,  # type: ignore[union-attr]
                    recovery.activation.run_fence,  # type: ignore[union-attr]
                    now=31.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert result.action is PrecreatedStartAction.RESUME_CLAIMED
        assert result.receipt.claim_action is StartClaimAction.RESUME
        assert result.receipt.claim_epoch == 2
        assert result.receipt.claim_expires_at == 30.0
        assert reopened.connection.execute(
            "SELECT version FROM workflow_start_admissions"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:ensure_precreated_start:before_workflow_leases_write",
        "workflow:ensure_precreated_start:after_workflow_leases_write",
        "workflow:ensure_precreated_start:before_workflow_start_admissions_write",
        "workflow:ensure_precreated_start:after_workflow_start_admissions_write",
        "workflow:ensure_precreated_start:before_runtime_start_dispatch_claims_write",
        "workflow:ensure_precreated_start:after_runtime_start_dispatch_claims_write",
    ],
)
def test_ensure_each_authority_fault_rolls_back_after_reopen(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / f"ensure-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admission = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )

        def fault(label: str) -> None:
            if label == fault_point:
                raise RuntimeError(label)

        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        admission.activation.execution_lease,  # type: ignore[union-attr]
                        admission.activation.run_fence,  # type: ignore[union-attr]
                        admission.dispatch_claim,
                        now=4.0,
                        ttl_seconds=10.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_start_admissions"
        ).fetchone()[0] == 0
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_leases WHERE namespace='native'"
        ).fetchone()[0] == 0
        assert reopened.connection.execute(
            "SELECT state FROM runtime_start_dispatch_claims"
        ).fetchone()[0] == "claimed"


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:recover_precreated_start:before_workflow_leases_write",
        "workflow:recover_precreated_start:after_workflow_leases_write",
        "workflow:recover_precreated_start:before_workflow_start_admissions_write",
        "workflow:recover_precreated_start:after_workflow_start_admissions_write",
    ],
)
def test_recover_each_authority_fault_rolls_back_after_reopen(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / f"recover-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    first.activation.execution_lease,  # type: ignore[union-attr]
                    first.activation.run_fence,  # type: ignore[union-attr]
                    first.dispatch_claim,
                    now=4.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=5 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET expires_at=5 WHERE run_id='run-1'"
        )
        database.connection.commit()
        recovery = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )

        def fault(label: str) -> None:
            if label == fault_point:
                raise RuntimeError(label)

        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.recover_precreated_start(
                        tx,
                        recovery.recovery_work,
                        recovery.activation.execution_lease,  # type: ignore[union-attr]
                        recovery.activation.run_fence,  # type: ignore[union-attr]
                        now=20.0,
                        ttl_seconds=10.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        start_row = reopened.connection.execute(
            "SELECT claim_owner,version FROM workflow_start_admissions"
        ).fetchone()
        assert tuple(start_row) == ("owner-a", 0)
        projection = reopened.connection.execute(
            "SELECT owner_id,epoch FROM workflow_leases WHERE namespace=?",
            (request.checkpoint_namespace,),
        ).fetchone()
        assert tuple(projection) == ("owner-a", 1)


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:launch_ticket:before_receipt_write",
        "workflow:launch_ticket:after_receipt_write",
        "workflow:runtime_start:before_session_write",
        "workflow:runtime_start:after_session_write",
        "workflow:runtime_start:before_run_write",
        "workflow:runtime_start:after_run_write",
        "workflow:runtime_start:before_snapshot_write",
        "workflow:runtime_start:after_snapshot_write",
        "workflow:runtime_start:before_child_link_write",
        "workflow:runtime_start:after_child_link_write",
        "workflow:runtime_start:before_child_command_write",
        "workflow:runtime_start:after_child_command_write",
        "workflow:runtime_start:before_event_write",
        "workflow:runtime_start:after_event_write",
        "workflow:runtime_start:before_runtime_lease_write",
        "workflow:runtime_start:after_runtime_lease_write",
        "workflow:runtime_start:before_run_fence_write",
        "workflow:runtime_start:after_run_fence_write",
        "workflow:runtime_start:before_receipt_write",
        "workflow:runtime_start:after_receipt_write",
        "workflow:runtime_start:before_dispatch_claim_write",
        "workflow:runtime_start:after_dispatch_claim_write",
    ],
)
def test_launch_and_admission_faults_rollback_after_reopen(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / f"fault-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        authority = _catalog()
        asyncio.run(
            _atomic(uow, lambda tx: uow.publish_catalog(tx, authority, 0, now=1.0))
        )
        issue_authority = asyncio.run(_spawn_issue_authority(uow))

        def fault(label: str) -> None:
            if label == fault_point:
                raise RuntimeError(label)

        if "launch_ticket" in fault_point:
            with pytest.raises(RuntimeError, match=fault_point):
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.issue(
                            tx,
                            _launch_request(),
                            issue_authority,
                            now=2.0,
                            fault=fault,
                        ),
                    )
                )
        else:
            launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
            start, request, snapshot = _start_inputs(ticket, verified, launch)
            with pytest.raises(RuntimeError, match=fault_point):
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.admit_runtime_start(
                            tx,
                            ticket,
                            start,
                            request,
                            snapshot,
                            RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                            now=3.0,
                            fault=fault,
                        ),
                    )
                )

    with Database.open(path) as reopened:
        expected_tickets = 0 if "launch_ticket" in fault_point else 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == expected_tickets
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id='run-1'"
        ).fetchone()[0] == 0
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runtime_start_dispatch_claims"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_point",
    [
        "workflow:runtime_start:before_runtime_takeover_write",
        "workflow:runtime_start:after_runtime_takeover_write",
        "workflow:runtime_start:before_fence_takeover_write",
        "workflow:runtime_start:after_fence_takeover_write",
        "workflow:runtime_start:before_dispatch_takeover_write",
        "workflow:runtime_start:after_dispatch_takeover_write",
    ],
)
def test_runtime_takeover_fault_rolls_back_every_authority_after_reopen(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / f"takeover-{fault_point.rsplit(':', 1)[-1]}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        assert first.disposition is RuntimeStartDisposition.START_NEW

        def fault(label: str) -> None:
            if label == fault_point:
                raise RuntimeError(label)

        with pytest.raises(RuntimeError, match=fault_point):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=20.0,
                        fault=fault,
                    ),
                )
            )

    with Database.open(path) as reopened:
        assert tuple(
            reopened.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id='run-1' AND namespace='runtime.kernel'"
            ).fetchone()
        ) == ("owner-a", 1, 13.0)
        assert tuple(
            reopened.connection.execute(
                "SELECT owner_id,epoch,runtime_lease_epoch,released_at "
                "FROM run_fences WHERE run_id='run-1'"
            ).fetchone()
        ) == ("owner-a", 1, 1, None)
        assert tuple(
            reopened.connection.execute(
                "SELECT owner_id,runtime_lease_epoch,claim_epoch,state "
                "FROM runtime_start_dispatch_claims WHERE run_id='run-1'"
            ).fetchone()
        ) == ("owner-a", 1, 1, "claimed")


def test_concurrent_different_runtime_admission_has_one_success_and_zero_extra_runs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "admission-concurrent-different.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
    different_payload = start_admission_request_to_json(request)
    different_payload["checkpoint_namespace"] = "workflow/different"
    different_request = start_admission_request_from_json(different_payload)
    different_snapshot = bind_start_snapshot(
        start,
        profile_key=verified.profile_key,
        driver_kind="workflow",
        workflow_admission=different_request,
    )
    ready = threading.Barrier(2)
    results: list[object] = []
    failures: list[BaseException] = []

    def admit(candidate_request, candidate_snapshot):  # type: ignore[no-untyped-def]
        database = Database.open(path, timeout=5.0)
        try:
            uow = SqliteExecutionUnitOfWork(database)
            ready.wait(5.0)
            results.append(
                asyncio.run(
                    _atomic(
                        uow,
                        lambda tx: uow.admit_runtime_start(
                            tx,
                            ticket,
                            start,
                            candidate_request,
                            candidate_snapshot,
                            RuntimeActivationClaim(
                                "owner", lease_ttl_seconds=10.0
                            ),
                            now=3.0,
                        ),
                    )
                )
            )
        except BaseException as error:  # noqa: BLE001 - concurrency evidence
            failures.append(error)
        finally:
            database.close()

    threads = [
        threading.Thread(target=admit, args=(request, snapshot)),
        threading.Thread(
            target=admit, args=(different_request, different_snapshot)
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], UnitOfWorkConflict)
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runtime_start_receipts"
        ).fetchone()[0] == 1


def test_catalog_publisher_first_is_final_authority_for_admission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-publisher-first.db"
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    with Database.open(path) as setup:
        uow = SqliteExecutionUnitOfWork(setup)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)

    def publish() -> None:
        database = Database.open(path, timeout=5.0)
        try:
            uow = SqliteExecutionUnitOfWork(database)

            def barrier(label: str) -> None:
                if label == "workflow:catalog:before_authority_write":
                    entered.set()
                    if not release.wait(5.0):
                        raise TimeoutError("publisher barrier was not released")

            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.publish_catalog(
                        tx,
                        _catalog(generation=2, version=2),
                        1,
                        now=3.0,
                        fault=barrier,
                    ),
                )
            )
        except BaseException as error:  # noqa: BLE001 - concurrency evidence
            failures.append(error)
        finally:
            database.close()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert entered.wait(5.0)
    admission_database = Database.open(path, timeout=5.0)
    admission_uow = SqliteExecutionUnitOfWork(admission_database)
    admission_failure: list[BaseException] = []

    def admit() -> None:
        try:
            asyncio.run(
                _atomic(
                    admission_uow,
                    lambda tx: admission_uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                        now=4.0,
                    ),
                )
            )
        except BaseException as error:  # noqa: BLE001 - concurrency evidence
            admission_failure.append(error)

    admission = threading.Thread(target=admit)
    admission.start()
    admission.join(0.05)
    assert admission.is_alive(), "admission bypassed publisher BEGIN IMMEDIATE"
    release.set()
    publisher.join(5.0)
    admission.join(5.0)
    admission_database.close()
    assert not failures
    assert len(admission_failure) == 1
    assert isinstance(admission_failure[0], UnitOfWorkConflict)
    with Database.open(path) as reopened:
        assert reopened.connection.execute(
            "SELECT generation FROM workflow_catalog_authorities"
        ).fetchone()[0] == 2
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (start.run_id.value,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("corruption", ["bytes", "ref", "hash"])
def test_durable_schema_reopen_is_only_validation_authority_and_corruption_fails_closed(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"schema-{corruption}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _verified = asyncio.run(_publish_and_issue(uow))
    with Database.open(path) as database:
        row = database.connection.execute(
            "SELECT canonical_profiles FROM workflow_catalog_authorities"
        ).fetchone()
        profiles = json.loads(str(row[0]))
        schema = profiles[0]["start_input_schema"]
        if corruption == "bytes":
            schema["canonical_schema"]["properties"]["objective"]["type"] = "integer"
        elif corruption == "ref":
            schema["schema_ref"] = "schema://forged/v1"
        else:
            schema["schema_hash"] = "0" * 64
        database.connection.execute(
            "UPDATE workflow_catalog_authorities SET canonical_profiles=?",
            (canonical_json(profiles),),
        )
        database.connection.commit()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        with pytest.raises((ValueError, UnitOfWorkConflict)):
            asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?",
            (launch.requested_run_id,),
        ).fetchone()[0] == 0
        assert launch.start_input["objective"] == "finish the durable task"


def test_fresh_runner_uses_durable_ticket_schema_and_compiled_binding_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-fresh-runner.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, _verified = asyncio.run(_publish_and_issue(uow))
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        verified = asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        start = RunStart(
            execution_session_id=ExecutionSessionId(verified.session_id),
            run_id=RunId(verified.resolved_run_id),
            request_id=RequestId(verified.request_id),
            turn_id=verified.turn_id,
            input=launch.start_input,
            tool_catalog_generation=verified.tool_catalog_generation,
        )
        request = _runner().prepare_start_admission(verified, start)
        snapshot = bind_start_snapshot(
            start,
            profile_key=verified.profile_key,
            driver_kind="workflow",
            workflow_admission=request,
        )
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        assert result.disposition is RuntimeStartDisposition.START_NEW
        invalid_origin = WorkflowSpawnOrigin(
            parent_run_id="parent-run",
            parent_request_id="parent-request",
            turn_id="turn-1",
            internal_tool_call_id="raw-spawn-call-invalid",
        )
        invalid_operation_id = workflow_spawn_operation_id(invalid_origin)
        issue_authority = asyncio.run(
            _spawn_issue_authority(
                uow,
                raw_call_id=invalid_origin.internal_tool_call_id,
                effect_id="spawn-effect-invalid",
                call_id="spawn-call-invalid",
                arguments={
                    "profile_key": "workflow.durable_task",
                    "objective": "finish the durable task",
                    "start_input": {"objective": 7},
                    "candidate_id": "candidate-1",
                },
            )
        )
        with pytest.raises(UnitOfWorkConflict, match="durable schema"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.issue(
                        tx,
                        _launch_request(
                            spawn_origin=invalid_origin,
                            request_key=invalid_operation_id,
                            requested_run_id="invalid-run",
                            child_command_id=workflow_spawn_child_command_id(
                                invalid_operation_id
                            ),
                            start_input={"objective": 7},
                        ),
                        issue_authority,
                        now=4.0,
                    ),
                )
            )
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == 1

        missing = _runner(WorkflowRegistry())
        with pytest.raises(Exception, match="graph version unavailable"):
            missing.prepare_start_admission(verified, start)


@pytest.mark.parametrize(
    "run_state", ["created", "admission_pending", "queued"]
)
def test_runtime_admission_rejects_non_running_pre_dispatch_states_without_writes(
    tmp_path: Path, run_state: str
) -> None:
    path = tmp_path / f"state-{run_state}.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        database.connection.execute(
            "UPDATE runs SET state=? WHERE run_id='run-1'", (run_state,)
        )
        database.connection.commit()
        before = tuple(
            database.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id='run-1' AND namespace='runtime.kernel'"
            ).fetchone()
        )
        with pytest.raises(UnitOfWorkConflict, match="requires RUNNING"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("foreign", lease_ttl_seconds=10.0),
                        now=20.0,
                    ),
                )
            )
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id='run-1' AND namespace='runtime.kernel'"
            ).fetchone()
        ) == before


@pytest.mark.parametrize(
    "run_state", ["created", "admission_pending", "queued"]
)
def test_precreated_dispatch_rejects_non_running_run_before_workflow_writes(
    tmp_path: Path, run_state: str
) -> None:
    with Database.open(tmp_path / f"dispatch-state-{run_state}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admission = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        database.connection.execute(
            "UPDATE runs SET state=? WHERE run_id='run-1'", (run_state,)
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="generic Run identity"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        admission.activation.execution_lease,  # type: ignore[union-attr]
                        admission.activation.run_fence,  # type: ignore[union-attr]
                        admission.dispatch_claim,
                        now=4.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_start_admissions"
        ).fetchone()[0] == 0


def test_precreated_recovery_rechecks_running_state_before_projection_write(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "recover-non-running.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        (_launch, ticket, _verified, start, request, snapshot, _, _) = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=5 WHERE run_id='run-1'"
        )
        database.connection.commit()
        recovery = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )
        database.connection.execute(
            "UPDATE runs SET state='queued' WHERE run_id='run-1'"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="requires RUNNING"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.recover_precreated_start(
                        tx,
                        recovery.recovery_work,
                        recovery.activation.execution_lease,  # type: ignore[union-attr]
                        recovery.activation.run_fence,  # type: ignore[union-attr]
                        now=20.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        assert database.connection.execute(
            "SELECT version FROM workflow_start_admissions"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_run_id", "requested-run-forged"),
        ("requested_trace_id", "requested-trace-forged"),
        ("requested_thread_id", "requested-thread-forged"),
        ("resolved_run_id", "resolved-run-forged"),
        ("resolved_trace_id", "resolved-trace-forged"),
        ("resolved_thread_id", "resolved-thread-forged"),
        ("start_input_schema_ref", "schema://forged/v1"),
        ("start_input_schema_hash", "0" * 64),
        ("terminal_request_factory_hash", "f" * 64),
    ],
)
def test_every_new_start_binding_field_is_independently_pinned_before_generic_write(
    tmp_path: Path, field: str, value: str
) -> None:
    with Database.open(tmp_path / f"binding-{field}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, _snapshot = _start_inputs(ticket, verified, launch)
        request_json = start_admission_request_to_json(request)
        request_json[field] = value
        changed = start_admission_request_from_json(request_json)
        changed_snapshot = bind_start_snapshot(
            start,
            profile_key=verified.profile_key,
            driver_kind="workflow",
            workflow_admission=changed,
        )
        with pytest.raises(UnitOfWorkConflict, match="verified launch ticket"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        changed,
                        changed_snapshot,
                        RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                        now=3.0,
                    ),
                )
            )
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id<>'parent-run'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("bound", "mutation"),
    [
        (False, "runtime_owner"),
        (False, "runtime_epoch"),
        (False, "runtime_expiry"),
        (False, "fence_owner"),
        (False, "fence_runtime_epoch"),
        (False, "dispatch_owner"),
        (False, "dispatch_runtime_epoch"),
        (False, "dispatch_expiry"),
        (True, "workflow_owner"),
        (True, "workflow_epoch"),
        (True, "workflow_expiry"),
    ],
)
@pytest.mark.parametrize("claim_owner", ["owner-a", "foreign-owner"])
def test_live_dispositions_fail_closed_on_every_split_authority(
    tmp_path: Path, bound: bool, mutation: str, claim_owner: str
) -> None:
    with Database.open(tmp_path / f"split-{bound}-{mutation}-{claim_owner}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admission = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        if bound:
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        admission.activation.execution_lease,  # type: ignore[union-attr]
                        admission.activation.run_fence,  # type: ignore[union-attr]
                        admission.dispatch_claim,
                        now=4.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        statements = {
            "runtime_owner": (
                (
                    "UPDATE workflow_leases SET owner_id='split' WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "runtime_epoch": (
                (
                    "UPDATE workflow_leases SET epoch=2 WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "runtime_expiry": (
                (
                    "UPDATE workflow_leases SET expires_at=12 WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "fence_owner": (
                "UPDATE run_fences SET owner_id='split' WHERE run_id='run-1'",
                (),
            ),
            "fence_runtime_epoch": (
                "UPDATE run_fences SET runtime_lease_epoch=2 WHERE run_id='run-1'",
                (),
            ),
            "dispatch_owner": (
                (
                    "UPDATE runtime_start_dispatch_claims SET owner_id='split' "
                    "WHERE run_id='run-1'"
                ),
                (),
            ),
            "dispatch_runtime_epoch": (
                (
                    "UPDATE runtime_start_dispatch_claims SET runtime_lease_epoch=2 "
                    "WHERE run_id='run-1'"
                ),
                (),
            ),
            "dispatch_expiry": (
                (
                    "UPDATE runtime_start_dispatch_claims SET expires_at=12 "
                    "WHERE run_id='run-1'"
                ),
                (),
            ),
            "workflow_owner": (
                (
                    "UPDATE workflow_leases SET owner_id='split' WHERE run_id='run-1' "
                    "AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
            "workflow_epoch": (
                (
                    "UPDATE workflow_leases SET epoch=2 WHERE run_id='run-1' "
                    "AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
            "workflow_expiry": (
                (
                    "UPDATE workflow_leases SET expires_at=12 WHERE run_id='run-1' "
                    "AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
        }
        statement, parameters = statements[mutation]
        database.connection.execute(statement, parameters)
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="split-brain|not claimable"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim(
                            claim_owner, lease_ttl_seconds=10.0
                        ),
                        now=5.0,
                    ),
                )
            )


def test_waiting_rejects_wrong_namespace_and_unlinked_decision_or_blocker(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "waiting-pinned.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        (_launch, ticket, _verified, start, request, snapshot, _, _) = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO workflow_checkpoints("
            "checkpoint_id,run_id,namespace,checkpoint_json,checkpoint_hash,"
            "lease_epoch,version,created_at) VALUES('evil-head','run-1','evil',"
            "'{}',?,1,0,7)",
            (hashlib.sha256(b"{}").hexdigest(),),
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,version,created_at) "
            "VALUES('evil-decision','run-1','approval','open',?,0,7)",
            (
                canonical_json(
                    {
                        "checkpoint_id": "evil-head",
                        "checkpoint_namespace": "evil",
                    }
                ),
            ),
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="matching workflow authority"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                        now=8.0,
                    ),
                )
            )
        database.connection.execute(
            "INSERT INTO run_wait_blockers("
            "blocker_id,run_id,kind,ledger_identity,handoff_attempt,observed_version,"
            "wake_consumed,created_at,version) "
            "VALUES('blocker-unlinked','run-1','tool','effect-x',1,1,0,7,1)"
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="matching workflow authority"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                        now=8.0,
                    ),
                )
            )
        linked_checkpoint = canonical_json(
            {
                "checkpoint_id": "checkpoint-1",
                "status": "running",
                "values": {},
                "wait_blocker_ids": ["blocker-unlinked"],
            }
        )
        database.connection.execute(
            "UPDATE workflow_checkpoints SET checkpoint_json=?,checkpoint_hash=? "
            "WHERE checkpoint_id='checkpoint-1'",
            (
                linked_checkpoint,
                hashlib.sha256(linked_checkpoint.encode()).hexdigest(),
            ),
        )
        database.connection.commit()
        waiting = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner", lease_ttl_seconds=10.0),
                    now=8.0,
                ),
            )
        )
        assert waiting.disposition is RuntimeStartDisposition.WAITING


def test_start_input_schema_enforces_all_frozen_resource_bounds_before_publish(
    tmp_path: Path,
) -> None:
    cyclic: dict[str, object] = {"type": "object", "properties": {}}
    cyclic["self"] = cyclic
    deep: dict[str, object] = {"type": "string"}
    for _ in range(13):
        deep = {
            "type": "object",
            "properties": {"nested": deep},
            "additionalProperties": False,
        }
    too_many_nodes = {
        "type": "object",
        "properties": {
            f"p{i}": {"type": "string", "enum": ["a", "b", "c"]}
            for i in range(64)
        },
        "additionalProperties": False,
    }
    invalid_schemas: list[tuple[object, str, bool]] = [
        ({"type": "object", "description": "x" * 32769}, "string exceeds", True),
        (
            {"type": "string", "enum": ["x" * 1000 for _ in range(40)]},
            "byte limit",
            True,
        ),
        (deep, "depth limit", True),
        (too_many_nodes, "node limit", True),
        (
            {
                "type": "object",
                "properties": {f"p{i}": {"type": "string"} for i in range(65)},
                "additionalProperties": False,
            },
            "property limit",
            True,
        ),
        (
            {"type": "string", "enum": [str(i) for i in range(65)]},
            "enum limit",
            True,
        ),
        (
            {
                "type": "object",
                "properties": {},
                "required": [f"p{i}" for i in range(257)],
            },
            "required limit",
            True,
        ),
        ({"type": "string", "const": "x" * 4097}, "string exceeds", True),
        ({"type": "number", "default": float("inf")}, "finite", False),
        ({"type": "object", "$ref": "https://invalid/schema"}, "unsupported", True),
        ({"type": "string", "pattern": ".*"}, "unsupported", True),
        (cyclic, "cyclic", False),
    ]
    with Database.open(tmp_path / "invalid-schema.db") as database:
        for schema, reason, canonicalizable in invalid_schemas:
            schema_hash = (
                hashlib.sha256(canonical_json(schema).encode()).hexdigest()
                if canonicalizable
                else "0" * 64
            )
            with pytest.raises(ValueError, match=reason):
                StartInputSchema(
                    "schema://invalid/v1",
                    schema,  # type: ignore[arg-type]
                    schema_hash,
                )
        with pytest.raises(ValueError, match="schema_ref"):
            StartInputSchema("x" * 4097, {"type": "object"}, "0" * 64)
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_catalog_authorities"
        ).fetchone()[0] == 0
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "start_input",
    [
        {"objective": "x" * 65537},
        {
            "objective": "valid",
            "nested": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": 1}}}}}}}}}}}}},
        },
    ],
)
def test_actual_start_input_resource_limits_reject_before_ticket_write(
    tmp_path: Path, start_input: dict[str, object]
) -> None:
    with Database.open(tmp_path / "invalid-actual-input.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.publish_catalog(tx, _catalog(), 0, now=1.0),
            )
        )
        with pytest.raises(ValueError, match="byte|depth"):
            _launch_request(start_input=start_input)  # type: ignore[arg-type]
        assert database.connection.execute(
            "SELECT COUNT(*) FROM workflow_launch_ticket_receipts"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("execution_session_id", "session-other"),
        ("request_id", "request-other"),
        ("root_run_id", "root-other"),
        ("parent_run_id", None),
        ("profile_key", "workflow.personal_v1"),
        ("driver_kind", "react"),
        ("state", "reserved_fork"),
    ],
)
def test_existing_runtime_admission_rejects_every_generic_run_identity_drift(
    tmp_path: Path, column: str, value: str | None
) -> None:
    with Database.open(tmp_path / f"generic-identity-{column}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        if column == "execution_session_id":
            database.connection.execute(
                "INSERT INTO execution_sessions(session_id,created_at) VALUES('session-other',3)"
            )
        database.connection.execute(
            f"UPDATE runs SET {column}=? WHERE run_id='run-1'",
            (value,),
        )
        database.connection.commit()
        before = database.connection.total_changes
        with pytest.raises(UnitOfWorkConflict, match="generic Run identity|RUNNING"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=4.0,
                    ),
                )
            )
        assert database.connection.total_changes == before


def test_consumed_dispatch_columns_are_audit_only_for_attach_and_takeover(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "consumed-audit-classification.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified, start, request, snapshot, admitted, _dispatch = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET owner_id='historical',"
            "runtime_lease_epoch=77,expires_at=1 WHERE run_id='run-1'"
        )
        database.connection.commit()
        attached = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=5.0,
                ),
            )
        )
        assert attached.disposition is RuntimeStartDisposition.ATTACH_CURRENT
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=6 WHERE run_id='run-1'"
        )
        database.connection.commit()
        recovered = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=20.0,
                ),
            )
        )
        assert recovered.disposition is RuntimeStartDisposition.RECOVER_START
        assert tuple(
            database.connection.execute(
                "SELECT owner_id,runtime_lease_epoch,expires_at FROM "
                "runtime_start_dispatch_claims WHERE run_id='run-1'"
            ).fetchone()
        ) == ("historical", 77, 1.0)
        assert admitted.receipt == recovered.receipt


def test_expired_runtime_cannot_take_over_a_live_foreign_workflow_projection(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "foreign-projection.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified, start, request, snapshot, _admitted, _dispatch = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=5 WHERE run_id='run-1' "
            "AND namespace='runtime.kernel'"
        )
        database.connection.execute(
            "UPDATE workflow_leases SET owner_id='foreign',expires_at=100 "
            "WHERE run_id='run-1' AND namespace=?",
            (request.checkpoint_namespace,),
        )
        database.connection.commit()
        before = tuple(
            database.connection.execute(
                "SELECT namespace,owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id='run-1' ORDER BY namespace"
            ).fetchall()
        )
        with pytest.raises(UnitOfWorkConflict, match="workflow projection"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=20.0,
                    ),
                )
            )
        assert tuple(
            database.connection.execute(
                "SELECT namespace,owner_id,epoch,expires_at FROM workflow_leases "
                "WHERE run_id='run-1' ORDER BY namespace"
            ).fetchall()
        ) == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("candidate_id", "candidate-forged"),
        ("profile_key", "workflow.personal_v1"),
        ("catalog_generation", 2),
        ("catalog_authority_version", 2),
        ("catalog_hash", "0" * 64),
        ("session_id", "session-forged"),
        ("request_id", "request-forged"),
        ("turn_id", "turn-forged"),
        ("requested_run_id", "requested-run-forged"),
        ("requested_trace_id", "requested-trace-forged"),
        ("requested_thread_id", "requested-thread-forged"),
        ("resolved_run_id", "resolved-run-forged"),
        ("resolved_trace_id", "resolved-trace-forged"),
        ("resolved_thread_id", "resolved-thread-forged"),
        ("tool_catalog_generation", 8),
        ("objective_hash", "1" * 64),
        ("start_input_hash", "2" * 64),
    ],
)
def test_ticket_verify_recomputes_every_duplicated_sql_column(
    tmp_path: Path, column: str, value: object
) -> None:
    with Database.open(tmp_path / f"ticket-column-{column}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified = asyncio.run(_publish_and_issue(uow))
        database.connection.execute(
            f"UPDATE workflow_launch_ticket_receipts SET {column}=?",
            (value,),
        )
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="ticket|catalog"):
            asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, ticket)))
        assert database.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id<>'parent-run'"
        ).fetchone()[0] == 0


def test_schema_raw_preflight_rejects_extreme_depth_without_recursion_error() -> None:
    deep: dict[str, object] = {"type": "string"}
    for _ in range(1500):
        deep = {
            "type": "object",
            "properties": {"nested": deep},
            "additionalProperties": False,
        }
    with pytest.raises(ValueError, match="depth limit"):
        StartInputSchema("schema://deep/v1", deep, "0" * 64)  # type: ignore[arg-type]


def test_registered_workflow_and_typed_dispatch_results_are_immutable_and_bound(
    tmp_path: Path,
) -> None:
    entry = WorkflowRegistry((_COMPILED,)).require("durable_task", "1")
    with pytest.raises(FrozenInstanceError):
        entry.workflow = _COMPILED

    with Database.open(tmp_path / "typed-dispatch.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        *_prefix, dispatch = _seed_started_precreated(uow)
        assert dispatch.activation is not None
        with pytest.raises(ValueError, match="action|phase"):
            PrecreatedStartDispatch(
                PrecreatedStartAction.RESUME_RUNNING,
                dispatch.receipt,
                dispatch.activation,
            )
        wrong_namespace = replace(
            dispatch.activation,
            workflow_lease=replace(
                dispatch.activation.workflow_lease,
                namespace="workflow/other/name/1",
            ),
        )
        with pytest.raises(ValueError, match="namespace"):
            PrecreatedStartDispatch(
                PrecreatedStartAction.NEW_CLAIMED,
                dispatch.receipt,
                wrong_namespace,
            )
        with pytest.raises(ValueError, match="activation differs"):
            replace(dispatch.receipt, claim_owner="forged-owner")
        with pytest.raises(ValueError, match="activation differs"):
            replace(dispatch.receipt, claim_epoch=2)
        with pytest.raises(ValueError, match="activation differs"):
            replace(dispatch.receipt, claim_expires_at=99.0)
        with pytest.raises(ValueError, match="advance"):
            replace(dispatch.receipt, claim_action=StartClaimAction.RESUME)
        with pytest.raises(ValueError, match="co-fenced"):
            replace(
                dispatch.activation,
                workflow_lease=replace(
                    dispatch.activation.workflow_lease,
                    runtime_lease_epoch=2,
                ),
            )

    resume_request = ResumeAdmissionRequest(
        "resume-1", "run-1", 1, "checkpoint-1", (), {}, hashlib.sha256(b"{}").hexdigest(), StartMode.PRECREATED
    )
    resume_receipt = ResumeAdmissionReceipt(
        resume_request,
        "b" * 64,
        ResumePhase.RETRY_WAIT,
        2,
        next_attempt_at=20.0,
    )
    with pytest.raises(ValueError, match="due"):
        WorkflowRecoveryWork(
            "run-1",
            WorkflowRecoveryReceiptKind.RESUME,
            "resume-1",
            2,
            StartMode.PRECREATED,
            21.0,
            "b" * 64,
            resume_receipt,
        )


def test_start_claim_receipt_first_settled_replays_after_terminal_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "start-claim-settled-replay.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        first = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    admitted.activation.execution_lease,  # type: ignore[union-attr]
                    admitted.activation.run_fence,  # type: ignore[union-attr]
                    admitted.dispatch_claim,
                    now=4.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        stale_recovery = WorkflowRecoveryWork(
            first.receipt.run_id,
            WorkflowRecoveryReceiptKind.START,
            first.receipt.request_key,
            first.receipt.version,
            StartMode.PRECREATED,
            None,
            first.receipt.request_fingerprint,
            first.receipt,
        )
        outcome = canonical_json({"state": "completed", "value": "ok"})
        database.connection.execute(
            "UPDATE workflow_start_admissions SET phase='settled',version=1,"
            "outcome_json=?,updated_at=8 WHERE request_key=?",
            (outcome, request.request_key),
        )
        database.connection.execute(
            "UPDATE runs SET state='completed',version=version+1,updated_at=8 "
            "WHERE run_id=?",
            (first.receipt.run_id,),
        )
        database.connection.execute(
            "DELETE FROM workflow_leases WHERE run_id=?", (first.receipt.run_id,)
        )
        database.connection.execute(
            "UPDATE run_fences SET state='released',released_at=8 WHERE run_id=?",
            (first.receipt.run_id,),
        )
        database.connection.execute(
            "DELETE FROM runtime_start_dispatch_claims WHERE run_id=?",
            (first.receipt.run_id,),
        )
        database.connection.commit()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    admitted.activation.execution_lease,  # type: ignore[union-attr]
                    admitted.activation.run_fence,  # type: ignore[union-attr]
                    replace(admitted.dispatch_claim, run_id="ignored-after-settle"),
                    now=20.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert replay.action is PrecreatedStartAction.SETTLED
        assert replay.activation is None
        assert replay.receipt.claim_action is StartClaimAction.NEW
        assert replay.receipt.claim_epoch == 1
        assert replay.receipt.claim_expires_at == 13.0
        assert replay.serialized_outcome == {"state": "completed", "value": "ok"}
        recovered_replay = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.recover_precreated_start(
                    tx,
                    stale_recovery,
                    admitted.activation.execution_lease,  # type: ignore[union-attr]
                    admitted.activation.run_fence,  # type: ignore[union-attr]
                    now=20.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        assert recovered_replay == replay


def test_start_claim_replay_rejects_forged_audit_expiry_after_heartbeat_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "start-claim-forged-expiry.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
        start, request, snapshot = _start_inputs(ticket, verified, launch)
        admitted = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                    now=3.0,
                ),
            )
        )
        asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.ensure_and_bind_precreated_start(
                    tx,
                    request,
                    admitted.activation.execution_lease,  # type: ignore[union-attr]
                    admitted.activation.run_fence,  # type: ignore[union-attr]
                    admitted.dispatch_claim,
                    now=4.0,
                    ttl_seconds=10.0,
                ),
            )
        )
        renewed = uow.renew_runtime_lease(
            admitted.activation.execution_lease,  # type: ignore[union-attr]
            now=5.0,
            lease_ttl_seconds=20.0,
        )
        database.connection.execute(
            "UPDATE workflow_start_admissions SET claim_expires_at=99 "
            "WHERE request_key=?",
            (request.request_key,),
        )
        database.connection.commit()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        before = tuple(
            reopened.connection.execute(
                "SELECT phase,version,claim_action,claim_owner,claim_epoch,"
                "claim_expires_at FROM workflow_start_admissions"
            ).fetchone()
        )
        with pytest.raises(UnitOfWorkConflict, match="start claim authority is stale"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.ensure_and_bind_precreated_start(
                        tx,
                        request,
                        renewed,
                        admitted.activation.run_fence,  # type: ignore[union-attr]
                        admitted.dispatch_claim,
                        now=7.0,
                        ttl_seconds=10.0,
                    ),
                )
            )
        assert tuple(
            reopened.connection.execute(
                "SELECT phase,version,claim_action,claim_owner,claim_epoch,"
                "claim_expires_at FROM workflow_start_admissions"
            ).fetchone()
        ) == before


def test_start_claim_schema_rejects_action_phase_and_version_drift(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "start-claim-schema.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        *_prefix, dispatch = _seed_started_precreated(uow)
        assert dispatch.receipt.claim_action is StartClaimAction.NEW
        for statement in (
            "UPDATE workflow_start_admissions SET version=0,claim_action='resume'",
            "UPDATE workflow_start_admissions SET phase='admitted'",
            "UPDATE workflow_start_admissions SET phase='settled',outcome_json=NULL",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.connection.execute(statement)
            database.connection.rollback()


@pytest.mark.parametrize(
    ("run_state", "start_phase", "resume_phase", "dispatch_state"),
    [
        ("running", None, None, "consumed"),
        ("running", "claimed", None, "claimed"),
        ("running", "admitted", None, "consumed"),
        ("running", "settled", None, "consumed"),
        ("running", "running", "retry_wait", "consumed"),
        ("waiting", "claimed", None, "consumed"),
        ("waiting", "running", "admitted", "consumed"),
        ("cancel_requested", "claimed", None, "consumed"),
    ],
)
def test_generic_state_start_resume_dispatch_matrix_fails_closed(
    tmp_path: Path,
    run_state: str,
    start_phase: str | None,
    resume_phase: str | None,
    dispatch_state: str,
) -> None:
    with Database.open(
        tmp_path / f"phase-{run_state}-{start_phase}-{resume_phase}-{dispatch_state}.db"
    ) as database:
        uow = SqliteExecutionUnitOfWork(database)
        if start_phase is None:
            launch, ticket, verified = asyncio.run(_publish_and_issue(uow))
            start, request, snapshot = _start_inputs(ticket, verified, launch)
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-a", lease_ttl_seconds=10.0),
                        now=3.0,
                    ),
                )
            )
        else:
            (
                _launch,
                ticket,
                _verified,
                start,
                request,
                snapshot,
                _admitted,
                _dispatch,
            ) = _seed_started_precreated(uow)
            if start_phase == "admitted":
                database.connection.execute(
                    "UPDATE workflow_start_admissions SET phase='admitted',"
                    "claim_action=NULL,claim_owner=NULL,claim_epoch=NULL,"
                    "claim_expires_at=NULL WHERE run_id='run-1'"
                )
            elif start_phase == "settled":
                database.connection.execute(
                    "UPDATE workflow_start_admissions SET phase='settled',"
                    "outcome_json='{}' WHERE run_id='run-1'"
                )
            else:
                database.connection.execute(
                    "UPDATE workflow_start_admissions SET phase=? WHERE run_id='run-1'",
                    (start_phase,),
                )
        database.connection.execute(
            "UPDATE runs SET state=? WHERE run_id='run-1'", (run_state,)
        )
        database.connection.execute(
            "UPDATE runtime_start_dispatch_claims SET state=? WHERE run_id='run-1'",
            (dispatch_state,),
        )
        if resume_phase is not None:
            resume_payload = {
                "receipt_id": "resume-invalid-phase",
                "run_id": "run-1",
                "expected_run_version": 1,
                "expected_checkpoint_head": "checkpoint-1",
                "pending_interrupts": [],
                "responses": {},
                "responses_hash": hashlib.sha256(b"{}").hexdigest(),
                "mode": "precreated",
            }
            resume_json = canonical_json(resume_payload)
            database.connection.execute(
                "INSERT INTO workflow_resume_admissions("
                "receipt_id,run_id,request_fingerprint,request_json,mode,"
                "expected_run_version,expected_checkpoint_head,phase,version,"
                "next_attempt_at,outcome_json,created_at,updated_at) "
                "VALUES('resume-invalid-phase','run-1',?,?,'precreated',1,"
                "'checkpoint-1',?,1,20,'{}',4,4)",
                (
                    hashlib.sha256(resume_json.encode()).hexdigest(),
                    resume_json,
                    resume_phase,
                ),
            )
        database.connection.commit()
        before = database.connection.total_changes
        with pytest.raises(
            UnitOfWorkConflict, match="phase|RUNNING|orphan|dispatch|cancel authority"
        ):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=5.0,
                    ),
                )
            )
        assert database.connection.total_changes == before


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime_owner",
        "runtime_epoch",
        "runtime_expiry",
        "fence_owner",
        "fence_runtime_epoch",
        "workflow_owner",
        "workflow_epoch",
        "workflow_expiry",
        "missing_runtime",
        "missing_workflow",
    ],
)
def test_waiting_validates_active_or_released_authority_matrix(
    tmp_path: Path, mutation: str
) -> None:
    with Database.open(tmp_path / f"waiting-authority-{mutation}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified, start, request, snapshot, _admitted, _dispatch = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,version,created_at) "
            "VALUES('decision-open','run-1','approval','open',?,0,7)",
            (
                canonical_json(
                    {
                        "checkpoint_id": "checkpoint-1",
                        "checkpoint_namespace": request.checkpoint_namespace,
                    }
                ),
            ),
        )
        statements = {
            "runtime_owner": (
                (
                    "UPDATE workflow_leases SET owner_id='split' "
                    "WHERE run_id='run-1' AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "runtime_epoch": (
                (
                    "UPDATE workflow_leases SET epoch=2 WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "runtime_expiry": (
                (
                    "UPDATE workflow_leases SET expires_at=7 WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "fence_owner": (
                "UPDATE run_fences SET owner_id='split' WHERE run_id='run-1'",
                (),
            ),
            "fence_runtime_epoch": (
                "UPDATE run_fences SET runtime_lease_epoch=2 WHERE run_id='run-1'",
                (),
            ),
            "workflow_owner": (
                (
                    "UPDATE workflow_leases SET owner_id='split' "
                    "WHERE run_id='run-1' AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
            "workflow_epoch": (
                (
                    "UPDATE workflow_leases SET epoch=2 WHERE run_id='run-1' "
                    "AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
            "workflow_expiry": (
                (
                    "UPDATE workflow_leases SET expires_at=7 WHERE run_id='run-1' "
                    "AND namespace=?"
                ),
                (request.checkpoint_namespace,),
            ),
            "missing_runtime": (
                (
                    "DELETE FROM workflow_leases WHERE run_id='run-1' "
                    "AND namespace='runtime.kernel'"
                ),
                (),
            ),
            "missing_workflow": (
                "DELETE FROM workflow_leases WHERE run_id='run-1' AND namespace=?",
                (request.checkpoint_namespace,),
            ),
        }
        statement, parameters = statements[mutation]
        database.connection.execute(statement, parameters)
        database.connection.commit()
        with pytest.raises(UnitOfWorkConflict, match="WAITING|split-brain"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=8.0,
                    ),
                )
            )


def test_waiting_accepts_fully_released_authority_without_reactivation(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "waiting-released.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified, start, request, snapshot, _admitted, _dispatch = (
            _seed_started_precreated(uow)
        )
        database.connection.execute(
            "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
        )
        database.connection.execute("DELETE FROM workflow_leases WHERE run_id='run-1'")
        database.connection.execute(
            "UPDATE run_fences SET state='released',released_at=7 WHERE run_id='run-1'"
        )
        database.connection.execute(
            "INSERT INTO decisions(decision_id,run_id,kind,state,request_json,version,created_at) "
            "VALUES('decision-open','run-1','approval','open',?,0,7)",
            (
                canonical_json(
                    {
                        "checkpoint_id": "checkpoint-1",
                        "checkpoint_namespace": request.checkpoint_namespace,
                    }
                ),
            ),
        )
        database.connection.commit()
        result = asyncio.run(
            _atomic(
                uow,
                lambda tx: uow.admit_runtime_start(
                    tx,
                    ticket,
                    start,
                    request,
                    snapshot,
                    RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                    now=8.0,
                ),
            )
        )
        assert result.disposition is RuntimeStartDisposition.WAITING
        assert result.activation is None


def test_prepare_catalog_rejects_registry_replace_during_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WorkflowRegistry((_COMPILED,))
    runner = _runner(registry)
    entered = threading.Event()
    release = threading.Event()
    original = runner._catalog_binding

    def paused_binding(entry, registration):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(5.0)
        return original(entry, registration)

    monkeypatch.setattr(runner, "_catalog_binding", paused_binding)
    outcomes: list[object] = []
    failures: list[BaseException] = []

    def prepare() -> None:
        try:
            outcomes.append(runner.prepare_catalog_authority(1, (_registration(),)))
        except BaseException as error:  # noqa: BLE001 - thread evidence
            failures.append(error)

    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(5.0)
    registry.register(_COMPILED_REPLACEMENT, replace=True)
    release.set()
    thread.join(5.0)
    assert not outcomes
    assert len(failures) == 1
    assert "registry changed" in str(failures[0])


@pytest.mark.parametrize(
    "payload_field",
    [
        "resolved_run_id",
        "resolved_trace_id",
        "resolved_thread_id",
        "objective_hash",
        "start_input_hash",
    ],
)
def test_ticket_verify_rejects_rehashed_canonical_payload_derived_drift(
    tmp_path: Path, payload_field: str
) -> None:
    with Database.open(tmp_path / f"ticket-payload-{payload_field}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _launch, ticket, _verified = asyncio.run(_publish_and_issue(uow))
        row = database.connection.execute(
            "SELECT canonical_payload FROM workflow_launch_ticket_receipts"
        ).fetchone()
        payload = json.loads(str(row["canonical_payload"]))
        payload[payload_field] = "0" * 64
        canonical_payload = canonical_json(payload)
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        ticket_id = hashlib.sha256(
            f"simple-harness.workflow.workflow-launch/ticket/v1|{payload_hash}".encode()
        ).hexdigest()
        database.connection.execute(
            "UPDATE workflow_launch_ticket_receipts SET canonical_payload=?,"
            "payload_hash=?,ticket_id=?",
            (canonical_payload, payload_hash, ticket_id),
        )
        database.connection.commit()
        forged_public = replace(ticket, payload_hash=payload_hash)
        with pytest.raises(UnitOfWorkConflict, match="ticket"):
            asyncio.run(_atomic(uow, lambda tx: uow.verify(tx, forged_public)))


def _seed_runtime_retry_wait(uow: SqliteExecutionUnitOfWork):  # type: ignore[no-untyped-def]
    _launch, ticket, _verified, start, request, snapshot, _admitted, _dispatch = (
        _seed_started_precreated(uow)
    )
    responses: dict[str, object] = {}
    responses_hash = hashlib.sha256(canonical_json(responses).encode()).hexdigest()
    resume_payload = {
        "receipt_id": "resume-retry",
        "run_id": "run-1",
        "expected_run_version": 1,
        "expected_checkpoint_head": "checkpoint-1",
        "pending_interrupts": [],
        "responses": responses,
        "responses_hash": responses_hash,
        "mode": "precreated",
    }
    resume_json = canonical_json(resume_payload)
    request_fingerprint = hashlib.sha256(resume_json.encode()).hexdigest()
    wake_core = {
        "run_id": "run-1",
        "receipt_id": "resume-retry",
        "receipt_version": 2,
        "mode": "precreated",
        "due_at": 10.0,
        "wait_event_id": "retry-wait-event",
        "generic_run_version": 2,
        "request_fingerprint": request_fingerprint,
        "responses_hash": responses_hash,
        "expected_checkpoint_head": "checkpoint-1",
    }
    outcome_hash = hashlib.sha256(canonical_json(wake_core).encode()).hexdigest()
    connection = uow.database.connection
    connection.execute(
        "INSERT INTO workflow_resume_admissions("
        "receipt_id,run_id,request_fingerprint,request_json,mode,"
        "expected_run_version,expected_checkpoint_head,phase,version,"
        "retry_attempt,next_attempt_at,outcome_json,created_at,updated_at) "
        "VALUES('resume-retry','run-1',?,?,'precreated',1,'checkpoint-1',"
        "'retry_wait',2,1,10,?,7,7)",
        (
            request_fingerprint,
            resume_json,
            canonical_json(
                {
                    "status": "retryable",
                    "wait_event_id": "retry-wait-event",
                    "generic_run_version": 2,
                    "outcome_hash": outcome_hash,
                }
            ),
        ),
    )
    connection.execute(
        "UPDATE runs SET state='waiting',version=2 WHERE run_id='run-1'"
    )
    connection.execute("DELETE FROM workflow_leases WHERE run_id='run-1'")
    connection.execute(
        "UPDATE run_fences SET state='released',released_at=7 WHERE run_id='run-1'"
    )
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "VALUES('retry-wait-event','run-1',2,'workflow.retry_waiting',?,7)",
        (canonical_json(wake_core),),
    )
    connection.commit()
    return ticket, start, request, snapshot


@pytest.mark.parametrize(
    "mutation",
    [
        "request_json",
        "request_fingerprint",
        "expected_head_column",
        "next_attempt_at",
        "outcome_json",
        "event_payload",
        "checkpoint_hash",
    ],
)
def test_retry_wake_recomputes_full_resume_snapshot_before_return(
    tmp_path: Path, mutation: str
) -> None:
    with Database.open(tmp_path / f"retry-binding-{mutation}.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        ticket, start, request, snapshot = _seed_runtime_retry_wait(uow)
        if mutation == "request_json":
            row = database.connection.execute(
                "SELECT request_json FROM workflow_resume_admissions"
            ).fetchone()
            payload = json.loads(str(row["request_json"]))
            payload["responses"] = {"forged": True}
            database.connection.execute(
                "UPDATE workflow_resume_admissions SET request_json=?",
                (canonical_json(payload),),
            )
        elif mutation == "request_fingerprint":
            database.connection.execute(
                "UPDATE workflow_resume_admissions SET request_fingerprint=?",
                ("0" * 64,),
            )
        elif mutation == "expected_head_column":
            database.connection.execute(
                "UPDATE workflow_resume_admissions SET expected_checkpoint_head='forged'"
            )
        elif mutation == "next_attempt_at":
            database.connection.execute(
                "UPDATE workflow_resume_admissions SET next_attempt_at=11"
            )
        elif mutation == "outcome_json":
            database.connection.execute(
                "UPDATE workflow_resume_admissions SET outcome_json='{}'"
            )
        elif mutation == "event_payload":
            database.connection.execute(
                "UPDATE run_events SET payload_json='{}' WHERE event_id='retry-wait-event'"
            )
        else:
            database.connection.execute(
                "UPDATE workflow_checkpoints SET checkpoint_hash=? "
                "WHERE checkpoint_id='checkpoint-1'",
                ("0" * 64,),
            )
        database.connection.commit()
        before = database.connection.total_changes
        with pytest.raises(UnitOfWorkConflict, match="retry|resume|checkpoint"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=20.0,
                    ),
                )
            )
        assert database.connection.total_changes == before


def test_retry_wait_never_dispatches_from_incorrect_running_state_even_when_due(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "retry-running.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        ticket, start, request, snapshot = _seed_runtime_retry_wait(uow)
        database.connection.execute(
            "UPDATE runs SET state='running' WHERE run_id='run-1'"
        )
        database.connection.commit()
        before = database.connection.total_changes
        with pytest.raises(UnitOfWorkConflict, match="resume phase"):
            asyncio.run(
                _atomic(
                    uow,
                    lambda tx: uow.admit_runtime_start(
                        tx,
                        ticket,
                        start,
                        request,
                        snapshot,
                        RuntimeActivationClaim("owner-b", lease_ttl_seconds=10.0),
                        now=20.0,
                    ),
                )
            )
        assert database.connection.total_changes == before


@pytest.mark.parametrize("shape", ["depth", "cycle", "nodes", "bytes"])
def test_actual_start_input_resource_preflight_is_iterative(shape: str) -> None:
    if shape == "depth":
        value: dict[str, object] = {"leaf": True}
        for _ in range(1500):
            value = {"nested": value}
    elif shape == "cycle":
        value = {}
        value["self"] = value
    elif shape == "nodes":
        value = {"items": list(range(1100))}
    else:
        value = {"payload": "x" * 65537}
    with pytest.raises(ValueError, match="depth|cyclic|node|byte"):
        _launch_request(start_input=value)


@pytest.mark.parametrize("mutation", ["manifest", "definition", "node_map"])
def test_registered_workflow_sealed_integrity_rejects_internal_mutation(
    mutation: str,
) -> None:
    source = _fresh_compiled()
    registry = WorkflowRegistry((source,))
    entry = registry.require("durable_task", "1")
    if mutation == "manifest":
        object.__setattr__(entry.workflow.manifest, "workflow_name", "forged")
    elif mutation == "definition":
        object.__setattr__(entry.workflow.definition, "name", "forged")
    else:
        object.__setattr__(entry.workflow, "_nodes", {})
    runner = _runner(registry)
    with pytest.raises(WorkflowDependencyUnavailable, match="integrity"):
        runner.prepare_catalog_authority(1, (_registration(),))
    with pytest.raises(WorkflowDependencyUnavailable, match="integrity"):
        entry.materialize(
            store=None,  # type: ignore[arg-type]
            terminal_projection_port=None,  # type: ignore[arg-type]
            terminal_commit_projection_port=None,  # type: ignore[arg-type]
            progress_port=None,
            observer_port=None,
        )


def test_registry_owns_a_sealed_copy_independent_from_source_mutation() -> None:
    source = _fresh_compiled()
    registry = WorkflowRegistry((source,))
    entry = registry.require("durable_task", "1")
    assert entry.workflow is not source
    object.__setattr__(source.definition, "name", "forged")
    authority = _runner(registry).prepare_catalog_authority(1, (_registration(),))
    assert authority.authority.profiles[0].workflow_name == "durable_task"


def test_catalog_snapshot_rechecks_sealed_integrity_after_concurrent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WorkflowRegistry((_fresh_compiled(),))
    runner = _runner(registry)
    entry = registry.require("durable_task", "1")
    entered = threading.Event()
    release = threading.Event()
    original = runner._catalog_binding

    def paused_binding(item, registration):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(5.0)
        return original(item, registration)

    monkeypatch.setattr(runner, "_catalog_binding", paused_binding)
    failures: list[BaseException] = []

    def prepare() -> None:
        try:
            runner.prepare_catalog_authority(1, (_registration(),))
        except BaseException as error:  # noqa: BLE001 - thread evidence
            failures.append(error)

    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(5.0)
    object.__setattr__(entry.workflow.definition, "name", "forged")
    release.set()
    thread.join(5.0)
    assert len(failures) == 1
    assert isinstance(failures[0], WorkflowDependencyUnavailable)
