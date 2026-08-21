# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

from simple_harness import Message, RequestId, RunId
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import ProviderBinding, ProviderInvocationCoordinator
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import CancelToken, ProviderRequest

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider


def test_resolver_keeps_provider_estimator_and_charge_on_each_run() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    release = asyncio.Event()
    providers = {
        RunId("run-a"): RecordingProvider(model="model-a", release=release),
        RunId("run-b"): RecordingProvider(model="model-b", release=release),
    }

    class Resolver:
        def resolve(self, run_id: RunId) -> ProviderBinding:
            provider = providers[run_id]
            estimator = FrozenPriceEstimator(
                f"price-{run_id.value}",
                provider.target.pricing_key,
                1_000_000 if run_id.value == "run-a" else 2_000_000,
                3_000_000,
            )
            return ProviderBinding(provider, estimator, BudgetPolicy())

    coordinator = ProviderInvocationCoordinator(uow=uow, resolver=Resolver())

    async def invoke(run: str) -> None:
        await coordinator.invoke(
            RunId(run),
            ProviderRequest(
                RequestId(f"request-{run}"),
                (Message("user", run),),
                max_output_tokens=10,
            ),
            cancel=CancelToken(),
            execution_lease=ExecutionLease(run, "runtime.kernel", "owner", 1, 100),
        )

    async def exercise() -> None:
        tasks = (
            asyncio.create_task(invoke("run-a")),
            asyncio.create_task(invoke("run-b")),
        )
        await asyncio.wait_for(
            asyncio.gather(*(provider.entered.wait() for provider in providers.values())),
            timeout=1,
        )
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(exercise())

    records = tuple(uow.records.values())
    assert {record.target.model for record in records} == {"model-a", "model-b"}
    assert len({record.estimator_digest for record in records}) == 2
    assert all(not record.budget_charge.is_unknown for record in records)
