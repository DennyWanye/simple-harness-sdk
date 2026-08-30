# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from simple_harness import (
    AgentIdentity,
    ConversationTurnInput,
    MemoryRecallResult,
    MemoryRecallStatus,
    Message,
    MessageRole,
    StartCommandIntent,
)
from simple_harness.contracts import RequestId, RunId
from simple_harness.execution.context_authority import ToolCatalogSnapshot
from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import RunState
from simple_harness.observability import (
    CorrelationContext,
    ObservabilityEventV1,
    ObservabilityRuntime,
    Outcome,
    RecordingSink,
    Severity,
)
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime import (
    CommandState,
    DriverResult,
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
    def __init__(self, fingerprint: str | None = None) -> None:
        self.fingerprint = fingerprint

    def current_generation(self) -> int:
        return 1

    def resolve(
        self, generation: int, content_fingerprint: str
    ) -> ToolCatalogSnapshot | None:
        if generation != 1 or content_fingerprint != self.fingerprint:
            return None
        return ToolCatalogSnapshot(
            1,
            content_fingerprint,
            (ProviderToolSpec("read_status", "Read status", {"type": "object"}),),
            0.0,
        )


class Driver:
    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        raise AssertionError((invocation, context, cancel))


class CommandMemory:
    async def recall_for_turn(self, request):  # type: ignore[no-untyped-def]
        return MemoryRecallResult(
            request.query_id,
            request.query_hash,
            "memory-result",
            {},
            MemoryRecallStatus.READY,
            0,
            1,
            "memory-fence",
        )

    async def release_recall(self, request):  # type: ignore[no-untyped-def]
        del request

    async def record_committed_turn(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)


class WaitingDriver:
    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        del invocation, context, cancel
        return DriverResult(RunState.WAITING, {"reason": "fixture"})


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
        "run_context_authority": object(),
        "runtime_decision_sink": object(),
        "task_execution_authority": object(),
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


def test_production_submit_start_accepts_nested_catalog_bound_input(tmp_path) -> None:
    async def case() -> None:
        fingerprint = "c" * 64
        runtime = build_production_runtime(
            _config(
                tmp_path / "catalog-command.db",
                [],
                memory=CommandMemory(),
                memory_ownership=ResourceOwnership.BORROWED,
                tool_catalog=Catalog(fingerprint),
                driver=WaitingDriver(),
            )
        )
        await runtime.start()
        conversation = ConversationTurnInput(
            AgentIdentity("deployment", "household", "actor", "session-tools"),
            Message(MessageRole.USER, "read status"),
            "read status",
        )
        intent = StartCommandIntent(
            "deployment/phone",
            "key-tools",
            "command-tools",
            RunId("run-tools"),
            RequestId("request-tools"),
            "turn-tools",
            conversation,
            input={
                "messages": [conversation.message.to_dict()],
                "capability_snapshot": {"tools": ["read_status"]},
                "max_output_tokens": 4096,
            },
            tool_catalog_generation=1,
            tool_catalog_fingerprint=fingerprint,
        )

        await runtime.client.submit_start(intent)
        for _ in range(100):
            command = await runtime.client.get_command(intent.command_id)
            if command.receipt.state.terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("production command did not settle")

        assert command.receipt.state is CommandState.APPLIED
        assert runtime.client.query(intent.run_id).state is RunState.WAITING
        await runtime.close()

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


def test_production_runtime_accepts_optional_observability_sink(tmp_path: Path) -> None:
    async def case() -> None:
        sink = RecordingSink()
        runtime = build_production_runtime(
            _config(
                tmp_path / "observability.db",
                [],
                observability_sink=sink,
                observability_queue_capacity=8,
            )
        )
        assert isinstance(runtime.observability, ObservabilityRuntime)
        accepted = runtime.observability.emit(
            ObservabilityEventV1(
                event_name="harness.runtime.composed",
                occurred_at=1.0,
                severity=Severity.INFO,
                component="harness",
                operation="runtime.compose",
                outcome=Outcome.SUCCEEDED,
                correlation=CorrelationContext.new_root(),
            )
        )
        assert accepted
        assert runtime.observability.flush(1)
        assert len(sink.events()) == 1
        snapshot = runtime.diagnostics_snapshot()
        assert snapshot["health"] == "healthy"
        assert snapshot["active_runs"] == 0
        assert snapshot["authorities"] == {
            "health": "healthy",
            "commands": {"health": "healthy", "counts": {}, "oldest_age_ms": None},
            "context": {"health": "healthy", "counts": {}, "oldest_age_ms": None},
            "outbox": {"health": "healthy", "counts": {}, "oldest_age_ms": None},
            "recovery": {"health": "healthy", "counts": {}, "oldest_age_ms": None},
            "recent_error_codes": {},
        }
        await runtime.close()
        closed = runtime.diagnostics_snapshot()
        assert closed["lifecycle"] == "closed"
        assert closed["authorities"]["health"] == "degraded"
        assert closed["authorities"]["error_code"] == "authority_query_failed"

    asyncio.run(case())
