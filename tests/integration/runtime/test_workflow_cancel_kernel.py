# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from simple_harness.contracts import (
    CallId,
    EffectId,
    ExecutionSessionId,
    RequestId,
    RunId,
    canonical_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution import effect_request_hash
from simple_harness.execution.budget import (
    BudgetCharge,
    BudgetChargeKind,
    BudgetPolicy,
)
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
    provider_invocation_id,
    provider_request_fingerprint,
    provider_request_json,
    provider_response_json,
)
from simple_harness.execution.recovery import (
    RecoveryKind,
    ResolutionOutcome,
    WaitBlockerSpec,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderRequest, ProviderResponse, ProviderTarget
from simple_harness.runtime import (
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    WorkflowRuntimeDriver,
    build_runtime,
)
from simple_harness.runtime.orchestration import (
    RuntimeActivationClaim,
    StartInputSchema,
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
from simple_harness.runtime.start_snapshot import RunStart, bind_start_snapshot
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.compiler import compile_workflow
from simple_harness.workflow.contracts import StatePatch
from simple_harness.workflow.definition import Edge, NodeDefinition, WorkflowDefinition
from simple_harness.workflow.execution_ports import (
    CheckpointExecutionAdapter,
    WorkflowExecutionPorts,
)
from simple_harness.workflow.runner import (
    WorkflowRegistry,
    WorkflowRunner,
)


class NoopPort:
    async def reconcile(self) -> None:
        return None


class Catalog:
    def current_generation(self) -> int:
        return 1


class NoBlobReferences:
    async def validate_references(self, transaction, *, blob_refs, **values):  # type: ignore[no-untyped-def]
        del transaction, blob_refs, values


class RecoveryPolicy:
    pass


class Trace:
    async def append(self, event, *, transaction):  # type: ignore[no-untyped-def]
        del event, transaction


class LegacyProjection:
    def project_public(self, workflow_name, workflow_version, raw, engine_status):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, raw, engine_status


class NoCommitProjection:
    def lookup(self, workflow_name, workflow_version, descriptor):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, descriptor


async def _never(_state, _context):  # type: ignore[no-untyped-def]
    await asyncio.Event().wait()
    return StatePatch({})


def _fixture(path: Path):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow, runner, runtime = _runtime_for(database)
    schema_value = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    schema_ref = "schema://kernel-cancel/v1"
    descriptor = ProfileDescriptor(
        key="agent.general",
        description="Kernel cancellation fixture workflow.",
        use_when="The root Run is the durable workflow fixture.",
        avoid_when="The fixture is not under test.",
        input_schema_ref=schema_ref,
        generation=1,
        fingerprint=profile_descriptor_fingerprint(
            "agent.general",
            "Kernel cancellation fixture workflow.",
            "The root Run is the durable workflow fixture.",
            "The fixture is not under test.",
            schema_ref,
            1,
        ),
    )
    registration = WorkflowProfileRegistration(
        descriptor=descriptor,
        workflow_name="kernel-cancel",
        workflow_version="1",
        start_input_schema=StartInputSchema(
            schema_ref,
            schema_value,
            hashlib.sha256(canonical_json(schema_value).encode()).hexdigest(),
        ),
    )
    authority = runner.prepare_catalog_authority(1, (registration,))
    origin = WorkflowSpawnOrigin(
        parent_run_id="kernel-cancel-parent",
        parent_request_id="kernel-cancel-parent-request",
        turn_id="turn",
        internal_tool_call_id="kernel-cancel-spawn-raw",
    )
    operation_id = workflow_spawn_operation_id(origin)
    launch = WorkflowLaunchRequest(
        request_key=operation_id,
        candidate_id="kernel-cancel-candidate",
        profile_key="agent.general",
        catalog_generation=1,
        session_id="session",
        request_id="request",
        turn_id="turn",
        requested_run_id="kernel-cancel-run",
        requested_trace_id="kernel-cancel-trace",
        requested_thread_id="kernel-cancel-thread",
        tool_catalog_generation=1,
        objective="exercise durable cancellation",
        start_input={},
        spawn_origin=origin,
        root_run_id="kernel-cancel-parent",
        attachment_policy=AttachmentPolicy.ATTACHED,
        child_command_id=workflow_spawn_child_command_id(operation_id),
    )

    async def admit():
        await uow.run_atomic(
            lambda tx: uow.publish_catalog(tx, authority, 0, now=0.0),
            fault_label="kernel-cancel:publish",
        )
        parent_start = RunStart(
            execution_session_id=ExecutionSessionId("session"),
            run_id=RunId(origin.parent_run_id),
            request_id=RequestId(origin.parent_request_id),
            turn_id=origin.turn_id,
            input={"objective": "parent"},
            tool_catalog_generation=1,
        )
        parent_snapshot = bind_start_snapshot(
            parent_start,
            profile_key="agent.general",
            driver_kind="react",
        )
        uow.create_with_start_snapshot(
            execution_session_id="session",
            run_id=origin.parent_run_id,
            request_id=origin.parent_request_id,
            profile_key="agent.general",
            driver_kind="react",
            snapshot=parent_snapshot.to_json(),
            event_id="kernel-cancel-parent:created",
            now=0.1,
        )
        _, parent_lease = uow.claim_runtime_activation(
            run_id=origin.parent_run_id,
            owner_id="runtime-owner",
            namespace="runtime.kernel",
            now=0.2,
            lease_ttl_seconds=30.0,
        )
        parent_fence = await uow.acquire(RunId(origin.parent_run_id), parent_lease, now=0.2)
        react_checkpoint = {
            "schema_version": 1,
            "started_at": 0.2,
            "last_observed_at": 0.2,
            "provider_turns_reserved_total": 1,
            "tool_calls_reserved_total": 1,
            "repeat_key": None,
            "repeat_streak": 1,
            "phase": "tool_batch_reserved",
            "provider_request_id": "kernel-cancel-provider-request",
            "tool_batch_id": "kernel-cancel-tool-batch",
            "context_revision": 0,
            "provider_request_snapshot": {},
            "provider_request_fingerprint": "kernel-cancel-provider-fingerprint",
            "provider_response_snapshot": {},
            "provider_response_digest": hashlib.sha256(b"{}").hexdigest(),
            "tool_result_progress": 0,
        }
        react_checkpoint_hash = hashlib.sha256(
            canonical_json(react_checkpoint).encode()
        ).hexdigest()
        uow.cas_react_checkpoint(
            run_id=origin.parent_run_id,
            lease=parent_lease,
            expected_version=None,
            checkpoint=react_checkpoint,
            checkpoint_hash=react_checkpoint_hash,
            now=0.3,
        )
        effect_arguments = {
            "profile_key": launch.profile_key,
            "objective": launch.objective,
            "start_input": {},
            "candidate_id": launch.candidate_id,
        }
        request_hash = effect_request_hash(tool_name="workflow_spawn", arguments=effect_arguments)
        effect = uow.prepare_effect(
            effect_id=EffectId("kernel-cancel-spawn-effect"),
            run_id=RunId(origin.parent_run_id),
            call_id=CallId("kernel-cancel-spawn-call"),
            raw_call_id=origin.internal_tool_call_id,
            tool_name="workflow_spawn",
            arguments=effect_arguments,
            request_hash=request_hash,
            authorization_receipt_ref="kernel-cancel-spawn-authorization",
            run_fence=parent_fence,
            execution_lease=parent_lease,
            now=0.4,
        )
        effect = uow.mark_effect_handed_off(
            effect.effect_id,
            expected_version=effect.version,
            run_fence=parent_fence,
            handoff_receipt_ref="kernel-cancel-spawn-handoff",
            execution_lease=parent_lease,
            now=0.5,
        )
        issue_authority = WorkflowSpawnIssueAuthority(
            react_checkpoint_revision=0,
            execution_lease=parent_lease,
            run_fence=parent_fence,
            workflow_lease=None,
            effect_id=effect.effect_id.value,
            effect_handoff_attempt=effect.handoff_attempt,
            effect_request_hash=effect.request_hash,
        )
        ticket = await uow.run_atomic(
            lambda tx: uow.issue(tx, launch, issue_authority, now=0.6),
            fault_label="kernel-cancel:issue",
        )
        verified = await uow.run_atomic(
            lambda tx: uow.verify(tx, ticket),
            fault_label="kernel-cancel:verify",
        )
        start = RunStart(
            ExecutionSessionId(verified.session_id),
            RunId(verified.resolved_run_id),
            RequestId(verified.request_id),
            verified.turn_id,
            launch.start_input,
            verified.tool_catalog_generation,
        )
        request = runner.prepare_start_admission(verified, start)
        snapshot = bind_start_snapshot(
            start,
            profile_key=verified.profile_key,
            driver_kind="workflow",
            workflow_admission=request,
        )
        admission = await uow.run_atomic(
            lambda tx: uow.admit_runtime_start(
                tx,
                ticket,
                start,
                request,
                snapshot,
                RuntimeActivationClaim("runtime-owner", lease_ttl_seconds=30.0),
                now=1.0,
            ),
            fault_label="kernel-cancel:runtime-admit",
        )
        assert admission.activation is not None
        assert admission.dispatch_claim is not None
        await runner.start_precreated(
            request=request,
            execution_lease=admission.activation.execution_lease,
            run_fence=admission.activation.run_fence,
            dispatch_claim=admission.dispatch_claim,
        )
        run = uow.read_run(admission.receipt.run_id)
        assert run is not None
        return run, admission.activation

    run, activation = asyncio.run(admit())
    return (
        database,
        uow,
        runtime,
        runner,
        run.run_id,
        activation.execution_lease,
        activation.run_fence,
    )


def _runtime_for(database: Database):  # type: ignore[no-untyped-def]
    uow = SqliteExecutionUnitOfWork(database)
    compiled = compile_workflow(
        WorkflowDefinition(
            "kernel-cancel",
            "1",
            1,
            "node",
            (NodeDefinition("node", _never),),
            {},
            5,
            4,
            edges=(Edge("node", "__end__"),),
        )
    )
    execution_ports = WorkflowExecutionPorts(
        uow, CheckpointExecutionAdapter(database), uow, uow, uow
    )
    runner = WorkflowRunner(
        registry=WorkflowRegistry((compiled,)),
        checkpoint=SqliteNativeCheckpointStore(execution_ports, blob_references=NoBlobReferences()),
        recovery=RecoveryPolicy(),  # type: ignore[arg-type]
        trace=Trace(),
        execution_ports=execution_ports,
        terminal_projection_port=LegacyProjection(),
        terminal_commit_projection_port=NoCommitProjection(),
        owner="runtime-owner",
        clock=lambda: 2.0,
    )
    noop = NoopPort()
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "workflow")},
        {},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database, clock=lambda: 2.0),
            delivery=noop,
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=Catalog(),
            owner_id="runtime-owner",
            clock=lambda: 2.0,
        ),
        workflow_runner=runner,
    )
    return uow, runner, runtime


def _seed_unknown_provider(
    uow: SqliteExecutionUnitOfWork,
    runner: WorkflowRunner,
    run_id: str,
    lease,  # type: ignore[no-untyped-def]
    fence,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    current = uow.read_run(run_id)
    assert current is not None

    async def bind(tx):  # type: ignore[no-untyped-def]
        return await runner.execution_ports.lifecycle.bind_activation(
            tx,
            run_id,
            current.version,
            lease,
            fence,
            now=2.0,
            ttl_seconds=30.0,
        )

    activation = asyncio.run(uow.run_atomic(bind, fault_label="test:bind"))
    request = ProviderRequest(
        RequestId("cancel-provider-request"),
        (Message(MessageRole.USER, "cancel while provider is uncertain"),),
        max_output_tokens=10,
    )
    target = ProviderTarget("fixture", "model", "fixture:model", "local", "fixture")
    record = ProviderInvocationRecord.claimed(
        invocation_id=provider_invocation_id(RunId(run_id), request.request_id),
        run_id=RunId(run_id),
        request_id=request.request_id,
        request_fingerprint=provider_request_fingerprint(request),
        target=target,
        estimator_snapshot=None,
        estimator_digest=None,
        reservation=BudgetCharge.unknown(),
        claimed_at=2.0,
        request_json=provider_request_json(request),
    )
    record = uow.claim_provider_invocation(
        record, budget_policy=BudgetPolicy(), execution_lease=lease
    )
    record = uow.hand_off_provider_invocation(
        record.invocation_id,
        expected_version=record.version,
        handed_off_at=2.1,
        execution_lease=lease,
        workflow_lease=activation.workflow_lease,
    )
    record = uow.settle_provider_invocation(
        record.settle_unknown(error_code="transport_lost", at=2.2, expected_version=record.version),
        expected_version=record.version,
    )
    assert record.state is ProviderInvocationState.UNKNOWN
    waiting, blocker = uow.commit_runtime_wait_with_blocker(
        run_id=run_id,
        expected_version=current.version,
        event_id=f"{run_id}:waiting:provider",
        payload={"reason": "provider_outcome_unknown"},
        blocker=WaitBlockerSpec(
            RecoveryKind.PROVIDER,
            record.invocation_id,
            record.handoff_attempt,
            record.version,
        ),
        lease=lease,
        now=2.3,
    )
    assert waiting.state is RunState.WAITING
    return record, blocker


def test_runtime_rejects_host_override_and_fake_official_driver(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "identity.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    ports = RuntimePorts(
        provider=noop,
        tools=noop,
        authorization=noop,
        context=SqliteContextPort(database, clock=lambda: 1.0),
        delivery=noop,
        tool_reconciliation=noop,
        reconciliation=noop,
        provider_reconciliation=noop,
        react_checkpoint=uow,
        tool_catalog=Catalog(),
    )
    with pytest.raises(ValueError, match="reserved"):
        build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "workflow")},
            {"workflow": object()},  # type: ignore[dict-item]
            ports,
        )
    with pytest.raises(TypeError, match="identity"):
        build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "workflow")},
            {},
            ports,
            workflow_runner=object(),
        )
    with pytest.raises(TypeError, match="factory"):
        WorkflowRuntimeDriver(object(), _token=object())  # type: ignore[arg-type]
    assert database.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    database.close()


def test_kernel_workflow_cancel_is_receipt_owned_and_reopens_exactly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kernel-cancel.db"
    database, uow, runtime, _runner, run_id, _lease, _fence = _fixture(path)

    async def first():  # type: ignore[no-untyped-def]
        await runtime.start()
        cancelled = await runtime.client.cancel(RunId(run_id))
        assert cancelled.state is RunState.CANCELLED
        await runtime.close()

    asyncio.run(first())
    row = database.connection.execute(
        "SELECT cancel_id,generation FROM workflow_cancel_receipts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert uow.verify_workflow_cancel_terminal(
        run_id=run_id, cancel_id=row["cancel_id"], generation=row["generation"]
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.cancelled'",
            (run_id,),
        ).fetchone()[0]
        == 1
    )
    database.close()

    with Database.open(path) as reopened:
        assert SqliteExecutionUnitOfWork(reopened).verify_workflow_cancel_terminal(
            run_id=run_id, cancel_id=row["cancel_id"], generation=row["generation"]
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )


def _cancel_with_unknown_provider(path: Path):  # type: ignore[no-untyped-def]
    database, uow, runtime, runner, run_id, lease, fence = _fixture(path)
    record, blocker = _seed_unknown_provider(uow, runner, run_id, lease, fence)

    async def cancel_once():  # type: ignore[no-untyped-def]
        await runtime.start()
        cancelling = await runtime.client.cancel(RunId(run_id))
        assert cancelling.state is RunState.CANCEL_REQUESTED
        await runtime.close()

    asyncio.run(cancel_once())
    receipt = database.connection.execute(
        "SELECT cancel_id,generation,phase,terminal FROM workflow_cancel_receipts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(receipt) == (receipt["cancel_id"], 0, "cancelling", None)
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='run.cancelled'",
            (run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.cancelled'",
            (run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM workflow_leases WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    database.close()
    return record, blocker, run_id, str(receipt["cancel_id"])


def test_cancel_requested_database_start_replays_receipt_and_stays_nonterminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cancel-requested-recovery.db"
    _record, _blocker, run_id, cancel_id = _cancel_with_unknown_provider(path)

    with Database.open(path) as reopened:
        uow, _runner, runtime = _runtime_for(reopened)

        async def recover_once():  # type: ignore[no-untyped-def]
            await runtime.start()
            assert uow.read_run(run_id).state is RunState.CANCEL_REQUESTED  # type: ignore[union-attr]
            await runtime.close()

        asyncio.run(recover_once())
        receipt = reopened.connection.execute(
            "SELECT cancel_id,generation,phase,terminal FROM workflow_cancel_receipts "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert tuple(receipt) == (cancel_id, 0, "cancelling", None)
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM workflow_leases WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )


def test_unknown_blocker_late_completion_converges_once_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cancel-late-resolution.db"
    record, blocker, run_id, cancel_id = _cancel_with_unknown_provider(path)

    with Database.open(path) as reopened:
        uow, _runner, runtime = _runtime_for(reopened)

        async def start_unresolved():  # type: ignore[no-untyped-def]
            await runtime.start()
            assert uow.read_run(run_id).state is RunState.CANCEL_REQUESTED  # type: ignore[union-attr]

        asyncio.run(start_unresolved())
        response = ProviderResponse(
            record.request_id,
            Message(MessageRole.ASSISTANT, "durable late completion"),
        )
        settled = uow.record_provider_reconciliation(
            record,
            outcome=ResolutionOutcome.COMPLETED,
            response_json=provider_response_json(response),
            usage_json={"usage": None, "budget": BudgetCharge.unknown().to_json()},
            budget_charge=BudgetCharge(BudgetChargeKind.TRUSTED_USAGE, 1, "fixture-price-v1"),
            evidence_ref="fixture:late-completed",
            now=4.0,
        )
        assert settled.state is ProviderInvocationState.SUCCEEDED
        assert (
            reopened.connection.execute(
                "SELECT resolution_id FROM run_wait_blockers WHERE blocker_id=?",
                (blocker.blocker_id,),
            ).fetchone()["resolution_id"]
            is not None
        )

        async def converge():  # type: ignore[no-untyped-def]
            await runtime.reconcile()
            assert uow.read_run(run_id).state is RunState.CANCELLED  # type: ignore[union-attr]
            await runtime.reconcile()
            await runtime.close()

        asyncio.run(converge())
        assert uow.verify_workflow_cancel_terminal(run_id=run_id, cancel_id=cancel_id, generation=0)
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='run.cancelled'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='workflow.cancelled'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
