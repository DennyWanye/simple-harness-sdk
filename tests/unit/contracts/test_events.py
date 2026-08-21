# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math

import pytest

from simple_harness import (
    ContractValidationError,
    CorrelationIds,
    EventEnvelope,
    EventId,
    EventKind,
    ExecutionSessionId,
    RequestId,
    RunId,
)


def _correlation() -> CorrelationIds:
    return CorrelationIds(
        execution_session_id=ExecutionSessionId("exec-session-1"),
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
    )


def test_typed_event_is_deeply_immutable_and_serializable() -> None:
    payload = {"progress": {"completed": 1}, "refs": ["receipt-1"]}
    event = EventEnvelope(
        event_id=EventId("event-1"),
        kind=EventKind.RUN_PROGRESS,
        correlation=_correlation(),
        sequence=2,
        occurred_at=10.5,
        payload=payload,
    )
    payload["progress"]["completed"] = 9

    assert event.to_dict() == {
        "schema_version": 1,
        "event_id": "event-1",
        "kind": "run.progress",
        "correlation": {
            "execution_session_id": "exec-session-1",
            "run_id": "run-1",
            "request_id": "request-1",
        },
        "sequence": 2,
        "occurred_at": 10.5,
        "payload": {"progress": {"completed": 1}, "refs": ["receipt-1"]},
    }
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]


@pytest.mark.parametrize("sequence", [0, -1, True, 1.5])
def test_event_sequence_must_be_a_positive_integer(sequence: object) -> None:
    with pytest.raises(ContractValidationError) as error:
        EventEnvelope(
            event_id=EventId("event-1"),
            kind=EventKind.RUN_STARTED,
            correlation=_correlation(),
            sequence=sequence,  # type: ignore[arg-type]
            occurred_at=1.0,
        )
    assert error.value.code == "invalid_event"


@pytest.mark.parametrize("occurred_at", [math.nan, math.inf, -1.0])
def test_event_timestamp_must_be_finite_and_non_negative(occurred_at: float) -> None:
    with pytest.raises(ContractValidationError) as error:
        EventEnvelope(
            event_id=EventId("event-1"),
            kind=EventKind.RUN_STARTED,
            correlation=_correlation(),
            sequence=1,
            occurred_at=occurred_at,
        )
    assert error.value.code == "invalid_event"
