# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib

import pytest

from simple_harness.contracts import CallId, EffectId, RequestId, RunId, canonical_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy
from simple_harness.execution.delivery import DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState, UnitOfWorkConflict
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
)
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationReceipt,
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
from simple_harness.workflow.lease import WorkflowLease


class Provider:
    target = ProviderTarget("fixture", "model", "model", "local", "fixture")

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, request, *, cancel):
        del cancel
        self.calls += 1
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "ok"),
            model="model",
        )


class Allow:
    async def authorize(self, prepared):
        return AuthorizationResult(
            AuthorizationDecision.ALLOW,
            receipt_ref=f"auth:{prepared.effect_id.value}",
        )

    async def bind_effect_handoff(
        self, prepared, authorization_receipt_ref, sdk_receipt
    ) -> AuthorizationReceipt:
        del prepared, authorization_receipt_ref
        return AuthorizationReceipt("host:handoff", "a" * 64, sdk_receipt.receipt_hash)


class Observe:
    async def observe(self, prepared):
        del prepared
        return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "fixture:unknown")


def _seed(uow: SqliteExecutionUnitOfWork):
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="root-request",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="run-created",
        now=1.0,
    )
    _, stale = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="old-owner",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=1.0,
    )
    _, current = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="new-owner",
        namespace="runtime.kernel",
        now=4.0,
        lease_ttl_seconds=30.0,
    )
    return stale, current


def test_stale_runtime_owner_cannot_write_or_hand_off_provider(tmp_path) -> None:
    database = Database.open(tmp_path / "provider.db")
    uow = SqliteExecutionUnitOfWork(database)
    stale, _ = _seed(uow)
    provider = Provider()
    coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(),
        estimator=None,
        clock=lambda: 5.0,
    )
    request = ProviderRequest(
        RequestId("provider-request"),
        (Message(MessageRole.USER, "hello"),),
    )
    with pytest.raises(UnitOfWorkConflict, match="lease"):
        asyncio.run(
            coordinator.invoke(
                RunId("run-1"),
                request,
                cancel=CancelToken(),
                execution_lease=stale,
            )
        )
    assert provider.calls == 0
    assert (
        database.connection.execute("SELECT count(*) FROM provider_invocations").fetchone()[0] == 0
    )
    database.close()


def test_stale_runtime_owner_cannot_prepare_or_hand_off_tool(tmp_path) -> None:
    database = Database.open(tmp_path / "tool.db")
    uow = SqliteExecutionUnitOfWork(database)
    stale, _ = _seed(uow)
    handler_calls = 0

    def handler(arguments, context):
        nonlocal handler_calls
        del arguments, context
        handler_calls += 1
        return ToolResult.succeeded(CallId("call-1"), {"ok": True})

    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry(
            [
                FunctionTool(
                    ToolSpec(
                        "fixture",
                        "fixture",
                        {"type": "object", "additionalProperties": False},
                    ),
                    handler,
                )
            ]
        ),
        authorization=Allow(),
        reconciliation=Observe(),
        clock=lambda: 5.0,
    )
    stale_fence = RunFenceLease(RunId("run-1"), 1, stale.owner_id, stale.epoch)
    with pytest.raises(UnitOfWorkConflict, match="lease"):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "fixture", {}),
                context=ToolContext(RunId("run-1"), RequestId("tool-request"), CancellationToken()),
                execution_lease=stale,
                run_fence=stale_fence,
            )
        )
    assert handler_calls == 0
    assert database.connection.execute("SELECT count(*) FROM execution_effects").fetchone()[0] == 0
    database.close()


def _seed_workflow_authority(uow: SqliteExecutionUnitOfWork):
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="workflow-run",
        request_id="workflow-request",
        profile_key="workflow.demo",
        driver_kind="workflow",
        snapshot={},
        event_id="workflow-run:created",
        now=1.0,
    )
    _, lease = uow.claim_runtime_activation(
        run_id="workflow-run",
        owner_id="workflow-owner",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=30.0,
    )
    fence = asyncio.run(uow.acquire(RunId("workflow-run"), lease, now=2.0))
    request_json = canonical_json(
        {
            "checkpoint_namespace": "native",
            "driver_kind": "workflow",
        }
    )
    request_hash = hashlib.sha256(request_json.encode()).hexdigest()
    uow.database.connection.execute(
        "INSERT INTO workflow_start_admissions(request_key,request_id,"
        "request_fingerprint,request_json,mode,run_id,trace_id,thread_id,phase,"
        "version,claim_action,claim_owner,claim_epoch,claim_expires_at,created_at,updated_at) "
        "VALUES('workflow-start','workflow-request',?,?, 'precreated',"
        "'workflow-run','workflow-trace','workflow-thread','claimed',0,'new',?,?,?,1,1)",
        (request_hash, request_json, lease.owner_id, lease.epoch, lease.expires_at),
    )
    uow.database.connection.execute(
        "INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) "
        "VALUES('workflow-run','native',?,?,?)",
        (lease.owner_id, lease.epoch, lease.expires_at),
    )
    uow.database.connection.commit()
    return (
        lease,
        fence,
        WorkflowLease(
            "workflow-run",
            lease.owner_id,
            lease.epoch,
            lease.expires_at,
            lease.epoch,
            "native",
        ),
    )


@pytest.mark.parametrize("kind", ["provider", "tool"])
def test_workflow_handoff_accepts_immutable_activation_token_after_heartbeat(
    tmp_path, kind: str
) -> None:
    database = Database.open(tmp_path / f"workflow-renewed-{kind}.db")
    uow = SqliteExecutionUnitOfWork(database)
    lease, fence, workflow_lease = _seed_workflow_authority(uow)
    database.connection.execute(
        "UPDATE workflow_leases SET expires_at=100 WHERE run_id='workflow-run'"
    )
    database.connection.commit()
    if kind == "provider":
        provider = Provider()
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=BudgetPolicy(),
            estimator=None,
            clock=lambda: 40.0,
        )
        asyncio.run(
            coordinator.invoke(
                RunId("workflow-run"),
                ProviderRequest(
                    RequestId("provider-renewed"),
                    (Message(MessageRole.USER, "hello"),),
                ),
                cancel=CancelToken(),
                execution_lease=lease,
                workflow_lease=workflow_lease,
            )
        )
        assert provider.calls == 1
    else:
        calls = 0

        def handler(arguments, context):
            nonlocal calls
            del arguments, context
            calls += 1
            return ToolResult.succeeded(CallId("workflow-call"), {"ok": True})

        executor = EffectExecutor(
            uow=uow,
            registry=ToolRegistry(
                [
                    FunctionTool(
                        ToolSpec(
                            "fixture",
                            "fixture",
                            {"type": "object", "additionalProperties": False},
                        ),
                        handler,
                    )
                ]
            ),
            authorization=Allow(),
            reconciliation=Observe(),
            clock=lambda: 40.0,
        )
        asyncio.run(
            executor.execute(
                effect_id=EffectId("workflow-effect-renewed"),
                call=ToolCall(CallId("workflow-call"), "fixture", {}),
                context=ToolContext(
                    RunId("workflow-run"),
                    RequestId("workflow-tool-renewed"),
                    CancellationToken(),
                ),
                execution_lease=lease,
                run_fence=fence,
                workflow_lease=workflow_lease,
            )
        )
        assert calls == 1
    database.close()


@pytest.mark.parametrize("authority", ["missing", "expired", "mismatch"])
def test_workflow_provider_handoff_requires_current_triple_fence(tmp_path, authority: str) -> None:
    database = Database.open(tmp_path / f"workflow-provider-{authority}.db")
    uow = SqliteExecutionUnitOfWork(database)
    lease, _fence, workflow_lease = _seed_workflow_authority(uow)
    provider = Provider()
    coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(),
        estimator=None,
        clock=lambda: 5.0,
    )
    passed = workflow_lease
    if authority == "missing":
        passed = None
    elif authority == "expired":
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=4 WHERE run_id='workflow-run' "
            "AND namespace='native'"
        )
        database.connection.commit()
        passed = WorkflowLease(
            "workflow-run", lease.owner_id, lease.epoch, 4.0, lease.epoch, "native"
        )
    else:
        passed = WorkflowLease(
            "workflow-run",
            lease.owner_id,
            lease.epoch + 1,
            lease.expires_at,
            lease.epoch,
            "native",
        )
    request = ProviderRequest(
        RequestId(f"provider-{authority}"),
        (Message(MessageRole.USER, "hello"),),
    )
    with pytest.raises(UnitOfWorkConflict, match="workflow"):
        asyncio.run(
            coordinator.invoke(
                RunId("workflow-run"),
                request,
                cancel=CancelToken(),
                execution_lease=lease,
                workflow_lease=passed,
            )
        )
    assert provider.calls == 0
    assert (
        database.connection.execute(
            "SELECT state FROM provider_invocations WHERE run_id='workflow-run'"
        ).fetchone()[0]
        == "claimed"
    )
    database.close()


@pytest.mark.parametrize("authority", ["missing", "expired", "mismatch"])
def test_workflow_tool_handoff_requires_current_workflow_lease(tmp_path, authority: str) -> None:
    database = Database.open(tmp_path / f"workflow-tool-{authority}.db")
    uow = SqliteExecutionUnitOfWork(database)
    lease, fence, workflow_lease = _seed_workflow_authority(uow)
    calls = 0

    def handler(arguments, context):
        nonlocal calls
        del arguments, context
        calls += 1
        return ToolResult.succeeded(CallId("workflow-call"), {"ok": True})

    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry(
            [
                FunctionTool(
                    ToolSpec(
                        "fixture",
                        "fixture",
                        {"type": "object", "additionalProperties": False},
                    ),
                    handler,
                )
            ]
        ),
        authorization=Allow(),
        reconciliation=Observe(),
        clock=lambda: 5.0,
    )
    passed = workflow_lease
    if authority == "missing":
        passed = None
    elif authority == "expired":
        database.connection.execute(
            "UPDATE workflow_leases SET expires_at=4 WHERE run_id='workflow-run' "
            "AND namespace='native'"
        )
        database.connection.commit()
        passed = WorkflowLease(
            "workflow-run", lease.owner_id, lease.epoch, 4.0, lease.epoch, "native"
        )
    else:
        passed = WorkflowLease(
            "workflow-run",
            lease.owner_id,
            lease.epoch + 1,
            lease.expires_at,
            lease.epoch,
            "native",
        )
    with pytest.raises(UnitOfWorkConflict, match="workflow"):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("workflow-effect"),
                call=ToolCall(CallId("workflow-call"), "fixture", {}),
                context=ToolContext(
                    RunId("workflow-run"),
                    RequestId("workflow-tool-request"),
                    CancellationToken(),
                ),
                execution_lease=lease,
                run_fence=fence,
                workflow_lease=passed,
            )
        )
    assert calls == 0
    assert uow.read_effect(EffectId("workflow-effect")).state.value == "prepared"
    database.close()


def test_run_fence_acquire_is_idempotent_for_same_runtime_lease(tmp_path) -> None:
    database = Database.open(tmp_path / "same-owner.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, current = _seed_current_only(uow, owner_id="same-owner", now=2.0)

    first = asyncio.run(uow.acquire(RunId("run-1"), current, now=2.0))
    second = asyncio.run(uow.acquire(RunId("run-1"), current, now=3.0))

    assert second == first
    row = database.connection.execute(
        "SELECT owner_id,runtime_lease_epoch,epoch,state FROM run_fences"
    ).fetchone()
    assert tuple(row) == ("same-owner", current.epoch, first.epoch, "active")
    database.close()


def test_stale_acquire_after_takeover_is_zero_write(tmp_path) -> None:
    database = Database.open(tmp_path / "takeover.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, stale = _seed_current_only(uow, owner_id="same-owner", now=2.0, lease_ttl_seconds=1.0)
    old_fence = asyncio.run(uow.acquire(RunId("run-1"), stale, now=2.0))
    _, current = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="same-owner",
        namespace="runtime.kernel",
        now=4.0,
        lease_ttl_seconds=30.0,
    )
    new_fence = asyncio.run(uow.acquire(RunId("run-1"), current, now=4.0))
    before = tuple(
        database.connection.execute(
            "SELECT owner_id,runtime_lease_epoch,epoch,state FROM run_fences"
        ).fetchone()
    )

    with pytest.raises(UnitOfWorkConflict, match="lease"):
        asyncio.run(uow.acquire(RunId("run-1"), stale, now=4.0))

    after = tuple(
        database.connection.execute(
            "SELECT owner_id,runtime_lease_epoch,epoch,state FROM run_fences"
        ).fetchone()
    )
    assert current.epoch == stale.epoch + 1
    assert new_fence.epoch == old_fence.epoch + 1
    assert (
        after
        == before
        == (
            "same-owner",
            current.epoch,
            new_fence.epoch,
            "active",
        )
    )
    database.close()


def test_acquire_at_exact_expiry_is_zero_write(tmp_path) -> None:
    database = Database.open(tmp_path / "exact-expiry.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, lease = _seed_current_only(uow, owner_id="owner-1", now=2.0, lease_ttl_seconds=3.0)

    with pytest.raises(UnitOfWorkConflict, match="lease"):
        asyncio.run(uow.acquire(RunId("run-1"), lease, now=5.0))

    assert database.connection.execute("SELECT COUNT(*) FROM run_fences").fetchone()[0] == 0
    database.close()


def test_new_runtime_lease_cannot_pair_with_old_fence_for_tool_handoff(
    tmp_path,
) -> None:
    database = Database.open(tmp_path / "mixed-fences.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, old_lease = _seed_current_only(uow, owner_id="same-owner", now=2.0, lease_ttl_seconds=1.0)
    old_fence = asyncio.run(uow.acquire(RunId("run-1"), old_lease, now=2.0))
    _, new_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="same-owner",
        namespace="runtime.kernel",
        now=4.0,
        lease_ttl_seconds=30.0,
    )
    handler_calls = 0

    def handler(arguments, context):
        nonlocal handler_calls
        del arguments, context
        handler_calls += 1
        return ToolResult.succeeded(CallId("call-1"), {"ok": True})

    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry(
            [
                FunctionTool(
                    ToolSpec(
                        "fixture",
                        "fixture",
                        {"type": "object", "additionalProperties": False},
                    ),
                    handler,
                )
            ]
        ),
        authorization=Allow(),
        reconciliation=Observe(),
        clock=lambda: 4.0,
    )

    with pytest.raises(UnitOfWorkConflict, match="fence"):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "fixture", {}),
                context=ToolContext(RunId("run-1"), RequestId("tool-request"), CancellationToken()),
                execution_lease=new_lease,
                run_fence=old_fence,
            )
        )

    assert handler_calls == 0
    assert database.connection.execute("SELECT COUNT(*) FROM execution_effects").fetchone()[0] == 0
    database.close()


def test_direct_handoff_rejects_new_execution_lease_with_old_run_fence(
    tmp_path,
) -> None:
    database = Database.open(tmp_path / "direct-mixed-handoff.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, old_lease = _seed_current_only(uow, owner_id="same-owner", now=2.0, lease_ttl_seconds=1.0)
    old_fence = asyncio.run(uow.acquire(RunId("run-1"), old_lease, now=2.0))
    prepared = uow.prepare_effect(
        effect_id=EffectId("effect-1"),
        run_id=RunId("run-1"),
        call_id=CallId("call-1"),
        tool_name="fixture",
        arguments={},
        request_hash="a" * 64,
        authorization_receipt_ref="auth:effect-1",
        run_fence=old_fence,
        execution_lease=old_lease,
        now=2.0,
    )
    _, new_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="same-owner",
        namespace="runtime.kernel",
        now=4.0,
        lease_ttl_seconds=30.0,
    )

    with pytest.raises(UnitOfWorkConflict, match="fence"):
        uow.mark_effect_handed_off(
            EffectId("effect-1"),
            expected_version=prepared.version,
            run_fence=old_fence,
            handoff_receipt_ref="handoff:effect-1",
            execution_lease=new_lease,
            now=4.0,
        )

    persisted = uow.read_effect(EffectId("effect-1"))
    assert persisted is not None and persisted.state.value == "prepared"
    assert persisted.version == prepared.version
    database.close()


def test_multiple_tools_share_kernel_fence_until_terminal_commit(tmp_path, monkeypatch) -> None:
    database = Database.open(tmp_path / "multi-tool-terminal.db")
    uow = SqliteExecutionUnitOfWork(database)
    _, lease = _seed_current_only(uow, owner_id="kernel-owner", now=2.0)
    fence = asyncio.run(uow.acquire(RunId("run-1"), lease, now=2.0))
    calls = 0
    executor_fence_calls = 0

    async def forbidden_fence_lifecycle(*args, **kwargs):
        nonlocal executor_fence_calls
        del args, kwargs
        executor_fence_calls += 1
        raise AssertionError("EffectExecutor must not own the Run fence lifecycle")

    monkeypatch.setattr(SqliteExecutionUnitOfWork, "acquire", forbidden_fence_lifecycle)
    monkeypatch.setattr(SqliteExecutionUnitOfWork, "release", forbidden_fence_lifecycle)

    def handler(arguments, context):
        nonlocal calls
        del arguments, context
        calls += 1
        return ToolResult.succeeded(CallId(f"call-{calls}"), {"ok": True})

    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry(
            [
                FunctionTool(
                    ToolSpec(
                        "fixture",
                        "fixture",
                        {"type": "object", "additionalProperties": False},
                    ),
                    handler,
                )
            ]
        ),
        authorization=Allow(),
        reconciliation=Observe(),
        clock=lambda: 3.0,
    )

    for ordinal in (1, 2, 3):
        asyncio.run(
            executor.execute(
                effect_id=EffectId(f"effect-{ordinal}"),
                call=ToolCall(CallId(f"call-{ordinal}"), "fixture", {}),
                context=ToolContext(
                    RunId("run-1"),
                    RequestId(f"tool-request-{ordinal}"),
                    CancellationToken(),
                ),
                execution_lease=lease,
                run_fence=fence,
            )
        )
        row = database.connection.execute(
            "SELECT runtime_lease_epoch,epoch,state FROM run_fences WHERE run_id='run-1'"
        ).fetchone()
        assert tuple(row) == (lease.epoch, fence.epoch, "active")

    run = uow.read_run("run-1")
    assert run is not None
    result = uow.commit_root_terminal_with_deliveries(
        run_id="run-1",
        expected_version=run.version,
        terminal_state=RunState.COMPLETED,
        event_id="run-1:terminal:completed",
        terminal_payload={"ok": True},
        deliveries=(DeliverySpec("delivery-1", "fixture", "terminal:run-1", {"ok": True}),),
        fence=fence,
        execution_lease=lease,
        terminal_fence_receipt_ref="runtime-fence:kernel-owner:1",
        now=4.0,
    )
    assert calls == 3
    assert executor_fence_calls == 0
    assert result.run.state is RunState.COMPLETED
    assert (
        database.connection.execute("SELECT state FROM run_fences WHERE run_id='run-1'").fetchone()[
            0
        ]
        == "released"
    )
    database.close()


def _seed_current_only(
    uow: SqliteExecutionUnitOfWork,
    *,
    owner_id: str,
    now: float,
    lease_ttl_seconds: float = 30.0,
):
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="root-request",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="run-created",
        now=1.0,
    )
    return uow.claim_runtime_activation(
        run_id="run-1",
        owner_id=owner_id,
        namespace="runtime.kernel",
        now=now,
        lease_ttl_seconds=lease_ttl_seconds,
    )
