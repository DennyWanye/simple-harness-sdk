# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetCharge, BudgetChargeKind, BudgetPolicy
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
from simple_harness.execution.uow import RunState, UnitOfWorkConflict
from simple_harness.providers import ProviderRequest, ProviderResponse, ProviderTarget


def _crash(point: str):
    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)

    return fault


def _unknown(tmp_path):
    database = Database.open(tmp_path / "h13.db")
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="root-request",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"schema_version": 1},
        event_id="run-1:created",
        now=1.0,
    )
    run, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-a",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )
    request = ProviderRequest(
        RequestId("provider-turn-1"),
        (Message(MessageRole.USER, "hello"),),
        max_output_tokens=10,
    )
    target = ProviderTarget("fixture", "model", "fixture:model", "local", "fixture")
    record = ProviderInvocationRecord.claimed(
        invocation_id=provider_invocation_id(RunId("run-1"), request.request_id),
        run_id=RunId("run-1"),
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
        handed_off_at=3.0,
        execution_lease=lease,
    )
    record = uow.settle_provider_invocation(
        record.settle_unknown(error_code="transport_lost", at=4.0, expected_version=record.version),
        expected_version=record.version,
    )
    assert record.state is ProviderInvocationState.UNKNOWN
    return database, uow, run, lease, record


def _response(record: ProviderInvocationRecord) -> ProviderResponse:
    return ProviderResponse(
        record.request_id,
        Message(MessageRole.ASSISTANT, "recovered"),
    )


def test_completed_provider_resolution_and_budget_are_one_transaction(tmp_path) -> None:
    database, uow, _, _, record = _unknown(tmp_path)
    response = _response(record)
    charge = BudgetCharge(BudgetChargeKind.TRUSTED_USAGE, 17, "fixture-price-v1")
    usage = {"usage": None, "budget": BudgetCharge.unknown().to_json()}

    with pytest.raises(RuntimeError, match="provider_reconciliation.ledger.after_write"):
        uow.record_provider_reconciliation(
            record,
            outcome=ResolutionOutcome.COMPLETED,
            response_json=provider_response_json(response),
            usage_json=usage,
            budget_charge=charge,
            evidence_ref="fixture:completed",
            now=5.0,
            fault=_crash("provider_reconciliation.ledger.after_write"),
        )
    after_rollback = uow.read_provider_invocation(record.invocation_id)
    assert after_rollback is not None
    assert after_rollback.state is ProviderInvocationState.UNKNOWN
    assert (
        database.connection.execute("SELECT count(*) FROM reconciliation_resolutions").fetchone()[0]
        == 0
    )
    assert uow.read_provider_budget(RunId("run-1")).has_unknown_charge is True

    settled = uow.record_provider_reconciliation(
        record,
        outcome=ResolutionOutcome.COMPLETED,
        response_json=provider_response_json(response),
        usage_json=usage,
        budget_charge=charge,
        evidence_ref="fixture:completed",
        now=5.0,
    )
    assert settled.state is ProviderInvocationState.SUCCEEDED
    budget = uow.read_provider_budget(RunId("run-1"))
    assert (
        budget.committed_micros,
        budget.reserved_micros,
        budget.has_unknown_charge,
    ) == (
        17,
        0,
        False,
    )
    database.close()


def test_resolution_before_wait_is_not_lost_and_receipt_rejects_foreign_owner(
    tmp_path,
) -> None:
    database, uow, run, lease, record = _unknown(tmp_path)
    uow.record_provider_reconciliation(
        record,
        outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
        response_json=None,
        usage_json=None,
        budget_charge=record.budget_charge,
        evidence_ref="fixture:not-started",
        now=5.0,
    )
    waiting, blocker = uow.commit_runtime_wait_with_blocker(
        run_id="run-1",
        expected_version=run.version,
        event_id="run-1:waiting:provider",
        payload={"reason": "provider_outcome_unknown"},
        blocker=WaitBlockerSpec(
            RecoveryKind.PROVIDER,
            record.invocation_id,
            record.handoff_attempt,
            record.version,
        ),
        lease=lease,
        now=6.0,
    )
    assert waiting.state is RunState.WAITING and blocker.resolution_id is not None
    assert uow.list_resolved_wait_blockers(
        owner_id="owner-a", namespace="runtime.kernel", now=7.0
    ) == (blocker,)
    assert (
        uow.list_resolved_wait_blockers(owner_id="owner-b", namespace="runtime.kernel", now=7.0)
        == ()
    )
    activated, replayed_lease, receipt = uow.consume_resolved_wait_and_claim_activation(
        blocker_id=blocker.blocker_id,
        owner_id="owner-a",
        namespace="runtime.kernel",
        now=7.0,
        lease_ttl_seconds=100.0,
    )
    assert activated.state is RunState.RUNNING
    assert replayed_lease.owner_id == receipt.owner_id == "owner-a"
    with pytest.raises(UnitOfWorkConflict, match="another Runtime owner"):
        uow.consume_resolved_wait_and_claim_activation(
            blocker_id=blocker.blocker_id,
            owner_id="owner-b",
            namespace="runtime.kernel",
            now=8.0,
            lease_ttl_seconds=100.0,
        )
    database.close()


def test_provider_not_started_allows_exactly_one_second_handoff_and_late_completion(
    tmp_path,
) -> None:
    database, uow, _, lease, record = _unknown(tmp_path)
    resolution_record = uow.record_provider_reconciliation(
        record,
        outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
        response_json=None,
        usage_json=None,
        budget_charge=record.budget_charge,
        evidence_ref="fixture:not-started:attempt-1",
        now=5.0,
    )
    resolution = uow.read_reconciliation_resolution(
        kind="provider",
        ledger_identity=record.invocation_id,
        handoff_attempt=record.handoff_attempt,
    )
    assert resolution is not None
    retry_ready = uow.reauthorize_provider_not_started(
        resolution_record,
        resolution=resolution,
        execution_lease=lease,
        now=6.0,
    )
    assert retry_ready.rehandoff_count == 1
    second = uow.hand_off_provider_invocation(
        retry_ready.invocation_id,
        expected_version=retry_ready.version,
        handed_off_at=7.0,
        execution_lease=lease,
    )
    assert second.handoff_attempt == 2 and second.rehandoff_count == 1
    second_unknown = uow.settle_provider_invocation(
        second.settle_unknown(
            error_code="second_transport_lost",
            at=8.0,
            expected_version=second.version,
        ),
        expected_version=second.version,
    )
    response = _response(second_unknown)
    final = uow.record_provider_reconciliation(
        second_unknown,
        outcome=ResolutionOutcome.COMPLETED,
        response_json=provider_response_json(response),
        usage_json={"usage": None, "budget": BudgetCharge.unknown().to_json()},
        budget_charge=BudgetCharge(BudgetChargeKind.TRUSTED_USAGE, 3, "price-v1"),
        evidence_ref="fixture:late-completed:attempt-2",
        now=9.0,
    )
    assert final.state is ProviderInvocationState.SUCCEEDED
    assert final.handoff_attempt == 2 and final.rehandoff_count == 1
    with pytest.raises(UnitOfWorkConflict, match="mismatch|CAS"):
        uow.reauthorize_provider_not_started(
            final,
            resolution=resolution,
            execution_lease=lease,
            now=10.0,
        )
    database.close()


def test_wait_blocker_cannot_claim_a_stale_or_foreign_ledger_observation(
    tmp_path,
) -> None:
    database, uow, run, lease, record = _unknown(tmp_path)
    with pytest.raises(UnitOfWorkConflict, match="observed ledger version is stale"):
        uow.commit_runtime_wait_with_blocker(
            run_id="run-1",
            expected_version=run.version,
            event_id="run-1:forged-wait",
            payload={"reason": "forged"},
            blocker=WaitBlockerSpec(
                RecoveryKind.PROVIDER,
                record.invocation_id,
                record.handoff_attempt,
                record.version + 1,
            ),
            lease=lease,
            now=5.0,
        )
    assert uow.read_run("run-1") == run
    assert database.connection.execute("SELECT count(*) FROM run_wait_blockers").fetchone()[0] == 0
    database.close()
