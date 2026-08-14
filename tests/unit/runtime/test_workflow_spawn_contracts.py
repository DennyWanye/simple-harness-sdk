# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen T4.2 workflow-spawn authority contracts."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from simple_harness.contracts import canonical_json
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.runtime.kernel import DriverInvocation, RunClient
from simple_harness.runtime.orchestration import (
    StartInputSchema,
    VerifiedWorkflowGraphUnavailable,
    WorkflowCatalogSelectionProfile,
    WorkflowCatalogSelectionSnapshot,
    WorkflowLaunchRequest,
    WorkflowLaunchTicketPort,
    WorkflowSpawnOrigin,
    WorkflowSpawnReadyActivationState,
    WorkflowSpawnSelection,
    _create_verified_workflow_graph_unavailable,
    workflow_catalog_selection_from_json,
    workflow_catalog_selection_hash,
    workflow_catalog_selection_to_json,
    workflow_spawn_child_command_id,
    workflow_spawn_child_request_id,
    workflow_spawn_child_run_id,
    workflow_spawn_operation_id,
)
from simple_harness.runtime.workflow_spawn import (
    WorkflowSpawnBatchAction,
    WorkflowSpawnChildControlKind,
    WorkflowSpawnHandlerOutcome,
)
from simple_harness.tools.contracts import ToolContext


def _catalog_selection_snapshot() -> WorkflowCatalogSelectionSnapshot:
    schema_json = {
        "type": "object",
        "properties": {"objective": {"type": "string"}},
        "required": ["objective"],
        "additionalProperties": False,
    }
    schema = StartInputSchema(
        "schema://workflow.task/v1",
        schema_json,
        hashlib.sha256(canonical_json(schema_json).encode()).hexdigest(),
    )
    profile = WorkflowCatalogSelectionProfile(
        "workflow.task",
        "Durable task",
        "Use for durable work",
        "Avoid for direct answers",
        "profile-fingerprint",
        schema,
    )
    profiles = (profile,)
    return WorkflowCatalogSelectionSnapshot(
        "model_spawnable",
        1,
        1,
        "catalog-hash",
        profiles,
        workflow_catalog_selection_hash(
            "model_spawnable", 1, 1, "catalog-hash", profiles
        ),
    )


def test_spawn_identity_is_payload_independent_and_child_is_attached() -> None:
    origin = WorkflowSpawnOrigin(
        parent_run_id="parent-run",
        parent_request_id="parent-request",
        turn_id="turn-7",
        internal_tool_call_id="call-3",
    )
    first = WorkflowSpawnSelection(
        profile_key="workflow.durable_task",
        objective="Build the SDK",
        start_input={"messages": [{"role": "user", "content": "one"}]},
    )
    second = WorkflowSpawnSelection(
        profile_key="workflow.personal_v1",
        objective="Remember this",
        start_input={"messages": [{"role": "user", "content": "two"}]},
        candidate_id="candidate-2",
    )

    operation_id = workflow_spawn_operation_id(origin)
    assert operation_id == workflow_spawn_operation_id(origin)
    assert first != second
    assert workflow_spawn_child_command_id(operation_id) == workflow_spawn_child_command_id(
        operation_id
    )
    assert workflow_spawn_child_request_id(operation_id) == workflow_spawn_child_request_id(
        operation_id
    )
    assert workflow_spawn_child_run_id(operation_id) == workflow_spawn_child_run_id(
        operation_id
    )
    assert workflow_spawn_child_request_id(operation_id) != workflow_spawn_child_run_id(
        operation_id
    )
    assert AttachmentPolicy.ATTACHED.value == "attached"

    with pytest.raises(FrozenInstanceError):
        origin.turn_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.start_input["messages"] = []  # type: ignore[index]


def test_ready_activation_state_is_closed() -> None:
    assert tuple(WorkflowSpawnReadyActivationState) == (
        WorkflowSpawnReadyActivationState.ACTIVE,
        WorkflowSpawnReadyActivationState.SUPERSEDED,
        WorkflowSpawnReadyActivationState.CONSUMED,
    )
    with pytest.raises(ValueError):
        WorkflowSpawnReadyActivationState("pending")


def test_launch_request_pins_attached_parent_and_child_command() -> None:
    origin = WorkflowSpawnOrigin("parent", "parent-request", "turn", "call")
    operation_id = workflow_spawn_operation_id(origin)
    request = WorkflowLaunchRequest(
        request_key=operation_id,
        candidate_id=None,
        profile_key="workflow.durable_task",
        catalog_generation=3,
        session_id="session",
        request_id="child-request",
        turn_id="turn",
        requested_run_id="child",
        requested_trace_id=None,
        requested_thread_id=None,
        tool_catalog_generation=5,
        objective="Do durable work",
        start_input={"messages": []},
        spawn_origin=origin,
        root_run_id="root",
        attachment_policy=AttachmentPolicy.ATTACHED,
        child_command_id=workflow_spawn_child_command_id(operation_id),
    )
    assert request.spawn_origin is origin
    assert request.root_run_id == "root"
    assert request.attachment_policy is AttachmentPolicy.ATTACHED

    with pytest.raises(ValueError, match="attached"):
        replace(request, attachment_policy=AttachmentPolicy.DETACHED)


def test_launch_port_exposes_only_canonical_spawn_commands() -> None:
    required = {
        "read_issued",
        "read_admitted",
        "claim_spawn_continuation",
        "mark_spawn_continuation_ready",
        "list_ready_spawn_continuations",
        "read_spawn_ready_blocker",
        "consume_spawn_ready_and_claim_activation",
        "read_spawn_ready_activation",
        "reclaim_spawn_ready_activation",
        "read_spawn_continuation_outcome",
        "read_spawn_admission_outcome",
        "continue_spawn_admission",
        "settle_spawn_continuation_catalog_stale",
        "settle_spawn_continuation_graph_unavailable",
        "settle_spawn_continuation_for_parent_terminal",
        "resume_admitted_runtime_start",
    }
    assert required <= set(WorkflowLaunchTicketPort.__dict__)


def test_driver_invocation_reserves_typed_spawn_ready_carrier() -> None:
    assert "workflow_spawn_ready_activation" in DriverInvocation.__dataclass_fields__


def test_public_spawn_surface_and_typed_tool_context_are_reserved() -> None:
    assert {
        "workflow_spawn_catalog",
        "bind_workflow_spawn",
        "workflow_spawn",
        "prove_graph_unavailable",
    } <= set(RunClient.__dict__)
    assert "workflow_spawn_context" in ToolContext.__dataclass_fields__


def test_catalog_selection_snapshot_round_trips_canonical_bytes() -> None:
    authority = _catalog_selection_snapshot()
    encoded = workflow_catalog_selection_to_json(authority)
    assert workflow_catalog_selection_from_json(encoded) == authority


def test_handler_and_child_control_algebras_are_closed() -> None:
    assert tuple(WorkflowSpawnChildControlKind) == tuple(
        WorkflowSpawnChildControlKind(kind)
        for kind in ("start", "recover", "attach", "waiting", "cancel", "terminal")
    )
    assert tuple(WorkflowSpawnBatchAction) == (
        WorkflowSpawnBatchAction.CONTINUE,
        WorkflowSpawnBatchAction.PARENT_TERMINAL,
    )
    assert WorkflowSpawnHandlerOutcome.__args__


def test_graph_unavailable_proof_is_sdk_factory_only_and_noncopyable() -> None:
    with pytest.raises(TypeError):
        VerifiedWorkflowGraphUnavailable()  # type: ignore[call-arg]

    proof = _create_verified_workflow_graph_unavailable(
        {
            "ticket_receipt_id": "ticket",
            "profile_key": "workflow.task",
            "workflow_name": "task",
            "workflow_version": "1",
            "expected_implementation_hash": "expected",
            "registry_content_digest": "digest",
            "activation_receipt_id": "activation",
            "parent_run_id": "parent",
            "owner_id": "owner",
            "runtime_lease_epoch": 1,
            "run_fence_epoch": 2,
            "workflow_lease_epoch": 3,
            "continuation_claim_epoch": 4,
            "observed_kind": "missing",
            "observed_implementation_hash": None,
        }
    )
    with pytest.raises(TypeError):
        copy.copy(proof)
    with pytest.raises(TypeError):
        copy.deepcopy(proof)
    with pytest.raises(TypeError):
        replace(proof, owner_id="forged")
