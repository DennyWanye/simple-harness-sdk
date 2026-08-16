# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public factory for the official personal-workflow v1 profile."""

from __future__ import annotations

from simple_harness.contracts import JsonValue
from simple_harness.workflow.definition import WorkflowDefinitionRegistration

from .._registration import build_registration
from .definition import (
    PERSONAL_WORKFLOW_V1_DEFINITION,
    PROFILE_KEY,
    create_initial_state,
)

START_SCHEMA_REF = "sdk://workflow/personal-v1/start"
START_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "personal_workflow_selection_json": {
            "type": "string",
            "minLength": 2,
            "maxLength": 262144,
        },
        "inputs_json": {"type": "string", "minLength": 2, "maxLength": 262144},
    },
    "required": ["personal_workflow_selection_json", "inputs_json"],
    "additionalProperties": False,
}


def build_personal_v1_registration(
    *, generation: int, transaction_owner: object
) -> WorkflowDefinitionRegistration:
    return build_registration(
        profile_key=PROFILE_KEY,
        description="Execute one frozen personal workflow selection.",
        use_when="A trusted parent has selected an immutable personal workflow graph.",
        avoid_when="The selection was not issued by the trusted parent runtime.",
        schema_ref=START_SCHEMA_REF,
        schema=START_SCHEMA,
        generation=generation,
        definition=PERSONAL_WORKFLOW_V1_DEFINITION,
        transaction_owner=transaction_owner,
    )


__all__ = (
    "START_SCHEMA",
    "START_SCHEMA_REF",
    "build_personal_v1_registration",
    "create_initial_state",
)
