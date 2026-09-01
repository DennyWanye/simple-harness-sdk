# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.execution.budget import BudgetPolicy, BudgetSnapshot
from simple_harness.runtime import build_react_driver
from simple_harness.runtime.termination import (
    TerminationBudgetExceeded,
    TerminationLimits,
    TerminationReason,
    TerminationState,
)

LIMITS = TerminationLimits(2, 3, 10.0, 100, 2)


@pytest.mark.parametrize(
    ("state", "now", "budget", "operation", "reason"),
    [
        (
            TerminationState(0, provider_turns_reserved_total=2),
            1,
            BudgetSnapshot(),
            "provider",
            TerminationReason.MAX_TURNS,
        ),
        (
            TerminationState(0, tool_calls_reserved_total=3),
            1,
            BudgetSnapshot(),
            "tool",
            TerminationReason.MAX_TOOL_CALLS,
        ),
        (
            TerminationState(0),
            10,
            BudgetSnapshot(),
            "provider",
            TerminationReason.WALL_CLOCK,
        ),
        (
            TerminationState(0),
            1,
            BudgetSnapshot(100),
            "provider",
            TerminationReason.COST,
        ),
        (
            TerminationState(0),
            1,
            BudgetSnapshot(has_unknown_charge=True),
            "provider",
            TerminationReason.COST,
        ),
        (
            TerminationState(0, repeat_key="x", repeat_streak=2),
            1,
            BudgetSnapshot(),
            "tool",
            TerminationReason.REPEATED_TOOL,
        ),
    ],
)
def test_each_hard_gate_fails_before_the_next_side_effect(
    state, now, budget, operation, reason
) -> None:
    with pytest.raises(TerminationBudgetExceeded) as caught:
        if operation == "provider":
            state.before_provider(LIMITS, now=now, budget=budget)
        else:
            state.before_tool("x", LIMITS, now=now, budget=budget)
    assert caught.value.reason is reason


def test_state_counts_provider_turns_tools_and_consecutive_names() -> None:
    state = TerminationState(0).before_provider(LIMITS, now=1, budget=BudgetSnapshot())
    state = state.before_tool("a", LIMITS, now=2, budget=BudgetSnapshot())
    state = state.before_tool("b", LIMITS, now=3, budget=BudgetSnapshot())
    assert (state.turns, state.tool_calls, state.consecutive_same_tool) == (1, 2, 1)


def test_oversized_batch_fails_without_returning_a_partially_advanced_state() -> None:
    state = TerminationState(0, tool_calls_reserved_total=2)
    with pytest.raises(TerminationBudgetExceeded) as caught:
        state.before_tool_batch(("a", "b"), LIMITS, now=1, budget=BudgetSnapshot())
    assert caught.value.reason is TerminationReason.MAX_TOOL_CALLS
    assert state.tool_calls == 2


def test_ordered_args_aware_repeat_batch_is_one_atomic_reservation() -> None:
    state = TerminationState(0, repeat_key="calculator:args-a", repeat_streak=1)
    advanced = state.before_tool_batch(
        ("calculator:args-b", "calculator:args-a"),
        LIMITS,
        now=1,
        budget=BudgetSnapshot(),
    )
    assert advanced.tool_calls_reserved_total == 2
    assert advanced.repeat_key == "calculator:args-a"
    assert advanced.repeat_streak == 1
    assert advanced.phase == "tool_batch_reserved"


def test_clock_rollback_fails_closed_before_reservation() -> None:
    state = TerminationState(10, last_observed_at=12)
    with pytest.raises(TerminationBudgetExceeded) as caught:
        state.before_provider(LIMITS, now=11, budget=BudgetSnapshot())
    assert caught.value.reason is TerminationReason.WALL_CLOCK
    assert state.provider_turns_reserved_total == 0


def test_checkpoint_roundtrip_preserves_durable_totals_and_phase() -> None:
    state = TerminationState(10, policy_fingerprint="a" * 64).before_provider(
        LIMITS, now=11, budget=BudgetSnapshot()
    )
    restored = TerminationState.from_json(state.to_json())
    assert restored == state
    assert restored.provider_request_id == "provider-turn:1"
    assert restored.policy_fingerprint == "a" * 64
    assert restored.to_json()["schema_version"] == 6


def test_checkpoint_roundtrip_preserves_workflow_catalog_pin() -> None:
    catalog = {
        "authority_id": "model_spawnable",
        "generation": 1,
        "version": 1,
        "catalog_hash": "catalog-hash",
        "profiles": [],
        "canonical_hash": "selection-hash",
    }
    # SHA-256 hash must be 64 hex characters
    selection_hash = "a" * 64
    state = TerminationState(
        10,
        workflow_catalog_selection=catalog,
        workflow_catalog_selection_hash=selection_hash,
    )
    restored = TerminationState.from_json(state.to_json())
    assert restored.workflow_catalog_selection == catalog
    assert restored.workflow_catalog_selection_hash == selection_hash


def test_checkpoint_roundtrip_preserves_tool_exposure_state() -> None:
    exposure = {
        "schema_version": 1,
        "catalog_fingerprint": "b" * 64,
        "revision": 2,
        "activated_ids": ["mcp:filesystem:read_file"],
    }
    restored = TerminationState.from_json(
        TerminationState(10, tool_exposure_state=exposure).to_json()
    )
    assert restored.tool_exposure_state == exposure


def test_legacy_checkpoint_defaults_to_static_tool_exposure() -> None:
    legacy = TerminationState(10).to_json()
    legacy["schema_version"] = 2
    for field in (
        "tool_exposure_state",
        "route_state",
        "route_receipt",
        "route_receipt_hash",
        "context_authority_receipt",
        "context_authority_receipt_hash",
        "context_snapshot_revision",
        "context_snapshot_bindings",
    ):
        legacy.pop(field)
    assert TerminationState.from_json(legacy).tool_exposure_state is None


def test_v5_checkpoint_remains_readable_and_unrouted() -> None:
    legacy = TerminationState(10).to_json()
    legacy["schema_version"] = 5
    restored = TerminationState.from_json(legacy)
    assert restored.source_schema_version == 5
    assert restored.route_state == "unrouted"
    assert restored.route_receipt is None


@pytest.mark.parametrize("mutation", ("missing_revision", "missing_bindings", "extra"))
def test_current_checkpoint_schema_rejects_missing_or_extra_lineage_fields(mutation: str) -> None:
    payload = TerminationState(
        10,
        context_snapshot_revision=7,
        context_snapshot_bindings=(("snapshot-7", "a" * 64),),
    ).to_json()
    if mutation == "missing_revision":
        payload.pop("context_snapshot_revision")
    elif mutation == "missing_bindings":
        payload.pop("context_snapshot_bindings")
    else:
        payload["unexpected_private"] = "CANARY"
    with pytest.raises(ValueError, match="fields differ"):
        TerminationState.from_json(payload)


@pytest.mark.parametrize(("field", "value"), (("phase", 1), ("route_state", True)))
def test_current_checkpoint_schema_rejects_string_coercion(field: str, value: object) -> None:
    payload = TerminationState(10).to_json()
    payload[field] = value  # type: ignore[assignment]
    with pytest.raises(TypeError, match="must be a string"):
        TerminationState.from_json(payload)


def test_public_react_builder_requires_and_freezes_hard_policy() -> None:
    with pytest.raises(TypeError):
        build_react_driver()  # type: ignore[call-arg]
    first = build_react_driver(
        limits=LIMITS,
        budget_policy=BudgetPolicy(hard_cap_micros=100, refuse_on_unknown=True),
        estimator=None,
    )
    second = build_react_driver(
        limits=TerminationLimits(3, 3, 10.0, 100, 2),
        budget_policy=BudgetPolicy(hard_cap_micros=100, refuse_on_unknown=True),
        estimator=None,
    )
    assert first.policy_fingerprint is not None
    assert first.policy_fingerprint != second.policy_fingerprint


def test_public_react_builder_keeps_run_local_tool_exposure_resolver() -> None:
    def resolver(_run_id: object) -> None:
        return None

    driver = build_react_driver(
        limits=LIMITS,
        budget_policy=BudgetPolicy(),
        estimator=None,
        tool_exposure_resolver=resolver,
    )
    assert driver._tool_exposure_resolver is resolver  # noqa: SLF001
