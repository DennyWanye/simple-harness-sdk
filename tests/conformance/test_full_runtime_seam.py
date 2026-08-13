# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen H7 full-runtime seam oracle.

T3.0 deliberately lands this as RED.  The only permitted initial failure is
the planned absence of ``simple_harness.runtime.build_runtime``.  T3.1 makes
the lifecycle cases green; T3.3 must make the ReAct cases green without
weakening these assertions.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass(frozen=True, slots=True)
class SeamExpectation:
    case: str
    ordered_components: tuple[str, ...]
    terminal: str
    invariants: tuple[str, ...]


H7_SEAM_MATRIX = (
    SeamExpectation(
        "capability_filters_provider_tools",
        ("context", "provider", "react_driver"),
        "running",
        ("allowed_tool_only", "denied_tool_never_prepared"),
    ),
    SeamExpectation(
        "provider_tool_batch_is_bounded",
        ("provider", "react_driver"),
        "failed",
        ("oversized_batch_never_prepared",),
    ),
    SeamExpectation(
        "single_tool_resumes_same_driver",
        ("provider", "react_driver", "kernel", "authorization", "tool", "react_driver"),
        "completed",
        ("one_effect", "one_resume", "same_run"),
    ),
    SeamExpectation(
        "mixed_tool_batch_waits_for_all_outcomes",
        ("provider", "react_driver", "kernel", "authorization", "tool", "react_driver"),
        "waiting",
        ("partial_batch_never_resumes", "unknown_not_replayed"),
    ),
    SeamExpectation(
        "late_tool_evidence_resumes_once",
        ("tool", "kernel", "react_driver"),
        "completed",
        ("one_resume", "stable_effect_ids"),
    ),
    SeamExpectation(
        "child_provider_failure_binds_identity",
        ("provider", "react_driver", "kernel"),
        "failed",
        ("provider_transport_zero", "child_identity_preserved"),
    ),
    SeamExpectation(
        "attached_child_failure_reaches_parent",
        ("kernel", "delivery"),
        "waiting",
        ("parent_signal_durable", "same_root"),
    ),
    SeamExpectation(
        "child_terminal_wakes_reconciliation",
        ("kernel", "delivery"),
        "completed",
        ("one_reconcile_wakeup",),
    ),
    SeamExpectation(
        "root_terminal_commits_delivery",
        ("kernel", "delivery"),
        "completed",
        ("terminal_and_outbox_atomic",),
    ),
    SeamExpectation(
        "delivery_retry_does_not_reopen_root",
        ("delivery",),
        "completed",
        ("same_idempotency_key", "root_stays_terminal"),
    ),
    SeamExpectation(
        "malformed_terminal_projection_is_rejected",
        ("kernel", "delivery"),
        "failed",
        ("strict_public_payload", "private_state_absent"),
    ),
    SeamExpectation(
        "restart_preserves_unknown_without_replay",
        ("kernel", "tool", "delivery"),
        "waiting",
        ("same_run", "same_effect", "provider_and_tool_not_replayed"),
    ),
)


@pytest.fixture
def runtime_seam_factory():
    # Planned T3.0 RED: this public module/symbol does not exist before T3.1.
    from simple_harness.runtime import build_runtime

    return build_runtime


@pytest.mark.parametrize("expected", H7_SEAM_MATRIX, ids=lambda item: item.case)
def test_h7_full_runtime_seam_is_preserved(runtime_seam_factory, expected) -> None:
    observed = runtime_seam_factory.conformance_case(expected.case)
    assert observed.ordered_components == expected.ordered_components
    assert observed.terminal == expected.terminal
    assert observed.invariants == expected.invariants
