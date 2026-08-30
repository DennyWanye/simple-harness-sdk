# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.execution.context_authority import TaskExecutionEnvelopeRequest
from simple_harness.execution.effects import TaskExecutionEnvelope
from simple_harness.tools.runtime_catalog import (
    ToolEffectClass,
    ToolExecutionPolicy,
    ToolRouteRequirement,
    ToolTaskScopeRequirement,
)


def _envelope(**changes: object) -> TaskExecutionEnvelope:
    values: dict[str, object] = {
        "run_id": RunId("run-1"),
        "call_id": CallId("call-1"),
        "effect_id": EffectId("effect-1"),
        "raw_call_id": "raw-1",
        "turn_ordinal": 1,
        "call_ordinal": 0,
        "tool_name": "write_project",
        "capability_id": "host:write-project",
        "capability_fingerprint": "a" * 64,
        "route_receipt_id": "route-1",
        "route_receipt_hash": "b" * 64,
        "task_scope_id": "task-1",
        "root_id": "root-1",
        "root_identity_hash": "c" * 64,
        "binding_set_revision": 2,
        "binding_set_receipt_id": "binding-set-2",
        "binding_set_receipt_hash": "d" * 64,
        "idempotency_key": "effect-1",
        "schema_version": 1,
    }
    values.update(changes)
    return TaskExecutionEnvelope(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("raw_call_id", 1),
        ("tool_name", 1),
        ("capability_id", 1),
        ("capability_fingerprint", None),
        ("route_receipt_id", 1),
        ("task_scope_id", 1),
        ("root_id", 1),
        ("binding_set_revision", True),
        ("binding_set_revision", 1.5),
        ("binding_set_receipt_id", 1),
        ("binding_set_receipt_hash", "bad"),
        ("schema_version", True),
    ),
)
def test_constructor_rejects_non_exact_identity_and_revision_types(
    field: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _envelope(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", 1),
        ("task_scope_id", 1),
        ("root_id", 1),
        ("binding_set_revision", True),
        ("binding_set_revision", 2.0),
        ("binding_set_receipt_id", 1),
        ("binding_set_receipt_hash", "bad"),
        ("schema_version", True),
    ),
)
def test_decoder_rejects_non_exact_identity_and_revision_types(field: str, value: object) -> None:
    payload = copy.deepcopy(_envelope().to_json())
    payload[field] = value  # type: ignore[assignment]
    with pytest.raises((TypeError, ValueError)):
        TaskExecutionEnvelope.from_json(payload)


def test_constructor_and_decoder_reject_embedded_nul_task_root_identity() -> None:
    with pytest.raises(ValueError, match="root_id"):
        _envelope(root_id="root\x00private")
    payload = _envelope().to_json()
    payload["task_scope_id"] = "task\x00private"
    with pytest.raises(ValueError, match="task_scope_id"):
        TaskExecutionEnvelope.from_json(payload)


def test_legacy_standalone_constructor_remains_valid_but_missing_project_receipt_fails() -> None:
    standalone = TaskExecutionEnvelope(
        RunId("run-1"),
        CallId("call-1"),
        EffectId("effect-1"),
        "raw-1",
        1,
        0,
        "read_public",
        "host:read-public",
        "a" * 64,
        None,
        None,
        None,
        None,
        None,
        None,
        "effect-1",
    )
    assert standalone.binding_set_receipt_id is None
    with pytest.raises(ValueError, match="execution authority must be complete"):
        _envelope(binding_set_receipt_id=None, binding_set_receipt_hash=None)


@pytest.mark.parametrize(("field", "value"), (("turn_ordinal", True), ("call_ordinal", -1)))
def test_envelope_request_rejects_invalid_ordinals(field: str, value: object) -> None:
    values: dict[str, object] = {
        "run_id": RunId("run-1"),
        "call_id": "call-1",
        "effect_id": "effect-1",
        "raw_call_id": "raw-1",
        "turn_ordinal": 1,
        "call_ordinal": 0,
        "tool_name": "write_project",
        "policy": ToolExecutionPolicy(
            "host:write-project",
            "a" * 64,
            ToolEffectClass.PROJECT_EFFECT,
            ToolRouteRequirement.REQUIRED,
            ToolTaskScopeRequirement.REQUIRED,
        ),
        "route_receipt": None,
    }
    values[field] = value
    with pytest.raises(ValueError, match="non-negative integer"):
        TaskExecutionEnvelopeRequest(**values)  # type: ignore[arg-type]
