# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

from simple_harness.contracts import RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetSnapshot
from simple_harness.execution.provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
)
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)


class FakeProviderInvocationUnitOfWork:
    def __init__(self) -> None:
        self.records: dict[str, ProviderInvocationRecord] = {}
        self.operations: list[str] = []

    def claim_provider_invocation(
        self, record, *, budget_policy, execution_lease
    ) -> ProviderInvocationRecord:
        del execution_lease
        self.operations.append("claim")
        existing = self.records.get(record.invocation_id)
        if existing is not None:
            return existing
        budget_policy.authorize(
            self.read_provider_budget(record.run_id),
            reservation_micros=record.budget_charge.amount_micros,
        )
        self.records[record.invocation_id] = record
        return record

    def read_provider_invocation(self, invocation_id: str):
        return self.records.get(invocation_id)

    def hand_off_provider_invocation(
        self, invocation_id, *, expected_version, handed_off_at, execution_lease
    ):
        del execution_lease
        self.operations.append("hand_off")
        record = self.records[invocation_id].hand_off(
            at=handed_off_at, expected_version=expected_version
        )
        self.records[invocation_id] = record
        return record

    def settle_provider_invocation(self, record, *, expected_version):
        current = self.records[record.invocation_id]
        if current.version != expected_version:
            raise ValueError("stale provider invocation version")
        self.records[record.invocation_id] = record
        return record

    def list_incomplete_provider_invocations(self):
        return tuple(
            record
            for record in self.records.values()
            if record.state
            in {ProviderInvocationState.CLAIMED, ProviderInvocationState.HANDED_OFF}
        )

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot:
        committed = 0
        reserved = 0
        unknown = False
        for record in self.records.values():
            if record.run_id != run_id:
                continue
            charge = record.budget_charge
            if record.state in {
                ProviderInvocationState.CLAIMED,
                ProviderInvocationState.HANDED_OFF,
            }:
                if charge.amount_micros is None:
                    unknown = True
                else:
                    reserved += charge.amount_micros
            elif record.state in {
                ProviderInvocationState.SUCCEEDED,
                ProviderInvocationState.FAILED,
                ProviderInvocationState.UNKNOWN,
            }:
                if charge.amount_micros is None:
                    unknown = True
                else:
                    committed += charge.amount_micros
        return BudgetSnapshot(committed, reserved, unknown)


class RecordingProvider:
    def __init__(
        self,
        *,
        release=None,
        error=None,
        usage=...,
        model="model-1",
        response_model=None,
        endpoint_identity="https://provider.invalid/v1/chat/completions",
    ) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = release
        self.error = error
        self.model = model
        self.response_model = model if response_model is None else response_model
        self.target = ProviderTarget(
            provider_id="provider-1",
            model=model,
            pricing_key=model,
            endpoint_identity=endpoint_identity,
            adapter_key="recording-provider.v1",
        )
        self.usage = (
            ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15)
            if usage is ...
            else usage
        )

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:
        self.calls += 1
        self.entered.set()
        if hasattr(self, "uow"):
            self.uow.operations.append("provider_call")
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            request_id=request.request_id,
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            usage=self.usage,
            model=self.response_model,
            finish_reason="stop",
        )
