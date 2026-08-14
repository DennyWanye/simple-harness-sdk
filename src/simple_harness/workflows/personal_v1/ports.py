# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Port interface for personal workflow runtime.

PersonalWorkflowRuntimePort is the minimal interface required by the personal_v1
workflow definition. Product implementations provide the interpreter that executes
the frozen graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from simple_harness.contracts import JsonValue

if TYPE_CHECKING:
    from .selection import PersonalWorkflowSelectionV1


class PersonalWorkflowRuntimePort(Protocol):
    """Port for executing personal workflow selections.

    The runtime port receives a frozen selection (graph + tool bindings) and
    executes it against the provided inputs, returning mapped outputs.

    Implementation responsibilities:
    - Parse and validate the graph structure
    - Execute nodes in topological order
    - Handle tool_call nodes (invoke tools with frozen bindings)
    - Apply template/condition/input/output node logic
    - Track checkpoints for resumption
    - Enforce max_steps budget
    """

    async def execute(
        self,
        *,
        child_run_id: str,
        selection: PersonalWorkflowSelectionV1,
        inputs: Mapping[str, JsonValue],
        execution_identity: Mapping[str, Any],
    ) -> Mapping[str, JsonValue]:
        """Execute personal workflow and return outputs.

        Args:
            child_run_id: Unique run identifier for checkpoint tracking
            selection: Frozen workflow selection (graph + tool bindings)
            inputs: Input values mapped to /input/* pointers
            execution_identity: Execution context (user, session, etc)

        Returns:
            Output values as specified by selection.workflow.outputs mapping

        Raises:
            RuntimeError: If execution fails or max_steps exceeded
        """
        ...


__all__ = [
    "PersonalWorkflowRuntimePort",
]
