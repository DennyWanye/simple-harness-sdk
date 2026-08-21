# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Capability builder workflow profile.

This is a bounded profile of the durable_task workflow, specialized for
building, testing, and installing capability packs. It uses the same workflow
graph as durable_task but with:

- Restricted budgets (proposal_budget: 40, fix_budget: 3)
- capability_build capability tag
- Model-spawnable when its complete Host service group is configured
- Host-issued admission contracts for candidate-only builds

The workflow reuses durable_task's graph definition and node handlers. This
module only documents the profile boundary and provides the builder-specific
data contracts already defined in simple_harness.capabilities.builder_contracts.
"""

from __future__ import annotations

from ._constants import (
    DEFAULT_FIX_BUDGET,
    DEFAULT_PROPOSAL_BUDGET,
    WORKFLOW_NAME,
    WORKFLOW_PROFILE_KEY,
    WORKFLOW_VERSION,
)
from .factory import (
    START_SCHEMA,
    START_SCHEMA_REF,
    CapabilityBuildAdmission,
    CapabilityBuildExecutionState,
    build_capability_build_definition,
    build_capability_build_registration,
    create_initial_state,
    run_capability_build_specialization,
)
from .ports import (
    CapabilityActivatePort,
    CapabilityBuildAuthorizationPort,
    CapabilitySearchPort,
    CapabilitySourcePolicyPort,
    IsolatedBuildPort,
    PackageStorePort,
)

__all__ = [
    "WORKFLOW_PROFILE_KEY",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "DEFAULT_PROPOSAL_BUDGET",
    "DEFAULT_FIX_BUDGET",
]

__all__ += [
    "CapabilityBuildAdmission",
    "CapabilityBuildExecutionState",
    "CapabilityActivatePort",
    "CapabilityBuildAuthorizationPort",
    "CapabilitySearchPort",
    "CapabilitySourcePolicyPort",
    "IsolatedBuildPort",
    "PackageStorePort",
    "START_SCHEMA",
    "START_SCHEMA_REF",
    "build_capability_build_definition",
    "build_capability_build_registration",
    "create_initial_state",
    "run_capability_build_specialization",
]
