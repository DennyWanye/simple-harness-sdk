# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1 red tests: recursion fuel is counted per obligation (§6.4 v1.2, ADR-08)."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.obligations import (
    AchieveOutcomeAdmission,
    BoundReachedReport,
    ExpansionRecord,
    FuelStatus,
    Obligation,
    ObligationLedger,
    ObligationLifecycle,
    Selector,
    ShapeChange,
    achieve_outcome_admission,
)


def duty(identifier: str = "obligation-1", *, parent: str | None = None) -> Obligation:
    return Obligation(
        obligation_id=identifier,  # type: ignore[arg-type]
        mission_id="mission-1",
        requirement_refs=("c-complete",),
        goal_signature_id="compare-sources",
        parameters={"subject": "alpha"},
        parent_obligation_id=parent,  # type: ignore[arg-type]
    )


def ledger_with(fuel: int = 3) -> ObligationLedger:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=fuel)
    return ledger


def expansion(method: str = "method-a", digest: str = "digest-1") -> ExpansionRecord:
    return ExpansionRecord(method_id=method, parameters_digest=digest)


def test_fuel_is_spent_per_obligation_not_per_goal_signature_and_parameters() -> None:
    ledger = ledger_with(2)
    target = duty().obligation_id

    first = ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))
    second = ledger.consume_fuel(target, expansion=expansion("method-b", "digest-2"))

    assert first.status is FuelStatus.GRANTED
    assert second.status is FuelStatus.GRANTED
    # Different method, different parameters — and still the same tank.
    assert ledger.remaining_fuel(target) == 0


def test_changing_parameters_method_or_agent_does_not_refill_the_tank() -> None:
    ledger = ledger_with(2)
    target = duty().obligation_id
    ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))

    ledger.note_shape_change(target, ShapeChange.PARAMETERS_REBOUND, detail="subject=beta")
    ledger.note_shape_change(target, ShapeChange.METHOD_SWITCHED, detail="method-b")
    ledger.note_shape_change(target, ShapeChange.AGENT_REASSIGNED, detail="worker-2")
    ledger.note_shape_change(target, ShapeChange.TASK_RENAMED, detail="task-1 -> task-9")

    assert ledger.remaining_fuel(target) == 1
    assert ledger.account(target).shape_changes == 4


def test_exhausted_fuel_reports_bound_reached_never_unsolvable() -> None:
    ledger = ledger_with(1)
    target = duty().obligation_id
    ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))

    decision = ledger.consume_fuel(target, expansion=expansion("method-c", "digest-3"))

    assert decision.status is FuelStatus.BOUND_REACHED
    assert decision.granted is False
    assert decision.remaining_fuel == 0
    assert "UNSOLVABLE" not in decision.reason.upper()
    assert str(FuelStatus.BOUND_REACHED) == "BOUND_REACHED"


def test_bound_reached_reports_the_expansions_made_and_the_duties_left_open() -> None:
    ledger = ledger_with(1)
    target = duty().obligation_id
    ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))

    report = BoundReachedReport(
        obligation_id=target,
        expansions=ledger.expansion_keys(target),
        open_child_obligation_ids=("obligation-child-1",),  # type: ignore[arg-type]
    )

    payload = report.to_json()
    assert payload["status"] == "BOUND_REACHED"
    assert payload["expansions"] == [["method-a", "digest-1"]]
    assert payload["open_child_obligation_ids"] == ["obligation-child-1"]


def test_the_same_unchanged_expansion_is_refused_without_burning_fuel() -> None:
    """§6.4: re-expanding the same goal with the same method and parameters loops."""

    ledger = ledger_with(3)
    target = duty().obligation_id
    ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))
    remaining = ledger.remaining_fuel(target)

    repeat = ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))

    assert repeat.status is FuelStatus.REPEATED_EXPANSION
    assert ledger.remaining_fuel(target) == remaining


def test_achieve_outcome_cannot_take_over_once_fuel_is_exhausted() -> None:
    ledger = ledger_with(1)
    target = duty().obligation_id
    ledger.consume_fuel(target, expansion=expansion("method-a", "digest-1"))

    decision = achieve_outcome_admission(
        ledger.account(target), selected_by=Selector.PLANNER_EXPLICIT
    )

    assert decision.allowed is False
    assert decision.admission is AchieveOutcomeAdmission.FUEL_EXHAUSTED_NO_ESCAPE


def test_achieve_outcome_needs_an_explicit_planner_choice() -> None:
    ledger = ledger_with(2)
    target = duty().obligation_id

    automatic = achieve_outcome_admission(
        ledger.account(target), selected_by=Selector.AUTOMATIC_FALLBACK
    )
    explicit = achieve_outcome_admission(
        ledger.account(target), selected_by=Selector.PLANNER_EXPLICIT
    )

    assert automatic.admission is AchieveOutcomeAdmission.NOT_EXPLICITLY_SELECTED
    assert explicit.allowed is True


def test_achieve_outcome_is_refused_for_a_duty_that_is_no_longer_open() -> None:
    ledger = ledger_with(2)
    target = duty().obligation_id
    ledger.set_lifecycle(target, ObligationLifecycle.CANCELLED)

    decision = achieve_outcome_admission(
        ledger.account(target), selected_by=Selector.PLANNER_EXPLICIT
    )

    assert decision.admission is AchieveOutcomeAdmission.OBLIGATION_NOT_ACTIVE


def test_a_second_obligation_has_its_own_tank() -> None:
    ledger = ledger_with(1)
    ledger.register(duty("obligation-2"), recursion_fuel=2)
    ledger.consume_fuel(duty().obligation_id, expansion=expansion())

    assert ledger.remaining_fuel(duty().obligation_id) == 0
    assert ledger.remaining_fuel(duty("obligation-2").obligation_id) == 2


def test_re_registering_an_obligation_cannot_reopen_its_allowance() -> None:
    ledger = ledger_with(1)
    ledger.consume_fuel(duty().obligation_id, expansion=expansion())

    with pytest.raises(ContractError, match="already registered"):
        ledger.register(duty(), recursion_fuel=5)

    assert ledger.remaining_fuel(duty().obligation_id) == 0


def test_zero_fuel_means_no_expansion_at_all() -> None:
    ledger = ledger_with(0)
    decision = ledger.consume_fuel(duty().obligation_id, expansion=expansion())
    assert decision.status is FuelStatus.BOUND_REACHED
