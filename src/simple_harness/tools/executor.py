# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable single-effect Tool executor over injected ledger and fence Ports."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    thaw_json,
)
from simple_harness.execution.effects import (
    EffectRecord,
    EffectState,
    EffectUnitOfWork,
    effect_request_hash,
)
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.recovery import RecoveryKind, ResolutionOutcome
from simple_harness.execution.uow import RUNTIME_LEASE_NAMESPACE, ExecutionLease
from simple_harness.providers import ProviderToolSpec
from simple_harness.workflow.lease import WorkflowLease

from .authorization import (
    AuthorizationDecision,
    AuthorizationPort,
    PreparedToolEffect,
)
from .contracts import ToolCall, ToolContext, ToolResult
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

    def provider_tool_specs(
        self, names: tuple[str, ...]
    ) -> tuple[ProviderToolSpec, ...]:
        """Project an allowlisted capability snapshot into Provider declarations."""

        requested = set(names)
        return tuple(
            ProviderToolSpec(
                spec.name,
                spec.description,
                cast(Mapping[str, JsonValue], thaw_json(spec.input_schema)),
            )
            for spec in self._registry.specs
            if spec.name in requested
        )

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
        workflow_lease: WorkflowLease | None = None,
        raw_call_id: str | None = None,
        turn_ordinal: int = 0,
        call_ordinal: int = 0,
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
        arguments = thaw_json(call.arguments)
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be a JSON object")
        request_hash = effect_request_hash(tool_name=call.name, arguments=arguments)
        existing = self._uow.read_effect(effect_id)
        not_started_resolution = None
        refresh_prepared_authority = False
        if existing is not None:
            if (
                existing.run_id != context.run_id
                or existing.call_id != call.call_id
                or existing.tool_name != call.name
                or existing.request_hash != request_hash
                or thaw_json(existing.arguments) != arguments
                or existing.raw_call_id != raw_call_id
                or existing.turn_ordinal != turn_ordinal
                or existing.call_ordinal != call_ordinal
            ):
                raise ValueError("Tool effect identity conflicts with frozen intent")
            if existing.terminal:
                assert existing.result is not None
                return EffectExecution(existing, existing.result)
            if existing.state in {EffectState.HANDED_OFF, EffectState.UNKNOWN}:
                reconciled = await self.reconcile(
                    existing,
                    context=context,
                    current_fence_epoch=run_fence.epoch,
                )
                if reconciled.terminal:
                    assert reconciled.result is not None
                    return EffectExecution(reconciled, reconciled.result)
                not_started_resolution = self._uow.read_reconciliation_resolution(
                    kind=RecoveryKind.TOOL.value,
                    ledger_identity=existing.effect_id.value,
                    handoff_attempt=existing.handoff_attempt,
                )
                if (
                    not_started_resolution is None
                    or not_started_resolution.outcome
                    is not ResolutionOutcome.CONFIRMED_NOT_STARTED
                ):
                    return EffectExecution(
                        reconciled,
                        reconciled.result
                        or ToolResult.unknown(
                            call.call_id, "Tool outcome is awaiting reconciliation."
                        ),
                    )
            elif existing.state is EffectState.PREPARED:
                refresh_prepared_authority = True
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
        if existing is not None and not_started_resolution is not None:
            record = self._uow.reauthorize_effect_not_started(
                existing,
                authorization_receipt_ref=authorization.receipt_ref,
                resolution=not_started_resolution,
                run_fence=run_fence,
                execution_lease=execution_lease,
                now=self._clock(),
            )
            self._registry.allow_confirmed_not_started(call.call_id)
        elif (
            existing is not None
            and refresh_prepared_authority
            and (
                existing.fence_epoch != run_fence.epoch
                or existing.authorization_receipt_ref != authorization.receipt_ref
            )
        ):
            record = self._uow.refresh_prepared_effect_authority(
                existing,
                authorization_receipt_ref=authorization.receipt_ref,
                run_fence=run_fence,
                execution_lease=execution_lease,
                now=self._clock(),
            )
        else:
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
                raw_call_id=raw_call_id,
                turn_ordinal=turn_ordinal,
                call_ordinal=call_ordinal,
            )
        handed_off = self._uow.mark_effect_handed_off(
            effect_id,
            expected_version=record.version,
            run_fence=run_fence,
            handoff_receipt_ref=(
                f"tool-handoff:{context.run_id.value}:{call.call_id.value}:"
                f"{effect_id.value}:{run_fence.epoch}"
            ),
            execution_lease=execution_lease,
            workflow_lease=workflow_lease,
            now=self._clock(),
        )
        try:
            result = await self._registry.invoke(
                call,
                context,
                accepted_result_call_id=(
                    None if raw_call_id is None else CallId(raw_call_id)
                ),
            )
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
        if result.call_id != call.call_id:
            result = ToolResult(
                call_id=call.call_id,
                outcome=result.outcome,
                value=cast(FrozenJsonValue, thaw_json(result.value)),
                error_code=result.error_code,
                public_message=result.public_message,
                retryable=result.retryable,
            )
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
        observation = await self._reconciliation.observe(record)
        if observation.state is ReconciliationState.COMPLETED:
            assert observation.result is not None
            return self._uow.record_tool_reconciliation(
                record,
                outcome=ResolutionOutcome.COMPLETED,
                result=observation.result,
                evidence_ref=observation.evidence_ref,
                now=self._clock(),
            )
        if observation.state is ReconciliationState.CONFIRMED_NOT_STARTED:
            return self._uow.record_tool_reconciliation(
                record,
                outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
                result=None,
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
