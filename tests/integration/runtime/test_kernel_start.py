# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.delivery import (
    DeliveryDispatcher,
    DeliverySpec,
    DeliveryState,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.runtime import (
    DriverResult,
    RunStart,
    RuntimePorts,
    RuntimeLifecycleState,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)
from simple_harness.runtime.start_snapshot import bind_start_snapshot


class NoopPort:
    async def reconcile(self) -> None:
        return None


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
        f"turn-{name}",
        {"prompt": name},
        generation,
    )


def runtime(
    tmp_path,
    *,
    driver=None,
    catalog=None,
    owner="owner-1",
    clock=lambda: 10.0,
    sleep=asyncio.sleep,
    close_timeout_seconds=5.0,
    delivery_sink=None,
):
    database = Database.open(tmp_path / "runtime.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = NoopPort()
    value = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver or Driver()},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database, clock=clock),
            delivery=(
                noop
                if delivery_sink is None
                else DeliveryDispatcher(
                    uow, {"fixture": delivery_sink}, clock=clock
                )
            ),
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=catalog or Catalog(),
            owner_id=owner,
            clock=clock,
            sleep=sleep,
            close_timeout_seconds=close_timeout_seconds,
        ),
    )
    return value, uow, database


async def seed_terminal_deliveries(
    uow: SqliteExecutionUnitOfWork, *, count: int
) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session-delivery",
        run_id="run-delivery",
        request_id="request-delivery",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={
            "schema_version": 1,
            "profile_key": "agent.general",
            "driver_kind": "react",
            "turn_id": "turn-delivery",
            "tool_catalog_generation": 1,
            "input": {},
        },
        event_id="run-delivery:created",
        now=1.0,
    )
    run, lease = uow.claim_runtime_activation(
        run_id="run-delivery",
        owner_id="delivery-seeder",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=30.0,
    )
    fence = await uow.acquire(RunId("run-delivery"), lease, now=2.0)
    uow.commit_root_terminal_with_deliveries(
        run_id=run.run_id,
        expected_version=run.version,
        terminal_state=RunState.COMPLETED,
        event_id="run-delivery:completed",
        terminal_payload={"answer": "done"},
        deliveries=tuple(
            DeliverySpec(
                f"delivery-{index:03d}",
                "fixture",
                f"delivery-key-{index:03d}",
                {"index": index},
            )
            for index in range(count)
        ),
        fence=fence,
        execution_lease=lease,
        terminal_fence_receipt_ref="runtime-fence:delivery-seeder:1",
        now=3.0,
    )
    uow.release_runtime_lease(lease, now=3.0)


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


def test_concurrent_start_publishes_ready_once_and_clients_fail_before_ready(
    tmp_path,
) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def reconcile() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        value._ports.reconciliation.reconcile = reconcile
        first = asyncio.create_task(value.start())
        second = asyncio.create_task(value.start())
        await entered.wait()
        assert value.state is RuntimeLifecycleState.STARTING
        try:
            await value.client.start(request("too-early"))
        except Exception as error:  # public error code is the contract
            assert getattr(error, "code", None) == "runtime_not_ready"
        else:
            raise AssertionError("client start must fail before READY")
        release.set()
        await asyncio.gather(first, second)
        assert calls == 1
        assert value.state is RuntimeLifecycleState.READY
        await value.close()
        assert value.state is RuntimeLifecycleState.CLOSED
        database.close()

    asyncio.run(case())


def test_start_failure_is_not_restartable_but_close_is_idempotent(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)

        async def fail() -> None:
            raise RuntimeError("startup failed")

        value._ports.reconciliation.reconcile = fail
        for _ in range(2):
            try:
                await value.start()
            except RuntimeError:
                pass
            else:
                raise AssertionError("failed Runtime must not become ready")
        assert value.state is RuntimeLifecycleState.FAILED
        await value.close()
        await value.close()
        assert value.state is RuntimeLifecycleState.CLOSED
        database.close()

    asyncio.run(case())


def test_policy_pin_rejects_drift_after_pre_checkpoint_restart(tmp_path) -> None:
    class BeforeCheckpointDriver:
        def __init__(self, fingerprint: str, *, block: bool) -> None:
            self.policy_fingerprint = fingerprint
            self.block = block
            self.calls = 0
            self.started = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.calls += 1
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            return DriverResult(RunState.COMPLETED, {"answer": "must-not-run"})

    async def case() -> None:
        first_driver = BeforeCheckpointDriver("policy-a", block=True)
        first, first_uow, first_database = runtime(
            tmp_path, driver=first_driver, owner="owner-policy-a"
        )
        await first.start()
        await first.client.start(request("policy-drift"))
        await first_driver.started.wait()
        assert first_uow.read_react_checkpoint("run-policy-drift") is None
        snapshot = first_uow.read_start_snapshot("run-policy-drift")
        assert snapshot is not None and snapshot["policy_fingerprint"] == "policy-a"
        await first.close()
        first_database.close()

        drifted_driver = BeforeCheckpointDriver("policy-b", block=False)
        second, second_uow, second_database = runtime(
            tmp_path, driver=drifted_driver, owner="owner-policy-b"
        )
        await second.start()
        await second.wait_idle(RunId("run-policy-drift"))
        recovered = second_uow.read_run("run-policy-drift")
        assert recovered is not None and recovered.state is RunState.FAILED
        assert drifted_driver.calls == 0
        assert second_uow.read_react_checkpoint("run-policy-drift") is None
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_start_racing_close_never_publishes_ready(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reconcile() -> None:
            entered.set()
            await release.wait()

        value._ports.reconciliation.reconcile = reconcile
        starting = asyncio.create_task(value.start())
        await entered.wait()
        closing = asyncio.create_task(value.close())
        await asyncio.wait_for(closing, timeout=0.2)
        outcome = await asyncio.gather(starting, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert value.state is RuntimeLifecycleState.CLOSED
        assert value._wake_drain_task is None
        assert value._delivery_pump_task is None
        release.set()
        database.close()

    asyncio.run(case())


def test_concurrent_close_is_single_terminal_lifecycle_transition(tmp_path) -> None:
    async def case() -> None:
        value, _, database = runtime(tmp_path)
        await value.start()
        await asyncio.gather(*(value.close() for _ in range(8)))
        assert value.state is RuntimeLifecycleState.CLOSED
        assert value._wake_drain_task is None
        assert value._delivery_pump_task is None
        assert value._leases == {}
        database.close()

    asyncio.run(case())


def test_delivery_sink_failure_is_released_and_retried_by_runtime(tmp_path) -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.calls = 0
            self.keys: list[str] = []

        async def deliver(self, payload, *, idempotency_key):
            del payload
            self.calls += 1
            self.keys.append(idempotency_key)
            if self.calls == 1:
                raise RuntimeError("transient sink failure")

    async def case() -> None:
        sink = FlakySink()
        value, uow, database = runtime(tmp_path, delivery_sink=sink)
        await seed_terminal_deliveries(uow, count=1)

        await value.start()

        delivery = uow.read_delivery("delivery-000")
        assert delivery is not None and delivery.state is DeliveryState.DELIVERED
        assert sink.calls == 2
        assert sink.keys == ["delivery-key-000", "delivery-key-000"]
        await value.close()
        database.close()

    asyncio.run(case())


def test_startup_backlog_remains_durable_and_drains_after_runtime_reopen(
    tmp_path,
) -> None:
    class FailingSink:
        def __init__(self) -> None:
            self.calls = 0

        async def deliver(self, payload, *, idempotency_key):
            del payload, idempotency_key
            self.calls += 1
            raise RuntimeError("sink remains offline")

    class RecordingSink:
        def __init__(self) -> None:
            self.keys: list[str] = []
            self.all_delivered = asyncio.Event()

        async def deliver(self, payload, *, idempotency_key):
            del payload
            self.keys.append(idempotency_key)
            if len(self.keys) == 101:
                self.all_delivered.set()

    async def case() -> None:
        failing = FailingSink()
        first, first_uow, first_database = runtime(
            tmp_path,
            owner="runtime-before-reopen",
            delivery_sink=failing,
        )
        await seed_terminal_deliveries(first_uow, count=101)
        await first.start()
        assert failing.calls >= 100
        assert (
            first_database.connection.execute(
                "SELECT count(*) FROM delivery_outbox WHERE state='pending'"
            ).fetchone()[0]
            == 101
        )
        await first.close()
        first_database.close()

        recording = RecordingSink()
        second, second_uow, second_database = runtime(
            tmp_path,
            owner="runtime-after-reopen",
            delivery_sink=recording,
        )
        await second.start()
        second._delivery_wake.set()
        await asyncio.wait_for(recording.all_delivered.wait(), timeout=0.2)
        delivered = second_database.connection.execute(
            "SELECT count(*) FROM delivery_outbox WHERE state='delivered'"
        ).fetchone()[0]
        assert delivered == 101
        assert len(recording.keys) == len(set(recording.keys)) == 101
        assert second_uow.read_delivery("delivery-100").state is DeliveryState.DELIVERED
        await second.close()
        second_database.close()

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


def test_workflow_start_snapshot_requires_typed_admission() -> None:
    start = request("workflow-roundtrip")
    try:
        bind_start_snapshot(
            start, profile_key="workflow.demo", driver_kind="workflow"
        )
    except ValueError as error:
        assert "workflow admission" in str(error)
    else:
        raise AssertionError("workflow start must carry durable admission")


def test_runtime_ports_require_all_authority_seams() -> None:
    try:
        RuntimePorts(  # type: ignore[call-arg]
            context=None,
            tool_catalog=Catalog(),
        )
    except TypeError:
        pass
    else:
        raise AssertionError("authority Ports must not be optional")


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
            "turn_id": "turn-run-recover",
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


def test_runtime_heartbeat_loss_cancels_stale_owner_before_any_write(tmp_path) -> None:
    class Clock:
        value = 10.0

        def __call__(self) -> float:
            return self.value

    class ControlledSleep:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, delay: float) -> None:
            assert delay > 0
            self.entered.set()
            await self.release.wait()

    class BlockingDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False
            self.stale_append_rejected = False

        async def start(self, invocation, *, context, cancel):
            del cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                from simple_harness.contracts.messages import Message, MessageRole
                from simple_harness.execution.uow import UnitOfWorkConflict

                try:
                    context.append(
                        RunId(invocation.run.run_id),
                        invocation.execution_lease,
                        0,
                        "stale-append",
                        (Message(MessageRole.USER, "must-not-write"),),
                    )
                except UnitOfWorkConflict:
                    self.stale_append_rejected = True
                raise

    async def case() -> None:
        clock = Clock()
        controlled_sleep = ControlledSleep()
        driver = BlockingDriver()
        value, uow, database = runtime(
            tmp_path,
            driver=driver,
            clock=clock,
            sleep=controlled_sleep,
            owner="owner-heartbeat",
        )
        await value.start()
        await value.client.start(request("heartbeat"))
        await driver.started.wait()
        await controlled_sleep.entered.wait()
        clock.value = 41.0
        _, stolen = uow.claim_runtime_activation(
            run_id="run-heartbeat",
            owner_id="owner-thief",
            namespace="runtime.kernel",
            now=clock(),
            lease_ttl_seconds=30.0,
        )
        assert stolen.owner_id == "owner-thief"
        controlled_sleep.release.set()
        for _ in range(100):
            if driver.cancelled:
                break
            await asyncio.sleep(0)
        assert driver.cancelled is True
        assert driver.stale_append_rejected is True
        assert (
            database.connection.execute(
                "SELECT count(*) FROM workflow_checkpoints WHERE run_id='run-heartbeat'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT count(*) FROM run_events WHERE run_id='run-heartbeat' "
                "AND kind IN ('run.completed','run.failed','run.cancelled')"
            ).fetchone()[0]
            == 0
        )
        await value.close()
        assert value._heartbeats == {}
        database.close()

    asyncio.run(case())


def test_close_is_bounded_for_noncooperative_driver_and_stops_heartbeat(
    tmp_path,
) -> None:
    class NonCooperativeDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                self.finished.set()
                raise

    async def case() -> None:
        driver = NonCooperativeDriver()
        value, uow, database = runtime(
            tmp_path,
            driver=driver,
            close_timeout_seconds=0.01,
        )
        await value.start()
        await value.client.start(request("noncooperative"))
        await driver.started.wait()

        await asyncio.wait_for(value.close(), timeout=0.2)

        assert value._heartbeats == {}
        assert value._leases == {}
        row = database.connection.execute(
            "SELECT expires_at FROM workflow_leases "
            "WHERE run_id='run-noncooperative' AND namespace='runtime.kernel'"
        ).fetchone()
        assert row is not None and row[0] == 10.0
        assert (
            database.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='run-noncooperative'"
            ).fetchone()[0]
            == "released"
        )

        driver.release.set()
        await asyncio.wait_for(driver.finished.wait(), timeout=0.2)
        assert uow.read_run("run-noncooperative").state is RunState.RUNNING
        database.close()

    asyncio.run(case())


def test_h13_combined_wake_drain_stops_before_noncooperative_driver_and_heartbeat(
    tmp_path,
) -> None:
    class NonCooperativeDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self, invocation, *, context, cancel):
            del invocation, context, cancel
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                raise

    async def case() -> None:
        driver = NonCooperativeDriver()
        value, _, database = runtime(
            tmp_path,
            driver=driver,
            close_timeout_seconds=0.01,
        )
        await value.start()
        assert value._wake_drain_task is not None
        await value.client.start(request("h13-close"))
        await driver.started.wait()
        await asyncio.wait_for(value.close(), timeout=0.2)
        assert value._wake_drain_task is None
        assert value._heartbeats == {}
        assert value._leases == {}
        driver.release.set()
        database.close()

    asyncio.run(case())
