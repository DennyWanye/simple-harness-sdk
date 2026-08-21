# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from simple_harness import Message, MessageRole, canonical_json, thaw_json
from simple_harness.execution.context_staging import (
    ContextStageKind,
    ContextStageState,
    ContextStagingRepository,
)
from simple_harness.execution.sqlite import Database
from simple_harness.runtime import (
    ConversationMemoryQueryStatus,
    ConversationMemoryRecallResult,
    ConversationTurnInput,
    context_query_id,
    prepare_sdk_conversation_context,
)


class RecallSpy:
    def __init__(self) -> None:
        self.calls = []

    async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
        self.calls.append(query)
        payload = {"items": [{"text": "memory is untrusted"}]}
        encoded = canonical_json(payload).encode()
        return ConversationMemoryRecallResult(
            query.context_query_id,
            "result-1",
            query.query_hash,
            payload,
            hashlib.sha256(encoded).hexdigest(),
            ConversationMemoryQueryStatus.COMPLETE,
            1,
            len(encoded),
        )

    async def close(self) -> None:
        return None


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

    asyncio.run(case())
