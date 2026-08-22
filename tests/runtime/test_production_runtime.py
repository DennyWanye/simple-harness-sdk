# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.sqlite import Database
from simple_harness.runtime import (
    ProductionRuntimeConfig,
    ResourceOwnership,
    RuntimeLifecycleState,
    RuntimeProfile,
    build_production_runtime,
)
from simple_harness.runtime.production import ProductionAuthorities


class Reconciliation:
    async def reconcile(self) -> None:
        return None


class Delivery:
    async def run_once(self) -> bool:
        return False


class Memory:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    async def recall_for_turn(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)

    async def release_recall(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)

    async def record_committed_turn(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)

    async def close(self) -> None:
        self.trace.append("memory.close")


class Pump:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    async def start(self) -> None:
        self.trace.append("pump.start")

    async def close(self) -> None:
        self.trace.append("pump.close")


class Catalog:
    def current_generation(self) -> int:
        return 1


class Driver:
    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        raise AssertionError((invocation, context, cancel))


def _config(path: Path, trace: list[str], **changes) -> ProductionRuntimeConfig:
    memory = Memory(trace)
    pump = Pump(trace)
    values = {
        "execution_path": path,
        "provider_builder": lambda _uow: object(),
        "tools_builder": lambda _uow: object(),
        "delivery_builder": lambda _uow: Delivery(),
        "context_builder": lambda _database: object(),
        "context_staging_builder": ContextStagingRepository,
        "authorization": object(),
        "tool_reconciliation": object(),
        "reconciliation": Reconciliation(),
        "provider_reconciliation": object(),
        "tool_catalog": Catalog(),
        "driver": Driver(),
        "profiles": {"agent.general": RuntimeProfile("agent.general", "react")},
        "memory": memory,
        "memory_ownership": ResourceOwnership.RUNTIME,
        "provider_budget_resolver": object(),
        "provider_projection_pump": pump,
        "run_binding": object(),
        "structured_message_services": object(),
        "owner_id": "production-worker",
    }
    values.update(changes)
    return ProductionRuntimeConfig(**values)  # type: ignore[arg-type]


def test_production_config_is_frozen_and_missing_authority_fails_fast(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "execution.db", [])
    with pytest.raises(FrozenInstanceError):
        config.owner_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="memory"):
        _config(tmp_path / "missing.db", [], memory=None)
    with pytest.raises(TypeError, match="recall_for_turn"):
        _config(tmp_path / "missing-release.db", [], memory=object())
    with pytest.raises(ValueError, match="agent.general"):
        _config(tmp_path / "profile.db", [], profiles={})


def test_production_runtime_retains_authorities_and_closes_in_order(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        trace: list[str] = []
        path = tmp_path / "execution.db"
        runtime = build_production_runtime(_config(path, trace))
        authorities = runtime.production_authorities
        assert isinstance(authorities, ProductionAuthorities)
        assert authorities.context_staging.database.is_open
        await runtime.start()
        assert runtime.state is RuntimeLifecycleState.READY
        await runtime.close()
        assert runtime.state is RuntimeLifecycleState.CLOSED
        assert trace == [
            "pump.start",
            "pump.close",
            "memory.close",
        ]
        assert not authorities.context_staging.database.is_open
        with Database.open(path) as reopened:
            assert reopened.integrity_check() == ("ok",)

    asyncio.run(case())


def test_demo_builder_remains_distinct_from_production_builder() -> None:
    from simple_harness.runtime import build_consumer_runtime

    assert build_consumer_runtime is not build_production_runtime


def test_production_build_failure_cleans_runtime_owned_memory(tmp_path: Path) -> None:
    trace: list[str] = []

    def fail_build(_uow) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("build failed")

    config = _config(
        tmp_path / "failed.db",
        trace,
        provider_builder=fail_build,
    )
    with pytest.raises(RuntimeError, match="build failed"):
        build_production_runtime(config)
    assert trace == ["memory.close"]
