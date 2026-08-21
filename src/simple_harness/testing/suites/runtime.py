# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness.testing.contracts import CaseDefinition

CASES = (
    CaseDefinition("runtime.no_tool", "runtime", "Root ReAct completion without a Tool", "no_tool"),
    CaseDefinition("runtime.one_tool", "runtime", "One durable Tool round trip", "one_tool"),
    CaseDefinition(
        "runtime.multi_turn_tool",
        "runtime",
        "Multi-turn and multi-Tool correlation",
        "multi_turn_tool",
    ),
    CaseDefinition(
        "runtime.session_persistence",
        "runtime",
        "Execution-session persistence",
        "session_persistence",
    ),
    CaseDefinition("runtime.hitl", "runtime", "Durable authorization wait and decision", "hitl"),
    CaseDefinition(
        "runtime.delivery", "runtime", "Terminal delivery retry and backfill", "delivery"
    ),
    CaseDefinition("runtime.budget", "runtime", "All hard termination budgets", "budget"),
    CaseDefinition(
        "runtime.restart_without_replay",
        "runtime",
        "Restart recovery without physical replay",
        "restart_without_replay",
    ),
)

__all__ = ("CASES",)
