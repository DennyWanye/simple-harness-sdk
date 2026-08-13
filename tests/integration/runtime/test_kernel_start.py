# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.runtime import (
    DriverResult,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    build_runtime,
)


class Context:
    def load(self, run_id):
        raise AssertionError

    def append(self, *args):
        raise AssertionError


class Catalog:
    def __init__(self, generation: int = 1) -> None:
        self.generation = generation

    def current_generation(self) -> int:
        return self.generation


class Driver:
    def __init__(self, *, state=RunState.COMPLETED) -> None:
        self.state = state
        self.calls = 0

    async def start(self, invocation, *, context, cancel):
        del invocation, context, cancel
        self.calls += 1
        return DriverResult(self.state, {"answer": "ok"})


def request(name: str = "one", *, generation: int = 1) -> RunStart:
    return RunStart(
        ExecutionSessionId("session-1"),
        RunId(f"run-{name}"),
        RequestId(f"request-{name}"),
        {"prompt": name},
        generation,
    )


def runtime(
    tmp_path, *, driver=None, catalog=None, owner="owner-1", clock=lambda: 10.0
):
    database = Database.open(tmp_path / "runtime.db")
    uow = SqliteExecutionUnitOfWork(database)
    value = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver or Driver()},
        RuntimePorts(
            context=Context(),
            tool_catalog=catalog or Catalog(),
            owner_id=owner,
            clock=clock,
        ),
    )
    return value, uow, database


def test_start_is_atomic_activated_and_idempotent(tmp_path) -> None:
    async def case() -> None:
        driver = Driver(state=RunState.WAITING)
        value, _, database = runtime(tmp_path, driver=driver)
        await value.start()
        first = await value.client.start(request())
        second = await value.client.start(request())
        assert first.run_id == second.run_id == "run-one"
        assert first.state is RunState.RUNNING
        await value.wait_idle(RunId("run-one"))
        assert value.client.query(RunId("run-one")).state is RunState.WAITING
        assert driver.calls == 1
        await value.close()
        database.close()

    asyncio.run(case())


def test_fixed_root_has_no_classifier_and_rejects_override(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        await value.start()
        await value.client.start(request("fixed"))
        await value.wait_idle(RunId("run-fixed"))
        record = value.client.query(RunId("run-fixed"))
        assert record.profile_key == "agent.general"
        await value.close()
        database.close()

    asyncio.run(case())

    value, uow, _ = runtime(tmp_path)
    try:
        build_runtime(
            uow,
            {"other": RuntimeProfile("other", "react")},
            {"react": Driver()},
            value._ports,
            root_profile_key="other",
        )
    except ValueError as error:
        assert "fixed" in str(error)
    else:
        raise AssertionError("root override must fail")
    value._uow.database.close()


def test_recovery_lease_is_single_owner_then_expiry_allows_takeover(tmp_path) -> None:
    _, uow, database = runtime(tmp_path, owner="owner-1", clock=lambda: 10.0)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-recover",
        request_id="request-recover",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={
            "schema_version": 1,
            "profile_key": "agent.general",
            "driver_kind": "react",
            "tool_catalog_generation": 1,
            "input": {},
        },
        event_id="run-recover:created",
        now=1.0,
    )
    first, first_lease = uow.claim_runtime_activation(
        run_id="run-recover",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=5.0,
    )
    assert first.state is RunState.RUNNING
    from simple_harness.execution.uow import UnitOfWorkConflict

    try:
        uow.claim_runtime_activation(
            run_id="run-recover",
            owner_id="owner-2",
            namespace="runtime.kernel",
            now=3.0,
            lease_ttl_seconds=5.0,
        )
    except UnitOfWorkConflict:
        pass
    else:
        raise AssertionError("live owner must not be stolen")
    _, second_lease = uow.claim_runtime_activation(
        run_id="run-recover",
        owner_id="owner-2",
        namespace="runtime.kernel",
        now=8.0,
        lease_ttl_seconds=5.0,
    )
    assert second_lease.epoch == first_lease.epoch + 1
    database.close()


def test_catalog_stale_terminalizes_once_without_driver(tmp_path) -> None:
    async def case() -> None:
        driver = Driver()
        value, _, database = runtime(tmp_path, driver=driver, catalog=Catalog(2))
        await value.start()
        await value.client.start(request("stale", generation=1))
        await value.wait_idle(RunId("run-stale"))
        assert value.client.query(RunId("run-stale")).state is RunState.FAILED
        assert driver.calls == 0
        failures = database.connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id=? AND kind='run.failed'",
            ("run-stale",),
        ).fetchone()[0]
        assert failures == 1
        await value.close()
        database.close()

    asyncio.run(case())


def test_runtime_heartbeat_prevents_takeover_and_close_stops_it(tmp_path) -> None:
    class BlockingDriver:
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            await self.release.wait()
            return DriverResult(RunState.WAITING, {})

    async def case() -> None:
        import time

        driver = BlockingDriver()
        value, uow, database = runtime(
            tmp_path, driver=driver, clock=time.time, owner="owner-heartbeat"
        )
        object.__setattr__(value._ports, "lease_ttl_seconds", 0.06)
        await value.start()
        await value.client.start(request("heartbeat"))
        await asyncio.sleep(0.12)
        from simple_harness.execution.uow import UnitOfWorkConflict

        try:
            uow.claim_runtime_activation(
                run_id="run-heartbeat",
                owner_id="owner-thief",
                namespace="runtime.kernel",
                now=time.time(),
                lease_ttl_seconds=0.06,
            )
        except UnitOfWorkConflict:
            pass
        else:
            raise AssertionError("heartbeat lease must not be stolen")
        await value.close()
        _, stolen = uow.claim_runtime_activation(
            run_id="run-heartbeat",
            owner_id="owner-thief",
            namespace="runtime.kernel",
            now=time.time() + 0.001,
            lease_ttl_seconds=0.06,
        )
        assert stolen.owner_id == "owner-thief"
        database.close()

    asyncio.run(case())
