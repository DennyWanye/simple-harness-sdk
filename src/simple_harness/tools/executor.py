# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable single-effect Tool executor over injected ledger and fence Ports."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from simple_harness.contracts import EffectId, thaw_json
from simple_harness.execution.effects import (
    EffectRecord,
    EffectState,
    EffectUnitOfWork,
    effect_request_hash,
)
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import RUNTIME_LEASE_NAMESPACE, ExecutionLease

from .authorization import (
    AuthorizationDecision,
    AuthorizationPort,
    PreparedToolEffect,
)
from .contracts import JsonObject, ToolCall, ToolContext, ToolResult
from .reconciliation import (
    ReconciliationState,
    ToolReconciliationPort,
)
from .registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class EffectExecution:
    effect: EffectRecord | None
    result: ToolResult


class EffectExecutor:
    """Prepare, fence, hand off, and settle one stable Tool effect."""

    def __init__(
        self,
        *,
        uow: EffectUnitOfWork,
        registry: ToolRegistry,
        authorization: AuthorizationPort,
        reconciliation: ToolReconciliationPort,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._authorization = authorization
        self._reconciliation = reconciliation
        self._clock = clock

    def _prepared(
        self,
        *,
        effect_id: EffectId,
        call: ToolCall,
        context: ToolContext,
    ) -> PreparedToolEffect:
        tool = self._registry.validate(call)
        return PreparedToolEffect(
            effect_id=effect_id,
            run_id=context.run_id,
            call=call,
            spec=tool.spec,
            context_metadata=context.metadata,
        )

    async def execute(
        self,
        *,
        effect_id: EffectId,
        call: ToolCall,
        context: ToolContext,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
    ) -> EffectExecution:
        if (
            execution_lease.run_id != context.run_id.value
            or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
        ):
            raise ValueError("Tool execution lease belongs to another Run")
        if (
            run_fence.run_id != context.run_id
            or run_fence.owner_id != execution_lease.owner_id
        ):
            raise ValueError("Tool Run fence differs from runtime lease")
        prepared = self._prepared(effect_id=effect_id, call=call, context=context)
        authorization = await self._authorization.authorize(prepared)
        if authorization.decision is not AuthorizationDecision.ALLOW:
            return EffectExecution(
                effect=None,
                result=ToolResult.rejected(
                    call.call_id,
                    authorization.reason_code or "authorization_denied",
                    authorization.public_message
                    or "Tool execution was not authorized.",
                ),
            )
        assert authorization.receipt_ref is not None
        arguments = thaw_json(call.arguments)
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be a JSON object")
        request_hash = effect_request_hash(tool_name=call.name, arguments=arguments)
        record = self._uow.prepare_effect(
                effect_id=effect_id,
                run_id=context.run_id,
                call_id=call.call_id,
                tool_name=call.name,
                arguments=cast(dict[str, object], arguments),
                request_hash=request_hash,
                authorization_receipt_ref=authorization.receipt_ref,
                run_fence=run_fence,
                execution_lease=execution_lease,
                now=self._clock(),
        )
        if record.terminal:
            assert record.result is not None
            return EffectExecution(record, record.result)
        if record.state in {EffectState.HANDED_OFF, EffectState.UNKNOWN}:
            reconciled = await self.reconcile(
                record, context=context, current_fence_epoch=run_fence.epoch
            )
            if not reconciled.dispatch_allowed:
                return EffectExecution(
                    reconciled,
                    reconciled.result
                    or ToolResult.unknown(
                        call.call_id, "Tool outcome is awaiting reconciliation."
                    ),
                )
            record = reconciled
        handed_off = self._uow.mark_effect_handed_off(
                effect_id,
                expected_version=record.version,
                run_fence=run_fence,
                handoff_receipt_ref=(
                    f"tool-handoff:{context.run_id.value}:{call.call_id.value}:"
                    f"{effect_id.value}:{run_fence.epoch}"
                ),
                execution_lease=execution_lease,
                now=self._clock(),
        )
        try:
            result = await self._registry.invoke(call, context)
        except BaseException:
            self._uow.mark_effect_unknown(
                    effect_id,
                    expected_version=handed_off.version,
                    expected_fence_epoch=run_fence.epoch,
                    evidence_ref=(
                        f"tool-dispatch-interrupted:{context.run_id.value}:"
                        f"{effect_id.value}"
                    ),
                    now=self._clock(),
            )
            raise
        settled = self._uow.settle_effect(
                effect_id,
                expected_version=handed_off.version,
                expected_fence_epoch=run_fence.epoch,
                result=result,
                evidence_ref=(
                    f"tool-handler-result:{context.run_id.value}:{effect_id.value}"
                ),
                now=self._clock(),
        )
        return EffectExecution(settled, result)

    async def reconcile(
        self,
        record: EffectRecord,
        *,
        context: ToolContext,
        current_fence_epoch: int,
    ) -> EffectRecord:
        if record.state not in {EffectState.HANDED_OFF, EffectState.UNKNOWN}:
            return record
        arguments = thaw_json(record.arguments)
        if not isinstance(arguments, dict):
            raise TypeError("effect arguments must be a JSON object")
        prepared = self._prepared(
            effect_id=record.effect_id,
            call=ToolCall(
                record.call_id, record.tool_name, cast(JsonObject, arguments)
            ),
            context=context,
        )
        observation = await self._reconciliation.observe(prepared)
        if observation.state is ReconciliationState.COMPLETED:
            assert observation.result is not None
            return self._uow.settle_effect(
                record.effect_id,
                expected_version=record.version,
                expected_fence_epoch=record.fence_epoch,
                result=observation.result,
                evidence_ref=observation.evidence_ref,
                now=self._clock(),
            )
        if observation.state is ReconciliationState.CONFIRMED_NOT_STARTED:
            return self._uow.reset_effect_not_started(
                record.effect_id,
                expected_version=record.version,
                expected_fence_epoch=record.fence_epoch,
                new_fence_epoch=current_fence_epoch,
                evidence_ref=observation.evidence_ref,
                now=self._clock(),
            )
        if record.state is EffectState.UNKNOWN:
            return record
        return self._uow.mark_effect_unknown(
            record.effect_id,
            expected_version=record.version,
            expected_fence_epoch=record.fence_epoch,
            evidence_ref=observation.evidence_ref,
            now=self._clock(),
        )


__all__ = ("EffectExecution", "EffectExecutor")
