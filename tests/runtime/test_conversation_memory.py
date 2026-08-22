# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from simple_harness import AgentIdentity, ContentBlock, Message, MessageRole, canonical_json
from simple_harness.runtime import (
    ConversationContinuationInput,
    ConversationTurnInput,
    ConversationTurnOutput,
)
from simple_harness.runtime.conversation_memory import (
    ConversationMemoryIntent,
    ConversationMemoryQueryStatus,
    ConversationMemoryRecallQuery,
    ConversationMemoryRecallResult,
    ConversationMemoryRole,
)


def test_conversation_dto_canonical_golden_round_trip() -> None:
    value = ConversationTurnInput(
        AgentIdentity("deployment-1", "household-1", "user-1", "session-1"),
        Message(MessageRole.USER, "Cafe\u0301\r\nnext"),
        "Cafe\u0301\r\nnext",
    )
    assert value.memory_text == "Caf\u00e9\nnext"
    assert ConversationTurnInput.from_json(value.to_json()) == value
    output = ConversationTurnOutput(Message(MessageRole.ASSISTANT, "answer"), "answer")
    assert ConversationTurnOutput.from_json(output.to_json()) == output
    continuation = ConversationContinuationInput(
        Message(MessageRole.USER, "continued"),
        "continued",
        "snapshot://continuation-1",
    )
    assert ConversationContinuationInput.from_json(continuation.to_json()) == continuation
    assert hashlib.sha256(canonical_json(continuation.to_json()).encode()).hexdigest() == (
        "b8b54707b6eba790d8cf204c7f300be59ab83f1e66dba3e808c47af807292092"
    )
    fallback = ConversationContinuationInput(
        Message(MessageRole.USER, "continued"),
        "continued",
    )
    assert fallback.context_source_snapshot_ref is None
    assert ConversationContinuationInput.from_json(fallback.to_json()) == fallback
    assert canonical_json(continuation.to_json()) != canonical_json(fallback.to_json())
    with pytest.raises(ValueError, match="NUL"):
        ConversationContinuationInput(
            Message(MessageRole.USER, "continued"),
            "continued",
            "snapshot://bad\x00ref",
        )
    with pytest.raises(TypeError, match="string or null"):
        ConversationContinuationInput.from_json(
            {
                "message": Message(MessageRole.USER, "continued").to_dict(),
                "memory_text": "continued",
                "context_source_snapshot_ref": 1,
            }
        )


def test_structured_attachment_is_preserved_but_not_projected() -> None:
    attachment = ContentBlock("image", {"media_type": "image/png", "body": "secret"})
    value = ConversationContinuationInput(Message(MessageRole.USER, (attachment,)), None)
    assert value.message.content == (attachment,)
    assert value.memory_text is None
    with pytest.raises(ValueError, match="text content blocks"):
        ConversationContinuationInput(
            Message(MessageRole.USER, (ContentBlock("text", {"text": "hello"}),)),
            None,
        )
    with pytest.raises(ValueError, match="non-empty"):
        ConversationContinuationInput(Message(MessageRole.USER, "hello"), "")


def test_recall_query_and_result_hashes_are_canonical() -> None:
    query = ConversationMemoryRecallQuery.create(
        context_query_id="query-1",
        user_id="user-1",
        session_id="session-1",
        query_text="hello",
        max_items=8,
        max_bytes=4096,
        timeout_seconds=0.5,
    )
    payload = {"items": [{"text": "remembered"}]}
    encoded = canonical_json(payload).encode()
    result = ConversationMemoryRecallResult(
        query.context_query_id,
        "result-1",
        query.query_hash,
        payload,
        hashlib.sha256(encoded).hexdigest(),
        ConversationMemoryQueryStatus.COMPLETE,
        1,
        len(encoded),
    )
    assert result.query_hash == query.query_hash
    with pytest.raises(ValueError, match="byte_count"):
        ConversationMemoryRecallResult(
            query.context_query_id,
            "result-1",
            query.query_hash,
            payload,
            hashlib.sha256(encoded).hexdigest(),
            ConversationMemoryQueryStatus.COMPLETE,
            1,
            len(encoded) + 1,
        )


@pytest.mark.parametrize(
    "timeout_seconds",
    (float("nan"), float("inf"), float("-inf"), 0.0, -1.0, True),
)
def test_recall_query_rejects_nonfinite_or_nonpositive_timeout(
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ConversationMemoryRecallQuery.create(
            context_query_id="query-invalid-timeout",
            user_id="user-1",
            session_id="session-1",
            query_text="hello",
            max_items=8,
            max_bytes=4096,
            timeout_seconds=timeout_seconds,
        )


def test_intent_has_no_consumer_supplied_event_id_or_payload_escape_hatch() -> None:
    intent = ConversationMemoryIntent(
        "harness-memory/v1/user/run-1",
        "user-1",
        "session-1",
        ConversationMemoryRole.USER,
        "hello",
    )
    assert set(intent.to_json()) == {
        "source_event_id",
        "user_id",
        "session_id",
        "role",
        "memory_text",
        "payload_hash",
    }
    assert len(intent.payload_hash) == 64
