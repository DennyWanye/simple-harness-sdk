# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect

from simple_harness.runtime import (
    ConversationMemoryApplyStatus,
    ConversationMemoryErrorCode,
    ConversationMemoryQueryPort,
    ConversationMemoryQueryStatus,
    ConversationMemorySinkPort,
)
from simple_harness.testing import (
    PROTOCOL_VERSION,
    CaseObservation,
    ConformanceHostMetadata,
    run_conformance,
)
from simple_harness.testing.runner import validate_suite_names


def test_conversation_port_signatures_and_stable_status_values() -> None:
    assert tuple(inspect.signature(ConversationMemoryQueryPort.recall_bounded).parameters) == (
        "self",
        "query",
    )
    assert tuple(inspect.signature(ConversationMemoryQueryPort.release).parameters) == (
        "self",
        "user_id",
        "context_query_id",
        "result_hash",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in tuple(
            inspect.signature(ConversationMemoryQueryPort.release).parameters.values()
        )[1:]
    )
    assert tuple(inspect.signature(ConversationMemorySinkPort.apply).parameters) == (
        "self",
        "intent",
    )
    assert inspect.iscoroutinefunction(ConversationMemoryQueryPort.close)
    assert inspect.iscoroutinefunction(ConversationMemoryQueryPort.release)
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
                "schema_version": 3,
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
