# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public factory for the official durable-task workflow profile."""

from __future__ import annotations

from simple_harness.contracts import JsonValue
from simple_harness.workflow.definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistration,
)

from .._registration import build_registration
from . import nodes
from .definition import create_definition, create_initial_state

PROFILE_KEY = "workflow.durable_task"
START_SCHEMA_REF = "sdk://workflow/durable-task/v1/start"
START_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "request": {"type": "string", "minLength": 1, "maxLength": 65536},
        "approval_required": {"type": "boolean"},
        "clarification_required": {"type": "boolean"},
        "proposal_budget": {"type": "integer", "minimum": 1, "maximum": 40},
        "fix_budget": {"type": "integer", "minimum": 0, "maximum": 8},
    },
    "required": ["request"],
    "additionalProperties": False,
}


def build_durable_task_definition() -> WorkflowDefinition:
    return create_definition(
        intake_handler=nodes.intake_handler,
        clarify_handler=nodes.clarify_handler,
        plan_handler=nodes.plan_handler,
        wait_approval_handler=nodes.wait_approval_handler,
        llm_proposal_handler=nodes.llm_proposal_handler,
        tool_execution_handler=nodes.tool_execution_handler,
        completion_decision_handler=nodes.completion_decision_handler,
        test_handler=nodes.test_handler,
        audit_handler=nodes.audit_handler,
        finalize_handler=nodes.finalize_handler,
        approval_route=nodes.approval_route,
        completion_route=nodes.completion_route,
        audit_route=nodes.audit_route,
    )


def build_durable_task_registration(
    *, generation: int, transaction_owner: object
) -> WorkflowDefinitionRegistration:
    return build_registration(
        profile_key=PROFILE_KEY,
        description="Durable multi-step task execution with approval and audit.",
        use_when="A task needs tools, durable recovery, testing, or approval.",
        avoid_when="A direct answer or a specialized official workflow is sufficient.",
        schema_ref=START_SCHEMA_REF,
        schema=START_SCHEMA,
        generation=generation,
        definition=build_durable_task_definition(),
        transaction_owner=transaction_owner,
    )


__all__ = (
    "PROFILE_KEY",
    "START_SCHEMA",
    "START_SCHEMA_REF",
    "build_durable_task_definition",
    "build_durable_task_registration",
    "create_initial_state",
)
