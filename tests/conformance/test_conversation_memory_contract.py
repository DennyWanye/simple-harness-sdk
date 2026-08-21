# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect

from simple_harness.runtime import (
    AgentMemoryErrorCode,
    AgentMemoryPort,
    CommittedTurnStatus,
    MemoryFailurePolicy,
    MemoryRecallStatus,
    ResourceOwnership,
)
from simple_harness.testing import (
    PROTOCOL_VERSION,
    CaseObservation,
    ConformanceHostMetadata,
    run_conformance,
)
from simple_harness.testing.runner import validate_suite_names


def test_conversation_port_signatures_and_stable_status_values() -> None:
    assert tuple(inspect.signature(AgentMemoryPort.recall_for_turn).parameters) == (
        "self",
        "request",
    )
    assert tuple(inspect.signature(AgentMemoryPort.release_recall).parameters) == (
        "self",
        "request",
    )
    assert tuple(inspect.signature(AgentMemoryPort.record_committed_turn).parameters) == (
        "self",
        "request",
    )
    assert inspect.iscoroutinefunction(AgentMemoryPort.recall_for_turn)
    assert inspect.iscoroutinefunction(AgentMemoryPort.release_recall)
    assert inspect.iscoroutinefunction(AgentMemoryPort.record_committed_turn)
    assert {value.value for value in MemoryRecallStatus} == {
        "ready",
        "empty",
        "truncated",
    }
    assert {value.value for value in CommittedTurnStatus} == {
        "applied",
        "already_applied",
        "rejected_erased",
        "conflict",
    }
    assert {value.value for value in AgentMemoryErrorCode} == {
        "memory_transient",
        "memory_timeout",
        "memory_corrupt_result",
        "memory_conflict",
        "memory_permanent",
    }
    assert ResourceOwnership.BORROWED.value == "borrowed"
    assert MemoryFailurePolicy.DEGRADE_RECALL_AND_RETRY_RECORD.value


class _ConversationSuite:
    async def conversation_contract(self) -> CaseObservation:
        return CaseObservation(
            "conversation.contract",
            {
                "dto_round_trip": True,
                "structured_preserved": True,
                "projection_text_only": True,
                "stable_statuses": True,
            },
        )

    async def conversation_schema_identity(self) -> CaseObservation:
        return CaseObservation(
            "conversation.schema_identity",
            {
                "schema_version": 4,
                "history_rows": 1,
                "fresh_only": True,
                "foreign_keys": True,
                "reopened": True,
            },
        )

    async def conversation_outbox_recovery(self) -> CaseObservation:
        return CaseObservation(
            "conversation.outbox_recovery",
            {
                "atomic": True,
                "replayed_source_event": True,
                "sink_calls": 2,
                "settled": True,
                "fake_terminal_intents": 0,
            },
        )

    async def aclose(self) -> None:
        return None


class _Context:
    async def __aenter__(self) -> _ConversationSuite:
        self.suite = _ConversationSuite()
        return self.suite

    async def __aexit__(self, *exc_info: object) -> None:
        await self.suite.aclose()


class _Host:
    metadata = ConformanceHostMetadata(
        PROTOCOL_VERSION,
        "conversation-host",
        "1.0.0",
        frozenset({"conversation"}),
    )

    def open_suite(self, name: str) -> _Context:
        assert name == "conversation"
        return _Context()


def test_public_runner_executes_conversation_capability() -> None:
    assert validate_suite_names(("conversation",)) == ("conversation",)
    report = asyncio.run(
        run_conformance(
            _Host,
            ("conversation",),
            artifact_sha256="a" * 64,
        )
    )
    assert report.passed
    assert [case.case_id for case in report.cases] == [
        "conversation.contract",
        "conversation.schema_identity",
        "conversation.outbox_recovery",
    ]
