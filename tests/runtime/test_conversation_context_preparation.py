# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from simple_harness import Message, MessageRole, canonical_json, thaw_json
from simple_harness.execution.context_staging import (
    ContextStageKind,
    ContextStageState,
    ContextStagingRepository,
)
from simple_harness.execution.sqlite import Database
from simple_harness.runtime import (
    ConversationMemoryError,
    ConversationMemoryErrorCode,
    ConversationMemoryQueryStatus,
    ConversationMemoryRecallResult,
    ConversationTurnInput,
    context_query_id,
    prepare_consumer_conversation_context,
    prepare_sdk_conversation_context,
)


class RecallSpy:
    def __init__(
        self,
        *,
        result_query_id: str | None = None,
        result_hash: str | None = None,
    ) -> None:
        self.calls = []
        self.release_calls: list[tuple[str, str, str]] = []
        self.release_effects: set[tuple[str, str, str]] = set()
        self.result_query_id = result_query_id
        self.result_hash = result_hash
        self.payload_hash = hashlib.sha256(
            canonical_json({"items": [{"text": "memory is untrusted"}]}).encode()
        ).hexdigest()

    async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
        self.calls.append(query)
        payload = {"items": [{"text": "memory is untrusted"}]}
        encoded = canonical_json(payload).encode()
        return ConversationMemoryRecallResult(
            self.result_query_id or query.context_query_id,
            "result-1",
            self.result_hash or query.query_hash,
            payload,
            hashlib.sha256(encoded).hexdigest(),
            ConversationMemoryQueryStatus.COMPLETE,
            1,
            len(encoded),
        )

    async def release(self, *, user_id: str, context_query_id: str, result_hash: str) -> None:
        call = (user_id, context_query_id, result_hash)
        self.release_calls.append(call)
        if result_hash != self.payload_hash:
            raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
        self.release_effects.add(call)

    async def close(self) -> None:
        return None


class FlakyReleaseSpy(RecallSpy):
    def __init__(self) -> None:
        super().__init__()
        self.release_failures = 1

    async def release(self, *, user_id: str, context_query_id: str, result_hash: str) -> None:
        if self.release_failures:
            self.release_failures -= 1
            self.release_calls.append((user_id, context_query_id, result_hash))
            raise ConversationMemoryError(ConversationMemoryErrorCode.TRANSIENT)
        await super().release(
            user_id=user_id,
            context_query_id=context_query_id,
            result_hash=result_hash,
        )


class OutOfBoundsRecallSpy(RecallSpy):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
        self.calls.append(query)
        payload = (
            {"items": [{"text": "x" * (10 * 1024)}]}
            if self.mode == "bytes"
            else {"items": [{"text": str(index)} for index in range(9)]}
        )
        encoded = canonical_json(payload).encode()
        self.payload_hash = hashlib.sha256(encoded).hexdigest()
        return ConversationMemoryRecallResult(
            query.context_query_id,
            "result-out-of-bounds",
            query.query_hash,
            payload,
            self.payload_hash,
            ConversationMemoryQueryStatus.COMPLETE,
            len(payload["items"]),
            len(encoded),
        )


class HangingRecallSpy(RecallSpy):
    async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
        self.calls.append(query)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class DriftRecallSpy(RecallSpy):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
        self.calls.append(query)
        payload = {
            "items": (
                ["not-an-object"] if self.mode == "item_structure" else [{"text": "remembered"}]
            )
        }
        encoded = canonical_json(payload).encode()
        self.payload_hash = hashlib.sha256(encoded).hexdigest()
        return SimpleNamespace(
            context_query_id=query.context_query_id,
            result_id="result-drift",
            query_hash=query.query_hash,
            payload=payload,
            result_hash=self.payload_hash,
            status=(
                "invalid-status"
                if self.mode == "status"
                else ConversationMemoryQueryStatus.COMPLETE
            ),
            item_count=2 if self.mode == "item_count" else 1,
            byte_count=(len(encoded) - 1 if self.mode == "byte_count" else len(encoded)),
        )


def test_sdk_preparation_has_one_logical_recall_and_reuses_private_bytes(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "execution.db") as database:
            repository = ContextStagingRepository(database)
            spy = RecallSpy()
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            kwargs = dict(
                stage_id="stage-1",
                kind=ContextStageKind.ROOT,
                identity_key="request-1",
                value=value,
                owner_id="worker-1",
                now=lambda: 1.0,
                lease_seconds=10.0,
                max_items=8,
                max_bytes=4096,
                timeout_seconds=0.5,
            )
            first = await prepare_sdk_conversation_context(repository, spy, **kwargs)
            second = await prepare_sdk_conversation_context(repository, spy, **kwargs)
            assert first.state is second.state is ContextStageState.STAGED
            assert first.private_snapshot_hash == second.private_snapshot_hash
            assert len(spy.calls) == 1
            assert spy.calls[0].context_query_id == context_query_id(
                ContextStageKind.ROOT, "request-1"
            )
            assert first.private_snapshot is not None
            messages = first.private_snapshot["provider_messages"]
            assert isinstance(messages, list)
            recalled = thaw_json(messages[0])
            assert recalled["role"] == "user"
            assert recalled["metadata"]["trust"] == "untrusted_data"
            assert len(spy.release_calls) == 2
            assert len(spy.release_effects) == 1

    asyncio.run(case())


@pytest.mark.parametrize(
    "spy",
    (
        RecallSpy(result_query_id="wrong-context-query"),
        RecallSpy(result_hash="f" * 64),
    ),
)
def test_sdk_preparation_rejects_mismatched_recall_identity_without_staging(
    tmp_path: Path, spy: RecallSpy
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "execution.db") as database:
            repository = ContextStagingRepository(database)
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_sdk_conversation_context(
                    repository,
                    spy,
                    stage_id="stage-mismatch",
                    kind=ContextStageKind.ROOT,
                    identity_key="request-1",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    max_items=8,
                    max_bytes=4096,
                    timeout_seconds=0.5,
                )
            assert captured.value.code is ConversationMemoryErrorCode.QUERY_CONFLICT
            record = repository.get("stage-mismatch")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert record.private_snapshot is None
            assert len(spy.release_calls) == 1

    asyncio.run(case())


def _consumer_snapshot(
    query_id: str,
    current_message: Message,
    *,
    memory_role: str = "user",
    memory_message_role: MessageRole | str = MessageRole.USER,
    memory_message_trust: str | None = "untrusted_data",
    result_hash: str,
) -> dict:
    memory_metadata = {"source": "memory"}
    if memory_message_trust is not None:
        memory_metadata["trust"] = memory_message_trust
    return {
        "schema_version": 1,
        "lineage": {
            "context_query_id": query_id,
            "memory_result_id": "result-1",
            "memory_result_hash": result_hash,
        },
        "memory": {
            "role": memory_role,
            "trust": "untrusted_data",
            "result": {"items": [{"text": "memory is untrusted"}]},
        },
        "current_message": current_message.to_dict(),
        "provider_messages": [
            Message(
                MessageRole.SYSTEM,
                "Keep the approved persona and skills.",
                metadata={"source": "persona"},
            ).to_dict(),
            Message(
                memory_message_role,
                "Untrusted recalled memory data.",
                metadata=memory_metadata,
            ).to_dict(),
            current_message.to_dict(),
        ],
    }


def test_consumer_preparation_validates_lineage_and_releases_memory_result(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "consumer-valid.db") as database:
            repository = ContextStagingRepository(database)
            memory = RecallSpy()
            current = Message(MessageRole.USER, "hello")
            value = ConversationTurnInput("user-1", "session-1", current, "hello")

            async def preparer(query_id: str):  # type: ignore[no-untyped-def]
                return _consumer_snapshot(query_id, current, result_hash=memory.payload_hash)

            kwargs = dict(
                stage_id="consumer-stage",
                kind=ContextStageKind.ROOT,
                identity_key="consumer-request",
                value=value,
                owner_id="worker-1",
                now=lambda: 1.0,
                lease_seconds=10.0,
                memory=memory,
            )
            first = await prepare_consumer_conversation_context(repository, preparer, **kwargs)
            second = await prepare_consumer_conversation_context(repository, preparer, **kwargs)

            assert first.state is second.state is ContextStageState.STAGED
            assert first.memory_result_id == "result-1"
            assert first.memory_result_hash == memory.payload_hash
            assert len(memory.release_calls) == 2
            assert len(memory.release_effects) == 1
            assert first.private_snapshot is not None
            messages = first.private_snapshot["provider_messages"]
            assert isinstance(messages, list)
            assert messages[0]["role"] == "system"
            assert messages[0]["metadata"]["source"] == "persona"
            assert messages[1]["role"] == "user"
            assert messages[1]["metadata"]["trust"] == "untrusted_data"

    asyncio.run(case())


def test_consumer_preparation_without_memory_keeps_system_persona(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "consumer-without-memory.db") as database:
            repository = ContextStagingRepository(database)
            current = Message(MessageRole.USER, "hello")
            value = ConversationTurnInput("user-1", "session-1", current, "hello")

            async def preparer(query_id: str):  # type: ignore[no-untyped-def]
                return {
                    "schema_version": 1,
                    "lineage": {"context_query_id": query_id},
                    "current_message": current.to_dict(),
                    "provider_messages": [
                        Message(
                            MessageRole.SYSTEM,
                            "Keep the approved persona and skills.",
                            metadata={"source": "persona"},
                        ).to_dict(),
                        current.to_dict(),
                    ],
                }

            staged = await prepare_consumer_conversation_context(
                repository,
                preparer,
                stage_id="stage-no-memory",
                kind=ContextStageKind.ROOT,
                identity_key="request-no-memory",
                value=value,
                owner_id="worker-1",
                now=lambda: 1.0,
                lease_seconds=10.0,
            )
            assert staged.state is ContextStageState.STAGED
            assert staged.memory_result_id is None
            assert staged.memory_result_hash is None

    asyncio.run(case())


@pytest.mark.parametrize(
    "malice",
    (
        "system_memory",
        "wrong_query",
        "missing_result_hash",
        "lineage_without_memory",
        "memory_without_lineage",
    ),
)
def test_consumer_preparation_rejects_invalid_authority_without_staging(
    tmp_path: Path,
    malice: str,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / f"consumer-{malice}.db") as database:
            repository = ContextStagingRepository(database)
            memory = RecallSpy()
            current = Message(MessageRole.USER, "hello")
            value = ConversationTurnInput("user-1", "session-1", current, "hello")

            async def preparer(query_id: str):  # type: ignore[no-untyped-def]
                snapshot = _consumer_snapshot(
                    query_id,
                    current,
                    memory_role="system" if malice == "system_memory" else "user",
                    memory_message_role=(
                        MessageRole.SYSTEM if malice == "system_memory" else MessageRole.USER
                    ),
                    memory_message_trust=(None if malice == "system_memory" else "untrusted_data"),
                    result_hash=memory.payload_hash,
                )
                if malice == "wrong_query":
                    snapshot["lineage"]["context_query_id"] = "wrong-query"
                if malice == "missing_result_hash":
                    del snapshot["lineage"]["memory_result_hash"]
                if malice == "lineage_without_memory":
                    del snapshot["memory"]
                    del snapshot["provider_messages"][1]
                if malice == "memory_without_lineage":
                    del snapshot["lineage"]["memory_result_id"]
                    del snapshot["lineage"]["memory_result_hash"]
                return snapshot

            with pytest.raises((TypeError, ValueError)):
                await prepare_consumer_conversation_context(
                    repository,
                    preparer,
                    stage_id=f"stage-{malice}",
                    kind=ContextStageKind.ROOT,
                    identity_key=f"request-{malice}",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    memory=memory,
                )
            record = repository.get(f"stage-{malice}")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert record.private_snapshot is None
            expected_release_calls = (
                1 if malice in {"system_memory", "lineage_without_memory"} else 0
            )
            assert len(memory.release_calls) == expected_release_calls

    asyncio.run(case())


def test_consumer_preparation_surfaces_release_hash_conflict_after_durable_stage(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "consumer-release-conflict.db") as database:
            repository = ContextStagingRepository(database)
            memory = RecallSpy()
            current = Message(MessageRole.USER, "hello")
            value = ConversationTurnInput("user-1", "session-1", current, "hello")

            async def preparer(query_id: str):  # type: ignore[no-untyped-def]
                return _consumer_snapshot(query_id, current, result_hash="f" * 64)

            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_consumer_conversation_context(
                    repository,
                    preparer,
                    stage_id="stage-release-conflict",
                    kind=ContextStageKind.ROOT,
                    identity_key="request-release-conflict",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    memory=memory,
                )
            assert captured.value.code is ConversationMemoryErrorCode.QUERY_CONFLICT
            record = repository.get("stage-release-conflict")
            assert record is not None
            assert record.state is ContextStageState.STAGED
            assert record.memory_result_hash == "f" * 64
            assert len(memory.release_calls) == 1

    asyncio.run(case())


def test_sdk_preparation_releases_result_when_stage_completion_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "sdk-cancelled.db") as database:
            repository = ContextStagingRepository(database)
            memory = RecallSpy()
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )

            def cancelled(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                raise asyncio.CancelledError

            monkeypatch.setattr(repository, "complete", cancelled)
            with pytest.raises(asyncio.CancelledError):
                await prepare_sdk_conversation_context(
                    repository,
                    memory,
                    stage_id="stage-cancelled",
                    kind=ContextStageKind.ROOT,
                    identity_key="request-cancelled",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    max_items=8,
                    max_bytes=4096,
                    timeout_seconds=0.5,
                )
            record = repository.get("stage-cancelled")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert len(memory.release_calls) == 1
            assert len(memory.release_effects) == 1

    asyncio.run(case())


def test_sdk_preparation_retries_release_without_recalling_or_rewriting_stage(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "sdk-release-retry.db") as database:
            repository = ContextStagingRepository(database)
            memory = FlakyReleaseSpy()
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            kwargs = dict(
                stage_id="stage-release-retry",
                kind=ContextStageKind.ROOT,
                identity_key="request-release-retry",
                value=value,
                owner_id="worker-1",
                now=lambda: 1.0,
                lease_seconds=10.0,
                max_items=8,
                max_bytes=4096,
                timeout_seconds=0.5,
            )
            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_sdk_conversation_context(repository, memory, **kwargs)
            assert captured.value.code is ConversationMemoryErrorCode.TRANSIENT
            durable = repository.get("stage-release-retry")
            assert durable is not None and durable.state is ContextStageState.STAGED
            snapshot_hash = durable.private_snapshot_hash

            replayed = await prepare_sdk_conversation_context(repository, memory, **kwargs)
            assert replayed.private_snapshot_hash == snapshot_hash
            assert len(memory.calls) == 1
            assert len(memory.release_calls) == 2
            assert len(memory.release_effects) == 1

    asyncio.run(case())


@pytest.mark.parametrize(
    ("parameter", "invalid"),
    tuple(
        (parameter, invalid)
        for parameter in ("lease_seconds", "timeout_seconds")
        for invalid in (float("nan"), float("inf"), float("-inf"), 0.0, True)
    )
    + tuple(
        ("wait_seconds", invalid)
        for invalid in (float("nan"), float("inf"), float("-inf"), -1.0, True)
    )
    + (("max_waits", 0), ("max_waits", -1), ("max_waits", True)),
)
def test_sdk_preparation_rejects_invalid_bounds_before_claim(
    tmp_path: Path,
    parameter: str,
    invalid: object,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "invalid-sdk-bounds.db") as database:
            repository = ContextStagingRepository(database)
            memory = RecallSpy()
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            kwargs = {
                "stage_id": "invalid-sdk-bounds",
                "kind": ContextStageKind.ROOT,
                "identity_key": "invalid-sdk-request",
                "value": value,
                "owner_id": "worker-1",
                "now": lambda: 1.0,
                "lease_seconds": 10.0,
                "max_items": 8,
                "max_bytes": 4096,
                "timeout_seconds": 0.5,
                "wait_seconds": 0.01,
                "max_waits": 100,
            }
            kwargs[parameter] = invalid
            with pytest.raises(ValueError):
                await prepare_sdk_conversation_context(
                    repository,
                    memory,
                    **kwargs,  # type: ignore[arg-type]
                )
            assert repository.get("invalid-sdk-bounds") is None
            assert memory.calls == []

    asyncio.run(case())


@pytest.mark.parametrize(
    "invalid",
    (float("nan"), float("inf"), float("-inf"), 0.0, True),
)
def test_consumer_preparation_rejects_invalid_release_timeout_before_claim(
    tmp_path: Path,
    invalid: object,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "invalid-consumer-timeout.db") as database:
            repository = ContextStagingRepository(database)
            current = Message(MessageRole.USER, "hello")
            value = ConversationTurnInput("user-1", "session-1", current, "hello")

            async def preparer(query_id: str):  # type: ignore[no-untyped-def]
                raise AssertionError(query_id)

            with pytest.raises(ValueError, match="timeout_seconds"):
                await prepare_consumer_conversation_context(
                    repository,
                    preparer,
                    stage_id="invalid-consumer-timeout",
                    kind=ContextStageKind.ROOT,
                    identity_key="invalid-consumer-request",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    release_timeout_seconds=invalid,  # type: ignore[arg-type]
                )
            assert repository.get("invalid-consumer-timeout") is None

    asyncio.run(case())


@pytest.mark.parametrize("mode", ("bytes", "items"))
def test_sdk_preparation_rejects_port_results_outside_query_bounds(
    tmp_path: Path,
    mode: str,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / f"sdk-bounds-{mode}.db") as database:
            repository = ContextStagingRepository(database)
            memory = OutOfBoundsRecallSpy(mode)
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_sdk_conversation_context(
                    repository,
                    memory,
                    stage_id=f"stage-bounds-{mode}",
                    kind=ContextStageKind.ROOT,
                    identity_key=f"request-bounds-{mode}",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    max_items=8,
                    max_bytes=64 if mode == "bytes" else 4096,
                    timeout_seconds=0.5,
                )
            assert captured.value.code is ConversationMemoryErrorCode.QUERY_CONFLICT
            record = repository.get(f"stage-bounds-{mode}")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert record.private_snapshot is None
            assert len(memory.release_calls) == 1
            assert len(memory.release_effects) == 1

    asyncio.run(case())


def test_sdk_preparation_times_out_hanging_recall_without_staging(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / "sdk-recall-timeout.db") as database:
            repository = ContextStagingRepository(database)
            memory = HangingRecallSpy()
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_sdk_conversation_context(
                    repository,
                    memory,
                    stage_id="stage-recall-timeout",
                    kind=ContextStageKind.ROOT,
                    identity_key="request-recall-timeout",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    max_items=8,
                    max_bytes=4096,
                    timeout_seconds=0.01,
                )
            assert captured.value.code is ConversationMemoryErrorCode.TIMEOUT
            record = repository.get("stage-recall-timeout")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert record.private_snapshot is None
            assert len(memory.calls) == 1
            assert memory.release_calls == []

    asyncio.run(case())


@pytest.mark.parametrize("mode", ("byte_count", "item_count", "item_structure", "status"))
def test_sdk_preparation_rejects_internally_inconsistent_port_result(
    tmp_path: Path,
    mode: str,
) -> None:
    async def case() -> None:
        with Database.open(tmp_path / f"sdk-drift-{mode}.db") as database:
            repository = ContextStagingRepository(database)
            memory = DriftRecallSpy(mode)
            value = ConversationTurnInput(
                "user-1",
                "session-1",
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            with pytest.raises(ConversationMemoryError) as captured:
                await prepare_sdk_conversation_context(
                    repository,
                    memory,
                    stage_id=f"stage-drift-{mode}",
                    kind=ContextStageKind.ROOT,
                    identity_key=f"request-drift-{mode}",
                    value=value,
                    owner_id="worker-1",
                    now=lambda: 1.0,
                    lease_seconds=10.0,
                    max_items=8,
                    max_bytes=4096,
                    timeout_seconds=0.5,
                )
            assert captured.value.code is ConversationMemoryErrorCode.QUERY_CONFLICT
            record = repository.get(f"stage-drift-{mode}")
            assert record is not None
            assert record.state is ContextStageState.PREPARING
            assert record.private_snapshot is None
            assert len(memory.release_calls) == 1

    asyncio.run(case())
