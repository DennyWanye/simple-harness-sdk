# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Personal Workflow v1 - Workflow definition.

Single-node wrapper workflow that delegates execution to PersonalWorkflowRuntimePort.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import JsonValue
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    StatePatch,
    WorkflowContext,
    WorkflowState,
)
from simple_harness.workflow.definition import (
    END_NODE,
    CompiledWorkflow,
    Edge,
    NodeDefinition,
    WorkflowDefinition,
    compile_workflow,
)

from .selection import PersonalWorkflowSelectionV1

if TYPE_CHECKING:
    from collections.abc import Mapping

WORKFLOW_NAME = "personal_workflow"
WORKFLOW_VERSION = "v1"
STATE_SCHEMA_VERSION = 1
PROFILE_KEY = "workflow.personal_v1"
SELECTION_EXTENSION_KEY = "deskpet.companion.selection.v1"


async def _execute_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Execute personal workflow via runtime port.

    Args:
        state: Current workflow state
        context: Workflow execution context

    Returns:
        StatePatch with outputs and terminal_status

    Raises:
        RuntimeError: If personal_workflow_runtime port unavailable
    """
    runtime = context.ports.get("personal_workflow_runtime")
    execute = getattr(runtime, "execute", None)
    if not callable(execute):
        raise TypeError("personal workflow runtime port is unavailable")

    values = dict(state.get("values") or {})
    selection = PersonalWorkflowSelectionV1.from_authoritative_mapping(
        values["personal_workflow_selection"]
    )

    result = await execute(
        child_run_id=str(state["run_id"]),
        selection=selection,
        inputs=dict(values.get("inputs") or {}),
        execution_identity=context.identity,
    )

    return StatePatch(
        {
            "values": {
                **values,
                "outputs": copy.deepcopy(dict(result)),
                "terminal_status": "success",
            }
        }
    )


def create_definition() -> WorkflowDefinition:
    """Create personal_workflow v1 definition.

    Returns:
        WorkflowDefinition with single execute node
    """
    return WorkflowDefinition(
        name=WORKFLOW_NAME,
        version=WORKFLOW_VERSION,
        state_schema_version=STATE_SCHEMA_VERSION,
        entry_node="execute",
        nodes=(NodeDefinition("execute", _execute_handler),),
        channels={
            "values": ChannelSpec(
                value_type=JsonType.OBJECT,
                reducer=ReducerKind.SINGLE_WRITER,
                allowed_writers=frozenset({"execute"}),
            )
        },
        edges=(Edge("execute", END_NODE),),
        recursion_limit=4,
        max_supersteps=2,
        prompt_manifest={"profile": PROFILE_KEY},
        policy_manifest={
            "implementation": "personal-workflow-interpreter-v1",
            "selection_source": "trusted-parent-start-snapshot-only",
            "effect_identity": "child-selection-graph-node-v1",
        },
    )


PERSONAL_WORKFLOW_V1_DEFINITION = create_definition()
PERSONAL_WORKFLOW_V1: CompiledWorkflow = compile_workflow(PERSONAL_WORKFLOW_V1_DEFINITION)


def create_initial_state(
    *,
    run_id: str,
    personal_workflow_selection: Mapping[str, Any],
    inputs: Mapping[str, JsonValue],
    thread_id: str | None = None,
    session_id: str = "",
    **_ignored: object,
) -> WorkflowState:
    """Create initial state for personal_workflow v1.

    Args:
        run_id: Unique run identifier
        personal_workflow_selection: Selection payload (schema_version=1)
        inputs: Input values for workflow execution
        thread_id: Optional thread identifier (defaults to run_id)
        session_id: Optional session identifier
        **_ignored: Ignored additional arguments

    Returns:
        Initial WorkflowState ready for execution

    Raises:
        PersonalWorkflowSelectionError: If selection validation fails
    """
    selection = PersonalWorkflowSelectionV1.from_authoritative_mapping(personal_workflow_selection)

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "workflow_name": WORKFLOW_NAME,
        "workflow_version": WORKFLOW_VERSION,
        "thread_id": thread_id or run_id,
        "run_id": run_id,
        "session_id": session_id,
        "active_nodes": [],
        "active_step_id": None,
        "status": "pending",
        "values": {
            "personal_workflow_selection": selection.to_child_payload(),
            "inputs": copy.deepcopy(dict(inputs)),
        },
        "blob_refs": [],
        "artifact_refs": [],
        "receipt_refs": [],
        "loop_counters": {},
        "budgets": {},
        "errors": [],
    }


__all__ = [
    "PERSONAL_WORKFLOW_V1",
    "PERSONAL_WORKFLOW_V1_DEFINITION",
    "PROFILE_KEY",
    "SELECTION_EXTENSION_KEY",
    "STATE_SCHEMA_VERSION",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "create_definition",
    "create_initial_state",
]
