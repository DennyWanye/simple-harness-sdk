# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public helpers for single-winner durable conversation context preparation."""

from __future__ import annotations

import asyncio
import hashlib
import math
import warnings
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from simple_harness.contracts import (
    CallId,
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.contracts.messages import ContentBlock, Message, MessageRole
from simple_harness.execution.context_staging import (
    ContextStageClaim,
    ContextStageKind,
    ContextStageRecord,
    ContextStageState,
    ContextStagingRepository,
)

from .conversation_context_provider import source_snapshot_ref
from .conversation_memory import (
    ContextPreparationMode,
    ConversationMemoryError,
    ConversationMemoryErrorCode,
    ConversationMemoryQueryStatus,
    ConversationMemoryRecallQuery,
    ConversationMemoryRecallResult,
    ConversationTurnInput,
)
from .ports import ConversationMemoryQueryPort


def _preparation_bounds(
    *,
    lease_seconds: float,
    wait_seconds: float,
    max_waits: int,
    timeout_seconds: float,
) -> None:
    for value, name in (
        (lease_seconds, "lease_seconds"),
        (timeout_seconds, "timeout_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or not math.isfinite(float(wait_seconds))
        or wait_seconds < 0
    ):
        raise ValueError("wait_seconds must be finite and non-negative")
    if isinstance(max_waits, bool) or not isinstance(max_waits, int) or max_waits < 1:
        raise ValueError("max_waits must be a positive integer")


def _private_message(value: object) -> Message:
    if not isinstance(value, Mapping):
        raise TypeError("consumer prepared messages must be objects")
    role = value.get("role")
    content = value.get("content")
    metadata = value.get("metadata", {})
    name = value.get("name")
    call_id = value.get("call_id")
    if not isinstance(role, str):
        raise TypeError("consumer prepared message role must be a string")
    if isinstance(content, str):
        normalized_content: str | tuple[ContentBlock, ...] = content
    elif isinstance(content, list):
        if not all(isinstance(item, Mapping) for item in content):
            raise TypeError("consumer prepared content blocks must be objects")
        normalized_content = tuple(ContentBlock.from_dict(item) for item in content)
    else:
        raise TypeError("consumer prepared message content is invalid")
    if not isinstance(metadata, Mapping):
        raise TypeError("consumer prepared message metadata must be an object")
    if name is not None and not isinstance(name, str):
        raise TypeError("consumer prepared message name must be a string")
    if call_id is not None and not isinstance(call_id, str):
        raise TypeError("consumer prepared message call_id must be a string")
    return Message(
        MessageRole(role),
        normalized_content,
        name=name,
        call_id=None if call_id is None else CallId(call_id),
        metadata=dict(metadata),
    )


def _consumer_private_snapshot(
    value: object,
    *,
    expected_context_query_id: str,
    current_message: Message,
) -> tuple[dict[str, JsonValue], str | None, str | None]:
    if not isinstance(value, Mapping):
        raise TypeError("consumer prepared context must be an object")
    frozen = freeze_json(dict(value))
    private = thaw_json(cast(FrozenJsonValue, frozen))
    assert isinstance(private, dict)
    if private.get("schema_version") != 1:
        raise ValueError("consumer prepared context schema_version must be 1")
    lineage = private.get("lineage")
    if not isinstance(lineage, dict):
        raise TypeError("consumer prepared context lineage must be an object")
    if lineage.get("context_query_id") != expected_context_query_id:
        raise ValueError("consumer prepared context query identity differs")
    result_id = lineage.get("memory_result_id")
    result_hash = lineage.get("memory_result_hash")
    if (result_id is None) != (result_hash is None):
        raise ValueError("consumer Memory result id/hash must be present together")
    if result_id is not None and (not isinstance(result_id, str) or not result_id.strip()):
        raise ValueError("consumer Memory result id must be non-empty")
    if result_hash is not None and (
        not isinstance(result_hash, str)
        or len(result_hash) != 64
        or any(character not in "0123456789abcdef" for character in result_hash)
    ):
        raise ValueError("consumer Memory result hash must be lowercase SHA-256")
    parsed_current = _private_message(private.get("current_message"))
    if canonical_json(parsed_current.to_dict()) != canonical_json(current_message.to_dict()):
        raise ValueError("consumer prepared current message differs")
    provider_values = private.get("provider_messages")
    if not isinstance(provider_values, list) or not provider_values:
        raise TypeError("consumer prepared provider_messages must be a non-empty list")
    provider_messages = tuple(_private_message(item) for item in provider_values)
    if canonical_json(provider_messages[-1].to_dict()) != canonical_json(current_message.to_dict()):
        raise ValueError("consumer prepared provider messages omit current message")
    memory_messages: list[Message] = []
    for message in provider_messages:
        metadata = thaw_json(cast(FrozenJsonValue, message.metadata))
        assert isinstance(metadata, dict)
        if metadata.get("source") != "memory":
            continue
        memory_messages.append(message)
        if message.role is not MessageRole.USER or metadata.get("trust") != "untrusted_data":
            raise ValueError("consumer prepared Memory messages must remain USER/untrusted data")
    memory = private.get("memory")
    if memory is None:
        if memory_messages:
            raise ValueError("consumer prepared Memory partition is missing")
        if result_id is not None:
            raise ValueError("consumer prepared Memory lineage lacks a partition")
    else:
        if not isinstance(memory, dict):
            raise TypeError("consumer prepared Memory partition must be an object")
        if memory.get("role") != "user" or memory.get("trust") != "untrusted_data":
            raise ValueError("consumer prepared Memory partition must remain USER/untrusted data")
        if "result" not in memory:
            raise ValueError("consumer prepared Memory result is missing")
        if not memory_messages:
            raise ValueError("consumer prepared Memory provider message is missing")
        if result_id is None:
            raise ValueError("consumer prepared Memory partition lacks result lineage")
    return private, cast(str | None, result_id), cast(str | None, result_hash)


def _consumer_release_candidate(value: object, *, expected_context_query_id: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    lineage = value.get("lineage")
    if not isinstance(lineage, Mapping):
        return None
    if lineage.get("context_query_id") != expected_context_query_id:
        return None
    result_id = lineage.get("memory_result_id")
    result_hash = lineage.get("memory_result_hash")
    if not isinstance(result_id, str) or not result_id.strip():
        return None
    if (
        not isinstance(result_hash, str)
        or len(result_hash) != 64
        or any(character not in "0123456789abcdef" for character in result_hash)
    ):
        return None
    return result_hash


async def _release_recall_result(
    memory: ConversationMemoryQueryPort,
    *,
    user_id: str,
    query_id: str,
    result_hash: str,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(
            memory.release(
                user_id=user_id,
                context_query_id=query_id,
                result_hash=result_hash,
            ),
            timeout=timeout_seconds,
        )
    except ConversationMemoryError:
        raise
    except TimeoutError:
        raise ConversationMemoryError(ConversationMemoryErrorCode.TIMEOUT) from None
    except Exception:
        raise ConversationMemoryError(ConversationMemoryErrorCode.TRANSIENT) from None


def _validated_recall_payload(
    result: ConversationMemoryRecallResult,
    query: ConversationMemoryRecallQuery,
) -> dict[str, JsonValue]:
    if result.context_query_id != query.context_query_id or result.query_hash != query.query_hash:
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    if not isinstance(result.result_id, str) or not result.result_id.strip():
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    if (
        not isinstance(result.item_count, int)
        or isinstance(result.item_count, bool)
        or result.item_count < 0
        or result.item_count > query.max_items
    ):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    if (
        not isinstance(result.byte_count, int)
        or isinstance(result.byte_count, bool)
        or result.byte_count < 0
        or result.byte_count > query.max_bytes
    ):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    try:
        ConversationMemoryQueryStatus(result.status)
    except (TypeError, ValueError):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT) from None
    if not isinstance(result.payload, Mapping):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    memory_payload = thaw_json(cast(FrozenJsonValue, result.payload))
    assert isinstance(memory_payload, dict)
    canonical_payload = canonical_json(memory_payload).encode("utf-8")
    if (
        len(canonical_payload) != result.byte_count
        or hashlib.sha256(canonical_payload).hexdigest() != result.result_hash
    ):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    items = memory_payload.get("items")
    if (
        not isinstance(items, list)
        or not all(isinstance(item, dict) for item in items)
        or len(items) != result.item_count
    ):
        raise ConversationMemoryError(ConversationMemoryErrorCode.QUERY_CONFLICT)
    return memory_payload


async def _release_staged_result(
    memory: ConversationMemoryQueryPort,
    record: ContextStageRecord,
    *,
    timeout_seconds: float,
) -> ContextStageRecord:
    if record.memory_result_id is None:
        return record
    assert record.memory_result_hash is not None
    await _release_recall_result(
        memory,
        user_id=record.user_id,
        query_id=context_query_id(record.kind, record.identity_key),
        result_hash=record.memory_result_hash,
        timeout_seconds=timeout_seconds,
    )
    return record


async def _release_consumer_staged_result(
    memory: ConversationMemoryQueryPort | None,
    record: ContextStageRecord,
    *,
    timeout_seconds: float,
) -> ContextStageRecord:
    if record.memory_result_id is None:
        return record
    if memory is None:
        raise TypeError("consumer prepared Memory lineage requires a conversation query port")
    return await _release_staged_result(memory, record, timeout_seconds=timeout_seconds)


def context_input_hash(value: ConversationTurnInput) -> str:
    return hashlib.sha256(canonical_json(value.to_json()).encode("utf-8")).hexdigest()


def context_query_id(kind: ContextStageKind, identity_key: str) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "protocol": "harness-memory-context-query-id-v1",
                "kind": ContextStageKind(kind).value,
                "identity": identity_key,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"harness-memory/v1/context/{digest}"


def claim_context_preparation(
    repository: ContextStagingRepository,
    *,
    stage_id: str,
    kind: ContextStageKind,
    identity_key: str,
    value: ConversationTurnInput,
    mode: ContextPreparationMode,
    owner_id: str,
    now: float,
    lease_seconds: float,
) -> ContextStageClaim:
    ref = value.context_source_snapshot_ref or source_snapshot_ref(
        {"current_message": value.message.to_dict()}
    )
    return repository.claim(
        stage_id=stage_id,
        kind=kind,
        identity_key=identity_key,
        user_id=value.user_id,
        session_id=value.session_id,
        input_hash=context_input_hash(value),
        mode=ContextPreparationMode(mode).value,
        owner_id=owner_id,
        now=now,
        lease_seconds=lease_seconds,
        source_snapshot_ref=ref,
    )


def complete_context_stage(
    repository: ContextStagingRepository,
    claim: ContextStageRecord,
    *,
    private_snapshot: Mapping[str, JsonValue],
    memory_result_id: str | None,
    memory_result_hash: str | None,
    now: float,
) -> ContextStageRecord:
    return repository.complete(
        claim,
        private_snapshot=private_snapshot,
        memory_result_id=memory_result_id,
        memory_result_hash=memory_result_hash,
        now=now,
    )


def get_staged_context(
    repository: ContextStagingRepository, stage_id: str
) -> ContextStageRecord | None:
    return repository.get(stage_id)


async def prepare_sdk_conversation_context(
    repository: ContextStagingRepository,
    memory: ConversationMemoryQueryPort,
    *,
    stage_id: str,
    kind: ContextStageKind,
    identity_key: str,
    value: ConversationTurnInput,
    owner_id: str,
    now: Callable[[], float],
    lease_seconds: float,
    max_items: int,
    max_bytes: int,
    timeout_seconds: float,
    wait_seconds: float = 0.01,
    max_waits: int = 100,
) -> ContextStageRecord:
    _preparation_bounds(
        lease_seconds=lease_seconds,
        wait_seconds=wait_seconds,
        max_waits=max_waits,
        timeout_seconds=timeout_seconds,
    )
    claim = claim_context_preparation(
        repository,
        stage_id=stage_id,
        kind=kind,
        identity_key=identity_key,
        value=value,
        mode=ContextPreparationMode.SDK_PREPARED,
        owner_id=owner_id,
        now=now(),
        lease_seconds=lease_seconds,
    )
    if claim.record.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
        return await _release_staged_result(memory, claim.record, timeout_seconds=timeout_seconds)
    if claim.owner:
        query = ConversationMemoryRecallQuery.create(
            context_query_id=context_query_id(kind, identity_key),
            user_id=value.user_id,
            session_id=value.session_id,
            query_text=value.memory_text or "[non-text conversation turn]",
            max_items=max_items,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        result = None
        try:
            try:
                result = await asyncio.wait_for(
                    memory.recall_bounded(query), timeout=query.timeout_seconds
                )
            except TimeoutError:
                raise ConversationMemoryError(ConversationMemoryErrorCode.TIMEOUT) from None
            memory_payload = _validated_recall_payload(result, query)
            private: dict[str, JsonValue] = {
                "schema_version": 1,
                "lineage": {
                    "context_query_id": result.context_query_id,
                    "memory_result_id": result.result_id,
                    "memory_result_hash": result.result_hash,
                },
                "memory": {
                    "trust": "untrusted_data",
                    "role": "user",
                    "result": memory_payload,
                },
                "current_message": value.message.to_dict(),
                "provider_messages": [
                    Message(
                        MessageRole.USER,
                        (
                            ContentBlock(
                                "text",
                                {
                                    "text": "Untrusted recalled memory data:\n"
                                    + canonical_json(memory_payload)
                                },
                            ),
                        ),
                        metadata={"trust": "untrusted_data", "source": "memory"},
                    ).to_dict(),
                    value.message.to_dict(),
                ],
            }
            staged = complete_context_stage(
                repository,
                claim.record,
                private_snapshot=private,
                memory_result_id=result.result_id,
                memory_result_hash=result.result_hash,
                now=now(),
            )
        except BaseException:
            if result is not None:
                try:
                    await _release_recall_result(
                        memory,
                        user_id=value.user_id,
                        query_id=query.context_query_id,
                        result_hash=result.result_hash,
                        timeout_seconds=timeout_seconds,
                    )
                except BaseException:
                    pass
            raise
        return await _release_staged_result(memory, staged, timeout_seconds=timeout_seconds)
    for _ in range(max_waits):
        await asyncio.sleep(wait_seconds)
        winner = repository.get(stage_id)
        if winner is None:
            raise RuntimeError("context stage disappeared")
        if winner.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
            return await _release_staged_result(memory, winner, timeout_seconds=timeout_seconds)
        if winner.state is ContextStageState.ABANDONED:
            raise RuntimeError("context stage was abandoned")
    raise TimeoutError("context preparation winner did not finish")


async def prepare_consumer_conversation_context(
    repository: ContextStagingRepository,
    preparer: Callable[[str], Awaitable[Mapping[str, JsonValue]]],
    *,
    stage_id: str,
    kind: ContextStageKind,
    identity_key: str,
    value: ConversationTurnInput,
    owner_id: str,
    now: Callable[[], float],
    lease_seconds: float,
    memory: ConversationMemoryQueryPort | None = None,
    release_timeout_seconds: float = 1.0,
    wait_seconds: float = 0.01,
    max_waits: int = 100,
) -> ContextStageRecord:
    warnings.warn(
        "prepare_consumer_conversation_context is an internal compatibility helper; "
        "compose a ConversationContextProviderPort and use RunClient.start_conversation",
        DeprecationWarning,
        stacklevel=2,
    )
    _preparation_bounds(
        lease_seconds=lease_seconds,
        wait_seconds=wait_seconds,
        max_waits=max_waits,
        timeout_seconds=release_timeout_seconds,
    )
    claim = claim_context_preparation(
        repository,
        stage_id=stage_id,
        kind=kind,
        identity_key=identity_key,
        value=value,
        mode=ContextPreparationMode.CONSUMER_PREPARED,
        owner_id=owner_id,
        now=now(),
        lease_seconds=lease_seconds,
    )
    if claim.record.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
        return await _release_consumer_staged_result(
            memory,
            claim.record,
            timeout_seconds=release_timeout_seconds,
        )
    if claim.owner:
        expected_query_id = context_query_id(kind, identity_key)
        prepared = await preparer(expected_query_id)
        release_candidate = _consumer_release_candidate(
            prepared, expected_context_query_id=expected_query_id
        )
        try:
            private, memory_result_id, memory_result_hash = _consumer_private_snapshot(
                prepared,
                expected_context_query_id=expected_query_id,
                current_message=value.message,
            )
        except BaseException:
            if memory is not None and release_candidate is not None:
                try:
                    await _release_recall_result(
                        memory,
                        user_id=value.user_id,
                        query_id=expected_query_id,
                        result_hash=release_candidate,
                        timeout_seconds=release_timeout_seconds,
                    )
                except BaseException:
                    pass
            raise
        if memory_result_id is not None and memory is None:
            raise TypeError("consumer prepared Memory lineage requires a conversation query port")
        try:
            staged = complete_context_stage(
                repository,
                claim.record,
                private_snapshot=private,
                memory_result_id=memory_result_id,
                memory_result_hash=memory_result_hash,
                now=now(),
            )
        except BaseException:
            if memory is not None and memory_result_hash is not None:
                try:
                    await _release_recall_result(
                        memory,
                        user_id=value.user_id,
                        query_id=expected_query_id,
                        result_hash=memory_result_hash,
                        timeout_seconds=release_timeout_seconds,
                    )
                except BaseException:
                    pass
            raise
        return await _release_consumer_staged_result(
            memory, staged, timeout_seconds=release_timeout_seconds
        )
    for _ in range(max_waits):
        await asyncio.sleep(wait_seconds)
        winner = repository.get(stage_id)
        if winner is None:
            raise RuntimeError("context stage disappeared")
        if winner.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
            return await _release_consumer_staged_result(
                memory, winner, timeout_seconds=release_timeout_seconds
            )
        if winner.state is ContextStageState.ABANDONED:
            raise RuntimeError("context stage was abandoned")
    raise TimeoutError("context preparation winner did not finish")


__all__ = (
    "claim_context_preparation",
    "complete_context_stage",
    "context_input_hash",
    "context_query_id",
    "get_staged_context",
    "prepare_consumer_conversation_context",
    "prepare_sdk_conversation_context",
)
