# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1 red tests: failure counts and spend accrue to the duty (§6.1; T025 / T069).

The in-memory half of T025 and T069: a successor task, a swapped method, a
different agent or a renamed goal inherits the accumulated failure count and the
consumed allowance.  Persisting the same counters is P1.2; wiring them into
``mission_tail_commits`` is P1.3.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts.htn import Requiredness
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.obligations import (
    ExpansionRecord,
    Obligation,
    ObligationLedger,
    ObligationLifecycle,
    SatisfactionPolicy,
    Selector,
    ShapeChange,
    achieve_outcome_admission,
    funding_owner_conflicts,
)


def duty(
    identifier: str = "obligation-1",
    *,
    goal: str = "compare-sources",
    budget: str | None = "budget-root",
) -> Obligation:
    return Obligation(
        obligation_id=identifier,  # type: ignore[arg-type]
        mission_id="mission-1",
        requirement_refs=("c-complete",),
        goal_signature_id=goal,
        parameters={"subject": "alpha"},
        budget_lineage_ref=budget,
        satisfaction_policy=SatisfactionPolicy(required_criterion_ids=("c-complete",)),
    )


def test_failure_count_and_spend_survive_every_change_of_shape() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    target = duty().obligation_id

    ledger.record_failure(target)
    ledger.record_spend(target, cost_micros=1_200, attempts=1)
    ledger.record_failure(target)
    ledger.record_spend(target, cost_micros=800, attempts=1)

    # The work is renamed, re-planned onto another method, handed to another agent
    # and finally re-issued as a successor task.
    ledger.note_shape_change(target, ShapeChange.TASK_RENAMED, detail="task-1 -> task-7")
    ledger.note_shape_change(target, ShapeChange.METHOD_SWITCHED, detail="method-b")
    ledger.note_shape_change(target, ShapeChange.AGENT_REASSIGNED, detail="worker-3")
    ledger.note_shape_change(target, ShapeChange.SUCCESSOR_TASK, detail="task-8")

    account = ledger.account(target)
    assert account.failure_count == 2
    assert account.consumed_cost_micros == 2_000
    assert account.consumed_attempts == 2


def test_a_successor_under_the_same_duty_keeps_spending_the_same_allowance() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    target = duty().obligation_id
    ledger.record_failure(target)
    ledger.record_spend(target, cost_micros=5_000, attempts=2)

    ledger.note_shape_change(target, ShapeChange.SUCCESSOR_TASK, detail="task-9")
    ledger.record_failure(target)
    ledger.record_spend(target, cost_micros=1_000, attempts=1)

    account = ledger.account(target)
    assert account.failure_count == 2
    assert account.consumed_cost_micros == 6_000
    assert account.consumed_attempts == 3


def test_a_genuinely_new_duty_needs_a_new_obligation_id() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    ledger.record_failure(duty().obligation_id, count=3)

    with pytest.raises(ContractError, match="new obligation_id"):
        ledger.register(duty())

    child = duty("obligation-2")
    ledger.register(child, recursion_fuel=2)
    assert ledger.account(child.obligation_id).failure_count == 0
    assert ledger.account(duty().obligation_id).failure_count == 3


def test_expansion_history_is_kept_across_method_changes() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id

    ledger.consume_fuel(
        target, expansion=ExpansionRecord(method_id="method-a", parameters_digest="d1")
    )
    ledger.note_shape_change(target, ShapeChange.METHOD_SWITCHED, detail="method-b")
    ledger.consume_fuel(
        target, expansion=ExpansionRecord(method_id="method-b", parameters_digest="d2")
    )

    assert ledger.expansion_keys(target) == (("method-a", "d1"), ("method-b", "d2"))
    assert ledger.account(target).expansions == 2


def test_the_ledger_refuses_to_read_an_unregistered_duty() -> None:
    ledger = ObligationLedger()
    with pytest.raises(ContractError, match="not registered"):
        ledger.account("obligation-missing")  # type: ignore[arg-type]


def test_a_satisfied_duty_must_name_its_resolution() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=1)

    with pytest.raises(ContractError, match="resolution_ref"):
        ledger.set_lifecycle(duty().obligation_id, ObligationLifecycle.SATISFIED)

    account = ledger.set_lifecycle(
        duty().obligation_id, ObligationLifecycle.SATISFIED, resolution_ref="resolution-1"
    )
    assert account.lifecycle is ObligationLifecycle.SATISFIED


def test_a_shared_goal_keeps_a_single_funding_owner() -> None:
    """§6.1: consumers share the cost; they do not each reserve the full price."""

    duties = (
        duty("obligation-a", goal="fetch-source", budget="budget-root"),
        duty("obligation-b", goal="fetch-source", budget="budget-root"),
        duty("obligation-c", goal="compare-sources", budget="budget-root"),
    )
    assert funding_owner_conflicts(duties) == ("obligation-b",)


def test_obligation_round_trips_through_json_unchanged() -> None:
    original = Obligation(
        obligation_id="obligation-1",  # type: ignore[arg-type]
        mission_id="mission-1",
        requirement_refs=("c-complete", "c-cited"),
        goal_signature_id="compare-sources",
        parameters={"subject": "alpha", "depth": 2},
        scope="mission-1",
        authority_ref="grant-1",
        requiredness=Requiredness.REQUIRED,
        budget_lineage_ref="budget-root",
        satisfaction_policy=SatisfactionPolicy(
            required_criterion_ids=("c-complete",), independent_review_required=True
        ),
        parent_obligation_id="obligation-root",  # type: ignore[arg-type]
    )

    restored = Obligation.from_json(original.to_json())

    assert restored == original
    assert restored.to_json() == original.to_json()


def test_an_obligation_cannot_be_its_own_parent() -> None:
    with pytest.raises(ContractError, match="own parent"):
        Obligation(
            obligation_id="obligation-1",  # type: ignore[arg-type]
            mission_id="mission-1",
            requirement_refs=("c-complete",),
            goal_signature_id="compare-sources",
            parent_obligation_id="obligation-1",  # type: ignore[arg-type]
        )


def test_an_unknown_field_never_rides_into_an_obligation() -> None:
    payload = duty().to_json()
    payload["authority_granted"] = True
    with pytest.raises(ContractError, match="unknown fields"):
        Obligation.from_json(payload)


# --------------------------------------------------------------------------------------
# A refused call changes nothing (partial writes are how counters quietly reset)
# --------------------------------------------------------------------------------------


def test_a_refused_set_lifecycle_leaves_the_duty_open() -> None:
    """A SATISFIED without a resolution must not park the duty in SATISFIED anyway.

    It would read as closed to every later check — ``achieve_outcome_admission``
    would answer OBLIGATION_NOT_ACTIVE — while no resolution was ever recorded.
    """

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id
    before = ledger.account(target)

    with pytest.raises(ContractError, match="resolution_ref"):
        ledger.set_lifecycle(target, ObligationLifecycle.SATISFIED)

    after = ledger.account(target)
    assert after == before
    assert after.lifecycle is ObligationLifecycle.UNSATISFIED
    assert after.resolution_ref is None
    assert achieve_outcome_admission(after, selected_by=Selector.PLANNER_EXPLICIT).allowed is True


def test_a_successful_set_lifecycle_stores_the_resolution_reference() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id

    account = ledger.set_lifecycle(
        target, ObligationLifecycle.SATISFIED, resolution_ref="resolution-7"
    )

    assert account.lifecycle is ObligationLifecycle.SATISFIED
    assert account.resolution_ref == "resolution-7"
    assert ledger.account(target).resolution_ref == "resolution-7"


def test_a_refused_lifecycle_value_changes_nothing() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id
    before = ledger.account(target)

    with pytest.raises(ContractError, match="must be one of"):
        ledger.set_lifecycle(target, "DONE")  # type: ignore[arg-type]

    assert ledger.account(target) == before


def test_a_refused_record_spend_does_not_leave_half_of_itself_behind() -> None:
    """The cost is validated before the attempt count, so a bad attempt count used
    to land the cost anyway and a retry would then double-count it."""

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id
    ledger.record_spend(target, cost_micros=1_000, attempts=1)
    before = ledger.account(target)

    with pytest.raises(ContractError, match="attempts"):
        ledger.record_spend(target, cost_micros=500, attempts=-1)

    assert ledger.account(target) == before
    assert ledger.account(target).consumed_cost_micros == 1_000


def test_a_refused_record_failure_does_not_increment_the_counter() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id
    ledger.record_failure(target)

    with pytest.raises(ContractError, match="count"):
        ledger.record_failure(target, count=0)

    assert ledger.account(target).failure_count == 1


def test_a_refused_shape_change_is_not_recorded() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    target = duty().obligation_id

    with pytest.raises(ContractError, match="must not be blank"):
        ledger.note_shape_change(target, ShapeChange.METHOD_SWITCHED, detail="")

    assert ledger.shape_changes(target) == ()
    assert ledger.account(target).shape_changes == 0
