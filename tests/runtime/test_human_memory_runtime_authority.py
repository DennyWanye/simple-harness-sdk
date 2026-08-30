# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import pytest

from simple_harness.contracts import (
    CallId,
    EffectId,
    Message,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.context_authority import (
    ContextRouteReceipt,
    ContextRouteState,
    RunContextAuthorityRequest,
    RunContextSnapshot,
)
from simple_harness.execution.effects import TaskExecutionEnvelope
from simple_harness.execution.provider_invocations import provider_request_fingerprint
from simple_harness.providers import ProviderRequest, ProviderToolSpec
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute
from simple_harness.runtime.termination import TerminationState
from simple_harness.tools.runtime_catalog import (
    CatalogRunToolExposure,
    ExecutableToolRecord,
    RuntimeToolCatalog,
    ToolEffectClass,
    ToolExposureMode,
    ToolRouteRequirement,
    ToolTaskScopeRequirement,
)


def test_private_tool_policy_is_hashed_but_not_provider_visible() -> None:
    record = ExecutableToolRecord(
        capability_id="host:context_route",
        namespace="host",
        source="host",
        source_revision="v1",
        exposure_mode=ToolExposureMode.DIRECT,
        provider_name="context_route",
        description="Route context.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect_class=ToolEffectClass.CONTEXT_CONTROL,
        route_requirement=ToolRouteRequirement.FORBIDDEN,
        task_scope_requirement=ToolTaskScopeRequirement.FORBIDDEN,
    )
    catalog = RuntimeToolCatalog((record,), generation=1)
    exposure = CatalogRunToolExposure(catalog)
    run_id = RunId("run-1")
    exposure.restore(run_id, None)

    spec = exposure.provider_specs(run_id)[0]
    assert "effect_class" not in canonical_json(thaw_json(spec.parameters))
    policy = exposure.execution_policy(run_id, "context_route")
    assert policy.effect_class is ToolEffectClass.CONTEXT_CONTROL
    assert len(policy.capability_fingerprint) == 64


def test_context_snapshot_hash_matches_exact_provider_request() -> None:
    message = Message(MessageRole.USER, "continue")
    tool = ProviderToolSpec("context_route", "Route", {"type": "object"})
    request = ProviderRequest(
        request_id=RequestId("request-1"),
        messages=(message,),
        tools=(tool,),
        metadata={"reasoning": "disabled"},
    )
    expected = provider_request_fingerprint(request)
    snapshot = RunContextSnapshot(
        "snapshot-1",
        "run-1",
        1,
        0,
        1,
        {"host": 1},
        (message,),
        (tool,),
        None,
        None,
        {"reasoning": "disabled"},
        expected,
    )
    assert snapshot.payload_hash == expected
    authority = RunContextAuthorityRequest(
        RunId("run-1"), 1, 0, ContextRouteState.UNROUTED, None, "a" * 64
    )
    assert authority.provider_turn_ordinal == 1


def test_route_and_effect_authority_survive_checkpoint_roundtrip() -> None:
    route = ContextRouteReceipt(
        "route-1",
        "run-1",
        "raw-1",
        "effect-1",
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        3,
    )
    envelope = TaskExecutionEnvelope(
        RunId("run-1"),
        CallId("call-1"),
        EffectId("effect-2"),
        "raw-2",
        1,
        0,
        "write_file",
        "host:write_file",
        "b" * 64,
        route.receipt_id,
        route.receipt_hash,
        "task-1",
        "root-1",
        "c" * 64,
        3,
        "effect-2",
    )
    assert len(envelope.envelope_hash) == 64
    state = TerminationState(
        1.0,
        route_state=route.route_state.value,
        route_receipt=route.to_json(),
        route_receipt_hash=route.receipt_hash,
    )
    assert TerminationState.from_json(state.to_json()) == state


def test_context_route_receipt_strict_json_roundtrip_and_rejects_coercion() -> None:
    receipt = ContextRouteReceipt(
        "route-1",
        "run-1",
        "raw-1",
        "effect-1",
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        3,
        ("memory-1",),
    )
    assert ContextRouteReceipt.from_json(receipt.to_json()) == receipt

    for field in ("receipt_id", "run_id", "raw_call_id", "effect_id", "route"):
        for invalid in (1, True, None):
            payload = receipt.to_json()
            payload[field] = invalid
            with pytest.raises((TypeError, ValueError)):
                ContextRouteReceipt.from_json(payload)
    for field, invalid_values in {
        "task_scope_id": (1, True, None),
        "binding_set_revision": ("3", 3.0, True, None),
        "schema_version": ("1", 1.0, True, None),
        "recall_refs": (1, True, None, [1], [True], [None]),
    }.items():
        for invalid in invalid_values:
            payload = receipt.to_json()
            payload[field] = invalid
            with pytest.raises((TypeError, ValueError)):
                ContextRouteReceipt.from_json(payload)

    for mutation in ({"extra": "field"}, {"receipt_id": None}):
        payload = receipt.to_json()
        if "extra" in mutation:
            payload.update(mutation)
        else:
            payload.pop("receipt_id")
        with pytest.raises(ValueError, match="fields differ"):
            ContextRouteReceipt.from_json(payload)
