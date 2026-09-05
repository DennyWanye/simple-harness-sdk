"""SDK-side public Memory port call facts survive ACK loss and legitimate cleanup."""

import asyncio

import pytest
from test_memory_outbox_recovery import IdempotentMemory, InjectedCrash
from test_memory_outbox_uow import _setup, _spec, _terminal

from simple_harness import CommittedTurnStatus, RunId
from simple_harness.execution.memory_outbox import MemoryDispatcher, MemoryOutboxRepository
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork


def test_real_terminal_memory_call_ack_crash_reopen_cleanup_keeps_attempts(tmp_path):
    path = tmp_path / "port.db"
    database, uow, lease, fence = _setup(path)
    _terminal(uow, lease, fence, _spec())
    memory = IdempotentMemory()
    clock = [3.0]
    repository = MemoryOutboxRepository(database)
    dispatcher = MemoryDispatcher(
        repository, memory, owner_id="worker-1", clock=lambda: clock[0], lease_seconds=1
    )

    def crash(point):
        if point == "memory_dispatcher.after_record_before_ack":
            raise InjectedCrash()

    with pytest.raises(InjectedCrash):
        asyncio.run(dispatcher.run_once(fault=crash))
    audit = uow.read_run_operation_audit(RunId("run-1"))
    attempts = [o for o in audit.operations if o.operation_name == "memory.record_committed_turn"]
    assert len(attempts) == 1 and attempts[0].state == "unknown"
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        repository = MemoryOutboxRepository(reopened)
        dispatcher = MemoryDispatcher(
            repository, memory, owner_id="worker-2", clock=lambda: 5.0, lease_seconds=1
        )
        assert asyncio.run(dispatcher.run_once())
        before = uow.read_run_operation_audit(RunId("run-1"))
        attempts = sorted(
            (o for o in before.operations if o.operation_name == "memory.record_committed_turn"),
            key=lambda o: o.claim_epoch,
        )
        assert [o.state for o in attempts] == ["unknown", "already_applied"]
        assert memory.calls == 2
        assert repository.cleanup_applied(settled_before=5, limit=1) == 1
        after = uow.read_run_operation_audit(RunId("run-1"))
        assert [
            o for o in after.operations if o.operation_name == "memory.record_committed_turn"
        ] == [o for o in before.operations if o.operation_name == "memory.record_committed_turn"]
        assert any(o.operation_name == "memory.outbox.cleaned" for o in after.operations)
        assert not [gap for gap in after.coverage_gaps if gap.startswith("memory_outbox")]


def test_rejected_erased_receipt_is_not_a_memory_write_success(tmp_path):
    database, uow, lease, fence = _setup(tmp_path / "erased.db")
    try:
        _terminal(uow, lease, fence, _spec())
        memory = IdempotentMemory(status=CommittedTurnStatus.REJECTED_ERASED)
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database), memory, owner_id="worker", clock=lambda: 3
        )
        assert asyncio.run(dispatcher.run_once())
        audit = uow.read_run_operation_audit(RunId("run-1"))
        attempts = [
            o for o in audit.operations if o.operation_name == "memory.record_committed_turn"
        ]
        assert len(attempts) == 1 and attempts[0].state == "rejected_erased"
        assert attempts[0].result_hash is not None
    finally:
        database.close()


@pytest.mark.parametrize("phase", ["claim", "release"])
def test_memory_port_cas_result_is_fixed_before_competing_owner(tmp_path, monkeypatch, phase):
    from contextlib import contextmanager

    from simple_harness.execution.uow import UnitOfWorkConflict

    path = tmp_path / "claim-race.db"
    database, uow, lease, fence = _setup(path)
    _terminal(uow, lease, fence, _spec())
    repository = MemoryOutboxRepository(database)
    claim = None if phase == "claim" else repository.claim(owner_id="one", now=3, lease_seconds=1)
    real_transaction = Database.transaction
    taken = []

    @contextmanager
    def interleave(self):
        with real_transaction(self) as connection:
            yield connection
        if self is database and not taken:
            taken.append(None)
            with Database.open(path) as contender_db:
                taken[0] = MemoryOutboxRepository(contender_db).claim(
                    owner_id="two", now=10, lease_seconds=10
                )

    monkeypatch.setattr(Database, "transaction", interleave)
    try:
        if phase == "claim":
            result = repository.claim(owner_id="one", now=3, lease_seconds=1)
            assert result.claim_owner == "one" and result.claim_epoch == 1
        else:
            result = repository.release(claim, now=3, backoff_seconds=0, error_code="retry")
            assert result.claim_owner is None and result.claim_epoch == 1
        assert taken[0].claim_owner == "two" and taken[0].claim_epoch == 2
        with pytest.raises(UnitOfWorkConflict):
            repository.applied(result, now=10)
    finally:
        database.close()


def test_memory_handoff_failure_prevents_port_call(tmp_path):
    database, uow, lease, fence = _setup(tmp_path / "handoff-fail.db")
    try:
        _terminal(uow, lease, fence, _spec())
        database.connection.execute("""CREATE TRIGGER fail_memory_handoff
            BEFORE INSERT ON run_events
            WHEN NEW.kind='audit.memory_outbox.v1'
            AND json_extract(NEW.payload_json,'$.operation')='handoff'
            BEGIN SELECT RAISE(ABORT,'injected'); END""")
        memory = IdempotentMemory()
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database), memory, owner_id="worker", clock=lambda: 3
        )
        with pytest.raises(Exception, match="injected"):
            asyncio.run(dispatcher.run_once())
        assert memory.calls == 0
    finally:
        database.close()


def test_lost_whole_memory_handoff_is_a_gap_even_without_settlement(tmp_path):
    database, uow, lease, fence = _setup(tmp_path / "lost-start.db")
    try:
        _terminal(uow, lease, fence, _spec())
        memory = IdempotentMemory()
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database), memory, owner_id="worker", clock=lambda: 3
        )

        def crash(point):
            raise InjectedCrash()

        with pytest.raises(InjectedCrash):
            asyncio.run(dispatcher.run_once(fault=crash))
        assert memory.calls == 1
        database.connection.execute(
            "DELETE FROM run_events WHERE kind='audit.memory_outbox.v1' "
            "AND json_extract(payload_json,'$.operation')='handoff'"
        )
        audit = uow.read_run_operation_audit(RunId("run-1"))
        assert "memory_outbox_physical_interval_unverified" in audit.coverage_gaps
    finally:
        database.close()


@pytest.mark.parametrize("boundary", ["begin", "settle"])
def test_foreign_real_payload_receipt_cannot_replace_actual_claim(tmp_path, boundary):
    from dataclasses import replace
    from simple_harness.execution.uow import UnitOfWorkConflict

    database, uow, lease, fence = _setup(tmp_path / "foreign.db")
    try:
        original = _spec()
        foreign = _spec("different-real-answer")
        _terminal(uow, lease, fence, original)
        repository = MemoryOutboxRepository(database)
        claim = repository.claim(owner_id="worker", now=3, lease_seconds=10)
        fake = replace(claim, payload_json=foreign.payload_json, payload_hash=foreign.payload_hash)
        real_receipt = asyncio.run(IdempotentMemory().record_committed_turn(foreign.turn))
        before = uow.read_run_operation_audit(RunId("run-1"))
        with pytest.raises(UnitOfWorkConflict):
            if boundary == "begin":
                from simple_harness.execution.sqlite.memory_port_audit import begin

                with database.transaction() as connection:
                    begin(connection, fake, 3)
            else:
                repository.applied(fake, now=3, receipt=real_receipt)
        assert repository.read(claim.intent_id) == replace(claim, claimed_from_state=None)
        assert uow.read_run_operation_audit(RunId("run-1")) == before
    finally:
        database.close()


def test_cleanup_whole_memory_audit_family_loss_is_detected_by_terminal(tmp_path):
    database, uow, lease, fence = _setup(tmp_path / "family.db")
    try:
        _terminal(uow, lease, fence, _spec())
        repository = MemoryOutboxRepository(database)
        dispatcher = MemoryDispatcher(
            repository, IdempotentMemory(), owner_id="worker", clock=lambda: 3
        )
        assert asyncio.run(dispatcher.run_once())
        assert repository.cleanup_applied(settled_before=3, limit=1) == 1
        before = uow.read_run_operation_audit(RunId("run-1"))
        assert not [g for g in before.coverage_gaps if g.startswith("memory_outbox")]
        database.connection.execute("DELETE FROM run_events WHERE kind='audit.memory_outbox.v1'")
        after = uow.read_run_operation_audit(RunId("run-1"))
        assert "memory_outbox_terminal_intent_unverified" in after.coverage_gaps
    finally:
        database.close()


def test_exact_terminal_replay_after_cleanup_uses_retained_intent_hash(tmp_path):
    database, uow, lease, fence = _setup(tmp_path / "terminal-replay.db")
    try:
        spec = _spec()
        _terminal(uow, lease, fence, spec)
        repository = MemoryOutboxRepository(database)
        dispatcher = MemoryDispatcher(
            repository, IdempotentMemory(), owner_id="worker", clock=lambda: 3
        )
        assert asyncio.run(dispatcher.run_once())
        assert repository.cleanup_applied(settled_before=3, limit=1) == 1
        before = uow.read_run_operation_audit(RunId("run-1"))
        _terminal(uow, lease, fence, spec)
        from simple_harness.execution.uow import UnitOfWorkConflict

        with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
            _terminal(uow, lease, fence, _spec("different"))
        assert uow.read_run_operation_audit(RunId("run-1")) == before
    finally:
        database.close()
