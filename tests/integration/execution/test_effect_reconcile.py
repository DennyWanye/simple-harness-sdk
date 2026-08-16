# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from simple_harness.contracts import CallId, EffectId, RequestId, RunId
from simple_harness.execution import (
    EffectRecord,
    EffectState,
    RunFenceLease,
    StaleFenceError,
)
from simple_harness.execution.effects import effect_request_hash
from simple_harness.execution.recovery import ResolutionOutcome
from simple_harness.execution.uow import ExecutionLease
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationResult,
    CancellationToken,
    EffectExecutor,
    FunctionTool,
    PreparedToolEffect,
    ReconciliationObservation,
    ReconciliationState,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class FakeUow:
    def __init__(self, record: EffectRecord | None = None) -> None:
        self.record = record
        self.trace: list[str] = []

    def prepare_effect(self, **values: object) -> EffectRecord:
        self.trace.append("prepare")
        if self.record is None:
            run_fence = values["run_fence"]
            assert isinstance(run_fence, RunFenceLease)
            self.record = EffectRecord(
                arguments=values["arguments"],
                fence_epoch=run_fence.epoch,
                state=EffectState.PREPARED,
                version=0,
                handoff_receipt_ref=None,
                evidence_ref=None,
                result=None,
                **{
                    key: value
                    for key, value in values.items()
                    if key not in {"arguments", "run_fence", "execution_lease", "now"}
                },
            )  # type: ignore[arg-type]
        return self.record

    def read_effect(self, effect_id: EffectId) -> EffectRecord | None:
        assert self.record is None or self.record.effect_id == effect_id
        return self.record

    def mark_effect_handed_off(
        self, effect_id: EffectId, **values: object
    ) -> EffectRecord:
        self.trace.append("handoff")
        assert self.record is not None and self.record.effect_id == effect_id
        assert values["expected_version"] == self.record.version
        self.record = replace(
            self.record,
            state=EffectState.HANDED_OFF,
            version=self.record.version + 1,
            handoff_receipt_ref=values["handoff_receipt_ref"],
        )
        return self.record

    def settle_effect(self, effect_id: EffectId, **values: object) -> EffectRecord:
        self.trace.append("settle")
        assert self.record is not None and self.record.effect_id == effect_id
        result = values["result"]
        assert isinstance(result, ToolResult)
        self.record = replace(
            self.record,
            state=EffectState(result.outcome.value),
            version=self.record.version + 1,
            result=result,
            evidence_ref=values["evidence_ref"],
        )
        return self.record

    def mark_effect_unknown(
        self, effect_id: EffectId, **values: object
    ) -> EffectRecord:
        self.trace.append("unknown")
        assert self.record is not None and self.record.effect_id == effect_id
        self.record = replace(
            self.record,
            state=EffectState.UNKNOWN,
            version=self.record.version + 1,
            evidence_ref=values["evidence_ref"],
        )
        return self.record

    def read_reconciliation_resolution(self, **_values):
        return None

    def refresh_prepared_effect_authority(
        self, record: EffectRecord, **values: object
    ) -> EffectRecord:
        self.trace.append("refresh")
        run_fence = values["run_fence"]
        assert isinstance(run_fence, RunFenceLease)
        self.record = replace(
            record,
            authorization_receipt_ref=str(values["authorization_receipt_ref"]),
            fence_epoch=run_fence.epoch,
            version=record.version + 1,
        )
        return self.record

    def record_tool_reconciliation(self, record, **values):
        if values["outcome"] is ResolutionOutcome.COMPLETED:
            return self.settle_effect(
                record.effect_id,
                result=values["result"],
                evidence_ref=values["evidence_ref"],
            )
        return record


class Allow:
    async def authorize(self, _prepared: PreparedToolEffect) -> AuthorizationResult:
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, receipt_ref="authorization:1"
        )

    async def bind_effect_handoff(
        self, _prepared, _authorization_receipt_ref, sdk_receipt
    ) -> AuthorizationReceipt:
        return AuthorizationReceipt("host:handoff:1", "a" * 64, sdk_receipt.receipt_hash)


class AllowWithoutHandoff:
    async def authorize(self, _prepared: PreparedToolEffect) -> AuthorizationResult:
        return AuthorizationResult(
            AuthorizationDecision.ALLOW, receipt_ref="authorization:missing-handoff"
        )


class Observe:
    def __init__(self, observation: ReconciliationObservation) -> None:
        self.observation = observation
        self.calls = 0

    async def observe(self, _effect: PreparedToolEffect) -> ReconciliationObservation:
        self.calls += 1
        return self.observation


def _spec() -> ToolSpec:
    return ToolSpec(
        "read",
        "Read data.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def _context() -> ToolContext:
    return ToolContext(RunId("run-1"), RequestId("request-1"), CancellationToken())


LEASE = ExecutionLease("run-1", "runtime.kernel", "worker-1", 1, 100.0)
FENCE = RunFenceLease(RunId("run-1"), 1, "worker-1", LEASE.epoch)


def _executor(
    uow: FakeUow,
    handler: object,
    observation: ReconciliationObservation,
    *,
    authorization: object | None = None,
) -> tuple[EffectExecutor, Observe]:
    observe = Observe(observation)
    executor = EffectExecutor(
        uow=uow,
        registry=ToolRegistry([FunctionTool(_spec(), handler)]),  # type: ignore[arg-type]
        authorization=authorization or Allow(),  # type: ignore[arg-type]
        reconciliation=observe,
    )
    return executor, observe


def test_missing_host_handoff_receipt_fails_before_physical_tool() -> None:
    uow = FakeUow()
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unused"),
        authorization=AllowWithoutHandoff(),
    )
    with pytest.raises(TypeError, match="bind_effect_handoff"):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("effect-missing-handoff"),
                call=ToolCall(CallId("call-1"), "read", {"path": "."}),
                context=_context(),
                execution_lease=LEASE,
                run_fence=FENCE,
            )
        )
    assert calls == 0
    assert uow.record is not None and uow.record.state is EffectState.PREPARED


def test_effect_is_handed_off_before_handler_and_then_settled() -> None:
    uow = FakeUow()
    calls = 0

    def handler(arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        assert uow.record is not None
        assert uow.record.state is EffectState.HANDED_OFF
        return ToolResult.succeeded(CallId("call-1"), {"path": arguments})

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unused"),
    )
    execution = asyncio.run(
        executor.execute(
            effect_id=EffectId("effect-1"),
            call=ToolCall(CallId("call-1"), "read", {"path": "."}),
            context=_context(),
            execution_lease=LEASE,
            run_fence=FENCE,
        )
    )

    assert execution.effect is not None
    assert execution.effect.state is EffectState.SUCCEEDED
    assert uow.trace == ["prepare", "handoff", "settle"]
    assert calls == 1


def test_mismatched_runtime_and_run_fence_owner_never_reaches_handler() -> None:
    uow = FakeUow()
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unused"),
    )
    mismatched = RunFenceLease(RunId("run-1"), 1, "different-owner", LEASE.epoch)

    with pytest.raises(ValueError, match="differs"):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "read", {"path": "."}),
                context=_context(),
                execution_lease=LEASE,
                run_fence=mismatched,
            )
        )

    assert calls == 0
    assert uow.trace == []


def test_still_unknown_reconciliation_never_dispatches_again() -> None:
    original_call = ToolCall(CallId("call-1"), "read", {"path": "."})
    uow = FakeUow(
        EffectRecord(
            effect_id=EffectId("effect-1"),
            run_id=RunId("run-1"),
            call_id=original_call.call_id,
            tool_name="read",
            request_hash=effect_request_hash(tool_name="read", arguments={"path": "."}),
            arguments=original_call.arguments,
            state=EffectState.UNKNOWN,
            version=2,
            fence_epoch=1,
            authorization_receipt_ref="authorization:1",
            handoff_receipt_ref="handoff:1",
            evidence_ref="crash:1",
        )
    )
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, observe = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "probe:1"),
    )
    execution = asyncio.run(
        executor.execute(
            effect_id=EffectId("effect-1"),
            call=original_call,
            context=_context(),
            execution_lease=LEASE,
            run_fence=FENCE,
        )
    )

    assert execution.result.outcome.value == "unknown"
    assert execution.effect is not None
    assert execution.effect.state is EffectState.UNKNOWN
    assert calls == 0
    assert observe.calls == 1
    assert "handoff" not in uow.trace


def test_completed_reconciliation_settles_without_dispatch() -> None:
    call = ToolCall(CallId("call-1"), "read", {"path": "."})
    uow = FakeUow(
        EffectRecord(
            effect_id=EffectId("effect-1"),
            run_id=RunId("run-1"),
            call_id=call.call_id,
            tool_name="read",
            request_hash=effect_request_hash(tool_name="read", arguments={"path": "."}),
            arguments=call.arguments,
            state=EffectState.HANDED_OFF,
            version=1,
            fence_epoch=1,
            authorization_receipt_ref="authorization:1",
            handoff_receipt_ref="handoff:1",
        )
    )
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(
            ReconciliationState.COMPLETED,
            "external-receipt:1",
            ToolResult.succeeded(CallId("call-1"), {"ok": True}),
        ),
    )
    execution = asyncio.run(
        executor.execute(
            effect_id=EffectId("effect-1"),
            call=call,
            context=_context(),
            execution_lease=LEASE,
            run_fence=FENCE,
        )
    )

    assert execution.effect is not None
    assert execution.effect.state is EffectState.SUCCEEDED
    assert calls == 0
    assert uow.trace == ["settle"]


def test_executor_cancellation_marks_handed_off_effect_unknown() -> None:
    uow = FakeUow()
    started = asyncio.Event()

    async def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "probe:1"),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "read", {"path": "."}),
                context=_context(),
                execution_lease=LEASE,
                run_fence=FENCE,
            )
        )
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert uow.record is not None
    assert uow.record.state is EffectState.UNKNOWN
    assert uow.trace == ["prepare", "handoff", "unknown"]


def test_stale_fence_prevents_physical_dispatch() -> None:
    class StaleHandoffUow(FakeUow):
        def mark_effect_handed_off(
            self, effect_id: EffectId, **values: object
        ) -> EffectRecord:
            del effect_id, values
            self.trace.append("handoff")
            raise StaleFenceError("stale Run fence")

    uow = StaleHandoffUow()
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unused"),
    )
    with pytest.raises(StaleFenceError):
        asyncio.run(
            executor.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "read", {"path": "."}),
                context=_context(),
                execution_lease=LEASE,
                run_fence=FENCE,
            )
        )

    assert calls == 0
    assert uow.trace == ["prepare", "handoff"]


def test_retry_prepared_uses_explicit_authority_refresh_before_dispatch() -> None:
    call = ToolCall(CallId("call-1"), "read", {"path": "."})
    uow = FakeUow(
        EffectRecord(
            effect_id=EffectId("effect-1"),
            run_id=RunId("run-1"),
            call_id=call.call_id,
            tool_name=call.name,
            request_hash=effect_request_hash(
                tool_name=call.name, arguments={"path": "."}
            ),
            arguments=call.arguments,
            state=EffectState.PREPARED,
            version=3,
            fence_epoch=1,
            authorization_receipt_ref="authorization:old",
            handoff_receipt_ref=None,
            evidence_ref="confirmed-not-started:1",
            handoff_attempt=1,
            rehandoff_count=1,
        )
    )
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    executor, _ = _executor(
        uow,
        handler,
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "unused"),
    )
    execution = asyncio.run(
        executor.execute(
            effect_id=EffectId("effect-1"),
            call=call,
            context=_context(),
            execution_lease=LEASE,
            run_fence=RunFenceLease(RunId("run-1"), 2, "worker-1", LEASE.epoch),
        )
    )
    assert execution.result.outcome.value == "succeeded"
    assert uow.trace == ["refresh", "handoff", "settle"]
    assert calls == 1
