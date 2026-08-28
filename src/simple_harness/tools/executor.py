# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable single-effect Tool executor over injected ledger and fence Ports."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import cast

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    RunId,
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
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    DecisionRecord,
    DecisionState,
    ExecutionLease,
)
from simple_harness.observability import CorrelationContext, ObservabilityRuntime, Outcome
from simple_harness.providers import ProviderToolSpec
from simple_harness.workflow.lease import WorkflowLease

from .authorization import (
    AuthorizationDecision,
    AuthorizationPort,
    AuthorizationRequest,
    PreparedToolEffect,
    bind_authorization_receipts,
    sdk_authorization_receipt,
)
from .contracts import ToolCall, ToolContext, ToolResult
from .reconciliation import (
    ReconciliationState,
    ToolReconciliationPort,
)
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EffectExecution:
    effect: EffectRecord | None
    result: ToolResult


class ToolAuthorizationPending(RuntimeError):
    """Control result: the Run must durably wait for this exact decision."""

    def __init__(self, prepared: PreparedToolEffect, request: AuthorizationRequest) -> None:
        super().__init__(f"authorization:{prepared.effect_id.value}")
        self.prepared = prepared
        self.request = request

    @property
    def decision_id(self) -> str:
        return f"authorization:{self.prepared.effect_id.value}"


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
        self._observability: ObservabilityRuntime | None = None

    def _emit_attempt(
        self,
        record: EffectRecord,
        *,
        outcome: Outcome,
        error_code: str | None = None,
    ) -> None:
        if self._observability is None:
            return
        self._observability.emit_transition(
            f"tool_attempt.{outcome.value}",
            component="tool",
            operation="invoke",
            outcome=outcome,
            correlation=CorrelationContext.from_authority_ids(
                run_id=record.run_id.value,
                call_id=record.call_id.value,
                effect_id=record.effect_id.value,
            ),
            attributes={
                "entity_kind": "tool_attempt",
                "entity_id": record.effect_id.value,
                "run_id": record.run_id.value,
                "attempt": record.handoff_attempt,
                "lease_epoch": record.fence_epoch,
                "state_version": record.version,
                "to_state": record.state.value,
                "error_code": error_code,
            },
        )

    def provider_tool_specs(self, names: tuple[str, ...]) -> tuple[ProviderToolSpec, ...]:
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

    async def _prepared(
        self,
        *,
        effect_id: EffectId,
        call: ToolCall,
        context: ToolContext,
    ) -> PreparedToolEffect:
        tool = self._registry.validate(call)
        resources = (
            ()
            if tool.spec.sidecar is None
            else await tool.spec.sidecar.resolve_resources(call.arguments, context)
        )
        return PreparedToolEffect(
            effect_id=effect_id,
            run_id=context.run_id,
            call=call,
            spec=tool.spec,
            context_metadata=context.metadata,
            resources=resources,
        )

    async def bind_decision(
        self,
        decision: DecisionRecord,
        outcome: AuthorizationDecision,
    ) -> str:
        """Bind a durable SDK decision to the Host store before Run wake."""

        request = _authorization_request(decision)
        prepared = self._prepared_from_decision(decision)
        sdk_receipt = sdk_authorization_receipt(
            "decision",
            {
                "decision": outcome.value,
                "decision_id": decision.decision_id,
                "decision_version": decision.version,
                "effect_id": prepared.effect_id.value,
                "nonce": request.nonce,
                "run_id": prepared.run_id.value,
            },
        )
        binder = getattr(self._authorization, "bind_decision", None)
        if binder is None:
            # Compatibility is intentionally limited to immediate ALLOW/DENY Hosts.
            raise TypeError("REQUIRE_USER authorization requires bind_decision")
        host_receipt = await binder(prepared, request, outcome, sdk_receipt)
        return bind_authorization_receipts(sdk_receipt, host_receipt)

    def _prepared_from_decision(self, decision: DecisionRecord) -> PreparedToolEffect:
        request = decision.request
        if not isinstance(request, Mapping):
            raise TypeError("authorization decision request must be an object")
        effect_id = EffectId(str(request["effect_id"]))
        run_id = RunId(decision.run_id)
        call_id = CallId(str(request["call_id"]))
        name = str(request["tool_name"])
        arguments = thaw_json(request.get("arguments"))
        if not isinstance(arguments, Mapping):
            raise TypeError("authorization decision arguments must be an object")
        tool = self._registry.get(name)
        sidecar_digest = request.get("sidecar_digest")
        if sidecar_digest is not None and (
            tool.spec.sidecar is None or tool.spec.sidecar.digest != sidecar_digest
        ):
            raise ValueError("authorization Tool sidecar authority changed")
        from .sidecar import ToolResource, resource_digest

        resources_raw = request.get("resources", [])
        if not isinstance(resources_raw, (list, tuple)):
            raise TypeError("authorization resources must be an array")
        resources: list[ToolResource] = []
        for raw in resources_raw:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("actions"), (list, tuple)):
                raise TypeError("authorization resource is malformed")
            resources.append(
                ToolResource(
                    str(raw.get("namespace") or ""),
                    str(raw.get("resource_id") or ""),
                    tuple(str(value) for value in raw["actions"]),
                )
            )
        if request.get("resources_digest") != resource_digest(resources):
            raise ValueError("authorization resource authority changed")
        return PreparedToolEffect(
            effect_id=effect_id,
            run_id=run_id,
            call=ToolCall(call_id, name, cast(dict[str, FrozenJsonValue], arguments)),
            spec=tool.spec,
            context_metadata={},
            resources=tuple(resources),
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
        if context.call_id is not None and context.call_id != call.call_id:
            raise ValueError("Tool context call_id differs from call")
        if context.effect_id is not None and context.effect_id != effect_id:
            raise ValueError("Tool context effect_id differs from effect")
        if (
            execution_lease.run_id != context.run_id.value
            or execution_lease.namespace != RUNTIME_LEASE_NAMESPACE
        ):
            raise ValueError("Tool execution lease belongs to another Run")
        if run_fence.run_id != context.run_id or run_fence.owner_id != execution_lease.owner_id:
            raise ValueError("Tool Run fence differs from runtime lease")
        trusted_context = replace(
            context,
            call_id=call.call_id,
            effect_id=effect_id,
        )
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
                    context=trusted_context,
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
                    or not_started_resolution.outcome is not ResolutionOutcome.CONFIRMED_NOT_STARTED
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
        prepared = await self._prepared(effect_id=effect_id, call=call, context=trusted_context)
        logger.info(
            "tool.invoked",
            extra={"tool": call.name, "args_keys": list(arguments)[:20]},
        )
        authorization_receipt_ref: str | None = None
        read_decision = getattr(self._uow, "read_decision", None)
        durable_decision = (
            None if read_decision is None else read_decision(f"authorization:{effect_id.value}")
        )
        if durable_decision is not None:
            if durable_decision.run_id != context.run_id.value:
                raise ValueError("authorization decision belongs to another Run")
            if durable_decision.state is DecisionState.OPEN:
                raise ToolAuthorizationPending(prepared, _authorization_request(durable_decision))
            if durable_decision.state is DecisionState.ALLOWED:
                response = durable_decision.response
                if not isinstance(response, Mapping):
                    raise ValueError("allowed authorization lacks a bound receipt")
                value = response.get("authorization_receipt_ref")
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("allowed authorization lacks a bound receipt")
                authorization_receipt_ref = value
            else:
                logger.warning(
                    "tool.denied",
                    extra={
                        "tool": call.name,
                        "reason": f"authorization_{durable_decision.state.value}",
                        "path": "durable",
                    },
                )
                return EffectExecution(
                    effect=None,
                    result=ToolResult.rejected(
                        call.call_id,
                        f"authorization_{durable_decision.state.value}",
                        "Tool execution was not authorized.",
                    ),
                )
        prepare = getattr(self._authorization, "prepare", None)
        if authorization_receipt_ref is None:
            if prepare is None:
                prepare = getattr(self._authorization, "authorize")
            authorization = await prepare(prepared)
        else:
            authorization = None
        if (
            authorization is not None
            and authorization.decision is AuthorizationDecision.REQUIRE_USER
        ):
            assert authorization.request is not None
            request = AuthorizationRequest(
                prompt=authorization.request.prompt,
                nonce=secrets.token_urlsafe(32),
                expires_at=authorization.request.expires_at,
                metadata=cast(
                    dict[str, JsonValue],
                    thaw_json(
                        cast(FrozenJsonValue, authorization.request.metadata)
                    ),
                ),
            )
            raise ToolAuthorizationPending(prepared, request)
        if authorization is not None and authorization.decision is not AuthorizationDecision.ALLOW:
            logger.warning(
                "tool.denied",
                extra={
                    "tool": call.name,
                    "reason": authorization.reason_code or "authorization_denied",
                    "path": "immediate",
                },
            )
            return EffectExecution(
                effect=None,
                result=ToolResult.rejected(
                    call.call_id,
                    authorization.reason_code or "authorization_denied",
                    authorization.public_message or "Tool execution was not authorized.",
                ),
            )
        if authorization is not None:
            assert authorization.receipt_ref is not None
            authorization_receipt_ref = authorization.receipt_ref
        assert authorization_receipt_ref is not None
        logger.info("tool.authorized", extra={"tool": call.name})
        if existing is not None and not_started_resolution is not None:
            record = self._uow.reauthorize_effect_not_started(
                existing,
                authorization_receipt_ref=authorization_receipt_ref,
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
                or existing.authorization_receipt_ref != authorization_receipt_ref
            )
        ):
            record = self._uow.refresh_prepared_effect_authority(
                existing,
                authorization_receipt_ref=authorization_receipt_ref,
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
                authorization_receipt_ref=authorization_receipt_ref,
                run_fence=run_fence,
                execution_lease=execution_lease,
                now=self._clock(),
                raw_call_id=raw_call_id,
                turn_ordinal=turn_ordinal,
                call_ordinal=call_ordinal,
            )
        sdk_handoff_receipt = sdk_authorization_receipt(
            "effect-handoff",
            {
                "authorization_receipt_ref": authorization_receipt_ref,
                "effect_id": effect_id.value,
                "effect_version": record.version,
                "fence_epoch": run_fence.epoch,
                "run_id": context.run_id.value,
            },
        )
        handoff_binder = getattr(self._authorization, "bind_effect_handoff", None)
        if handoff_binder is None:
            raise TypeError("Tool handoff requires bind_effect_handoff")
        host_handoff_receipt = await handoff_binder(
            prepared, authorization_receipt_ref, sdk_handoff_receipt
        )
        handoff_receipt_ref = bind_authorization_receipts(sdk_handoff_receipt, host_handoff_receipt)
        handed_off = self._uow.mark_effect_handed_off(
            effect_id,
            expected_version=record.version,
            run_fence=run_fence,
            handoff_receipt_ref=handoff_receipt_ref,
            execution_lease=execution_lease,
            workflow_lease=workflow_lease,
            now=self._clock(),
        )
        self._emit_attempt(handed_off, outcome=Outcome.STARTED)
        try:
            result = await self._registry.invoke(
                call,
                trusted_context,
                accepted_result_call_id=(None if raw_call_id is None else CallId(raw_call_id)),
            )
        except BaseException:
            unknown = self._uow.mark_effect_unknown(
                effect_id,
                expected_version=handed_off.version,
                expected_fence_epoch=run_fence.epoch,
                evidence_ref=(
                    f"tool-dispatch-interrupted:{context.run_id.value}:{effect_id.value}"
                ),
                now=self._clock(),
            )
            self._emit_attempt(
                unknown, outcome=Outcome.DEGRADED, error_code="tool_dispatch_interrupted"
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
            evidence_ref=(f"tool-handler-result:{context.run_id.value}:{effect_id.value}"),
            now=self._clock(),
        )
        logger.info(
            "tool.effect_settled",
            extra={"tool": call.name, "effect_id": effect_id.value},
        )
        tool_outcome = (
            Outcome.SUCCEEDED
            if settled.state is EffectState.SUCCEEDED
            else Outcome.FAILED
        )
        self._emit_attempt(settled, outcome=tool_outcome, error_code=result.error_code)
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


def _authorization_request(decision: DecisionRecord) -> AuthorizationRequest:
    value = decision.request
    if not isinstance(value, Mapping):
        raise TypeError("authorization decision request must be an object")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("authorization metadata must be an object")
    expires_at = value.get("expires_at")
    return AuthorizationRequest(
        prompt=str(value["prompt"]),
        nonce=str(value["nonce"]),
        expires_at=None if expires_at is None else float(expires_at),
        metadata=cast(
            dict[str, JsonValue],
            thaw_json(cast(FrozenJsonValue, metadata)),
        ),
    )


__all__ = ("EffectExecution", "EffectExecutor", "ToolAuthorizationPending")
