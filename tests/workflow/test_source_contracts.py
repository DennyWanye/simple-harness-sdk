# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import fields

import pytest

from simple_harness.workflow.contracts import (
    PHYSICAL_WORKFLOW_PORT_NAMES,
    NodeExecutionIdentity,
    StatePatch,
    WorkflowContext,
    WorkflowRunStatus,
    WorkflowState,
)
from simple_harness.workflow.control import (
    ExecutionControl,
    WorkflowSuspended,
    bind_execution_control,
    workflow_interrupt,
)
from simple_harness.workflow.definition import ConditionalEdge, WorkflowManifest
from simple_harness.workflow.errors import (
    InvalidStatePatch,
    WorkflowContractError,
)
from simple_harness.workflow.errors import (
    WorkflowDefinitionError as ErrorVocabularyWorkflowDefinitionError,
)


def test_frozen_status_and_state_surface_is_not_reduced() -> None:
    assert {"cancel_requested", "cancelling"} <= {item.value for item in WorkflowRunStatus}
    assert {
        "thread_id", "session_id", "active_step_id", "blob_refs", "artifact_refs",
        "receipt_refs", "budgets",
    } <= WorkflowState.__required_keys__
    assert {
        "recursion_limit", "max_supersteps", "state_hash", "prompt_hash", "tool_hash",
        "policy_hash", "callable_source_hash", "dependency_lock_hash",
    } <= {field.name for field in fields(WorkflowManifest)}


def test_definition_error_has_one_machine_readable_authority() -> None:
    from simple_harness.workflow.contracts import WorkflowDefinitionError

    assert WorkflowDefinitionError is ErrorVocabularyWorkflowDefinitionError
    failure = WorkflowDefinitionError(
        "invalid_definition", "invalid", details={"field": "entry"}
    )
    assert isinstance(failure, WorkflowContractError)
    assert failure.code == "invalid_definition"
    assert failure.details == {"field": "entry"}


def test_state_patch_is_defensive_comparable_and_mapping_only() -> None:
    source = {"items": [{"id": "one"}]}
    patch = StatePatch(source)
    source["items"][0]["id"] = "changed"  # type: ignore[index]
    assert patch == StatePatch({"items": [{"id": "one"}]})
    assert "StatePatch" in repr(patch)
    with pytest.raises(InvalidStatePatch, match="mapping"):
        StatePatch([])  # type: ignore[arg-type]


def test_context_unknown_port_fails_closed_and_interrupt_view_has_no_authority() -> None:
    with pytest.raises(ValueError, match="Unknown workflow ports"):
        WorkflowContext(ports={"host_backdoor": object()})
    identity = NodeExecutionIdentity(
        "demo", "1", "thread", "run", "checkpoint", "native", "task", "node", 1
    )
    ports = {"tool": object(), "observer": object(), "progress": object(), "clock": object()}
    context = WorkflowContext(ports=ports).for_node(identity, pure_before_interrupt=True)
    assert PHYSICAL_WORKFLOW_PORT_NAMES.isdisjoint(context.ports)
    assert context.ports == {}


def test_conditional_route_requires_explicit_pure_policy() -> None:
    def selector(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return "done"

    with pytest.raises(ValueError, match="must declare pure"):
        ConditionalEdge("route", selector, {"done": "finish"})


def test_pure_route_context_has_no_callable_or_live_port_surface() -> None:
    from simple_harness.workflow.contracts import PureRouteContext

    context = PureRouteContext(
        "demo", "1", "run", "checkpoint", "task", "route",
        {"logical_timestamp": 10.0, "value": "stable"}, 10.0,
    )
    assert not hasattr(context, "ports")
    assert not hasattr(context, "clock")
    assert all(not callable(value) for value in vars(context).values())
    nested = context.state["value"]
    assert nested == "stable"


def test_pure_route_context_recursively_freezes_state() -> None:
    from simple_harness.workflow.contracts import PureRouteContext

    source = {"nested": {"items": [{"value": "frozen"}]}}
    context = PureRouteContext(
        "demo", "1", "run", "checkpoint", "task", "route", source, 10.0
    )
    source["nested"]["items"][0]["value"] = "mutated"  # type: ignore[index]
    nested = context.state["nested"]
    assert isinstance(nested, Mapping)
    items = nested["items"]
    assert isinstance(items, tuple)
    assert isinstance(items[0], Mapping)
    assert items[0]["value"] == "frozen"
    with pytest.raises(TypeError):
        items[0]["value"] = "blocked"  # type: ignore[index]


def test_interrupt_payload_only_stable_identity_and_exact_resume() -> None:
    first = ExecutionControl("task-1")
    with bind_execution_control(first), pytest.raises(WorkflowSuspended) as suspended:
        workflow_interrupt({"question": "continue?"})
    interrupt = suspended.value.interrupt
    assert interrupt.task_id == "task-1"
    assert interrupt.ordinal == 0
    assert len(interrupt.id) == 64

    resumed = ExecutionControl("task-1", responses={interrupt.id: {"approved": True}})
    with bind_execution_control(resumed):
        assert workflow_interrupt({"question": "continue?"}) == {"approved": True}
    assert resumed.consumed_interrupt_ids == [interrupt.id]


def test_interrupt_cannot_be_swallowed_by_except_exception_or_called_twice() -> None:
    control = ExecutionControl("task-1")
    caught = False
    with bind_execution_control(control):
        try:
            workflow_interrupt({"question": "continue?"})
        except Exception:  # noqa: BLE001 - proves the control signal is not an Exception
            caught = True
        except WorkflowSuspended:
            pass
    assert caught is False

    interrupt_id = hashlib.sha256(b"task-1|0").hexdigest()
    answered = ExecutionControl("task-1", responses={interrupt_id: True})
    with bind_execution_control(answered):
        try:
            workflow_interrupt({})
        except WorkflowSuspended:
            pass
        with pytest.raises(InvalidStatePatch, match="only one interrupt"):
            workflow_interrupt({})


def test_workflow_interrupt_requires_bound_execution() -> None:
    with pytest.raises(InvalidStatePatch, match="inside a native workflow task"):
        workflow_interrupt({"question": "continue?"})


def test_interrupt_payload_is_a_detached_mapping() -> None:
    payload: Mapping[str, object] = {"nested": {"value": 1}}
    control = ExecutionControl("task-2")
    with bind_execution_control(control), pytest.raises(WorkflowSuspended) as suspended:
        workflow_interrupt(payload)  # type: ignore[arg-type]
    assert suspended.value.interrupt.value == {"nested": {"value": 1}}
