# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for capability_build workflow profile specification.

Validates:
- Profile constants match product expectations
- Profile reuses durable_task workflow graph
- Budget constraints are enforced
"""

from __future__ import annotations

from simple_harness.workflows.capability_build import (
    DEFAULT_FIX_BUDGET,
    DEFAULT_PROPOSAL_BUDGET,
    WORKFLOW_NAME,
    WORKFLOW_PROFILE_KEY,
    WORKFLOW_VERSION,
)


def test_profile_constants():
    """Test capability_build profile has expected constants."""
    assert WORKFLOW_PROFILE_KEY == "workflow.capability_build"
    assert WORKFLOW_NAME == "durable_task"
    assert WORKFLOW_VERSION == "capability_build_v1"
    assert DEFAULT_PROPOSAL_BUDGET == 40
    assert DEFAULT_FIX_BUDGET == 3


def test_profile_keeps_durable_task_ownership_with_an_isolated_graph_version():
    """The specialization remains durable-task-owned without graph collision."""
    assert WORKFLOW_NAME == "durable_task"
    assert WORKFLOW_VERSION == "capability_build_v1"


def test_profile_budgets_are_constrained():
    """Test capability_build has restricted budgets compared to standard durable_task."""
    # capability_build uses tighter budgets for safety:
    # - proposal_budget: 40 (vs unlimited in general durable_task)
    # - fix_budget: 3 (vs higher default)
    assert DEFAULT_PROPOSAL_BUDGET == 40
    assert DEFAULT_FIX_BUDGET == 3

    # These are smaller than typical durable_task budgets
    assert DEFAULT_PROPOSAL_BUDGET < 100  # Reasonable upper bound
    assert DEFAULT_FIX_BUDGET < 10  # Reasonable upper bound
