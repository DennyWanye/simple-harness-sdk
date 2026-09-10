# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Test-side alias of the shipped fixture provider (``agent_orchestrator.testing.fixtures``)."""

from agent_orchestrator.testing.fixtures import (  # noqa: F401
    DEMO_BAD,
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    MODEL,
    RoleScriptedProvider,
    UnknownAfterHandoff,
    critic_step,
    demo_single_task_provider,
    demo_worker_script,
    envelope_step,
    package_of,
    proposal_step,
    role_of,
)
