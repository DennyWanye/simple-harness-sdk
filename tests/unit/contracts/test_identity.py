# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simple_harness import (
    CallId,
    ContractValidationError,
    CorrelationIds,
    EffectId,
    EventId,
    ExecutionSessionId,
    RequestId,
    RunId,
)


def test_identifiers_are_distinct_immutable_value_types() -> None:
    run_id = RunId("run-1")
    request_id = RequestId("run-1")

    assert str(run_id) == "run-1"
    assert run_id.to_json() == "run-1"
    assert run_id != request_id
    assert len({run_id, RunId("run-1")}) == 1
    with pytest.raises(FrozenInstanceError):
        run_id.value = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", "   ", "line\nbreak", "x" * 256, 123])
def test_identifiers_reject_ambiguous_or_unsafe_values(value: object) -> None:
    with pytest.raises(ContractValidationError) as error:
        RunId(value)  # type: ignore[arg-type]
    assert error.value.code == "invalid_identifier"


def test_correlation_ids_are_typed_and_json_serializable() -> None:
    ids = CorrelationIds(
        execution_session_id=ExecutionSessionId("exec-session-1"),
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
        call_id=CallId("call-1"),
        effect_id=EffectId("effect-1"),
    )
    assert ids.to_dict() == {
        "execution_session_id": "exec-session-1",
        "run_id": "run-1",
        "request_id": "request-1",
        "call_id": "call-1",
        "effect_id": "effect-1",
    }


def test_effect_identity_requires_call_identity() -> None:
    with pytest.raises(ContractValidationError, match="effect_id requires call_id"):
        CorrelationIds(
            execution_session_id=ExecutionSessionId("exec-session-1"),
            run_id=RunId("run-1"),
            request_id=RequestId("request-1"),
            effect_id=EffectId("effect-1"),
        )


def test_event_id_is_not_interchangeable_with_run_id() -> None:
    assert EventId("same") != RunId("same")
