# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen H7 full-runtime seam oracle.

This is intentionally RED at T3.0 because the public runtime symbols do not
exist yet.  The assertions use the public Runtime against a temporary SQLite
database and observable fake Ports; no production-only conformance shortcut is
allowed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sdk():
    # Planned T3.0 RED: build_runtime is introduced by T3.1.
    from simple_harness.runtime import (
        DriverResult,
        RunStart,
        RuntimePorts,
        RuntimeProfile,
        build_runtime,
    )

    return SimpleNamespace(
        DriverResult=DriverResult,
        RunStart=RunStart,
        RuntimePorts=RuntimePorts,
        RuntimeProfile=RuntimeProfile,
        build_runtime=build_runtime,
    )


class InstrumentedContext:
    def __init__(self) -> None:
        self.revision = 0
        self.messages: list[object] = []
        self.append_ids: set[str] = set()

    def load(self, run_id):
        del run_id
        return SimpleNamespace(revision=self.revision, messages=tuple(self.messages))

    def append(self, run_id, expected_revision, append_id, entries):
        del run_id
        if append_id in self.append_ids:
            return self.load(None)
        assert expected_revision == self.revision
        self.messages.extend(entries)
        self.append_ids.add(append_id)
        self.revision += 1
        return self.load(None)


class InstrumentedProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def invoke(self, tools: tuple[str, ...]) -> str:
        self.calls.append(tools)
        return "calculator"


class InstrumentedAuthorization:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def authorize(self, tool_name: str, allowed: tuple[str, ...]) -> bool:
        self.calls.append(tool_name)
        return tool_name in allowed


class InstrumentedTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name: str) -> dict[str, int]:
        self.calls.append(tool_name)
        return {"answer": 42}


class InstrumentedDeliverySink:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.attempts: list[str] = []

    async def deliver(self, payload, *, idempotency_key: str) -> None:
        del payload
        self.attempts.append(idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("fixture sink failure")


@dataclass
class InstrumentedReActDriver:
    sdk: object
    provider: InstrumentedProvider
    authorization: InstrumentedAuthorization
    tool: InstrumentedTool
    context: InstrumentedContext

    def __post_init__(self) -> None:
        self.invocation_ids: list[int] = []
        self.components: list[str] = []

    async def start(self, invocation, *, context, cancel):
        del context
        assert not cancel.is_cancelled
        self.invocation_ids.append(id(self))
        self.components.extend(("context", "provider"))
        raw = invocation.start.input
        allowed = tuple(raw.get("allowed_tools", ()))
        offered = tuple(raw.get("offered_tools", ()))
        if len(offered) > 32:
            return self.sdk.DriverResult(
                "failed", {"code": "tool_batch_too_large", "count": len(offered)}
            )
        visible = tuple(name for name in offered if name in allowed)
        selected = self.provider.invoke(visible)
        self.components.append("react_driver")
        if not self.authorization.authorize(selected, allowed):
            return self.sdk.DriverResult("failed", {"code": "tool_not_exposed"})
        self.components.extend(("kernel", "authorization", "tool"))
        result = self.tool.execute(selected)
        snapshot = self.context.load(invocation.run.run_id)
        self.context.append(
            invocation.run.run_id,
            snapshot.revision,
            f"{invocation.run.run_id}:tool-result:1",
            (result,),
        )
        self.components.append("react_driver")
        from simple_harness.execution.delivery import DeliverySpec

        return self.sdk.DriverResult(
            "completed",
            {"answer": result["answer"]},
            (
                DeliverySpec(
                    f"{invocation.run.run_id}:delivery",
                    "fixture",
                    f"{invocation.run.run_id}:terminal",
                    {"answer": result["answer"]},
                ),
            ),
        )


class StaticCatalog:
    def __init__(self, generation: int = 1) -> None:
        self.generation = generation

    def current_generation(self) -> int:
        return self.generation


@dataclass
class Seam:
    runtime: object
    uow: object
    database: object
    driver: InstrumentedReActDriver
    provider: InstrumentedProvider
    authorization: InstrumentedAuthorization
    tool: InstrumentedTool
    context: InstrumentedContext


async def _seam(sdk, path: Path) -> Seam:
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    provider = InstrumentedProvider()
    authorization = InstrumentedAuthorization()
    tool = InstrumentedTool()
    context = InstrumentedContext()
    driver = InstrumentedReActDriver(sdk, provider, authorization, tool, context)
    runtime = sdk.build_runtime(
        uow,
        {"agent.general": sdk.RuntimeProfile("agent.general", "react")},
        {"react": driver},
        sdk.RuntimePorts(
            context=context,
            tool_catalog=StaticCatalog(),
            owner_id="h7-runtime",
            clock=lambda: 10.0,
        ),
    )
    await runtime.start()
    return Seam(runtime, uow, database, driver, provider, authorization, tool, context)


def _start(sdk, *, run_id: str, input_value):
    from simple_harness.contracts import ExecutionSessionId, RequestId, RunId

    return sdk.RunStart(
        ExecutionSessionId("h7-session"),
        RunId(run_id),
        RequestId(f"request-{run_id}"),
        input_value,
        1,
    )


def test_capability_snapshot_filters_provider_and_denied_tool_never_executes(
    sdk, tmp_path: Path
) -> None:
    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "capability.db")
        value = _start(
            sdk,
            run_id="run-capability",
            input_value={
                "allowed_tools": ["allowed"],
                "offered_tools": ["allowed", "denied"],
            },
        )
        await seam.runtime.client.start(value)
        await seam.runtime.wait_idle(value.run_id)
        assert seam.provider.calls == [("allowed",)]
        assert seam.tool.calls == []
        assert seam.runtime.client.query(value.run_id).state.value == "failed"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def test_provider_tool_effect_resumes_the_same_driver_once(sdk, tmp_path: Path) -> None:
    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "roundtrip.db")
        value = _start(
            sdk,
            run_id="run-roundtrip",
            input_value={
                "allowed_tools": ["calculator"],
                "offered_tools": ["calculator"],
            },
        )
        await seam.runtime.client.start(value)
        await seam.runtime.wait_idle(value.run_id)
        assert seam.driver.components == [
            "context",
            "provider",
            "react_driver",
            "kernel",
            "authorization",
            "tool",
            "react_driver",
        ]
        assert len(set(seam.driver.invocation_ids)) == 1
        assert seam.tool.calls == ["calculator"]
        assert seam.context.revision == 1
        assert seam.runtime.client.query(value.run_id).state.value == "completed"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def test_provider_tool_batch_is_bounded_before_authorization(
    sdk, tmp_path: Path
) -> None:
    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "batch.db")
        tools = [f"tool-{index}" for index in range(33)]
        value = _start(
            sdk,
            run_id="run-batch",
            input_value={"allowed_tools": tools, "offered_tools": tools},
        )
        await seam.runtime.client.start(value)
        await seam.runtime.wait_idle(value.run_id)
        assert seam.authorization.calls == []
        assert seam.tool.calls == []
        assert seam.runtime.client.query(value.run_id).state.value == "failed"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def test_child_provider_failure_preserves_child_and_root_identity(
    sdk, tmp_path: Path
) -> None:
    # The child closure is exercised through the durable UoW, never an in-memory list.
    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "child-failure.db")
        launched = _create_attached_child(seam)
        assert launched.child_run_id == "child-1"
        assert seam.uow.read_run("child-1").root_run_id == "root-child"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def test_child_terminal_commit_durably_enqueues_parent_signal(
    sdk, tmp_path: Path
) -> None:
    from simple_harness.execution.uow import RunState

    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "child-signal.db")
        _create_attached_child(seam)
        signal = seam.uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-1",
            expected_child_version=0,
            terminal_state=RunState.FAILED,
            signal_id="signal-child-1",
            signal_payload={"code": "provider_failed", "child_run_id": "child-1"},
            event_id="child-terminal",
            now=13.0,
        )
        row = seam.database.connection.execute(
            "SELECT state,payload_json FROM child_signals WHERE signal_id=?",
            (signal.signal_id,),
        ).fetchone()
        assert row[0] == "pending"
        assert "provider_failed" in row[1]
        assert seam.uow.read_run("root-child").state.value == "created"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def _create_attached_child(seam):
    from simple_harness.execution.contracts.children import (
        ProfileLaunchTicket,
        child_launch_fingerprint,
    )

    seam.uow.create_with_start_snapshot(
        execution_session_id="h7-child-session",
        run_id="root-child",
        request_id="request-root-child",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"input": {}},
        event_id="root-child-created",
        now=10.0,
    )
    launch_request = {
        "profile_key": "workflow.durable_task",
        "driver_kind": "workflow",
        "catalog_generation": 1,
    }
    seam.uow.issue_profile_launch_ticket(
        ProfileLaunchTicket(
            "ticket-1",
            "root-child",
            "workflow.durable_task",
            1,
            child_launch_fingerprint(launch_request),
        ),
        now=11.0,
    )
    return seam.uow.claim_profile_launch_and_commit_child(
        ticket_id="ticket-1",
        expected_catalog_generation=1,
        launch_request=launch_request,
        command_id="command-1",
        child_run_id="child-1",
        request_id="request-child-1",
        attachment_policy="attached",
        start_snapshot={"input": {}},
        event_id="child-created",
        now=12.0,
    )


def test_root_terminal_and_delivery_outbox_are_atomic(sdk, tmp_path: Path) -> None:
    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "delivery.db")
        value = _start(
            sdk,
            run_id="run-delivery",
            input_value={
                "allowed_tools": ["calculator"],
                "offered_tools": ["calculator"],
            },
        )
        await seam.runtime.client.start(value)
        await seam.runtime.wait_idle(value.run_id)
        row = seam.database.connection.execute(
            "SELECT state,idempotency_key FROM delivery_outbox WHERE run_id=?",
            (value.run_id.value,),
        ).fetchone()
        assert tuple(row) == ("pending", "run-delivery:terminal")
        assert seam.runtime.client.query(value.run_id).state.value == "completed"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


def test_delivery_retry_never_reopens_terminal_root(sdk, tmp_path: Path) -> None:
    from simple_harness.execution.delivery import DeliveryDispatcher

    async def case() -> None:
        seam = await _seam(sdk, tmp_path / "retry.db")
        value = _start(
            sdk,
            run_id="run-retry",
            input_value={
                "allowed_tools": ["calculator"],
                "offered_tools": ["calculator"],
            },
        )
        await seam.runtime.client.start(value)
        await seam.runtime.wait_idle(value.run_id)
        sink = InstrumentedDeliverySink(fail_once=True)
        dispatcher = DeliveryDispatcher(seam.uow, {"fixture": sink}, clock=lambda: 20.0)
        assert await dispatcher.dispatch_one() is True
        assert seam.runtime.client.query(value.run_id).state.value == "completed"
        assert await dispatcher.dispatch_one() is True
        assert sink.attempts == ["run-retry:terminal", "run-retry:terminal"]
        assert seam.runtime.client.query(value.run_id).state.value == "completed"
        await seam.runtime.close()
        seam.database.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    "private_value",
    (
        {"raw_query": "secret"},
        {"provider_secret": "secret"},
        {"private_state": {"token": "secret"}},
        {"metrics": {"hits": -1}},
        {"diagnostic_codes": ["secret_query"]},
    ),
)
def test_driver_failure_projects_only_strict_public_error(
    sdk, tmp_path: Path, private_value
) -> None:
    class FailingDriver:
        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            raise RuntimeError(repr(private_value))

    async def case() -> None:
        from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

        database = Database.open(
            tmp_path / f"strict-{abs(hash(repr(private_value)))}.db"
        )
        uow = SqliteExecutionUnitOfWork(database)
        runtime = sdk.build_runtime(
            uow,
            {"agent.general": sdk.RuntimeProfile("agent.general", "react")},
            {"react": FailingDriver()},
            sdk.RuntimePorts(
                context=InstrumentedContext(),
                tool_catalog=StaticCatalog(),
                owner_id="h7-strict",
                clock=lambda: 10.0,
            ),
        )
        await runtime.start()
        value = _start(sdk, run_id="run-strict", input_value={})
        await runtime.client.start(value)
        await runtime.wait_idle(value.run_id)
        row = database.connection.execute(
            "SELECT payload_json FROM run_events WHERE kind='run.failed'"
        ).fetchone()
        assert "secret" not in str(row[0])
        assert "driver_failed" in str(row[0])
        await runtime.close()
        database.close()

    asyncio.run(case())
