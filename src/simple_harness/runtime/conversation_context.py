# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public helpers for single-winner durable conversation context preparation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
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

from .conversation_memory import (
    ContextPreparationMode,
    ConversationMemoryRecallQuery,
    ConversationTurnInput,
)
from .ports import ConversationMemoryQueryPort


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
        return claim.record
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
        result = await memory.recall_bounded(query)
        memory_payload = thaw_json(cast(FrozenJsonValue, result.payload))
        assert isinstance(memory_payload, dict)
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
        return complete_context_stage(
            repository,
            claim.record,
            private_snapshot=private,
            memory_result_id=result.result_id,
            memory_result_hash=result.result_hash,
            now=now(),
        )
    for _ in range(max_waits):
        await asyncio.sleep(wait_seconds)
        winner = repository.get(stage_id)
        if winner is None:
            raise RuntimeError("context stage disappeared")
        if winner.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
            return winner
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
    wait_seconds: float = 0.01,
    max_waits: int = 100,
) -> ContextStageRecord:
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
        return claim.record
    if claim.owner:
        private = await preparer(context_query_id(kind, identity_key))
        return complete_context_stage(
            repository,
            claim.record,
            private_snapshot=private,
            memory_result_id=None,
            memory_result_hash=None,
            now=now(),
        )
    for _ in range(max_waits):
        await asyncio.sleep(wait_seconds)
        winner = repository.get(stage_id)
        if winner is None:
            raise RuntimeError("context stage disappeared")
        if winner.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
            return winner
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
