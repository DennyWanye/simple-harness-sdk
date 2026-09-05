"""Real SQLite delivery authority, physical attempts, and stable audit prefixes."""

import asyncio
import json

import pytest

from simple_harness.contracts import RunId
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec, DeliveryState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState


async def terminal(database):
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="run-1:created",
        now=1.0,
    )
    run, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=30.0,
    )
    fence = await uow.acquire(RunId("run-1"), lease, now=2.0)
    uow.commit_root_terminal_with_deliveries(
        run_id="run-1",
        expected_version=run.version,
        terminal_state=RunState.COMPLETED,
        event_id="run-1:completed",
        terminal_payload={"answer": 42},
        deliveries=(DeliverySpec("delivery-1", "presenter", "terminal:run-1", {"answer": 42}),),
        fence=fence,
        execution_lease=lease,
        terminal_fence_receipt_ref="runtime-fence:owner-1:1",
        now=3.0,
    )
    return uow


def delivery_ops(uow):
    return [
        o for o in uow.read_run_operation_audit(RunId("run-1")).operations if o.kind == "delivery"
    ]


def test_sink_exception_unknown_then_success_and_prefix_reopen(tmp_path):
    async def case():
        path = tmp_path / "failure.db"
        database = Database.open(path)
        uow = await terminal(database)

        class Sink:
            calls = 0

            async def deliver(self, payload, *, idempotency_key):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("PRIVATE-DELIVERY-ERROR")

        sink = Sink()
        dispatcher = DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 5)
        assert await dispatcher.run_once()
        before = uow.read_run_operation_audit(RunId("run-1"))
        attempts = [
            o for o in before.operations if o.kind == "delivery" and o.record_type == "boundary"
        ]
        assert len(attempts) == 1 and attempts[0].state == "unknown"
        assert attempts[0].error_code == "delivery_sink_exception"
        assert "PRIVATE-DELIVERY-ERROR" not in json.dumps(before.to_json())
        page = uow.open_run_operation_audit(RunId("run-1"), page_size=2)
        expected = []
        cursor = page.next_cursor
        while cursor:
            part = uow.read_run_operation_audit_page(RunId("run-1"), cursor=cursor)
            expected.append(part)
            cursor = part.next_cursor
        assert await dispatcher.run_once()
        assert sink.calls == 2
        after = sorted(
            (o for o in delivery_ops(uow) if o.record_type == "boundary"),
            key=lambda o: o.source_version,
        )
        assert [o.state for o in after] == ["unknown", "completed"]
        assert after[1].handoff_to_settlement_seconds == 0
        # Exact complete replay cannot add another attempt or version.
        head = uow.read_delivery("delivery-1")
        snapshot = uow.read_run_operation_audit(RunId("run-1"))
        uow.complete_delivery("delivery-1", expected_version=head.version - 1, now=6)
        assert uow.read_run_operation_audit(RunId("run-1")) == snapshot
        database.close()
        with Database.open(path) as reopened:
            reader = SqliteExecutionUnitOfWork(reopened)
            cursor = page.next_cursor
            for expected_page in expected:
                actual = reader.read_run_operation_audit_page(RunId("run-1"), cursor=cursor)
                assert actual == expected_page
                cursor = actual.next_cursor
        assert sink.calls == 2

    asyncio.run(case())


def test_handoff_persistence_failure_does_not_call_sink(tmp_path):
    async def case():
        with Database.open(tmp_path / "rollback.db") as database:
            uow = await terminal(database)
            database.connection.execute("""CREATE TRIGGER inject_handoff_failure
                BEFORE INSERT ON run_events
                WHEN NEW.kind='audit.delivery.v1'
                AND json_extract(NEW.payload_json,'$.domain')='attempt'
                BEGIN SELECT RAISE(ABORT,'injected'); END""")

            class Sink:
                calls = 0

                async def deliver(self, payload, *, idempotency_key):
                    self.calls += 1

            sink = Sink()
            with pytest.raises(Exception, match="injected"):
                await DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 5).run_once()
            assert sink.calls == 0
            assert uow.read_delivery("delivery-1").version == 1
            assert not [o for o in delivery_ops(uow) if o.record_type == "boundary"]

    asyncio.run(case())


def test_cancellation_after_actual_handoff_stays_unknown(tmp_path):
    async def case():
        with Database.open(tmp_path / "cancel.db") as database:
            uow = await terminal(database)

            class Sink:
                calls = 0

                async def deliver(self, payload, *, idempotency_key):
                    self.calls += 1
                    raise asyncio.CancelledError()

            sink = Sink()
            with pytest.raises(asyncio.CancelledError):
                await DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 5).run_once()
            attempts = [o for o in delivery_ops(uow) if o.record_type == "boundary"]
            assert sink.calls == 1 and len(attempts) == 1
            assert attempts[0].state == "unknown" and attempts[0].settled_at is None
            assert uow.read_delivery("delivery-1").state is DeliveryState.CLAIMED

    asyncio.run(case())


def test_missing_entire_handoff_pair_is_not_covered(tmp_path):
    async def case():
        with Database.open(tmp_path / "corrupt.db") as database:
            uow = await terminal(database)

            class Sink:
                async def deliver(self, payload, *, idempotency_key):
                    pass

            assert await DeliveryDispatcher(uow, {"presenter": Sink()}, clock=lambda: 5).run_once()
            from simple_harness.execution.sqlite.delivery_audit import coverage

            assert not coverage(database.connection, "run-1")
            # Isolated corruption negative; retain every canonical head/version fact.
            database.connection.execute(
                "DELETE FROM run_events WHERE kind='audit.delivery.v1' "
                "AND json_extract(payload_json,'$.domain')='attempt'"
            )
            assert "delivery_physical_handoff_unverified" in coverage(database.connection, "run-1")

    asyncio.run(case())


@pytest.mark.parametrize("phase", ["claim", "handoff", "release"])
def test_delivery_returns_own_cas_record_not_new_owner_version(tmp_path, phase):
    async def case():
        with Database.open(tmp_path / "owner-race.db") as database:
            uow = await terminal(database)
            competing = []

            def takeover(point):
                if point == "delivery_" + phase + ".after_commit":
                    claim = uow.claim_delivery(
                        sink_kinds=("presenter",), now=10, claim_ttl_seconds=1
                    )
                    competing.append(
                        uow.record_delivery_handoff(
                            claim.delivery_id, expected_version=claim.version, now=10
                        )
                    )

            if phase == "claim":
                result = uow.claim_delivery(
                    sink_kinds=("presenter",), now=5, claim_ttl_seconds=1, fault=takeover
                )
                assert result.version == 1
            else:
                claim = uow.claim_delivery(sink_kinds=("presenter",), now=5, claim_ttl_seconds=1)
                if phase == "handoff":
                    result = uow.record_delivery_handoff(
                        claim.delivery_id, expected_version=claim.version, now=5, fault=takeover
                    )
                    assert result.version == 2
                else:
                    result = uow.release_delivery(
                        claim.delivery_id, expected_version=claim.version, now=5, fault=takeover
                    )
                    assert result.version == 2 and result.state is DeliveryState.PENDING
            from simple_harness.execution.delivery import DeliveryConflictError

            with pytest.raises(DeliveryConflictError):
                uow.complete_delivery(result.delivery_id, expected_version=result.version, now=10)
            assert uow.read_delivery("delivery-1") == competing[0]

    asyncio.run(case())
