# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect

from simple_harness.runtime import (
    ConversationMemoryApplyStatus,
    ConversationMemoryErrorCode,
    ConversationMemoryQueryPort,
    ConversationMemoryQueryStatus,
    ConversationMemorySinkPort,
)


def test_conversation_port_signatures_and_stable_status_values() -> None:
    assert tuple(inspect.signature(ConversationMemoryQueryPort.recall_bounded).parameters) == (
        "self",
        "query",
    )
    assert tuple(inspect.signature(ConversationMemorySinkPort.apply).parameters) == (
        "self",
        "intent",
    )
    assert inspect.iscoroutinefunction(ConversationMemoryQueryPort.close)
    assert inspect.iscoroutinefunction(ConversationMemorySinkPort.close)
    assert {value.value for value in ConversationMemoryQueryStatus} == {
        "complete",
        "truncated",
        "timeout",
    }
    assert {value.value for value in ConversationMemoryApplyStatus} == {
        "applied",
        "already_applied",
    }
    assert {value.value for value in ConversationMemoryErrorCode} == {
        "memory_query_conflict",
        "memory_apply_conflict",
        "memory_transient",
        "memory_permanent",
        "memory_timeout",
    }
