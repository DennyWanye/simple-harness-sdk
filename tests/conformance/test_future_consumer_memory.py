# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import inspect
from pathlib import Path

import pytest
from future_consumer_fixture import FutureConsumerFixture, RichProductContextProvider

from simple_harness import (
    CommandOutputState,
    CommandState,
    CommittedTurn,
    CommittedTurnReceipt,
    CommittedTurnStatus,
    ConversationTurnInput,
    JsonValue,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryRecallStatus,
    MemoryReleaseRequest,
    MemoryScopeKind,
    Message,
    MessageRole,
    RequestId,
    RunClient,
    RunId,
    StartCommandIntent,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


def _ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


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


class _BlockingRecallMemory(_MemoryProbe):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.allow_recall = asyncio.Event()

    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        self.started.set()
        await self.allow_recall.wait()
        return await super().recall_for_turn(request)


class _InjectedCrash(BaseException):
    pass


class _CrashBeforeContinuationProvider(RichProductContextProvider):
    def __init__(self, continuation_id: str) -> None:
        super().__init__()
        self.continuation_id = continuation_id
        self.crashed = False

    async def prepare_once(self, request):  # type: ignore[no-untyped-def]
        if request.continuation_id == self.continuation_id and not self.crashed:
            self.requests.append(request)
            self.crashed = True
            raise _InjectedCrash("claim persisted before provider")
        return await super().prepare_once(request)


class _CrashAfterProviderMemory(_MemoryProbe):
    def __init__(self, continuation_id: str) -> None:
        super().__init__()
        self.continuation_id = continuation_id
        self.crashed = False

    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        if request.turn_id == self.continuation_id and not self.crashed:
            self.crashed = True
            raise _InjectedCrash("provider returned before stage completion")
        return await super().recall_for_turn(request)


class _CrashFirstCommandRecall(_MemoryProbe):
    def __init__(self) -> None:
        super().__init__()
        self.crashed = asyncio.Event()

    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        del request
        self.crashed.set()
        raise _InjectedCrash("command recall crash cut")


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


def test_command_commit_precedes_memory_and_provider_calls(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _BlockingRecallMemory()
        fixture = FutureConsumerFixture(tmp_path / "command-memory.db", memory)
        runtime = await fixture.build()
        identity = fixture.identity(household="home-a", actor="alice", session="command")
        intent = StartCommandIntent(
            "future-consumer/deployment",
            "projection-key-1",
            "command-memory-order",
            RunId("run-command-memory-order"),
            RequestId("request-command-memory-order"),
            "turn-command-memory-order",
            ConversationTurnInput(
                identity,
                Message(MessageRole.USER, "remember this"),
                "remember this",
            ),
        )
        async with runtime:
            accepted = await RunClient(runtime).submit_start(intent)
            assert accepted.state is CommandState.ACCEPTED
            await asyncio.wait_for(memory.started.wait(), timeout=1)
            blocked = await RunClient(runtime).get_command(intent.command_id)
            assert blocked.receipt.state is CommandState.CONTEXT_CALL_INTENT
            assert blocked.output_state is CommandOutputState.PENDING
            assert fixture.provider.requests == []
            memory.allow_recall.set()
            for _ in range(200):
                settled = await RunClient(runtime).get_command(intent.command_id)
                if settled.receipt.state.terminal:
                    break
                await asyncio.sleep(0.01)
            assert settled.receipt.state is CommandState.APPLIED
            assert settled.output_state is CommandOutputState.PRESENT
        assert len(memory.recalls) == len(memory.releases) == len(memory.committed) == 1
        assert len(fixture.provider.requests) == 1

    asyncio.run(case())


def test_command_memory_crash_reclaims_durable_intent_after_lease_expiry(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        path = tmp_path / "command-memory-crash.db"
        crashing_memory = _CrashFirstCommandRecall()
        first = FutureConsumerFixture(path, crashing_memory, rich_context=True)
        runtime = await first.build()
        await runtime.start()
        identity = first.identity(household="home-a", actor="alice", session="crash")
        intent = StartCommandIntent(
            "future-consumer/deployment",
            "projection-key-1",
            "command-memory-crash",
            RunId("run-command-memory-crash"),
            RequestId("request-command-memory-crash"),
            "turn-command-memory-crash",
            ConversationTurnInput(
                identity,
                Message(MessageRole.USER, "recover me"),
                "recover me",
                context_source_snapshot_ref=_ref("command-memory-crash"),
            ),
        )
        accepted = await RunClient(runtime).submit_start(intent)
        assert accepted.state is CommandState.ACCEPTED
        await asyncio.wait_for(crashing_memory.crashed.wait(), timeout=1)
        for _ in range(100):
            if runtime._command_pump_task is not None and runtime._command_pump_task.done():
                break
            await asyncio.sleep(0.01)
        assert runtime._command_pump_task is not None and runtime._command_pump_task.done()
        crashed = await RunClient(runtime).get_command(intent.command_id)
        assert crashed.receipt.state is CommandState.CONTEXT_CALL_INTENT
        assert crashed.output_state is CommandOutputState.PENDING
        await runtime.close()

        with Database.open(path) as database:
            database.connection.execute(
                "UPDATE conversation_commands SET lease_expires_at=0 WHERE command_id=?",
                (intent.command_id,),
            )
            database.connection.execute(
                "UPDATE context_preparation_staging SET lease_expires_at=0 "
                "WHERE identity_key=? AND state='preparing'",
                (intent.run_id.value,),
            )

        healthy_memory = _MemoryProbe()
        second = FutureConsumerFixture(path, healthy_memory, rich_context=True)
        recovered_runtime = await second.build()
        async with recovered_runtime:
            for _ in range(200):
                recovered = await RunClient(recovered_runtime).get_command(intent.command_id)
                if recovered.receipt.state.terminal:
                    break
                await asyncio.sleep(0.01)
            assert recovered.receipt.state is CommandState.APPLIED
            assert recovered.output_state is CommandOutputState.PRESENT
        assert len(healthy_memory.recalls) == 1
        assert len(healthy_memory.releases) == 1
        assert len(healthy_memory.committed) == 1

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


def test_future_consumer_root_and_two_continuations_use_independent_refs(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        memory = _MemoryProbe()
        fixture = FutureConsumerFixture(
            tmp_path / "continuation-refs.db",
            memory,
            rich_context=True,
            block_provider=True,
        )
        runtime = await fixture.build()
        identity = fixture.identity(household="home-a", actor="alice", session="session-refs")
        async with runtime:
            run_id = await fixture.start_turn(
                runtime,
                identity=identity,
                run_id="run-refs",
                text="root",
                context_source_snapshot_ref=_ref("root"),
            )
            await asyncio.wait_for(fixture.provider.started.wait(), timeout=1.0)
            await fixture.continue_turn(
                runtime,
                run_id=run_id,
                continuation_id="continuation-1",
                text="first continuation",
                context_source_snapshot_ref=_ref("continuation-1"),
            )
            await fixture.continue_turn(
                runtime,
                run_id=run_id,
                continuation_id="continuation-2",
                text="fallback continuation",
            )
            with pytest.raises(UnitOfWorkConflict, match="reused differently"):
                await fixture.continue_turn(
                    runtime,
                    run_id=run_id,
                    continuation_id="continuation-1",
                    text="first continuation",
                    context_source_snapshot_ref=_ref("changed-ref"),
                )
            with pytest.raises(UnitOfWorkConflict, match="reused differently"):
                await fixture.continue_turn(
                    runtime,
                    run_id=run_id,
                    continuation_id="continuation-1",
                    text="changed payload",
                    context_source_snapshot_ref=_ref("continuation-1"),
                )
            rows = runtime._ports.context_staging.database.connection.execute(
                "SELECT identity_key,source_snapshot_ref FROM context_preparation_staging "
                "ORDER BY created_at,identity_key"
            ).fetchall()
            refs = {str(row[0]): str(row[1]) for row in rows}
            assert refs["run-refs"] == _ref("root")
            assert refs["continuation-1"] == _ref("continuation-1")
            assert refs["continuation-2"].startswith("sha256:")
            assert len(set(refs.values())) == 3
            fallback = runtime._uow.read_continuation("continuation-2")
            assert fallback is not None
            fallback_payload = thaw_json(fallback.payload)
            assert isinstance(fallback_payload, dict)
            prepared = fallback_payload["prepared_context"]
            assert isinstance(prepared, dict)
            product = prepared["product_context"]
            assert isinstance(product, dict)
            current_message = product["current_message"]
            assert isinstance(current_message, dict)
            assert current_message["role"] == "user"
            assert current_message["content"] == "fallback continuation"
            fallback_json = canonical_json(product)
            assert "first continuation" not in fallback_json
            assert '"content":"root"' not in fallback_json
            fixture.provider.allow.set()
            await runtime.wait_idle(run_id)

    asyncio.run(case())


@pytest.mark.parametrize("crash_point", ("before_provider", "after_provider"))
def test_continuation_preparation_crash_replays_the_claimed_ref(
    tmp_path: Path,
    crash_point: str,
) -> None:
    async def case() -> None:
        continuation_id = f"continuation-{crash_point}"
        memory: _MemoryProbe = (
            _CrashAfterProviderMemory(continuation_id)
            if crash_point == "after_provider"
            else _MemoryProbe()
        )
        fixture = FutureConsumerFixture(
            tmp_path / f"{crash_point}.db",
            memory,
            rich_context=True,
            block_provider=True,
        )
        context = (
            _CrashBeforeContinuationProvider(continuation_id)
            if crash_point == "before_provider"
            else RichProductContextProvider()
        )
        fixture.context_provider = context
        runtime = await fixture.build()
        identity = fixture.identity(household="home-a", actor="alice", session=crash_point)
        await runtime.start()
        run_id = await fixture.start_turn(
            runtime,
            identity=identity,
            run_id=f"run-{crash_point}",
            text="root",
            context_source_snapshot_ref=_ref(f"root-{crash_point}"),
        )
        await asyncio.wait_for(fixture.provider.started.wait(), timeout=1.0)
        with pytest.raises(_InjectedCrash):
            await fixture.continue_turn(
                runtime,
                run_id=run_id,
                continuation_id=continuation_id,
                text="crash-safe continuation",
                context_source_snapshot_ref=_ref(continuation_id),
            )
        repository = runtime._ports.context_staging
        stage = repository.database.connection.execute(
            "SELECT state,source_snapshot_ref FROM context_preparation_staging "
            "WHERE identity_key=?",
            (continuation_id,),
        ).fetchone()
        assert tuple(stage) == ("preparing", _ref(continuation_id))
        with repository.database.transaction() as connection:
            connection.execute(
                "UPDATE context_preparation_staging SET lease_expires_at=0 "
                "WHERE identity_key=?",
                (continuation_id,),
            )
        await runtime.close()

        reopened_fixture = FutureConsumerFixture(
            fixture.database_path,
            memory,
            rich_context=True,
            block_provider=True,
        )
        reopened_fixture.context_provider = context
        reopened = await reopened_fixture.build()
        async with reopened:
            await reopened_fixture.continue_turn(
                reopened,
                run_id=run_id,
                continuation_id=continuation_id,
                text="crash-safe continuation",
                context_source_snapshot_ref=_ref(continuation_id),
            )
            reopened_repository = reopened._ports.context_staging
            replayed = reopened_repository.get(
                "agent-memory-stage/v1/"
                + memory.recalls[-1].query_id.rsplit("/", 1)[-1]
            )
            assert replayed is not None and replayed.state.value == "consumed"
            assert replayed.source_snapshot_ref == _ref(continuation_id)
            continuation_requests = [
                request
                for request in context.requests
                if request.continuation_id == continuation_id
            ]
            assert len(continuation_requests) == 2
            assert {request.source_snapshot_ref for request in continuation_requests} == {
                _ref(continuation_id)
            }
            recalls_before_replay = len(memory.recalls)
            requests_before_replay = len(context.requests)
            await reopened_fixture.continue_turn(
                reopened,
                run_id=run_id,
                continuation_id=continuation_id,
                text="crash-safe continuation",
                context_source_snapshot_ref=_ref(continuation_id),
            )
            assert len(memory.recalls) == recalls_before_replay == 2
            assert len(context.requests) == requests_before_replay
            with pytest.raises(UnitOfWorkConflict, match="reused differently"):
                await reopened_fixture.continue_turn(
                    reopened,
                    run_id=run_id,
                    continuation_id=continuation_id,
                    text="crash-safe continuation",
                    context_source_snapshot_ref=_ref(continuation_id + "-changed"),
                )
            with pytest.raises(UnitOfWorkConflict, match="reused differently"):
                await reopened_fixture.continue_turn(
                    reopened,
                    run_id=run_id,
                    continuation_id=continuation_id,
                    text="changed crash-safe payload",
                    context_source_snapshot_ref=_ref(continuation_id),
                )
            assert len(memory.recalls) == recalls_before_replay
            assert len(context.requests) == requests_before_replay
            assert reopened_repository.database.connection.execute(
                "SELECT COUNT(*) FROM context_preparation_staging WHERE identity_key=?",
                (continuation_id,),
            ).fetchone()[0] == 1
            reopened_fixture.provider.allow.set()
            await reopened.wait_idle(run_id)

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

    assert "`simple_harness` has completed exact-wheel product" in readme
    assert "| `simple_harness` | integrated and real-UI validated |" in status
    assert "Version 0.5.1 is the release candidate" in status
    assert "Memory SDK 0.5.1" in status
    for consumer in ("AIPhone", "K6/AgentOS"):
        assert f"| {consumer} | interface ready, not integrated |" in status
        assert consumer in contracts
    assert "have not been integrated or tested" in contracts
