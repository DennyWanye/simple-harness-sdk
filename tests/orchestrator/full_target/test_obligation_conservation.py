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

from agent_orchestrator.contracts.htn import (
    BudgetInheritance,
    GoalSignature,
    ObligationOpening,
    ObligationRelation,
    Requiredness,
)
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
from agent_orchestrator.contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    VersionedRef,
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


# --------------------------------------------------------------------------------------
# Contract round 4: the token axis and admitted demand (P1.2)
# --------------------------------------------------------------------------------------


def test_tokens_accrue_beside_money_and_attempts() -> None:
    """Three ceilings, three counters: a run can be inside its cost and out of context."""

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id

    ledger.record_spend(target, cost_micros=1_200, attempts=1, tokens=4_000)
    account = ledger.record_spend(target, cost_micros=300, attempts=1, tokens=1_500)

    assert account.consumed_cost_micros == 1_500
    assert account.consumed_attempts == 2
    assert account.consumed_tokens == 5_500
    assert account.to_json()["consumed_tokens"] == 5_500


def test_tokens_survive_a_change_of_shape_like_every_other_counter() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id
    ledger.record_spend(target, tokens=9_000)

    ledger.note_shape_change(target, ShapeChange.METHOD_SWITCHED, detail="method-b")

    assert ledger.account(target).consumed_tokens == 9_000


def test_a_refused_token_amount_leaves_every_counter_alone() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id
    ledger.record_spend(target, cost_micros=1_000, attempts=1, tokens=2_000)
    before = ledger.account(target)

    with pytest.raises(ContractError, match="tokens"):
        ledger.record_spend(target, cost_micros=500, attempts=1, tokens=-1)

    assert ledger.account(target) == before


def test_a_demand_can_be_admitted_and_withdrawn() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id

    assert ledger.account(target).has_admitted_demand is False
    assert ledger.admit_demand(target).has_admitted_demand is True
    assert ledger.withdraw_demand(target).has_admitted_demand is False


def test_a_second_admission_is_refused_rather_than_silently_merged() -> None:
    """Two admissions that look like one is how a withdrawal releases someone else's share."""

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id
    ledger.admit_demand(target)

    with pytest.raises(ContractError, match="already has an admitted demand"):
        ledger.admit_demand(target)

    assert ledger.account(target).has_admitted_demand is True


def test_withdrawing_a_demand_that_was_never_admitted_is_refused() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)

    with pytest.raises(ContractError, match="no admitted demand"):
        ledger.withdraw_demand(duty().obligation_id)


def test_a_demand_may_not_be_admitted_against_a_closed_duty() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=3)
    target = duty().obligation_id
    ledger.set_lifecycle(target, ObligationLifecycle.CANCELLED)

    with pytest.raises(ContractError, match="open obligation"):
        ledger.admit_demand(target)


def test_an_expansion_record_round_trips_through_its_codec() -> None:
    record = ExpansionRecord(method_id="method-a", parameters_digest="digest-1", task_id="task-7")
    assert ExpansionRecord.from_json(record.to_json()) == record
    assert ExpansionRecord.from_json({"method_id": "m", "parameters_digest": "d"}).task_id is None


def test_an_expansion_record_refuses_an_unknown_field() -> None:
    payload = ExpansionRecord(method_id="method-a", parameters_digest="digest-1").to_json()
    payload["fuel_refunded"] = True
    with pytest.raises(ContractError, match="unknown fields"):
        ExpansionRecord.from_json(payload)


# --------------------------------------------------------------------------------------
# Contract round 6 (CR#6): opening a duty says where its authority and fuel come from
# --------------------------------------------------------------------------------------


def _goal_signature() -> GoalSignature:
    return GoalSignature(
        signature_id="extract-evidence",
        version=1,
        parameter_schema_ref=VersionedRef(id="parameters", version=1, content_hash="a" * 64),
        output_schema_ref=VersionedRef(id="outputs", version=1, content_hash="a" * 64),
        statement="extract the evidence the parent duty needs",
    )


def _opening(**overrides: object) -> ObligationOpening:
    payload: dict[str, object] = {
        "obligation_id": "obligation-child",
        "parent_obligation_id": "obligation-1",
        "relation": ObligationRelation.REFINES_PARENT,
        "requirement_refs": ("c-complete",),
        "goal_signature": _goal_signature(),
        "budget_inheritance": BudgetInheritance.INHERIT_PARENT_FUEL_SHARE,
        "fuel_share": 2,
    }
    payload.update(overrides)
    return ObligationOpening(**payload)  # type: ignore[arg-type]


def _authority() -> TypedRef:
    return TypedRef(kind=TypedRefKind.REQUIREMENTS, id="grant-1", revision=1, content_hash="a" * 64)


def test_an_opening_round_trips_through_its_codec() -> None:
    opening = _opening()
    assert ObligationOpening.from_json(opening.to_json()) == opening


def test_an_independently_authorised_duty_must_name_its_authority() -> None:
    """Planning alone does not create responsibility (§6.1)."""

    with pytest.raises(ContractError, match="must name the authority"):
        _opening(
            relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
            budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
            fuel_share=None,
            grant_ref="grant-1",
        )

    authorised = _opening(
        relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
        budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
        fuel_share=None,
        grant_ref="grant-1",
        authorization_ref=_authority(),
    )
    assert authorised.grant_ref == "grant-1"


def test_a_refinement_may_not_take_a_separate_grant() -> None:
    """A fresh grant for a refinement is a fresh retry budget by another name."""

    with pytest.raises(ContractError, match="fresh retry budget"):
        _opening(
            budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
            fuel_share=None,
            grant_ref="grant-1",
        )


def test_an_inherited_allowance_must_say_how_much_it_takes() -> None:
    with pytest.raises(ContractError, match="how much fuel it takes"):
        _opening(fuel_share=None)
    with pytest.raises(ContractError, match="no separate grant to reference"):
        _opening(grant_ref="grant-1")


def test_a_model_may_not_open_a_responsibility() -> None:
    with pytest.raises(ContractError, match="never self-declared by a model"):
        _opening(opened_by=Provenance.MODEL)

    assert _opening(opened_by=Provenance.HUMAN).opened_by is Provenance.HUMAN


def test_opening_a_refinement_moves_fuel_out_of_the_parent() -> None:
    """Decomposition redistributes the allowance; it never creates any."""

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    parent = duty().obligation_id

    child = ledger.open_from(_opening(fuel_share=2), ledger.account(parent))

    assert child.fuel_limit == 2
    assert ledger.remaining_fuel(parent) == 3
    assert ledger.obligation(child.obligation_id).parent_obligation_id == parent
    assert ledger.account(child.obligation_id).failure_count == 0


def test_a_parent_cannot_hand_over_fuel_it_no_longer_has() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=2)
    parent = duty().obligation_id
    ledger.consume_fuel(
        parent, expansion=ExpansionRecord(method_id="method-a", parameters_digest="d1")
    )
    before = ledger.account(parent)

    with pytest.raises(ContractError, match="cannot hand over"):
        ledger.open_from(_opening(fuel_share=2), before)

    assert ledger.account(parent) == before
    assert "obligation-child" not in [str(item) for item in ledger.obligation_ids()]


def test_a_separately_granted_duty_starts_from_its_grant_and_costs_the_parent_nothing() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    parent = duty().obligation_id

    child = ledger.open_from(
        _opening(
            relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
            budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
            fuel_share=None,
            grant_ref="grant-1",
            authorization_ref=_authority(),
        ),
        ledger.account(parent),
        granted_fuel=4,
    )

    assert child.fuel_limit == 4
    assert ledger.remaining_fuel(parent) == 5


def test_a_separate_grant_without_its_fuel_is_refused() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)

    with pytest.raises(ContractError, match="fuel its grant actually provides"):
        ledger.open_from(
            _opening(
                relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
                budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
                fuel_share=None,
                grant_ref="grant-1",
                authorization_ref=_authority(),
            ),
            ledger.account(duty().obligation_id),
        )


def test_opening_against_a_stale_parent_view_is_refused() -> None:
    """Two children funded out of one share is what a stale read buys."""

    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    parent = duty().obligation_id
    stale = ledger.account(parent)
    ledger.record_failure(parent)

    with pytest.raises(ContractError, match="has changed since"):
        ledger.open_from(_opening(), stale)


def test_opening_a_duty_that_already_exists_is_refused() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    ledger.register(duty("obligation-child"), recursion_fuel=1)
    before = ledger.account(duty().obligation_id)

    with pytest.raises(ContractError, match="already registered"):
        ledger.open_from(_opening(), before)

    assert ledger.account(duty().obligation_id) == before


def test_a_closed_parent_cannot_fund_new_work() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    parent = duty().obligation_id
    ledger.set_lifecycle(parent, ObligationLifecycle.CANCELLED)

    with pytest.raises(ContractError, match="cannot fund new work"):
        ledger.open_from(_opening(), ledger.account(parent))


def test_open_from_refuses_another_duty_s_account() -> None:
    ledger = ObligationLedger()
    ledger.register(duty(), recursion_fuel=5)
    ledger.register(duty("obligation-other"), recursion_fuel=5)

    with pytest.raises(ContractError, match="another duty's account"):
        ledger.open_from(_opening(), ledger.account(duty("obligation-other").obligation_id))
