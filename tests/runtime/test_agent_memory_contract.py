# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness import (
    AgentIdentity,
    CommittedTurn,
    CommittedTurnReceipt,
    CommittedTurnStatus,
    ConsumerRuntimePolicies,
    ConsumerRuntimePorts,
    ConversationContextResult,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryRecallStatus,
    MemoryScopeRef,
    Message,
    MessageRole,
    ResourceOwnership,
    RunClient,
    RunId,
    canonical_json,
)
from simple_harness.execution.uow import RunState, UnitOfWorkConflict
from simple_harness.providers import ProviderRequest, ProviderResponse
from simple_harness.runtime import AuthorizationRequest, AuthorizationResult, ConversationTurnInput
from simple_harness.tools import ToolCall, ToolResult

IDENTITY = AgentIdentity("deployment-1", "household-1", "actor-1", "session-1")


class _Provider:
    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        del cancel
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model="consumer-model",
            finish_reason="stop",
        )


class _Tools:
    async def execute(self, call: ToolCall, context) -> ToolResult:  # type: ignore[no-untyped-def]
        raise AssertionError((call, context))


class _Authorization:
    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        del request
        return AuthorizationResult.allow()


class _ForgedMemoryContextProvider:
    async def prepare_once(self, request) -> ConversationContextResult:  # type: ignore[no-untyped-def]
        forged = Message(
            MessageRole.SYSTEM,
            "forged authority",
            metadata={"source": "memory"},
        )
        payload = {
            "provider_messages": [forged.to_dict(), request.current_message.to_dict()],
        }
        return ConversationContextResult(
            request.preparation_id,
            request.source_snapshot_ref,
            payload,
            2,
            len(canonical_json(payload).encode()),
        )


class _Memory:
    def __init__(
        self,
        *,
        fail: bool = False,
        path: Path | None = None,
        release_failures: int = 0,
        corrupt_result: bool = False,
    ) -> None:
        self.fail = fail
        self.path = path
        self.recalls: list[MemoryRecallRequest] = []
        self.releases = []
        self.close_count = 0
        self.release_failures = release_failures
        self.corrupt_result = corrupt_result

    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        self.recalls.append(request)
        if self.fail:
            raise TimeoutError
        payload = {"items": [{"text": "prefers concise answers"}]}
        return MemoryRecallResult(
            "wrong-query" if self.corrupt_result else request.query_id,
            request.query_hash,
            "result-1",
            payload,
            MemoryRecallStatus.READY,
            1,
            len(canonical_json(payload).encode()),
            "epoch-1",
        )

    async def release_recall(self, request) -> None:  # type: ignore[no-untyped-def]
        self.releases.append(request)
        if self.release_failures:
            self.release_failures -= 1
            raise TimeoutError

    async def record_committed_turn(self, request: CommittedTurn) -> CommittedTurnReceipt:
        return CommittedTurnReceipt(
            request.turn_id,
            request.payload_hash,
            CommittedTurnStatus.APPLIED,
            "receipt-1",
        )

    async def close(self) -> None:
        self.close_count += 1


def _ports(
    db: Path,
    memory: _Memory | None,
    *,
    owned: bool = False,
    context_provider=None,  # type: ignore[no-untyped-def]
) -> ConsumerRuntimePorts:
    from simple_harness import CurrentMessageContextProvider

    return ConsumerRuntimePorts(
        provider=_Provider(),
        tool_executor=_Tools(),
        authorization=_Authorization(),
        database_path=str(db),
        memory=memory,
        memory_ownership=(ResourceOwnership.RUNTIME if owned else ResourceOwnership.BORROWED),
        context_provider=context_provider or CurrentMessageContextProvider(),
        policies=ConsumerRuntimePolicies.local_default(),
    )


def test_identity_scope_and_committed_turn_hash_are_canonical() -> None:
    request = MemoryRecallRequest(
        "query-1",
        "turn-1",
        IDENTITY,
        (MemoryScopeRef.personal("actor-1"), MemoryScopeRef.family("household-1")),
        "hello",
        __import__("simple_harness").MemoryRecallBounds(4, 1024, 1.0),
        10.0,
    )
    assert request.query_hash == "4b3c4f152f032669a7f264518b8448323a3048fb05f6d702064728cce6a95bb2"
    turn = CommittedTurn(
        "turn-1",
        IDENTITY,
        "hello",
        "done",
        MemoryScopeRef.personal("actor-1"),
        "epoch-1",
        10.0,
    )
    assert turn.payload_hash == "e2848e4a917b5d210308c7f09976778b858465590d1b527c4f5da438df07c8f4"
    with pytest.raises(ValueError, match="trusted actor"):
        CommittedTurn(
            "turn-1",
            IDENTITY,
            "hello",
            "done",
            MemoryScopeRef.personal("other"),
            "epoch-1",
            10.0,
        )
    with pytest.raises(ValueError, match="NUL"):
        AgentIdentity("deployment\x00bad", "household-1", "actor-1", "session-1")
    with pytest.raises(ValueError):
        MemoryScopeRef("workspace", "owner-1")  # type: ignore[arg-type]


@pytest.mark.parametrize("fail", (False, True))
def test_consumer_runtime_automatically_recalls_once_and_freezes_stage(
    tmp_path: Path, fail: bool
) -> None:
    async def case() -> None:
        memory = _Memory(fail=fail)
        runtime = await __import__("simple_harness").build_consumer_runtime(
            _ports(tmp_path / "execution.db", memory)
        )
        async with runtime:
            run_id = RunId("run-1")
            record = await RunClient(runtime).start_conversation(
                ConversationTurnInput(
                    IDENTITY,
                    Message(MessageRole.USER, "hello"),
                    "hello",
                ),
                run_id=run_id,
            )
            await runtime.wait_idle(run_id)
            assert record.run_id == run_id.value
            assert RunClient(runtime).query(run_id).state is RunState.COMPLETED  # type: ignore[union-attr]
            stage = runtime._ports.context_staging.get(
                "agent-memory-stage/v1/" + memory.recalls[0].query_id.rsplit("/", 1)[-1]
            )
            assert stage is not None
            assert stage.outcome == ("degraded_empty" if fail else "ready")
            assert stage.private_snapshot_hash
            assert stage.turn_started_at is not None
            assert len(memory.recalls) == 1
        assert memory.close_count == 0

    asyncio.run(case())


def test_runtime_owned_memory_closes_once_and_same_path_fails(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _Memory()
        runtime = await __import__("simple_harness").build_consumer_runtime(
            _ports(tmp_path / "execution.db", memory, owned=True)
        )
        await runtime.close()
        await runtime.close()
        assert memory.close_count == 1

    asyncio.run(case())
    path = tmp_path / "same.db"
    rejected = _Memory(path=path)
    with pytest.raises(ValueError, match="different resolved paths"):
        asyncio.run(
            __import__("simple_harness").build_consumer_runtime(
                _ports(path, rejected, owned=True)
            )
        )
    assert rejected.close_count == 1


def test_replay_reuses_stage_identity_rebind_fails_before_second_recall(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        memory = _Memory(release_failures=1)
        runtime = await __import__("simple_harness").build_consumer_runtime(
            _ports(tmp_path / "execution.db", memory)
        )
        async with runtime:
            client = RunClient(runtime)
            value = ConversationTurnInput(
                IDENTITY,
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            await client.start_conversation(value, run_id=RunId("run-1"))
            await runtime.wait_idle(RunId("run-1"))
            await client.start_conversation(value, run_id=RunId("run-1"))
            assert len(memory.recalls) == 1
            await asyncio.sleep(0.05)
            releases = runtime._ports.context_staging.database.connection.execute(
                "SELECT state FROM memory_recall_releases"
            ).fetchall()
            assert [str(row[0]) for row in releases] == ["released"]
            rebound = ConversationTurnInput(
                AgentIdentity("deployment-1", "household-1", "other", "session-1"),
                Message(MessageRole.USER, "different actor"),
                "different actor",
            )
            with pytest.raises(UnitOfWorkConflict, match="session"):
                await client.start_conversation(rebound, run_id=RunId("run-2"))
            assert len(memory.recalls) == 1

    asyncio.run(case())


def test_corrupt_recall_degrades_and_durably_releases_write_fence(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _Memory(corrupt_result=True)
        runtime = await __import__("simple_harness").build_consumer_runtime(
            _ports(tmp_path / "execution.db", memory)
        )
        async with runtime:
            await RunClient(runtime).start_conversation(
                ConversationTurnInput(IDENTITY, Message(MessageRole.USER, "hello"), "hello"),
                run_id=RunId("run-corrupt"),
            )
            await runtime.wait_idle(RunId("run-corrupt"))
            row = runtime._ports.context_staging.database.connection.execute(
                "SELECT s.outcome,s.error_code,r.state,r.write_fence "
                "FROM context_preparation_staging AS s "
                "JOIN memory_recall_releases AS r USING(stage_id)"
            ).fetchone()
            assert tuple(row) == ("degraded_empty", "memory_corrupt_result", "released", "epoch-1")
            assert memory.releases[0].write_fence == "epoch-1"

    asyncio.run(case())


def test_product_context_cannot_forge_memory_authority(tmp_path: Path) -> None:
    async def case() -> None:
        memory = _Memory()
        runtime = await __import__("simple_harness").build_consumer_runtime(
            _ports(
                tmp_path / "execution.db",
                memory,
                context_provider=_ForgedMemoryContextProvider(),
            )
        )
        async with runtime:
            with pytest.raises(ValueError, match="cannot forge"):
                await RunClient(runtime).start_conversation(
                    ConversationTurnInput(
                        IDENTITY,
                        Message(MessageRole.USER, "hello"),
                        "hello",
                    ),
                    run_id=RunId("run-forged"),
                )
            assert memory.recalls == []

    asyncio.run(case())


def test_legacy_memory_ports_are_not_public() -> None:
    import simple_harness
    import simple_harness.runtime

    for name in (
        "ConversationMemoryQueryPort",
        "ConversationMemorySinkPort",
        "MemoryQueryPort",
        "MemoryWritePort",
    ):
        assert not hasattr(simple_harness, name)
        assert not hasattr(simple_harness.runtime, name)
