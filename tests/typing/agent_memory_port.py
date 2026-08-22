# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static structural-conformance fixture for third-party Memory managers."""

from simple_harness import (
    AgentMemoryPort,
    CommittedTurn,
    CommittedTurnReceipt,
    ConversationContinuationInput,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryReleaseRequest,
    Message,
    MessageRole,
)


class StructuralMemoryManager:
    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        raise NotImplementedError(request)

    async def release_recall(self, request: MemoryReleaseRequest) -> None:
        raise NotImplementedError(request)

    async def record_committed_turn(self, request: CommittedTurn) -> CommittedTurnReceipt:
        raise NotImplementedError(request)


def accepts_memory(memory: AgentMemoryPort) -> AgentMemoryPort:
    return memory


STRUCTURAL_CONFORMANCE: AgentMemoryPort = accepts_memory(StructuralMemoryManager())
CONTINUATION_WITH_EXPLICIT_REF = ConversationContinuationInput(
    Message(MessageRole.USER, "continued"),
    "continued",
    "sha256:" + "a" * 64,
)
CONTINUATION_WITH_FALLBACK = ConversationContinuationInput(
    Message(MessageRole.USER, "continued"),
    "continued",
)
