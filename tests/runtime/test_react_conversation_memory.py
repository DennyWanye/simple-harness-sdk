# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness import ContentBlock, Message, MessageRole
from simple_harness.execution.uow import RunState
from simple_harness.runtime import ConversationTurnOutput, DriverResult
from simple_harness.runtime.drivers.react import _assistant_memory_text


def test_assistant_projection_uses_only_explicit_text_blocks() -> None:
    message = Message(
        MessageRole.ASSISTANT,
        (
            ContentBlock("output_text", {"text": "visible"}),
            ContentBlock("image", {"body": "attachment-secret"}),
            ContentBlock("reasoning", {"text": "hidden-secret"}),
        ),
    )
    assert _assistant_memory_text(message) == "visible"
    output = ConversationTurnOutput(message, "visible")
    result = DriverResult(
        RunState.COMPLETED,
        {"response_present": True, "finish_reason": "stop"},
        conversation_output=output,
    )
    assert result.payload.get("message") is None
    assert result.conversation_output is output


def test_waiting_and_failed_results_reject_typed_output() -> None:
    output = ConversationTurnOutput(
        Message(MessageRole.ASSISTANT, "answer"), "answer"
    )
    for state in (RunState.WAITING, RunState.FAILED):
        with pytest.raises(ValueError, match="only COMPLETED"):
            DriverResult(state, {}, conversation_output=output)
