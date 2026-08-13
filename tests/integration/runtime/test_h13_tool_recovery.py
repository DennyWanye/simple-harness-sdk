# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.execution.effects import effect_request_hash
from simple_harness.execution.recovery import (
    RecoveryKind,
    ResolutionOutcome,
    WaitBlockerSpec,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.tools import ToolResult


def _crash(point: str):
    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)

    return fault


def _unknown(tmp_path):
    database = Database.open(tmp_path / "tool-recovery.db")
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="s",
        run_id="run-1",
        request_id="q",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"schema_version": 1},
        event_id="created",
        now=1.0,
    )
    run, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-a",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )
    import asyncio

    fence = asyncio.run(uow.acquire(RunId("run-1"), lease, now=2.0))
    args: dict[str, object] = {"x": 1}
    prepared = uow.prepare_effect(
        effect_id=EffectId("effect-1"),
        run_id=RunId("run-1"),
        call_id=CallId("internal-call-1"),
        raw_call_id="raw-call",
        turn_ordinal=1,
        call_ordinal=0,
        tool_name="calculator",
        arguments=args,
        request_hash=effect_request_hash(tool_name="calculator", arguments=args),
        authorization_receipt_ref="auth:epoch-1",
        run_fence=fence,
        execution_lease=lease,
        now=3.0,
    )
    handed = uow.mark_effect_handed_off(
        prepared.effect_id,
        expected_version=prepared.version,
        run_fence=fence,
        handoff_receipt_ref="handoff:1",
        execution_lease=lease,
        now=4.0,
    )
    unknown = uow.mark_effect_unknown(
        handed.effect_id,
        expected_version=handed.version,
        expected_fence_epoch=fence.epoch,
        evidence_ref="unknown:1",
        now=5.0,
    )
    return database, uow, run, lease, fence, unknown


def test_tool_resolution_lost_wake_and_reauthorize_fault_reopen(tmp_path) -> None:
    database, uow, run, lease, fence, unknown = _unknown(tmp_path)
    resolved = uow.record_tool_reconciliation(
        unknown,
        outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
        result=None,
        evidence_ref="external:not-started:1",
        now=6.0,
    )
    assert resolved.state.value == "unknown"
    waiting, blocker = uow.commit_runtime_wait_with_blocker(
        run_id="run-1",
        expected_version=run.version,
        event_id="waiting-tool",
        payload={"reason": "tool_outcome_unknown"},
        blocker=WaitBlockerSpec(
            RecoveryKind.TOOL,
            unknown.effect_id.value,
            unknown.handoff_attempt,
            unknown.version,
        ),
        lease=lease,
        now=7.0,
    )
    assert waiting.state is RunState.WAITING and blocker.resolution_id is not None
    active, active_lease, _ = uow.consume_resolved_wait_and_claim_activation(
        blocker_id=blocker.blocker_id,
        owner_id="owner-a",
        namespace="runtime.kernel",
        now=8.0,
        lease_ttl_seconds=100.0,
    )
    assert active.state is RunState.RUNNING
    resolution = uow.read_reconciliation_resolution(
        kind="tool", ledger_identity=unknown.effect_id.value, handoff_attempt=1
    )
    assert resolution is not None
    with pytest.raises(RuntimeError, match="effect_reauthorize.after_commit"):
        uow.reauthorize_effect_not_started(
            unknown,
            authorization_receipt_ref="auth:fresh",
            resolution=resolution,
            run_fence=fence,
            execution_lease=active_lease,
            now=9.0,
            fault=_crash("effect_reauthorize.after_commit"),
        )
    database.close()
    database = Database.open(tmp_path / "tool-recovery.db")
    uow = SqliteExecutionUnitOfWork(database)
    prepared = uow.read_effect(EffectId("effect-1"))
    assert prepared is not None
    assert prepared.state.value == "prepared"
    assert prepared.handoff_attempt == 1 and prepared.rehandoff_count == 1
    uow.release_runtime_lease(active_lease, now=10.0)
    _, takeover_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-b",
        namespace="runtime.kernel",
        now=11.0,
        lease_ttl_seconds=100.0,
    )
    import asyncio

    takeover_fence = asyncio.run(uow.acquire(RunId("run-1"), takeover_lease, now=11.0))
    # Refreshing a retry PREPARED row is an explicit fenced CAS. A crash after
    # commit must reopen to the new authority without changing uncertainty epoch.
    with pytest.raises(RuntimeError, match="effect_refresh.after_commit"):
        uow.refresh_prepared_effect_authority(
            prepared,
            authorization_receipt_ref="auth:fresh-owner-b",
            run_fence=takeover_fence,
            execution_lease=takeover_lease,
            now=12.0,
            fault=_crash("effect_refresh.after_commit"),
        )
    database.close()
    database = Database.open(tmp_path / "tool-recovery.db")
    uow = SqliteExecutionUnitOfWork(database)
    refreshed = uow.read_effect(EffectId("effect-1"))
    assert refreshed is not None
    assert refreshed.authorization_receipt_ref == "auth:fresh-owner-b"
    assert refreshed.fence_epoch == takeover_fence.epoch
    assert refreshed.handoff_attempt == 1 and refreshed.rehandoff_count == 1

    handed = uow.mark_effect_handed_off(
        refreshed.effect_id,
        expected_version=refreshed.version,
        run_fence=takeover_fence,
        handoff_receipt_ref="handoff:2",
        execution_lease=takeover_lease,
        now=13.0,
    )
    assert handed.handoff_attempt == 2 and handed.rehandoff_count == 1
    unknown_again = uow.mark_effect_unknown(
        handed.effect_id,
        expected_version=handed.version,
        expected_fence_epoch=takeover_fence.epoch,
        evidence_ref="unknown:2",
        now=14.0,
    )
    completed = uow.record_tool_reconciliation(
        unknown_again,
        outcome=ResolutionOutcome.COMPLETED,
        result=ToolResult.succeeded(CallId("raw-call"), {"ok": True}),
        evidence_ref="external:late-completed:2",
        now=15.0,
    )
    assert completed.state.value == "succeeded"
    assert completed.handoff_attempt == 2 and completed.rehandoff_count == 1
    replay = uow.record_tool_reconciliation(
        unknown_again,
        outcome=ResolutionOutcome.COMPLETED,
        result=ToolResult.succeeded(CallId("raw-call"), {"ok": True}),
        evidence_ref="external:late-completed:2",
        now=16.0,
    )
    assert replay == completed
    database.close()


def test_tool_completed_resolution_settles_once_without_rehandoff(tmp_path) -> None:
    database, uow, _, _, _, unknown = _unknown(tmp_path)
    completed = uow.record_tool_reconciliation(
        unknown,
        outcome=ResolutionOutcome.COMPLETED,
        result=ToolResult.succeeded(CallId("raw-call"), {"ok": True}),
        evidence_ref="external:completed:1",
        now=6.0,
    )
    assert completed.state.value == "succeeded"
    assert completed.handoff_attempt == 1 and completed.rehandoff_count == 0
    replay = uow.record_tool_reconciliation(
        unknown,
        outcome=ResolutionOutcome.COMPLETED,
        result=ToolResult.succeeded(CallId("raw-call"), {"ok": True}),
        evidence_ref="external:completed:1",
        now=7.0,
    )
    assert replay == completed
    database.close()


def test_initial_prepared_reopens_under_takeover_with_explicit_refresh(
    tmp_path,
) -> None:
    database = Database.open(tmp_path / "prepared-takeover.db")
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="s",
        run_id="run-1",
        request_id="q",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"schema_version": 1},
        event_id="created",
        now=1.0,
    )
    _, first_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-a",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )
    import asyncio

    first_fence = asyncio.run(uow.acquire(RunId("run-1"), first_lease, now=2.0))
    arguments: dict[str, object] = {"x": 1}
    prepared = uow.prepare_effect(
        effect_id=EffectId("effect-initial"),
        run_id=RunId("run-1"),
        call_id=CallId("internal-initial"),
        tool_name="calculator",
        arguments=arguments,
        request_hash=effect_request_hash(tool_name="calculator", arguments=arguments),
        authorization_receipt_ref="auth:owner-a",
        run_fence=first_fence,
        execution_lease=first_lease,
        now=3.0,
    )
    uow.release_runtime_lease(first_lease, now=4.0)
    _, takeover_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-b",
        namespace="runtime.kernel",
        now=5.0,
        lease_ttl_seconds=100.0,
    )
    takeover_fence = asyncio.run(uow.acquire(RunId("run-1"), takeover_lease, now=5.0))
    refreshed = uow.refresh_prepared_effect_authority(
        prepared,
        authorization_receipt_ref="auth:owner-b",
        run_fence=takeover_fence,
        execution_lease=takeover_lease,
        now=6.0,
    )
    assert refreshed.state.value == "prepared"
    assert refreshed.handoff_attempt == refreshed.rehandoff_count == 0
    assert refreshed.fence_epoch == takeover_fence.epoch
    assert refreshed.authorization_receipt_ref == "auth:owner-b"
    database.close()
