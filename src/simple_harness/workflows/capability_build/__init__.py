# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Capability builder workflow profile.

This is a bounded profile of the durable_task workflow, specialized for
building, testing, and installing capability packs. It uses the same workflow
graph as durable_task but with:

- Restricted budgets (proposal_budget: 40, fix_budget: 3)
- capability_build capability tag
- Reserved control launch policy (not model-spawnable)
- Host-issued admission contracts for candidate-only builds

The workflow reuses durable_task's graph definition and node handlers. This
module only documents the profile boundary and provides the builder-specific
data contracts already defined in simple_harness.capabilities.builder_contracts.
"""

from __future__ import annotations

__all__ = [
    "WORKFLOW_PROFILE_KEY",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "DEFAULT_PROPOSAL_BUDGET",
    "DEFAULT_FIX_BUDGET",
]

WORKFLOW_PROFILE_KEY = "workflow.capability_build"
WORKFLOW_NAME = "durable_task"
WORKFLOW_VERSION = "v1"
DEFAULT_PROPOSAL_BUDGET = 40
DEFAULT_FIX_BUDGET = 3
