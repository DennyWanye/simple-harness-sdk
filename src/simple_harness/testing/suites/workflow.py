# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness.testing.contracts import CaseDefinition

CASES = (
    CaseDefinition("workflow.host_owned", "workflow", "Host-owned definition through the SDK Runner", "host_owned"),
    CaseDefinition("workflow.official_durable_task", "workflow", "Official durable_task Profile", "official_durable_task"),
    CaseDefinition("workflow.official_personal_v1", "workflow", "Official personal_v1 Profile", "official_personal_v1"),
    CaseDefinition("workflow.official_capability_build", "workflow", "Official capability_build Profile", "official_capability_build"),
    CaseDefinition("workflow.ticket_fingerprint", "workflow", "Ticket and frozen fingerprint rejection", "ticket_fingerprint"),
    CaseDefinition("workflow.reopen", "workflow", "Checkpoint close and reopen recovery", "reopen"),
)

__all__ = ("CASES",)
