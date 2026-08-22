# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness import (
    AgentIdentity,
    AgentMemoryError,
    AgentMemoryErrorCode,
    CommittedTurn,
    CommittedTurnReceipt,
    CommittedTurnStatus,
    MemoryScopeRef,
)
from simple_harness.execution.memory_outbox import (
    CommittedTurnSpec,
    MemoryDispatcher,
    MemoryOutboxRepository,
    MemoryOutboxState,
)
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.observability import ObservabilityRuntime, RecordingSink


class InjectedCrash(BaseException):
    pass


def _spec(turn_id: str = "turn-1", answer: str = "answer") -> CommittedTurnSpec:
    return CommittedTurnSpec.from_domain(
        CommittedTurn(
            turn_id,
            AgentIdentity("deployment-1", "household-1", "actor-1", "session-1"),
            "hello",
            answer,
            MemoryScopeRef.personal("actor-1"),
            "epoch-1",
            1.0,
        )
    )


def _insert(database: Database, spec: CommittedTurnSpec, *, created_at: float = 1.0) -> None:
    turn = spec.turn
    database.connection.execute(
        "INSERT OR IGNORE INTO execution_users VALUES('actor-1',?)",
        (created_at,),
    )
    database.connection.execute(
        "INSERT OR IGNORE INTO execution_sessions VALUES('session-1','actor-1',?)",
        (created_at,),
    )
    database.connection.execute(
        "INSERT INTO runs(run_id,execution_session_id,request_id,root_run_id,parent_run_id,"
        "profile_key,driver_kind,state,version,created_at,updated_at) "
        "VALUES(?, 'session-1', ?, ?, NULL, 'agent.general','react','completed',1,?,?)",
        (
            f"run-{turn.turn_id}",
            f"request-{turn.turn_id}",
            f"run-{turn.turn_id}",
            created_at,
            created_at,
        ),
    )
    database.connection.execute(
        "INSERT INTO memory_outbox(intent_id,run_id,turn_id,deployment_id,household_id,"
        "actor_id,session_id,payload_json,payload_hash,state,claim_owner,claim_epoch,"
        "claim_expires_at,attempt_count,retry_at,error_code,created_at,settled_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,0,NULL,0,?,NULL,?,NULL)",
        (
            spec.intent_id,
            f"run-{turn.turn_id}",
            turn.turn_id,
            turn.identity.deployment_id,
            turn.identity.household_id,
            turn.identity.actor_id,
            turn.identity.session_id,
            spec.payload_json,
            spec.payload_hash,
            created_at,
            created_at,
        ),
    )


class IdempotentMemory:
    def __init__(self, *, status: CommittedTurnStatus = CommittedTurnStatus.APPLIED) -> None:
        self.status = status
        self.seen: dict[str, str] = {}
        self.calls = 0
        self.failures = 0

    async def recall_for_turn(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)

    async def release_recall(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(request)

    async def record_committed_turn(self, turn: CommittedTurn) -> CommittedTurnReceipt:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise AgentMemoryError(AgentMemoryErrorCode.TRANSIENT)
        prior = self.seen.setdefault(turn.turn_id, turn.payload_hash)
        if prior != turn.payload_hash:
            return CommittedTurnReceipt(
                turn.turn_id,
                turn.payload_hash,
                CommittedTurnStatus.CONFLICT,
                "receipt-conflict",
            )
        status = self.status
        if self.calls > 1 and status is CommittedTurnStatus.APPLIED:
            status = CommittedTurnStatus.ALREADY_APPLIED
        return CommittedTurnReceipt(turn.turn_id, turn.payload_hash, status, "receipt-1")


class FailingMemory(IdempotentMemory):
    def __init__(self, code: AgentMemoryErrorCode) -> None:
        super().__init__()
        self.code = code

    async def record_committed_turn(self, turn: CommittedTurn) -> CommittedTurnReceipt:
        del turn
        raise AgentMemoryError(self.code)


def test_after_record_before_ack_reopens_and_replays_idempotently(tmp_path: Path) -> None:
    async def case() -> None:
        path = tmp_path / "execution.db"
        database = Database.open(path)
        spec = _spec()
        _insert(database, spec)
        clock = [1.0]
        memory = IdempotentMemory()
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database),
            memory,
            owner_id="worker-1",
            clock=lambda: clock[0],
            lease_seconds=5.0,
        )

        def crash(point: str) -> None:
            if point == "memory_dispatcher.after_record_before_ack":
                raise InjectedCrash(point)

        with pytest.raises(InjectedCrash):
            await dispatcher.run_once(fault=crash)
        claimed = dispatcher.repository.read(spec.intent_id)
        assert claimed is not None and claimed.state is MemoryOutboxState.CLAIMED
        database.close()

        database = Database.open(path)
        clock[0] = 7.0
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database),
            memory,
            owner_id="worker-2",
            clock=lambda: clock[0],
            lease_seconds=5.0,
        )
        assert await dispatcher.run_once()
        applied = dispatcher.repository.read(spec.intent_id)
        assert applied is not None and applied.state is MemoryOutboxState.APPLIED
        assert memory.calls == 2
        database.close()

    asyncio.run(case())


def test_transient_retry_rejected_erased_and_backlog_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="simple_harness.execution.memory_outbox")

    async def case() -> None:
        with Database.open(tmp_path / "states.db") as database:
            spec = _spec()
            _insert(database, spec)
            clock = [1.0]
            memory = IdempotentMemory(status=CommittedTurnStatus.REJECTED_ERASED)
            memory.failures = 1
            repository = MemoryOutboxRepository(database)
            dispatcher = MemoryDispatcher(
                repository,
                memory,
                owner_id="worker",
                clock=lambda: clock[0],
            )
            assert await dispatcher.run_once()
            retrying = repository.read(spec.intent_id)
            assert retrying is not None and retrying.state is MemoryOutboxState.RETRY_WAIT
            assert retrying.error_code == AgentMemoryErrorCode.TRANSIENT.value
            clock[0] = retrying.retry_at
            assert await dispatcher.run_once()
            applied = repository.read(spec.intent_id)
            assert applied is not None and applied.state is MemoryOutboxState.APPLIED
            assert applied.error_code == CommittedTurnStatus.REJECTED_ERASED.value
            privacy_events = [
                record
                for record in caplog.records
                if record.getMessage() == "memory.committed_turn_rejected_erased"
            ]
            assert len(privacy_events) == 1
            assert privacy_events[0].turn_id == spec.turn.turn_id
            assert privacy_events[0].payload_hash == spec.payload_hash
            assert "hello" not in caplog.text and "answer" not in caplog.text
            assert repository.backlog()["applied"] == 1
            assert repository.cleanup_applied(settled_before=clock[0], limit=1) == 1
            assert repository.backlog()["applied"] == 0

    asyncio.run(case())


def test_outbox_transition_events_are_ordered_correlated_and_content_free(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "observed-states.db") as database:
            spec = _spec(answer="MEMORY正文-CANARY")
            _insert(database, spec)
            sink = RecordingSink()
            observability = ObservabilityRuntime(sink)
            clock = [1.0]
            memory = IdempotentMemory()
            memory.failures = 1
            repository = MemoryOutboxRepository(database, observability)
            dispatcher = MemoryDispatcher(
                repository,
                memory,
                owner_id="worker",
                clock=lambda: clock[0],
            )
            assert await dispatcher.run_once()
            retrying = repository.read(spec.intent_id)
            assert retrying is not None
            clock[0] = retrying.retry_at
            assert await dispatcher.run_once()
            assert observability.flush(1)
            events = sink.events()
            assert [item.event_name for item in events] == [
                "memory_outbox.claimed",
                "memory_outbox.retry_wait",
                "memory_outbox.claimed",
                "memory_outbox.applied",
            ]
            assert [item.sequence for item in events] == [1, 2, 3, 4]
            assert len({item.correlation.root_id for item in events}) == 1
            serialized = "\n".join(str(item.to_dict()) for item in events)
            assert "MEMORY正文-CANARY" not in serialized
            observability.close()

    asyncio.run(case())


def test_claim_epoch_fences_stale_owner_after_takeover(tmp_path: Path) -> None:
    with Database.open(tmp_path / "claim.db") as database:
        spec = _spec()
        _insert(database, spec)
        repository = MemoryOutboxRepository(database)
        first = repository.claim(owner_id="one", now=1.0, lease_seconds=2.0)
        assert first is not None
        assert repository.claim(owner_id="contender", now=1.0, lease_seconds=2.0) is None
        second = repository.claim(owner_id="two", now=3.0, lease_seconds=2.0)
        assert second is not None and second.claim_epoch == first.claim_epoch + 1
        with pytest.raises(UnitOfWorkConflict, match="settlement CAS"):
            repository.applied(first, now=3.0)
        assert repository.applied(second, now=3.0).state is MemoryOutboxState.APPLIED


def test_only_one_owner_claims_and_bounded_drain_reports_remaining_work(tmp_path: Path) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "concurrency.db") as database:
            first_spec = _spec("turn-1")
            second_spec = _spec("turn-2")
            _insert(database, first_spec)
            _insert(database, second_spec)
            repository = MemoryOutboxRepository(database)
            first = repository.claim(owner_id="owner-1", now=1.0, lease_seconds=10.0)
            assert first is not None
            second = repository.claim(owner_id="owner-2", now=1.0, lease_seconds=10.0)
            assert second is not None and second.intent_id != first.intent_id
            assert repository.claim(owner_id="owner-3", now=1.0, lease_seconds=10.0) is None
            repository.release(first, now=1.0, backoff_seconds=0.0, error_code="retry")
            repository.release(second, now=1.0, backoff_seconds=0.0, error_code="retry")
            dispatcher = MemoryDispatcher(
                repository,
                IdempotentMemory(),
                owner_id="drain-owner",
                clock=lambda: 1.0,
            )
            assert not await dispatcher.drain(limit=1)
            assert repository.backlog()["applied"] == 1
            assert repository.backlog()["retry_wait"] == 1

    asyncio.run(case())


@pytest.mark.parametrize(
    "memory,expected_code",
    (
        (
            IdempotentMemory(status=CommittedTurnStatus.CONFLICT),
            AgentMemoryErrorCode.CONFLICT.value,
        ),
        (
            FailingMemory(AgentMemoryErrorCode.PERMANENT),
            AgentMemoryErrorCode.PERMANENT.value,
        ),
    ),
)
def test_conflict_and_permanent_failure_dead_letter(
    tmp_path: Path,
    memory: IdempotentMemory,
    expected_code: str,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / f"{expected_code}.db") as database:
            spec = _spec()
            _insert(database, spec)
            repository = MemoryOutboxRepository(database)
            dispatcher = MemoryDispatcher(
                repository,
                memory,
                owner_id="worker",
                clock=lambda: 1.0,
            )
            assert await dispatcher.run_once()
            record = repository.read(spec.intent_id)
            assert record is not None and record.state is MemoryOutboxState.DEAD_LETTER
            assert record.error_code == expected_code

    asyncio.run(case())
