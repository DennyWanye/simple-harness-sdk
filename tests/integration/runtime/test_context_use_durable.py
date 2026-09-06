"""Owned SQLite fault/recovery tests with actual Memory receipts, separate from
public consumer evidence. Only this source suite inspects SDK execution storage.
"""

import asyncio
import dataclasses as dc
import time

import pytest

import simple_harness as h
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import (
    ProviderInvocationCoordinator,
    ProviderInvocationUnknownError,
)
from simple_harness.execution.provider_invocations import (
    provider_request_fingerprint,
    provider_request_json,
)
from simple_harness.execution.recovery import ResolutionOutcome
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.runtime.start_snapshot import StartSnapshot

from .context_use_public_fixture import PublicMemoryFixture
from .test_context_use_public_memory import canonical, digest


class LocalProvider:
    target = ProviderTarget("fixture", "model", "model", "local", "fixture")

    def __init__(self):
        self.calls = []
        self.unknown = False
        self.entered = None
        self.release = None

    async def invoke(self, request, *, cancel):
        self.calls.append(request)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.unknown:
            raise OSError("test transport outcome unknown")
        return ProviderResponse(
            request.request_id,
            h.Message(h.MessageRole.ASSISTANT, "done"),
            usage=ProviderUsage(20, 5, 25),
            model="model",
            finish_reason="stop",
        )


async def setup(tmp_path):
    fixture = await PublicMemoryFixture(tmp_path / "memory.sqlite").open()
    await fixture.seed()
    fragments = await fixture.recall()
    database = Database.open(tmp_path / "execution.sqlite")
    uow = SqliteExecutionUnitOfWork(database)
    now = time.time()
    root = StartSnapshot(
        "agent.general",
        "react",
        fixture.turn_id,
        1,
        {"messages": [{"role": "user", "content": "preferences"}]},
    )
    uow.create_with_start_snapshot(
        execution_session_id="session",
        run_id=fixture.run_id,
        request_id="root",
        profile_key="agent.general",
        driver_kind="react",
        snapshot=root.to_json(),
        event_id="created",
        now=now,
    )
    _, lease = uow.claim_runtime_activation(
        run_id=fixture.run_id,
        owner_id="owner",
        namespace="runtime.kernel",
        now=now,
        lease_ttl_seconds=120,
    )
    messages = tuple(
        h.Message(h.MessageRole.SYSTEM, canonical(h.thaw_json(f.public_payload)).decode())
        for f in fragments
    )
    request = ProviderRequest(
        h.RequestId(fixture.run_id + ":provider-turn:1"), messages, max_output_tokens=128
    )
    wire = provider_request_json(request)
    intent = h.RecallContextUseIntentV1(
        fragments, tuple((i + 1, digest(m)) for i, m in enumerate(wire["messages"]))
    )
    attempt = h.ProviderContextUseAttemptV1(
        fixture.authority_scope_ref,
        fixture.principal.actor_id,
        fixture.run_id,
        fixture.turn_id,
        None,
        request.request_id.value,
        1,
        1,
        "snapshot",
        1,
        provider_request_fingerprint(request),
        now,
        (intent,),
    )
    checkpoint = DurableReactCheckpoint(uow, clock=time.time)
    state, version = checkpoint.load_or_create(h.RunId(fixture.run_id), lease)
    state = dc.replace(
        state,
        source_schema_version=7,
        context_use_authority_scope=fixture.authority_scope_ref,
        active_turn_id=fixture.turn_id,
        context_use_attempt=attempt.to_json(),
        phase="provider_reserved",
        provider_turns_reserved_total=1,
        provider_request_id=request.request_id.value,
        provider_request_snapshot=wire,
        provider_request_fingerprint=attempt.request_fingerprint,
    )
    checkpoint.cas(h.RunId(fixture.run_id), lease, version, state)
    provider = LocalProvider()
    return fixture, database, uow, lease, request, attempt, provider


def coordinator(uow, fixture, provider):
    return ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(),
        estimator=FrozenPriceEstimator("fixture-price", "model", 0, 0),
        context_use_authority=fixture,
    )


async def invoke(uow, fixture, provider, lease, request, attempt):
    return await coordinator(uow, fixture, provider).invoke(
        h.RunId(fixture.run_id),
        request,
        execution_lease=lease,
        cancel=CancelToken(),
        context_use=attempt,
    )


@pytest.mark.parametrize("failure", ["memory_commit", "claim_transaction", "lost_lease"])
def test_original_time_and_atomic_claim_after_real_authorization(tmp_path, monkeypatch, failure):
    async def run():
        fixture, db, uow, lease, request, attempt, provider = await setup(tmp_path)
        try:
            if failure == "claim_transaction":
                original = uow._audit_operation_head

                def fail(*args, **kwargs):
                    if args[1] == "provider":
                        raise RuntimeError("claim transaction crash")
                    return original(*args, **kwargs)

                monkeypatch.setattr(uow, "_audit_operation_head", fail)
            else:

                async def after():
                    if failure == "lost_lease":
                        uow.release_runtime_lease(lease, now=time.time())
                    else:
                        raise RuntimeError("after Memory commit before ACK")

                fixture.after_authorize = after
            with pytest.raises(Exception):
                await invoke(uow, fixture, provider, lease, request, attempt)
            assert len(fixture.receipts) == 1 and not provider.calls
            assert (
                db.connection.execute("SELECT count(*) FROM provider_invocations").fetchone()[0]
                == 0
            )
            assert (
                db.connection.execute(
                    "SELECT count(*) FROM provider_context_use_receipt_bindings"
                ).fetchone()[0]
                == 0
            )
            row = db.connection.execute(
                "SELECT intent_json,grant_json FROM provider_context_use_attempts"
            ).fetchone()
            assert row is not None and row["grant_json"] is None
            if failure == "lost_lease":
                return
            fixture.after_authorize = None
            if failure == "claim_transaction":
                monkeypatch.setattr(uow, "_audit_operation_head", original)
            with pytest.raises(Exception, match="conflict"):
                await invoke(
                    uow,
                    fixture,
                    provider,
                    lease,
                    request,
                    dc.replace(attempt, requested_at=attempt.requested_at + 1),
                )
            assert len(fixture.receipts) == 1
            # Reopen the actual store before replaying the same time/intent.
            db.close()
            db = Database.open(tmp_path / "execution.sqlite")
            uow = SqliteExecutionUnitOfWork(db)
            await invoke(uow, fixture, provider, lease, request, attempt)
            await invoke(uow, fixture, provider, lease, request, attempt)
            assert len(provider.calls) == 1
            assert fixture.receipts[0] == fixture.receipts[1]
            view = coordinator(uow, fixture, provider).read_provider_context_use(
                h.RunId(fixture.run_id), request.request_id
            )
            assert view.requested_at == attempt.requested_at
            assert view.receipts == (fixture.receipts[0],)
        finally:
            db.close()
            await fixture.close()

    asyncio.run(run())


@pytest.mark.parametrize("suppress_before_retry", [False, True])
def test_unknown_never_resends_and_confirmed_not_started_needs_new_grant(
    tmp_path, suppress_before_retry
):
    async def run():
        fixture, db, uow, lease, request, attempt, provider = await setup(tmp_path)
        try:
            provider.unknown = True
            with pytest.raises(ProviderInvocationUnknownError):
                await invoke(uow, fixture, provider, lease, request, attempt)
            with pytest.raises(ProviderInvocationUnknownError):
                await invoke(uow, fixture, provider, lease, request, attempt)
            assert len(provider.calls) == 1 and len(fixture.receipts) == 1
            view = coordinator(uow, fixture, provider).read_provider_context_use(
                h.RunId(fixture.run_id), request.request_id
            )
            record = uow.read_provider_invocation(view.invocation_id)
            uow.record_provider_reconciliation(
                record,
                outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
                response_json=None,
                usage_json=None,
                budget_charge=record.budget_charge,
                evidence_ref="source-test:transport-confirmed-not-started",
                now=time.time(),
            )
            if suppress_before_retry:
                await fixture.forget()
            provider.unknown = False
            if suppress_before_retry:
                with pytest.raises(Exception):
                    await invoke(uow, fixture, provider, lease, request, attempt)
                assert len(provider.calls) == 1 and len(fixture.receipts) == 1
            else:
                await invoke(uow, fixture, provider, lease, request, attempt)
                after = coordinator(uow, fixture, provider).read_provider_context_use(
                    h.RunId(fixture.run_id), request.request_id
                )
                assert after.handoff_attempt == 2 and after.invocation_state == "succeeded"
                assert after.provider_attempt_id != view.provider_attempt_id
                assert after.receipts[0].receipt_id != view.receipts[0].receipt_id
                assert len(fixture.receipts) == 2
                await invoke(uow, fixture, provider, lease, request, attempt)
                assert len(provider.calls) == 2
        finally:
            db.close()
            await fixture.close()

    asyncio.run(run())


def test_concurrent_same_request_has_one_real_durable_handoff(tmp_path):
    async def run():
        fixture, db, uow, lease, request, attempt, provider = await setup(tmp_path)
        task = None
        try:
            provider.entered, provider.release = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(invoke(uow, fixture, provider, lease, request, attempt))
            await asyncio.wait_for(provider.entered.wait(), 5)
            with pytest.raises(Exception):
                await invoke(uow, fixture, provider, lease, request, attempt)
            provider.release.set()
            await task
            assert len(provider.calls) == 1
            assert (
                db.connection.execute(
                    "SELECT handoff_attempt FROM provider_invocations"
                ).fetchone()[0]
                == 1
            )
        finally:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            db.close()
            await fixture.close()

    asyncio.run(run())


def test_actual_claimed_continuation_cannot_borrow_root_receipt(tmp_path):
    async def run():
        fixture, db, uow, lease, request, attempt, provider = await setup(tmp_path)
        try:
            await invoke(uow, fixture, provider, lease, request, attempt)
            root = fixture.receipts[0]
            uow.enqueue_continuation(
                continuation_id="continuation-2",
                run_id=fixture.run_id,
                payload={
                    "kind": "conversation_user",
                    "context_use_turn_id": "different-turn-2",
                    "conversation": {
                        "message": {"role": "user", "content": "next"},
                        "memory_text": "next",
                        "context_source_snapshot_ref": None,
                    },
                },
                now=time.time(),
            )
            continuation = uow.claim_continuation(
                run_id=fixture.run_id, execution_lease=lease, now=time.time()
            )
            assert continuation.continuation_id == "continuation-2"
            fragments = await fixture.recall(turn_id="different-turn-2")
            messages = tuple(
                h.Message(h.MessageRole.SYSTEM, canonical(h.thaw_json(f.public_payload)).decode())
                for f in fragments
            )
            new_request = ProviderRequest(
                h.RequestId(fixture.run_id + ":provider-turn:2"), messages, max_output_tokens=128
            )
            wire = provider_request_json(new_request)
            intent = h.RecallContextUseIntentV1(
                fragments, tuple((i + 1, digest(m)) for i, m in enumerate(wire["messages"]))
            )
            new_attempt = dc.replace(
                attempt,
                turn_id="different-turn-2",
                continuation_id="continuation-2",
                provider_request_id=new_request.request_id.value,
                provider_turn_ordinal=2,
                context_snapshot_id="snapshot-2",
                context_snapshot_revision=2,
                request_fingerprint=provider_request_fingerprint(new_request),
                requested_at=time.time(),
                intents=(intent,),
            )
            with pytest.raises(ValueError):
                h.ProviderContextUseGrantV1(new_attempt, (root,))
            cp = DurableReactCheckpoint(uow, clock=time.time)
            state, version = cp.load_or_create(h.RunId(fixture.run_id), lease)
            state = dc.replace(
                state,
                active_turn_id=new_attempt.turn_id,
                active_continuation_id=new_attempt.continuation_id,
                context_use_attempt=new_attempt.to_json(),
                provider_turns_reserved_total=2,
                provider_request_id=new_request.request_id.value,
                provider_request_snapshot=wire,
                provider_request_fingerprint=new_attempt.request_fingerprint,
            )
            cp.cas(h.RunId(fixture.run_id), lease, version, state)
            await invoke(uow, fixture, provider, lease, new_request, new_attempt)
            view = coordinator(uow, fixture, provider).read_provider_context_use(
                h.RunId(fixture.run_id), new_request.request_id
            )
            assert view.turn_id == "different-turn-2"
            assert view.continuation_id == "continuation-2"
            assert view.receipts[0].receipt_id != root.receipt_id
            assert len(provider.calls) == 2
        finally:
            db.close()
            await fixture.close()

    asyncio.run(run())
