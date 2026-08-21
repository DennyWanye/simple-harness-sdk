# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.execution import EffectRecord, EffectState, effect_request_hash
from simple_harness.tools import ToolResult


def _record(**changes: object) -> EffectRecord:
    values = {
        "effect_id": EffectId("effect-1"),
        "run_id": RunId("run-1"),
        "call_id": CallId("call-1"),
        "tool_name": "read_summary",
        "request_hash": effect_request_hash(tool_name="read_summary", arguments={"path": "."}),
        "arguments": {"path": "."},
        "state": EffectState.PREPARED,
        "version": 0,
        "fence_epoch": 1,
        "authorization_receipt_ref": "authorization:1",
    }
    values.update(changes)
    return EffectRecord(**values)  # type: ignore[arg-type]


def test_prepared_is_only_dispatchable_state() -> None:
    assert _record().dispatch_allowed is True
    handed_off = _record(
        state=EffectState.HANDED_OFF,
        version=1,
        handoff_receipt_ref="handoff:1",
    )
    assert handed_off.dispatch_allowed is False
    assert handed_off.terminal is False


def test_unknown_requires_handoff_and_evidence_and_cannot_dispatch() -> None:
    unknown = _record(
        state=EffectState.UNKNOWN,
        version=2,
        handoff_receipt_ref="handoff:1",
        evidence_ref="process-crash:1",
    )

    assert unknown.dispatch_allowed is False
    assert unknown.terminal is False
    with pytest.raises(ValueError):
        _record(
            state=EffectState.UNKNOWN,
            handoff_receipt_ref="handoff:1",
        )


def test_terminal_requires_matching_five_state_tool_result() -> None:
    result = ToolResult.succeeded(CallId("call-1"), {"summary": "ok"})
    settled = _record(
        state=EffectState.SUCCEEDED,
        version=2,
        handoff_receipt_ref="handoff:1",
        evidence_ref="handler:1",
        result=result,
    )

    assert settled.terminal is True
    with pytest.raises(ValueError):
        _record(
            state=EffectState.FAILED,
            handoff_receipt_ref="handoff:1",
            result=result,
        )


def test_request_hash_binds_tool_name_and_arguments() -> None:
    original = effect_request_hash(tool_name="read", arguments={"path": "."})

    assert original == effect_request_hash(tool_name="read", arguments={"path": "."})
    assert original != effect_request_hash(tool_name="write", arguments={"path": "."})
    assert original != effect_request_hash(tool_name="read", arguments={"path": "other"})
