# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.workflow import (
    CapabilityBuildHostServices,
    CheckpointExecutionAdapter,
    WorkflowContext,
    WorkflowExecutionPorts,
    WorkflowHostServices,
    WorkflowRegistry,
    WorkflowRunner,
    compile_workflow_registration,
)
from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
from simple_harness.workflow.execution_ports import StartAdmissionRequest, StartMode
from simple_harness.workflow.recovery import RecoveryDecision, RecoveryDisposition
from simple_harness.workflows.capability_build import (
    CapabilityBuildAdmission,
    CapabilityBuildExecutionState,
    build_capability_build_registration,
    run_capability_build_specialization,
)


class _Search:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def search(self, *, query, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key
        self.calls.append("search")
        return {"source": "https://example.invalid/capability", "candidate": query}


class _SourcePolicy:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def authorize_source(self, *, source, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key
        self.calls.append("source_policy")
        return {"allowed": True, "source": source, "policy_receipt": "policy-1"}


class _Build:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def build(self, *, candidate, source_policy, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key
        self.calls.append("isolated_build")
        return {"package": {"name": candidate, "validated": True}}


class _Store:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def store(self, *, package, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key
        self.calls.append("package_store")
        assert package["validated"] is True
        return {"package_ref": "pkg://capability/sha256:abc"}


class _Activate:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def activate(self, *, package_ref, activation_key, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key == activation_key
        self.calls.append("activate")
        return {
            "active": True,
            "package_ref": package_ref,
            "activation_key": activation_key,
            "receipt_id": "activation-1",
        }


class _Authorization:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def authorize_build(self, *, operation_key, admission):  # type: ignore[no-untyped-def]
        assert operation_key
        self.calls.append("authorization")
        return {"allowed": True, "receipt_id": "authorization-1"}


class _Proposal:
    async def propose(self, state):  # type: ignore[no-untyped-def]
        return state


class _Workspace:
    async def execute_tools(self, calls, **kwargs):  # type: ignore[no-untyped-def]
        del calls, kwargs
        return {}


class _BlobReferences:
    async def validate_references(self, transaction, *, blob_refs, **values):  # type: ignore[no-untyped-def]
        del transaction, blob_refs, values


class _Recovery:
    def classify(self, error, *, attempt, max_attempts):  # type: ignore[no-untyped-def]
        del error, attempt, max_attempts
        return RecoveryDecision(RecoveryDisposition.FAIL, "node_failed", None)

    async def quarantine(self, **values):  # type: ignore[no-untyped-def]
        del values

    async def recover_expired(self, **values):  # type: ignore[no-untyped-def]
        del values
        return ()

    async def repair_head(self, checkpoint, *, transaction):  # type: ignore[no-untyped-def]
        del checkpoint, transaction


class _Trace:
    async def append(self, event, *, transaction):  # type: ignore[no-untyped-def]
        del event, transaction


class _TerminalProjection:
    def project_public(self, workflow_name, workflow_version, raw, engine_status):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, raw, engine_status
        return None


class _TerminalCommitProjection:
    def lookup(self, workflow_name, workflow_version, descriptor):  # type: ignore[no-untyped-def]
        del workflow_name, workflow_version, descriptor
        return None


def _services(calls: list[str]) -> WorkflowHostServices:
    return WorkflowHostServices(
        capability_build=CapabilityBuildHostServices(
            proposal=_Proposal(),
            workspace=_Workspace(),
            search=_Search(calls),
            source_policy=_SourcePolicy(calls),
            isolated_build=_Build(calls),
            package_store=_Store(calls),
            activate=_Activate(calls),
            authorization=_Authorization(calls),
        )
    )


def _runner(
    path: Path,
    calls: list[str],
    *,
    host_services: WorkflowHostServices | None = None,
    workflow_fault=None,  # type: ignore[no-untyped-def]
    clock=lambda: 10.0,
):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database, workflow_fault=workflow_fault)
    ports = WorkflowExecutionPorts(uow, CheckpointExecutionAdapter(database), uow, uow, uow)
    registry = WorkflowRegistry(transaction_owner=uow.transaction_owner)
    registration = build_capability_build_registration(
        generation=1, transaction_owner=uow.transaction_owner
    )
    registry.register_definition(registration)
    runner = WorkflowRunner(
        registry=registry,
        checkpoint=SqliteNativeCheckpointStore(ports, blob_references=_BlobReferences()),
        recovery=_Recovery(),
        trace=_Trace(),
        execution_ports=ports,
        terminal_projection_port=_TerminalProjection(),
        terminal_commit_projection_port=_TerminalCommitProjection(),
        host_services=host_services or _services(calls),
        owner="capability-build-test",
        clock=clock,
    )
    return database, uow, registration, runner


class _IdempotentPhysicalHost:
    def __init__(self) -> None:
        self.last_stage: str | None = None
        self.attempts: dict[str, int] = {}
        self.physical: dict[str, int] = {}
        self.receipts: dict[str, dict[str, object]] = {}

    async def propose(self, state):  # type: ignore[no-untyped-def]
        return state

    async def execute_tools(self, calls, **kwargs):  # type: ignore[no-untyped-def]
        del calls, kwargs
        return {}

    def _effect(self, stage: str, key: str, result: dict[str, object]):
        self.last_stage = stage
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        existing = self.receipts.get(key)
        if existing is not None:
            return existing
        self.physical[stage] = self.physical.get(stage, 0) + 1
        receipt = {**result, "operation_key": key, "receipt_id": f"receipt:{key}"}
        self.receipts[key] = receipt
        return receipt

    async def authorize_build(self, *, operation_key, admission):  # type: ignore[no-untyped-def]
        del admission
        return self._effect("authorization", operation_key, {"allowed": True})

    async def search(self, *, query, operation_key, admission):  # type: ignore[no-untyped-def]
        del admission
        return self._effect(
            "search",
            operation_key,
            {"source": "https://example.invalid/capability", "candidate": query},
        )

    async def authorize_source(self, *, source, operation_key, admission):  # type: ignore[no-untyped-def]
        del admission
        return self._effect("source_policy", operation_key, {"allowed": True, "source": source})

    async def build(self, *, candidate, source_policy, operation_key, admission):  # type: ignore[no-untyped-def]
        del source_policy, admission
        return self._effect(
            "isolated_build",
            operation_key,
            {"package": {"name": candidate, "validated": True}},
        )

    async def store(self, *, package, operation_key, admission):  # type: ignore[no-untyped-def]
        del package, admission
        return self._effect(
            "package_store",
            operation_key,
            {"package_ref": "pkg://capability/sha256:abc"},
        )

    async def activate(self, *, package_ref, activation_key, operation_key, admission):  # type: ignore[no-untyped-def]
        del admission
        assert operation_key == activation_key
        return self._effect(
            "activate",
            operation_key,
            {"active": True, "package_ref": package_ref, "activation_key": activation_key},
        )


def _idempotent_services(host: _IdempotentPhysicalHost) -> WorkflowHostServices:
    return WorkflowHostServices(
        capability_build=CapabilityBuildHostServices(
            proposal=host,
            workspace=host,
            search=host,
            source_policy=host,
            isolated_build=host,
            package_store=host,
            activate=host,
            authorization=host,
        )
    )


async def _native_config(
    uow: SqliteExecutionUnitOfWork,
    registration,  # type: ignore[no-untyped-def]
    run_id: str,
) -> dict[str, object]:
    request = StartAdmissionRequest(
        request_key=f"start-{run_id}",
        mode=StartMode.STANDALONE,
        session_id="session",
        request_id=f"request-{run_id}",
        turn_id="turn",
        profile_key="workflow.capability_build",
        driver_kind="workflow",
        tool_catalog_generation=1,
        workflow_name=registration.definition.name,
        workflow_version=registration.definition.version,
        requested_run_id=run_id,
        requested_trace_id=f"trace-{run_id}",
        requested_thread_id=run_id,
        resolved_run_id=run_id,
        resolved_trace_id=f"trace-{run_id}",
        resolved_thread_id=run_id,
        checkpoint_namespace="native",
        manifest_hash=registration.expected_manifest_hash,
        implementation_hash=registration.expected_implementation_fingerprint,
        state_schema_version=1,
        start_input_schema_ref=registration.profile.start_input_schema.schema_ref,
        start_input_schema_hash=registration.profile.start_input_schema.schema_hash,
        terminal_projection_descriptor=None,
        terminal_request_factory_hash=None,
        start_input={
            "request": "build the missing capability",
            "search_miss_receipt": "miss-receipt-crash",
        },
        capability_snapshot={},
    )
    admitted = await uow.run_atomic(
        lambda transaction: uow.admit_start_standalone(transaction, request, now=0.0),
        fault_label="test:admit_start",
    )
    activation = await uow.run_atomic(
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
    return {
        "run_id": run_id,
        "thread_id": run_id,
        "checkpoint_ns": "native",
        "logical_timestamp": 2.0,
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


def _native(registration, uow, database):  # type: ignore[no-untyped-def]
    ports = WorkflowExecutionPorts(uow, CheckpointExecutionAdapter(database), uow, uow, uow)
    store = SqliteNativeCheckpointStore(ports, blob_references=_BlobReferences())
    compiled = compile_workflow_registration(registration, transaction_owner=uow.transaction_owner)
    return compiled.bind(
        store=store,
        terminal_projection_port=_TerminalProjection(),
        terminal_commit_projection_port=_TerminalCommitProjection(),
    )


def _state(run_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_name": "durable_task",
        "workflow_version": "capability_build_v1",
        "thread_id": run_id,
        "run_id": run_id,
        "session_id": "session",
        "active_nodes": [],
        "active_step_id": None,
        "status": "pending",
        "values": {
            "request": "build the missing capability",
            "search_miss_receipt": "miss-receipt-1",
            "proposal_budget": 40,
            "fix_budget": 3,
        },
        "blob_refs": [],
        "artifact_refs": [],
        "receipt_refs": [],
        "loop_counters": {},
        "budgets": {},
        "errors": [],
    }


@pytest.mark.anyio
async def test_public_specialization_runs_all_ports_and_activation_exactly_once() -> None:
    calls: list[str] = []
    services = CapabilityBuildHostServices(
        proposal=_Proposal(),
        workspace=_Workspace(),
        search=_Search(calls),
        source_policy=_SourcePolicy(calls),
        isolated_build=_Build(calls),
        package_store=_Store(calls),
        activate=_Activate(calls),
        authorization=_Authorization(calls),
    )
    admission = CapabilityBuildAdmission(
        run_id="run-capability-1",
        request="build the missing capability",
        search_miss_receipt="miss-receipt-1",
        proposal_budget=40,
        fix_budget=3,
    )

    completed = await run_capability_build_specialization(
        admission=admission,
        services=services,
    )
    assert completed.active is True
    assert completed.terminal_status == "completed"
    assert completed.phase == "activated"
    assert calls == [
        "authorization",
        "search",
        "source_policy",
        "isolated_build",
        "package_store",
        "activate",
    ]

    reopened = CapabilityBuildExecutionState.from_json(json.loads(json.dumps(completed.to_json())))
    recovered = await run_capability_build_specialization(
        admission=admission,
        services=services,
        prior_state=reopened,
    )
    assert recovered == completed
    assert calls.count("activate") == 1


@pytest.mark.parametrize(
    ("proposal_budget", "fix_budget"),
    [(0, 3), (41, 3), (40, -1), (40, 4)],
)
def test_admission_rejects_out_of_profile_budgets(proposal_budget: int, fix_budget: int) -> None:
    with pytest.raises(ValueError, match="budget"):
        CapabilityBuildAdmission(
            run_id="run-capability-2",
            request="build",
            search_miss_receipt="miss-receipt-2",
            proposal_budget=proposal_budget,
            fix_budget=fix_budget,
        )


@pytest.mark.anyio
async def test_public_runner_executes_specialization_and_reopen_does_not_replay(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    path = tmp_path / "capability-build.sqlite"
    database, uow, registration, runner = _runner(path, calls)
    run_id = await runner.start(
        session_id="session",
        request_id="request",
        turn_id="turn",
        profile_key="workflow.capability_build",
        tool_catalog_generation=1,
        workflow_name=registration.definition.name,
        workflow_version=registration.definition.version,
        start_input={
            "request": "build the missing capability",
            "search_miss_receipt": "miss-receipt-1",
            "proposal_budget": 40,
            "fix_budget": 3,
        },
        capability_snapshot={},
        run_id="run-capability-public",
    )
    result = await runner.run(run_id, _state(run_id), WorkflowContext())
    persisted = uow.read_run(run_id)
    assert persisted is not None and persisted.state.value == "completed"
    assert result.output["values"]["active"] is True
    assert calls == [
        "authorization",
        "search",
        "source_policy",
        "isolated_build",
        "package_store",
        "activate",
    ]
    database.close()

    reopened_database, reopened_uow, _registration, reopened = _runner(path, calls)
    recovered = await reopened.recover(run_id, WorkflowContext())
    restored = reopened_uow.read_run(run_id)
    assert recovered.run_id == run_id
    assert recovered.status.value == "completed"
    assert recovered.error is None
    assert recovered.output["values"]["active"] is True
    assert restored is not None and restored.state.value == "completed"
    assert calls.count("activate") == 1
    reopened_database.close()


@pytest.mark.anyio
async def test_public_runner_recover_reclaims_expired_mid_node_and_keeps_effect_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capability-build-mid-node.sqlite"
    host = _IdempotentPhysicalHost()
    services = _idempotent_services(host)
    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if (
            armed
            and host.last_stage == "authorization"
            and point == "workflow_native:task_result.before_commit"
        ):
            armed = False
            raise ConnectionError("crash-after-physical:authorization")

    database, _uow, registration, runner = _runner(
        path,
        [],
        host_services=services,
        workflow_fault=fault,
        clock=lambda: 10.0,
    )
    run_id = await runner.start(
        session_id="session",
        request_id="request-mid-node",
        turn_id="turn",
        profile_key="workflow.capability_build",
        tool_catalog_generation=1,
        workflow_name=registration.definition.name,
        workflow_version=registration.definition.version,
        start_input={
            "request": "build after crash",
            "search_miss_receipt": "miss-mid-node",
            "proposal_budget": 40,
            "fix_budget": 3,
        },
        capability_snapshot={},
        run_id="run-capability-mid-node",
    )
    with pytest.raises(ConnectionError, match="crash-after-physical:authorization"):
        await runner.run(run_id, _state(run_id), WorkflowContext())
    database.close()

    reopened_database, reopened_uow, _registration, reopened = _runner(
        path,
        [],
        host_services=services,
        clock=lambda: 100.0,
    )
    recovered = await reopened.recover(run_id, WorkflowContext())
    restored = reopened_uow.read_run(run_id)
    assert recovered.status.value == "completed"
    assert recovered.output["values"]["active"] is True
    assert restored is not None and restored.state.value == "completed"
    assert host.attempts["authorization"] == 2
    assert host.physical["authorization"] == 1
    assert all(count == 1 for count in host.physical.values())
    reopened_database.close()


@pytest.mark.anyio
async def test_public_runner_rejects_missing_miss_receipt_before_nodes_or_ports(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    database, uow, registration, runner = _runner(tmp_path / "missing-receipt.sqlite", calls)
    with pytest.raises(Exception, match="search_miss_receipt"):
        await runner.start(
            session_id="session",
            request_id="request",
            turn_id="turn",
            profile_key="workflow.capability_build",
            tool_catalog_generation=1,
            workflow_name=registration.definition.name,
            workflow_version=registration.definition.version,
            start_input={"request": "build the missing capability"},
            capability_snapshot={},
            run_id="run-capability-invalid",
        )
    assert calls == []
    assert uow.read_run("run-capability-invalid") is None
    database.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "crash_stage",
    (
        "authorization",
        "search",
        "source_policy",
        "isolated_build",
        "package_store",
        "activate",
    ),
)
async def test_each_physical_stage_reopens_with_stable_idempotency_key(
    tmp_path: Path, crash_stage: str
) -> None:
    path = tmp_path / f"crash-{crash_stage}.sqlite"
    host = _IdempotentPhysicalHost()
    services = _idempotent_services(host)
    armed = True

    def fault(point: str) -> None:
        nonlocal armed
        if (
            armed
            and host.last_stage == crash_stage
            and point == "workflow_native:task_result.before_commit"
        ):
            armed = False
            raise ConnectionError(f"crash-after-physical:{crash_stage}")

    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database, workflow_fault=fault)
    registration = build_capability_build_registration(
        generation=1, transaction_owner=uow.transaction_owner
    )
    run_id = f"run-crash-{crash_stage}"
    config = await _native_config(uow, registration, run_id)
    executable = _native(registration, uow, database)
    context = WorkflowContext(ports=services.capability_build.as_ports())
    with pytest.raises(ConnectionError, match=f"crash-after-physical:{crash_stage}"):
        await executable.ainvoke(
            _state(run_id),
            context,
            thread_id=run_id,
            run_id=run_id,
            checkpoint_ns="native",
            configurable=config,
        )
    database.close()

    reopened_database = Database.open(path)
    reopened_uow = SqliteExecutionUnitOfWork(reopened_database)
    reopened_registration = build_capability_build_registration(
        generation=1, transaction_owner=reopened_uow.transaction_owner
    )
    reopened = _native(reopened_registration, reopened_uow, reopened_database)
    result = await reopened.ainvoke(
        None,
        context,
        thread_id=run_id,
        run_id=run_id,
        checkpoint_ns="native",
        configurable=config,
    )
    restored = reopened_uow.read_run(run_id)
    assert restored is not None and restored.state.value == "completed", (
        result,
        host.attempts,
        host.physical,
    )
    assert result["values"]["active"] is True
    progress = result["values"]["capability_build_progress"]
    assert set(progress) == {
        "authorization",
        "search",
        "source_policy",
        "isolated_build",
        "package_store",
        "activate",
    }
    assert {item["operation_key"] for item in progress.values()} == set(host.receipts)
    assert len({item["admission_fingerprint"] for item in progress.values()}) == 1
    expected_attempts = {
        "authorization": 1,
        "search": 1,
        "source_policy": 1,
        "isolated_build": 1,
        "package_store": 1,
        "activate": 1,
    }
    expected_attempts[crash_stage] = 2
    assert host.attempts == expected_attempts
    assert host.physical == {
        "authorization": 1,
        "search": 1,
        "source_policy": 1,
        "isolated_build": 1,
        "package_store": 1,
        "activate": 1,
    }
    assert len(host.receipts) == 6
    reopened_database.close()
