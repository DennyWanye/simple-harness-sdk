# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from future_consumer_fixture import FutureConsumerFixture, RichProductContextProvider

from simple_harness import (
    CommittedTurn,
    CommittedTurnReceipt,
    CommittedTurnStatus,
    JsonValue,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryRecallStatus,
    MemoryReleaseRequest,
    MemoryScopeKind,
    RunClient,
    RunId,
    canonical_json,
)
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import RunState


class _MemoryProbe:
    def __init__(self, *, erased: bool = False) -> None:
        self.erased = erased
        self.recalls: list[MemoryRecallRequest] = []
        self.releases: list[MemoryReleaseRequest] = []
        self.committed: list[CommittedTurn] = []

    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        self.recalls.append(request)
        payload: dict[str, JsonValue] = {"items": [{"text": "prefers concise answers"}]}
        return MemoryRecallResult(
            request.query_id,
            request.query_hash,
            f"result-{len(self.recalls)}",
            payload,
            MemoryRecallStatus.READY,
            1,
            len(canonical_json(payload).encode("utf-8")),
            f"fence-{len(self.recalls)}",
        )

    async def release_recall(self, request: MemoryReleaseRequest) -> None:
        self.releases.append(request)

    async def record_committed_turn(self, request: CommittedTurn) -> CommittedTurnReceipt:
        self.committed.append(request)
        status = CommittedTurnStatus.REJECTED_ERASED if self.erased else CommittedTurnStatus.APPLIED
        return CommittedTurnReceipt(
            request.turn_id,
            request.payload_hash,
            status,
            f"receipt-{len(self.committed)}",
        )


class _BlockingMemoryProbe(_MemoryProbe):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.allow_record = asyncio.Event()

    async def record_committed_turn(self, request: CommittedTurn) -> CommittedTurnReceipt:
        self.started.set()
        await self.allow_record.wait()
        return await super().record_committed_turn(request)


def test_future_consumer_minimal_identity_scopes_committed_turn_and_memory_none(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        memory = _MemoryProbe()
        fixture = FutureConsumerFixture(tmp_path / "with-memory.db", memory)
        runtime = await fixture.build()
        identity = fixture.identity(household="home-a", actor="alice", session="session-a")
        async with runtime:
            await fixture.complete_turn(
                runtime,
                identity=identity,
                run_id="run-with-memory",
                text="remember concise answers",
            )
        assert len(memory.recalls) == len(memory.releases) == len(memory.committed) == 1
        recall = memory.recalls[0]
        assert recall.identity == identity
        assert {(scope.kind, scope.owner_id) for scope in recall.scopes} == {
            (MemoryScopeKind.PERSONAL, "alice"),
            (MemoryScopeKind.FAMILY, "home-a"),
        }
        assert memory.committed[0].identity == identity
        assert memory.committed[0].user_text == "remember concise answers"
        assert memory.committed[0].assistant_text == "future consumer answer"

        without_memory = FutureConsumerFixture(tmp_path / "without-memory.db", None)
        runtime_without_memory = await without_memory.build()
        async with runtime_without_memory:
            await without_memory.complete_turn(
                runtime_without_memory,
                identity=without_memory.identity(
                    household="home-b", actor="bob", session="session-b"
                ),
                run_id="run-without-memory",
                text="ordinary run",
            )
            record = RunClient(runtime_without_memory).query(RunId("run-without-memory"))
            assert record is not None and record.state is RunState.COMPLETED

    asyncio.run(case())


def test_future_consumer_rich_context_is_frozen_across_replay_and_restart(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        memory = _MemoryProbe()
        path = tmp_path / "restart.db"
        first = FutureConsumerFixture(path, memory, rich_context=True)
        identity = first.identity(household="home-a", actor="alice", session="session-a")
        runtime = await first.build()
        async with runtime:
            await first.complete_turn(
                runtime,
                identity=identity,
                run_id="run-restart",
                text="use product context",
            )
            await first.complete_turn(
                runtime,
                identity=identity,
                run_id="run-restart",
                text="use product context",
            )
        context = first.context_provider
        assert isinstance(context, RichProductContextProvider)
        assert len(context.requests) == len(first.provider.requests) == 1
        assert len(memory.recalls) == len(memory.committed) == 1

        second = FutureConsumerFixture(path, memory, rich_context=True)
        reopened = await second.build()
        async with reopened:
            await second.complete_turn(
                reopened,
                identity=identity,
                run_id="run-restart",
                text="use product context",
            )
        second_context = second.context_provider
        assert isinstance(second_context, RichProductContextProvider)
        assert second_context.requests == []
        assert second.provider.requests == []
        assert len(memory.recalls) == len(memory.committed) == 1

    asyncio.run(case())


def test_future_consumer_erasure_receipt_converges_without_retry(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _MemoryProbe(erased=True)
        path = tmp_path / "erased.db"
        fixture = FutureConsumerFixture(path, memory)
        runtime = await fixture.build()
        async with runtime:
            await fixture.complete_turn(
                runtime,
                identity=fixture.identity(
                    household="home-a", actor="alice", session="session-erased"
                ),
                run_id="run-erased",
                text="do not resurrect erased memory",
            )
        assert len(memory.committed) == 1
        with Database.open(path) as database:
            assert tuple(
                database.connection.execute(
                    "SELECT state,attempt_count,error_code FROM memory_outbox"
                ).fetchone()
            ) == ("applied", 1, "rejected_erased")

    asyncio.run(case())


def test_runtime_close_waits_for_inflight_committed_turn(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _BlockingMemoryProbe()
        fixture = FutureConsumerFixture(tmp_path / "slow-memory.db", memory)
        runtime = await fixture.build()

        async def release_record() -> None:
            await asyncio.sleep(0.01)
            memory.allow_record.set()

        async with runtime:
            await fixture.complete_turn(
                runtime,
                identity=fixture.identity(
                    household="home-a", actor="alice", session="session-slow"
                ),
                run_id="run-slow-memory",
                text="wait for the committed turn",
            )
            await asyncio.wait_for(memory.started.wait(), timeout=1.0)
            release_task = asyncio.create_task(release_record())
        await release_task
        assert len(memory.committed) == 1

    asyncio.run(case())


def test_future_consumer_fixture_uses_only_official_memory_boundary() -> None:
    source = inspect.getsource(__import__("future_consumer_fixture"))
    for retired in (
        "ConversationMemoryQueryPort",
        "ConversationMemorySinkPort",
        "MemoryQueryPort",
        "MemoryWritePort",
        "recall_bounded(",
        "append_message(",
    ):
        assert retired not in source
    assert "ConsumerRuntimePorts(" in source
    assert "memory=self.memory" in source
    assert "ConsumerRuntimePolicies.local_default()" in source


def test_candidate_docs_do_not_overstate_product_integration() -> None:
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    status = (root / "INTEGRATION_STATUS.md").read_text(encoding="utf-8")
    contracts = (root / "docs/api/contracts.md").read_text(encoding="utf-8")

    assert "simple_harness` is the only" in readme
    assert "designated real-test consumer; cutover pending" in status
    for consumer in ("AIPhone", "K6/AgentOS"):
        assert f"| {consumer} | interface ready, not integrated |" in status
        assert consumer in contracts
    assert "have not been integrated or tested" in contracts
