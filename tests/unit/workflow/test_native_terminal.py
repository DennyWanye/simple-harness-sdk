# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy

import pytest

from simple_harness.contracts import canonical_json
from simple_harness.workflow.errors import InvalidStatePatch
from simple_harness.workflow.native import NativeWorkflowExecutable


def _public() -> dict[str, object]:
    return {
        "metrics": {"actual_requests": 2, "hits": 1},
        "diagnostic_codes": ["timeout", "no_results"],
        "skipped_stage_ids": ["fetch", "persist"],
        "retry_action_id": "retry_from_start",
    }


def _project(public: object):
    return NativeWorkflowExecutable.terminal_intents(
        {"values": {"terminal_public": public, "private": "must not leak"}},
        run_id="run-1",
        status="failed",
        error={"code": "failed", "message": "failed safely"},
        recovery_action="retry_from_start",
    )[0]


def test_public_projection_is_exact_and_detached_from_input() -> None:
    public = _public()
    intent = _project(public)
    public["metrics"]["hits"] = 999  # type: ignore[index]

    assert intent.event_type == "workflow.final"
    assert set(intent.payload) == {
        "kind",
        "status",
        "error",
        "recovery_action",
        "card",
        "metrics",
        "diagnostic_codes",
        "skipped_stage_ids",
        "retry_action_id",
    }
    assert intent.payload["metrics"] == {"actual_requests": 2, "hits": 1}
    assert intent.payload["card"]["metrics"] == {  # type: ignore[index]
        "actual_requests": 2,
        "hits": 1,
    }
    assert "private" not in canonical_json(intent.payload)


def test_frozen_diagnostic_allowlist_accepts_boundary_codes() -> None:
    public = _public()
    public["diagnostic_codes"] = [
        "half_open_busy",
        "blob_unavailable",
        "body_bytes_below_threshold",
    ]
    assert _project(public).payload["diagnostic_codes"] == public["diagnostic_codes"]


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update({"private": "leak"}),
        lambda value: value["metrics"].update({"unknown": 1}),
        lambda value: value["metrics"].update({"hits": True}),
        lambda value: value["metrics"].update({"hits": -1}),
        lambda value: value["metrics"].update({"hits": 1_000_001}),
        lambda value: value.update({"diagnostic_codes": ["secret"]}),
        lambda value: value.update({"diagnostic_codes": ["timeout"] * 17}),
        lambda value: value.update({"skipped_stage_ids": ["fetch", "fetch"]}),
        lambda value: value.update({"skipped_stage_ids": ["private"]}),
        lambda value: value.update({"retry_action_id": "retry_with_private"}),
    ),
)
def test_public_projection_rejects_unbounded_or_unknown_values(mutation) -> None:
    public = deepcopy(_public())
    mutation(public)
    with pytest.raises(InvalidStatePatch, match="terminal"):
        _project(public)


@pytest.mark.parametrize("public", (None, [], {"metrics": {}}))
def test_public_projection_rejects_incomplete_schema(public) -> None:
    if public is None:
        public = {"metrics": {}, "diagnostic_codes": []}
    with pytest.raises(InvalidStatePatch, match="terminal"):
        _project(public)


def test_legacy_projection_has_exact_canonical_shape() -> None:
    intent = NativeWorkflowExecutable.terminal_intents(
        {"values": {"private": "ignored"}},
        run_id="legacy-run",
        status="failed",
        error={"code": "legacy", "message": "legacy"},
        recovery_action="retry",
    )[0]
    assert canonical_json(intent.payload).encode() == (
        b'{"card":null,"error":{"code":"legacy","message":"legacy"},'
        b'"kind":"workflow_terminal","recovery_action":"retry","status":"failed"}'
    )


def test_no_terminal_status_emits_no_intent() -> None:
    assert NativeWorkflowExecutable.terminal_intents(
        {}, run_id="run-1", status=None, error=None, recovery_action=None
    ) == ()


@pytest.mark.parametrize("status", ("", "running", "waiting"))
def test_nonterminal_status_is_rejected(status: str) -> None:
    with pytest.raises(InvalidStatePatch, match="terminal"):
        NativeWorkflowExecutable.terminal_intents(
            {}, run_id="run-1", status=status, error=None, recovery_action=None
        )


def test_invalid_state_patch_exposes_stable_code_and_message() -> None:
    error = InvalidStatePatch("invalid_terminal_public", "terminal value is invalid")
    assert error.code == "invalid_terminal_public"
    assert str(error) == "terminal value is invalid"
    assert str(error) == "terminal value is invalid"
