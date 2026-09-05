"""Public Run audit against actual SQLite kernel; no diagnostic oracle."""

import asyncio

import pytest

from simple_harness.contracts import RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.tools import AuthorizationDecision

from .test_react_sqlite_runtime import (
    AuthorizationScenario,
    PhysicalToolCounter,
    authorization_runtime,
    start_authorization_wait,
    wait_for_scenario,
)


@pytest.mark.parametrize("outcome", [AuthorizationDecision.DENY, AuthorizationDecision.ALLOW])
def test_kernel_authorization_audit_reopens_without_dispatch(tmp_path, outcome):
    async def case():
        path = tmp_path / "audit.db"
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            path,
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="audit-owner",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        waiting = uow.read_run_operation_audit(RunId("run-fault"))
        assert any(o.kind == "tool" and o.state == "waiting" for o in waiting.operations)
        await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=outcome,
        )
        if outcome is AuthorizationDecision.ALLOW:
            await wait_for_scenario(runtime, lambda: physical.calls == 1)
        await runtime.wait_idle(RunId("run-fault"))
        before = uow.read_run_operation_audit(RunId("run-fault"))
        assert not before.truncated
        assert any(
            o.kind == "decision"
            and o.state == ("denied" if outcome is AuthorizationDecision.DENY else "allowed")
            for o in before.operations
        )
        assert physical.calls == (0 if outcome is AuthorizationDecision.DENY else 1)
        await runtime.close()
        database.close()
        reopened = Database.open(path)
        after = SqliteExecutionUnitOfWork(reopened).read_run_operation_audit(RunId("run-fault"))
        assert before.snapshot_hash == after.snapshot_hash
        assert physical.calls == (0 if outcome is AuthorizationDecision.DENY else 1)
        reopened.close()

    asyncio.run(case())


def test_kernel_unknown_then_authorized_retry_is_audited(tmp_path):
    from simple_harness.contracts import ExecutionSessionId, Message, MessageRole, RequestId
    from simple_harness.execution.recovery import ResolutionOutcome
    from simple_harness.providers import ProviderResponse, ProviderTimeoutError
    from simple_harness.runtime import RunStart

    from .test_react_sqlite_runtime import Provider

    class TimeoutOnce(Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            self.now[0] += 4.0
            if len(self.requests) == 1:
                raise ProviderTimeoutError()
            from simple_harness.providers import ProviderUsage

            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "done"),
                model="model",
                usage=ProviderUsage(7, 3, 10, cache_tokens=1, reasoning_tokens=2),
            )

    async def case():
        path = tmp_path / "retry.db"
        provider = TimeoutOnce()
        physical = PhysicalToolCounter()
        now = [10.0]
        provider.now = now
        runtime, uow, database = authorization_runtime(
            path,
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="retry-owner",
            provider=provider,
            clock=lambda: now[0],
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-fault"),
                RunId("run-fault"),
                RequestId("request-fault"),
                "turn-fault",
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "capability_snapshot": {"tools": []},
                    "max_output_tokens": 100,
                },
                1,
            )
        )
        await runtime.wait_idle(RunId("run-fault"))
        first = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        assert any(o.kind == "provider" and o.state == "unknown" for o in first.operations)
        unknown_head = next(
            o for o in first.operations if o.kind == "provider" and o.record_type == "head"
        )
        assert unknown_head.handoff_to_settlement_seconds == 4.0
        assert unknown_head.usage.input_tokens is None
        assert unknown_head.error_code is not None
        assert len(provider.requests) == 1 and physical.calls == 0
        await runtime.close()
        database.close()
        database = Database.open(path)
        uow = SqliteExecutionUnitOfWork(database)
        assert first.snapshot_hash == uow.read_run_operation_audit(RunId("run-fault")).snapshot_hash
        (record,) = uow.list_incomplete_provider_invocations()
        assert record.state.value == "unknown"
        now[0] = 30.0
        uow.record_provider_reconciliation(
            record,
            outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
            response_json=None,
            usage_json=None,
            budget_charge=record.budget_charge,
            evidence_ref="test:actual-not-started-observation",
            now=now[0],
        )
        database.close()
        runtime, uow, database = authorization_runtime(
            path,
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="retry-owner-2",
            provider=provider,
            clock=lambda: now[0],
        )
        await runtime.start()
        await wait_for_scenario(
            runtime, lambda: uow.read_run("run-fault").state.value == "completed"
        )
        final = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        heads = [o for o in final.operations if o.kind == "provider" and o.record_type == "head"]
        assert len(heads) == 1 and heads[0].state == "succeeded"
        assert heads[0].usage.total_tokens == 10
        assert heads[0].usage.cache_tokens == 1 and heads[0].usage.reasoning_tokens == 2
        assert final.to_json()["snapshot_hash"] == final.snapshot_hash
        assert heads[0].handoff_to_settlement_seconds == 4.0
        assert heads[0].created_at < heads[0].handed_off_at
        assert heads[0].handoff_attempt == 2 and heads[0].rehandoff_count == 1
        assert any(
            o.kind == "provider"
            and o.record_type == "transition"
            and o.state == "unknown"
            and o.handoff_attempt == 1
            for o in final.operations
        )
        assert len(provider.requests) == 2 and physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize("mode", ["immediate_deny", "unknown_tool", "bad_arguments"])
def test_pre_effect_rejection_is_durable_without_an_effect(tmp_path, mode):
    from simple_harness import CallId, ExecutionSessionId, Message, MessageRole, RequestId
    from simple_harness.providers import ProviderResponse, ProviderToolCall
    from simple_harness.runtime import RunStart
    from simple_harness.tools.authorization import AuthorizationResult

    from .test_react_sqlite_runtime import Provider

    class Deny(AuthorizationScenario):
        async def prepare(self, prepared):
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                reason_code="policy_denied",
                public_message="Do not export this arbitrary text.",
            )

    class Calls(Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            calls = ()
            if len(self.requests) == 1:
                calls = (
                    ProviderToolCall(
                        CallId("raw-fault"),
                        "missing_tool" if mode == "unknown_tool" else "write_note",
                        {"extra": "credential-shaped-value-not-for-export"}
                        if mode == "bad_arguments"
                        else {},
                    ),
                )
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "tool proposal"),
                tool_calls=calls,
                model="model",
            )

    async def case():
        path = tmp_path / "deny.db"
        physical = PhysicalToolCounter()
        provider = Calls()
        runtime, uow, database = authorization_runtime(
            path,
            authorization=Deny(),
            physical=physical,
            owner_id="deny-owner",
            provider=provider,
            clock=lambda: 10.0,
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-fault"),
                RunId("run-fault"),
                RequestId("request-fault"),
                "turn-fault",
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "capability_snapshot": {"tools": ["write_note"]},
                    "max_output_tokens": 100,
                },
                1,
            )
        )
        await runtime.wait_idle(RunId("run-fault"))
        snapshot = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        assert not any(o.kind == "effect" for o in snapshot.operations)
        assert any(o.kind == "tool" and o.state == "requested" for o in snapshot.operations)
        assert any(
            o.kind == "tool" and o.state in {"failed", "rejected"} for o in snapshot.operations
        )
        assert any(
            o.kind == "tool"
            and o.operation_name == ("missing_tool" if mode == "unknown_tool" else "write_note")
            for o in snapshot.operations
        )
        assert physical.calls == 0
        assert "credential-shaped-value-not-for-export" not in str(snapshot.to_json())
        tiny = await runtime.client.read_run_operation_audit(RunId("run-fault"), limit=1)
        assert tiny.truncated and not tiny.current_source_complete
        await runtime.close()
        database.close()
        reopened = Database.open(path)
        assert (
            SqliteExecutionUnitOfWork(reopened)
            .read_run_operation_audit(RunId("run-fault"))
            .snapshot_hash
            == snapshot.snapshot_hash
        )
        reopened.close()

    asyncio.run(case())


def test_diagnostic_overflow_and_absent_sink_do_not_supply_audit(tmp_path):
    from simple_harness.observability import CorrelationContext, ObservabilityRuntime, RecordingSink

    async def case():
        sink = RecordingSink(capacity=1)
        diagnostics = ObservabilityRuntime(sink, queue_capacity=1)
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "diagnostics.db",
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="diagnostics-owner",
            clock=lambda: 10.0,
            observability=diagnostics,
        )
        await start_authorization_wait(runtime, uow)
        before = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        for _ in range(100):
            diagnostics.emit_transition(
                "test.audit",
                component="runtime",
                operation="test",
                outcome="succeeded",
                correlation=CorrelationContext.from_authority_ids(run_id="run-fault"),
            )
        diagnostics.flush(timeout=0.5)
        assert sink.overflow_count + diagnostics.counters.overflow_dropped > 0
        assert (
            await runtime.client.read_run_operation_audit(RunId("run-fault"))
        ).snapshot_hash == before.snapshot_hash
        await runtime.close()
        database.close()
        diagnostics.close()

    asyncio.run(case())


def test_failed_audit_append_rolls_back_canonical_provider_settlement(tmp_path):
    from simple_harness.execution.provider_invocations import provider_response_json
    from simple_harness.execution.recovery import ResolutionOutcome

    from .test_h13_provider_recovery import _response, _unknown

    database, uow, _, _, record = _unknown(tmp_path)
    before = uow.read_run_operation_audit(RunId("run-1"))

    def fail(point):
        if point == "operation_audit.provider.succeeded.after_write":
            raise RuntimeError("audit write failed")

    uow.workflow_fault = fail
    with pytest.raises(RuntimeError, match="audit write failed"):
        uow.record_provider_reconciliation(
            record,
            outcome=ResolutionOutcome.COMPLETED,
            response_json=provider_response_json(_response(record)),
            usage_json={"usage": None, "budget": record.budget_charge.to_json()},
            budget_charge=record.budget_charge,
            evidence_ref="test:completed",
            now=5.0,
        )
    assert uow.read_provider_invocation(record.invocation_id).state.value == "unknown"
    assert uow.read_run_operation_audit(RunId("run-1")).snapshot_hash == before.snapshot_hash
    database.close()


def test_same_raw_call_across_runs_preserves_distinct_effect_links(tmp_path):
    from simple_harness import CallId, ExecutionSessionId, Message, MessageRole, RequestId
    from simple_harness.providers import ProviderResponse, ProviderToolCall
    from simple_harness.runtime import RunStart
    from simple_harness.tools.authorization import AuthorizationResult

    from .test_react_sqlite_runtime import Provider

    class Allow(AuthorizationScenario):
        async def prepare(self, prepared):
            return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref="test:allow")

    class TwoRuns(Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "done"),
                tool_calls=(ProviderToolCall(CallId("raw-fault"), "write_note", {}),)
                if len(self.requests) % 2
                else (),
                model="model",
            )

    async def case():
        physical = PhysicalToolCounter()
        provider = TwoRuns()
        runtime, uow, database = authorization_runtime(
            tmp_path / "two.db",
            authorization=Allow(),
            physical=physical,
            provider=provider,
            owner_id="two-owner",
            clock=lambda: 10.0,
        )
        await runtime.start()
        heads = []
        for name in ("run-fault", "run-other"):
            await runtime.client.start(
                RunStart(
                    ExecutionSessionId("session-fault"),
                    RunId(name),
                    RequestId("request-" + name),
                    "turn-" + name,
                    {
                        "messages": [{"role": "user", "content": "write"}],
                        "capability_snapshot": {"tools": ["write_note"]},
                        "max_output_tokens": 100,
                    },
                    1,
                )
            )
            for _ in range(100):
                await runtime.wait_idle(RunId(name))
                if uow.read_run(name).state.value == "completed":
                    break
                await asyncio.sleep(0)
            assert uow.read_run(name).state.value == "completed"
            snapshot = await runtime.client.read_run_operation_audit(RunId(name))
            (head,) = [
                o for o in snapshot.operations if o.kind == "effect" and o.record_type == "head"
            ]
            assert head.operation_name == "write_note" and head.raw_call_id == "raw-fault"
            assert head.request_hash is not None and head.result_hash is not None
            assert head.provider_invocation_id is not None
            assert any(
                o.kind == "provider" and o.source_id == head.provider_invocation_id
                for o in snapshot.operations
            )
            assert head.call_id != head.raw_call_id
            heads.append(head)
        assert heads[0].operation_id != heads[1].operation_id
        assert heads[0].call_id != heads[1].call_id
        assert physical.calls == 2 and len(provider.requests) == 4
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_cancel_before_authorization_is_recorded_without_tool_handoff(tmp_path):
    async def case():
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "cancel.db",
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="cancel-owner",
            clock=lambda: 10.0,
        )
        await start_authorization_wait(runtime, uow)
        await runtime.client.cancel(RunId("run-fault"))
        snapshot = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        assert snapshot.run_state == "cancelled"
        assert any(o.kind == "decision" and o.state == "cancelled" for o in snapshot.operations)
        assert not any(o.kind == "effect" for o in snapshot.operations)
        assert physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_audit_unavailable_bounds_and_corruption_are_explicit(tmp_path):
    from simple_harness import RunAuditUnavailable

    from .test_h13_provider_recovery import _unknown

    database, uow, _, _, _ = _unknown(tmp_path)
    with pytest.raises(RunAuditUnavailable, match="run_not_found"):
        uow.read_run_operation_audit(RunId("other-run"))
    for limit in (True, 0, -1, 4097):
        with pytest.raises(ValueError):
            uow.read_run_operation_audit(RunId("run-1"), limit=limit)
    snapshot = uow.read_run_operation_audit(RunId("run-1"))
    assert snapshot.current_source_complete
    assert snapshot.history_coverage == "partial"
    # Explicit corruption negative, never passed off as legacy original history.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE run_events SET payload_json='{}' WHERE run_id='run-1' "
            "AND kind='audit.transition.v1'"
        )
    with pytest.raises(RunAuditUnavailable, match="audit_fact_hash_mismatch"):
        uow.read_run_operation_audit(RunId("run-1"))
    database.close()


@pytest.mark.parametrize("outcome", ["failed", "partial", "unknown"])
def test_real_tool_settlement_preserves_outcome_and_redacts_payload(tmp_path, outcome):
    from simple_harness import CallId, ExecutionSessionId, RequestId
    from simple_harness.runtime import RunStart
    from simple_harness.tools import ToolResult
    from simple_harness.tools.authorization import AuthorizationResult

    class Allow(AuthorizationScenario):
        async def prepare(self, prepared):
            return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref="test:allow")

    def result():
        if outcome == "failed":
            return ToolResult.failed(
                CallId("raw-fault"), "fixture_failed", "Bearer AUDIT_SECRET_CANARY"
            )
        if outcome == "partial":
            return ToolResult.partial(CallId("raw-fault"), {"private": "AUDIT_SECRET_CANARY"})
        return ToolResult.unknown(CallId("raw-fault"), "AUDIT_SECRET_CANARY")

    async def case():
        physical = PhysicalToolCounter()
        path = tmp_path / "outcome.db"
        runtime, uow, database = authorization_runtime(
            path,
            authorization=Allow(),
            physical=physical,
            owner_id="outcome-owner",
            clock=lambda: 10.0,
            tool_result_factory=result,
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-fault"),
                RunId("run-fault"),
                RequestId("request-fault"),
                "turn-fault",
                {
                    "messages": [{"role": "user", "content": "write"}],
                    "capability_snapshot": {"tools": ["write_note"]},
                    "max_output_tokens": 100,
                },
                1,
            )
        )
        await runtime.wait_idle(RunId("run-fault"))
        snapshot = await runtime.client.read_run_operation_audit(RunId("run-fault"))
        (head,) = [o for o in snapshot.operations if o.kind == "effect" and o.record_type == "head"]
        assert head.state == outcome
        assert head.handoff_attempt == 1 and physical.calls == 1
        if outcome == "failed":
            assert head.error_code == "fixture_failed"
        assert "AUDIT_SECRET_CANARY" not in str(snapshot.to_json())
        assert snapshot.run_state == ("waiting" if outcome == "unknown" else "completed")
        await runtime.close()
        database.close()
        reopened = Database.open(path)
        assert (
            SqliteExecutionUnitOfWork(reopened)
            .read_run_operation_audit(RunId("run-fault"))
            .snapshot_hash
            == snapshot.snapshot_hash
        )
        assert physical.calls == 1
        reopened.close()

    asyncio.run(case())


def test_snapshot_is_atomic_when_another_connection_settles_same_run(tmp_path):
    from simple_harness.execution.provider_invocations import provider_response_json
    from simple_harness.execution.recovery import ResolutionOutcome

    from .test_h13_provider_recovery import _response, _unknown

    database, uow, _, _, record = _unknown(tmp_path)
    writer_database = Database.open(database.path, wal=True)
    writer = SqliteExecutionUnitOfWork(writer_database)
    before = uow.read_run_operation_audit(RunId("run-1"))
    writes = []

    def concurrent_settle(sql):
        if sql.startswith("SELECT * FROM provider_invocations WHERE run_id=") and not writes:
            writes.append(True)
            writer.record_provider_reconciliation(
                record,
                outcome=ResolutionOutcome.COMPLETED,
                response_json=provider_response_json(_response(record)),
                usage_json={"usage": None, "budget": record.budget_charge.to_json()},
                budget_charge=record.budget_charge,
                evidence_ref="test:complete",
                now=5.0,
            )

    database.connection.set_trace_callback(concurrent_settle)
    during = uow.read_run_operation_audit(RunId("run-1"))
    database.connection.set_trace_callback(None)
    after = uow.read_run_operation_audit(RunId("run-1"))
    assert writes == [True]
    assert during.snapshot_hash == before.snapshot_hash
    assert after.snapshot_hash != before.snapshot_hash
    assert (
        next(o for o in after.operations if o.kind == "provider" and o.record_type == "head").state
        == "succeeded"
    )
    writer_database.close()
    database.close()


def test_terminal_effect_replay_with_expired_lease_does_not_restamp_audit(tmp_path):
    from simple_harness.contracts import CallId, RequestId
    from simple_harness.execution.recovery import ResolutionOutcome
    from simple_harness.tools import (
        CancellationToken,
        EffectExecutor,
        ToolCall,
        ToolContext,
        ToolRegistry,
        ToolResult,
    )

    from .test_h13_tool_recovery import _unknown

    database, uow, _, lease, fence, unknown = _unknown(tmp_path)
    completed = uow.record_tool_reconciliation(
        unknown,
        outcome=ResolutionOutcome.COMPLETED,
        result=ToolResult.succeeded(CallId("raw-call"), {"ok": True}),
        evidence_ref="external:completed",
        now=6.0,
    )
    before = uow.read_run_operation_audit(RunId("run-1")).snapshot_hash
    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry(),
        authorization=object(),
        reconciliation=object(),
        clock=lambda: 1000.0,
    )
    values = dict(
        effect_id=completed.effect_id,
        context=ToolContext(RunId("run-1"), RequestId("request-1"), CancellationToken()),
        execution_lease=lease,
        run_fence=fence,
        raw_call_id="raw-call",
        turn_ordinal=1,
        call_ordinal=0,
    )
    replay = asyncio.run(
        executor.execute(
            call=ToolCall(completed.call_id, "calculator", {"x": 1}),
            **values,
        )
    )
    assert replay.effect == completed
    assert replay.result == completed.result
    with pytest.raises(ValueError, match="identity conflicts"):
        asyncio.run(
            executor.execute(
                call=ToolCall(completed.call_id, "calculator", {"x": 2}),
                **values,
            )
        )
    assert uow.read_run_operation_audit(RunId("run-1")).snapshot_hash == before
    database.close()
