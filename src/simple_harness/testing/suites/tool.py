# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness.testing.contracts import CaseDefinition

CASES = (
    CaseDefinition("tool.schema", "tool", "Closed and bounded Tool input schemas", "schema"),
    CaseDefinition(
        "tool.five_state",
        "tool",
        "Succeeded, partial, rejected, failed, and unknown outcomes",
        "five_state",
    ),
    CaseDefinition(
        "tool.reconcile", "tool", "Unknown effect reconciliation without blind replay", "reconcile"
    ),
    CaseDefinition(
        "tool.malformed_duplicate_late",
        "tool",
        "Malformed, duplicate, and late Tool results",
        "malformed_duplicate_late",
    ),
)

__all__ = ("CASES",)
