# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c: the allocator's ``form=compound`` gate, and the legacy entry left alone.

Plan §18.5 hard constraints 2 and 4 are two statements that pull in opposite
directions, and this suite is where both are pinned:

2. ``scheduling/allocator.py`` **keeps** its old READY entry.  ``EligiblePrimitiveTask``
   is an *additional* gate for the hierarchical mode, not the only input type
   ``allocate()`` accepts, and a legacy Mission with no semantic bindings is
   dispatched exactly as it was.  So ``frontier()`` and ``allocate()`` are hashed
   here: a byte of either changing fails a test.
4. The new mode does **not** redefine ``TaskStatus.READY``.  A compound Task's READY
   is a rebuildable display index, and the gate that intercepts it is ``form=compound``
   from the semantic binding — not the semantics version, not the status string.  The
   verdict comes from ``graph.eligibility.legacy_ready_is_not_eligibility``, which had
   no positive test before this slice, so it gets one here.

The third property is §24.1 decision 6: the v2 frontier takes only records
``admit_for_dispatch`` built.  A "ready" judgement, a hand-built record and an
admission issued for another Task are each refused with their own reason.

The last section mutates the production gate and asserts the suite notices, so a
green run cannot be green because the gate does nothing.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# The readiness suite already builds a whole hierarchical world out of contracts —
# one compound root, two primitives, an order edge, a manifest and a witness.
# Rebuilding it here would give the allocator a *different* world to be judged
# against, which is the one thing a gate test must not do.
from test_readiness_reasons import (  # noqa: E402
    MISSION,
    NOW_MS,
    O_A,
    OCC_A,
    OCC_B,
    T_A,
    T_B,
    T_ROOT,
    binding_for,
    consumer_binding,
    consumer_view,
    manifest_of,
    plan_of,
    ready_report,
    report_for,
    root_view,
    vref,
)

from agent_orchestrator.artifacts.input_bindings import InputManifest  # noqa: E402
from agent_orchestrator.contracts import (  # noqa: E402
    Attempt,
    AttemptStatus,
    Budget,
    Task,
    TaskStatus,
)
from agent_orchestrator.contracts.htn import (  # noqa: E402
    ObligationId,
    PortSpec,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.graph.eligibility import (  # noqa: E402
    EligiblePrimitiveTask,
    NotEligible,
    ReadinessReason,
    TaskView,
    admit_for_dispatch,
    evaluate_readiness,
    legacy_ready_is_not_eligibility,
)
from agent_orchestrator.scheduling import allocator as allocator_module  # noqa: E402
from agent_orchestrator.scheduling.allocator import (  # noqa: E402
    ALLOCATOR_V2_VERSION,
    WEIGHTS,
    AllocationPlanV2,
    FrontierRefusal,
    FrontierV2,
    allocate,
    allocate_v2,
    evaluate_frontier_v2,
    frontier,
    frontier_v2,
    score_tasks,
)
from agent_orchestrator.scheduling.backpressure import (  # noqa: E402
    RAISED,
    BackpressureState,
)

# The hashes of the two legacy functions as they stand.  They are the *source* of
# ``frontier`` and ``allocate``, so a refactor that preserves behaviour still fails
# here — which is the point: §18.5 constraint 2 is about the old entry being left
# alone, not about it being equivalent to something new.
LEGACY_FRONTIER_SHA256 = "0ae7cd4c24ee902e1fac2f8e8193920a64e9ff10c408baf0f33d1d6cc366e1b8"
LEGACY_ALLOCATE_SHA256 = "5940ab39e18168a83f9af8c422a9924758faf41ddf8793021b5a89c6cb5ab78c"

ALL_STATUSES = tuple(TaskStatus)


# --------------------------------------------------------------------------------------
# Builders: the legacy Task rows beside the hierarchical world
# --------------------------------------------------------------------------------------


def task_row(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.READY,
    priority: float = 1.0,
    paused: bool = False,
    deps: tuple[str, ...] = (),
    tokens: int = 10_000,
    kind: str = "work",
    ready_at: float | None = None,
) -> Task:
    return Task(
        id=task_id,
        mission_id=str(MISSION),
        parent_task_ids=(),
        dependency_ids=deps,
        goal=f"goal of {task_id}",
        rationale="r",
        success_criteria=("file:x",),
        verification_policy=("format_check",),
        allowed_tools=(),
        budget=Budget(max_tokens=tokens, max_attempts=3),
        priority=priority,
        status=status,
        version=1,
        paused=paused,
        ready_at=ready_at,
        kind=kind,
    )


def attempt_row(task_id: str, ordinal: int, status: AttemptStatus) -> Attempt:
    return Attempt(
        id=f"{task_id}:attempt-{ordinal}",
        task_id=task_id,
        mission_id=str(MISSION),
        role="worker",
        model="x",
        prompt_version="v",
        context_version="c",
        budget_reserved=Budget(max_tokens=1),
        lease_owner=None,
        lease_expires_at=None,
        status=status,
        retry_of=None,
        created_at=1.0,
        version=1,
        ordinal=ordinal,
        creation_key=f"k-{task_id}-{ordinal}",
        input_id="i",
        failure=None,
    )


def admitted_for_b() -> EligiblePrimitiveTask:
    """The one admission this world can legitimately produce (occurrence ``occ-b``)."""

    return admit_for_dispatch(
        ready_report(), consumer_view(), plan_of(), manifest_of(), now_ms=NOW_MS
    )


def producer_binding() -> TaskSemanticBindingV1:
    """``t-a``: the fixture world's other primitive — one output port, no inputs."""

    return binding_for(
        T_A,
        O_A,
        TaskForm.PRIMITIVE,
        output_ports=(PortSpec(port_key="out", schema_ref=vref("schema-in")),),
    )


def admitted_for_a() -> EligiblePrimitiveTask:
    """A second genuine admission, so the frontier's ordering has two things to order."""

    view = TaskView(
        occurrence_id=OCC_A,
        task_id=T_A,
        binding=producer_binding(),
        legacy_status=str(TaskStatus.READY),
    )
    plan = plan_of()
    manifest = InputManifest(consumer_task_ref=T_A, bindings=(), pending=())
    report = evaluate_readiness(view, plan, {}, evidence_for_a(), input_ok_for_a(), now_ms=NOW_MS)
    assert report.ready, report.details
    return admit_for_dispatch(report, view, plan, manifest, now_ms=NOW_MS)


def evidence_for_a() -> Any:
    from agent_orchestrator.graph.eligibility import EvidenceView

    return EvidenceView(witnesses={}, observer_available=True, operation_range_revision=6)


def input_ok_for_a() -> Any:
    from agent_orchestrator.artifacts.input_bindings import ResolutionResult

    return ResolutionResult(manifest=InputManifest(consumer_task_ref=T_A, bindings=(), pending=()))


def compound_binding() -> TaskSemanticBindingV1:
    return binding_for(T_ROOT, ObligationId("o-root"), TaskForm.COMPOUND)


def primitive_binding(task_id: TaskRef = T_A) -> TaskSemanticBindingV1:
    return binding_for(task_id, ObligationId("o-a"), TaskForm.PRIMITIVE)


def world(
    *,
    statuses: dict[str, TaskStatus] | None = None,
    paused: frozenset[str] = frozenset(),
    priorities: dict[str, float] | None = None,
) -> tuple[list[Task], dict[str, TaskSemanticBindingV1], dict[str, EligiblePrimitiveTask]]:
    """One compound root plus one admitted primitive, as the scheduler would see them."""

    chosen = {str(T_ROOT): TaskStatus.READY, str(T_B): TaskStatus.READY}
    chosen.update(statuses or {})
    weight = {str(T_ROOT): 2.0, str(T_B): 1.0}
    weight.update(priorities or {})
    tasks = [
        task_row(
            task_id,
            status=chosen[task_id],
            priority=weight[task_id],
            paused=task_id in paused,
        )
        for task_id in (str(T_ROOT), str(T_B))
    ]
    bindings = {str(T_ROOT): compound_binding(), str(T_B): consumer_binding()}
    return tasks, bindings, {str(T_B): admitted_for_b()}


# ======================================================================================
# 1. §18.5 constraint 4: the gate is the form, whatever the status says
# ======================================================================================


@pytest.mark.parametrize("status", ALL_STATUSES, ids=[str(item) for item in ALL_STATUSES])
def test_a_compound_never_enters_the_frontier_whatever_its_status_is(status: TaskStatus) -> None:
    tasks, bindings, readiness = world(statuses={str(T_ROOT): status})
    admitted = frontier_v2(tasks, bindings, readiness)
    assert [str(item.task_id) for item in admitted] == [str(T_B)]


@pytest.mark.parametrize("status", ALL_STATUSES, ids=[str(item) for item in ALL_STATUSES])
def test_a_compound_is_refused_with_needs_refinement_whatever_its_status_is(
    status: TaskStatus,
) -> None:
    tasks, bindings, readiness = world(statuses={str(T_ROOT): status})
    refusal = evaluate_frontier_v2(tasks, bindings, readiness).refusal_for(str(T_ROOT))
    assert refusal is not None
    assert refusal.reason is ReadinessReason.NEEDS_REFINEMENT


def test_the_compound_refusal_explains_that_ready_is_only_a_display_index() -> None:
    tasks, bindings, readiness = world()
    refusal = evaluate_frontier_v2(tasks, bindings, readiness).refusal_for(str(T_ROOT))
    assert refusal is not None
    assert "legacy status 'READY'" in refusal.detail
    assert "form=compound from the semantic binding" in refusal.detail


def test_needs_refinement_lists_exactly_the_compounds() -> None:
    tasks, bindings, readiness = world()
    assert evaluate_frontier_v2(tasks, bindings, readiness).needs_refinement == (str(T_ROOT),)


def test_the_form_gate_runs_before_the_admission_so_a_compound_with_one_is_still_refused() -> None:
    """A compound must be refused whatever else is true of it (§18.5 constraint 4)."""

    tasks, bindings, readiness = world()
    # An admission for ``occ-b`` filed under the compound's id: the form gate has to
    # fire first, or a mis-keyed map would dispatch a compound to a Worker.
    readiness = {**readiness, str(T_ROOT): admitted_for_b()}
    computed = evaluate_frontier_v2(tasks, bindings, readiness)
    refusal = computed.refusal_for(str(T_ROOT))
    assert refusal is not None and refusal.reason is ReadinessReason.NEEDS_REFINEMENT
    assert [str(item.task_id) for item in computed.admitted] == [str(T_B)]


def test_allocate_v2_never_grants_an_attempt_to_a_compound() -> None:
    tasks, bindings, readiness = world()
    plan = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=None)
    assert plan.granted_task_ids == (str(T_B),)
    assert str(T_ROOT) not in plan.granted_task_ids


def test_a_compound_cannot_be_admitted_for_dispatch_at_all() -> None:
    """The other half of the same rule, from the gate's own side (P2.1c)."""

    with pytest.raises(NotEligible):
        admit_for_dispatch(
            report_for(root_view()), root_view(), plan_of(), manifest_of(), now_ms=NOW_MS
        )


# ======================================================================================
# 2. Only an admission enters (§24.1 decision 6)
# ======================================================================================


def test_a_primitive_with_an_admission_enters_the_frontier() -> None:
    tasks, bindings, readiness = world()
    admitted = frontier_v2(tasks, bindings, readiness)
    assert len(admitted) == 1
    assert admitted[0].occurrence_id == OCC_B
    assert admitted[0].gate_passed


def test_a_primitive_without_an_admission_is_not_selected() -> None:
    tasks, bindings, _ = world()
    computed = evaluate_frontier_v2(tasks, bindings, {})
    assert computed.admitted == ()
    refusal = computed.refusal_for(str(T_B))
    assert refusal is not None and refusal.reason is ReadinessReason.NOT_SELECTED
    assert "admit_for_dispatch" in refusal.detail


def test_a_task_with_no_semantic_binding_is_graph_integrity_not_a_fallback() -> None:
    tasks, _bindings, readiness = world()
    computed = evaluate_frontier_v2(tasks, {}, readiness)
    assert computed.admitted == ()
    reasons = {item.task_id: item.reason for item in computed.refusals}
    assert reasons == {
        str(T_ROOT): ReadinessReason.GRAPH_INTEGRITY,
        str(T_B): ReadinessReason.GRAPH_INTEGRITY,
    }


def test_a_record_nobody_admitted_is_refused_as_corruption() -> None:
    """``gate_passed`` is the mark ``admit_for_dispatch`` writes after construction."""

    tasks, bindings, readiness = world()
    forged = admitted_for_b()
    object.__setattr__(forged, "_token", object())
    assert not forged.gate_passed
    computed = evaluate_frontier_v2(tasks, bindings, {str(T_B): forged})
    refusal = computed.refusal_for(str(T_B))
    assert refusal is not None and refusal.reason is ReadinessReason.GRAPH_INTEGRITY
    assert computed.admitted == ()


def test_something_that_is_not_an_admission_at_all_is_refused() -> None:
    tasks, bindings, _ = world()
    junk: Any = {str(T_B): object()}
    computed = evaluate_frontier_v2(tasks, bindings, junk)
    refusal = computed.refusal_for(str(T_B))
    assert refusal is not None and refusal.reason is ReadinessReason.GRAPH_INTEGRITY
    assert computed.admitted == ()


def test_an_admission_issued_for_another_task_is_not_transferable() -> None:
    tasks, bindings, _ = world()
    bindings = {**bindings, str(T_A): primitive_binding()}
    tasks = [*tasks, task_row(str(T_A))]
    computed = evaluate_frontier_v2(tasks, bindings, {str(T_A): admitted_for_b()})
    refusal = computed.refusal_for(str(T_A))
    assert refusal is not None and refusal.reason is ReadinessReason.GRAPH_INTEGRITY
    assert "not transferable" in refusal.detail


def test_a_paused_route_is_not_allocated() -> None:
    tasks, bindings, readiness = world(paused=frozenset({str(T_B)}))
    computed = evaluate_frontier_v2(tasks, bindings, readiness)
    refusal = computed.refusal_for(str(T_B))
    assert refusal is not None and refusal.reason is ReadinessReason.NOT_SELECTED
    assert "paused" in refusal.detail
    assert computed.admitted == ()


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_a_finished_task_wants_no_further_dispatch(status: TaskStatus) -> None:
    tasks, bindings, readiness = world(statuses={str(T_B): status})
    computed = evaluate_frontier_v2(tasks, bindings, readiness)
    refusal = computed.refusal_for(str(T_B))
    assert refusal is not None and refusal.reason is ReadinessReason.NOT_SELECTED
    assert computed.admitted == ()


def two_admitted_world(
    *, priorities: dict[str, float]
) -> tuple[list[Task], dict[str, TaskSemanticBindingV1], dict[str, EligiblePrimitiveTask]]:
    tasks = [
        task_row(str(T_A), priority=priorities[str(T_A)]),
        task_row(str(T_B), priority=priorities[str(T_B)]),
    ]
    bindings = {str(T_A): producer_binding(), str(T_B): consumer_binding()}
    readiness = {str(T_A): admitted_for_a(), str(T_B): admitted_for_b()}
    return tasks, bindings, readiness


def test_the_v2_frontier_orders_by_priority_like_the_legacy_one() -> None:
    tasks, bindings, readiness = two_admitted_world(priorities={str(T_A): 1.0, str(T_B): 9.0})
    assert [str(item.task_id) for item in frontier_v2(tasks, bindings, readiness)] == [
        str(T_B),
        str(T_A),
    ]
    assert [task.id for task in frontier(tasks)] == [str(T_B), str(T_A)]


def test_the_v2_frontier_breaks_a_priority_tie_on_the_task_ordinal() -> None:
    tasks, bindings, readiness = two_admitted_world(priorities={str(T_A): 1.0, str(T_B): 1.0})
    admitted = [str(item.task_id) for item in frontier_v2(tasks, bindings, readiness)]
    assert admitted == [task.id for task in frontier(tasks)]


def test_allocate_v2_orders_two_admissions_the_way_the_legacy_allocator_would() -> None:
    tasks, bindings, readiness = two_admitted_world(priorities={str(T_A): 1.0, str(T_B): 9.0})
    plan = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=None, now=0.0)
    legacy = allocate(tasks, (), concurrency_limit=None, now=0.0)
    assert plan.granted_task_ids == tuple(task.id for task, _ordinal in legacy.grants)


# ======================================================================================
# 3. legacy_ready_is_not_eligibility — §18.5 constraints 2 and 4, positively
# ======================================================================================


@pytest.mark.parametrize("status", ALL_STATUSES, ids=[str(item) for item in ALL_STATUSES])
def test_no_legacy_status_ever_admits_a_dispatch(status: TaskStatus) -> None:
    for form in (None, TaskForm.PRIMITIVE, TaskForm.COMPOUND):
        assert not legacy_ready_is_not_eligibility(str(status), form=form).admits_dispatch


@pytest.mark.parametrize("status", ALL_STATUSES, ids=[str(item) for item in ALL_STATUSES])
def test_a_compound_answers_needs_refinement_for_every_status(status: TaskStatus) -> None:
    verdict = legacy_ready_is_not_eligibility(str(status), form=TaskForm.COMPOUND)
    assert verdict.gate_reason is ReadinessReason.NEEDS_REFINEMENT


def test_a_primitive_answers_ask_evaluate_readiness_rather_than_a_guessed_refusal() -> None:
    verdict = legacy_ready_is_not_eligibility(str(TaskStatus.READY), form=TaskForm.PRIMITIVE)
    assert verdict.gate_reason is None
    assert "evaluate_readiness" in verdict.explanation


def test_the_allocator_and_the_eligibility_module_give_the_same_compound_answer() -> None:
    """The one function both wirings ask, so P2.3b and P2.3c cannot disagree."""

    tasks, bindings, readiness = world()
    refusal = evaluate_frontier_v2(tasks, bindings, readiness).refusal_for(str(T_ROOT))
    verdict = legacy_ready_is_not_eligibility(str(TaskStatus.READY), form=TaskForm.COMPOUND)
    assert refusal is not None
    assert refusal.reason is verdict.gate_reason
    assert refusal.detail == verdict.explanation


# ======================================================================================
# 4. §18.5 constraint 2: the legacy entry is untouched
# ======================================================================================


def test_the_legacy_frontier_source_is_byte_for_byte_unchanged() -> None:
    source = inspect.getsource(frontier)
    assert hashlib.sha256(source.encode()).hexdigest() == LEGACY_FRONTIER_SHA256


def test_the_legacy_allocate_source_is_byte_for_byte_unchanged() -> None:
    source = inspect.getsource(allocate)
    assert hashlib.sha256(source.encode()).hexdigest() == LEGACY_ALLOCATE_SHA256


def test_the_legacy_frontier_still_admits_a_ready_compound_row() -> None:
    """It does not know about forms, and §18.5 constraint 2 says it must not learn.

    A legacy Mission has no semantic bindings at all; the compound interception is
    the *new* entry's job, and teaching the old one would change how a legacy
    Mission is dispatched.
    """

    tasks = [task_row(str(T_ROOT)), task_row(str(T_B))]
    assert {task.id for task in frontier(tasks)} == {str(T_ROOT), str(T_B)}


def test_the_legacy_allocate_grants_a_legacy_mission_with_no_bindings() -> None:
    tasks = [task_row("t-1"), task_row("t-2")]
    plan = allocate(tasks, (), concurrency_limit=None)
    assert {task.id for task, _ordinal in plan.grants} == {"t-1", "t-2"}


def test_the_two_entry_points_carry_two_version_strings() -> None:
    assert allocator_module.ALLOCATOR_VERSION == "allocator-v1"
    assert ALLOCATOR_V2_VERSION == "allocator-v2"
    assert allocator_module.ALLOCATOR_VERSION != ALLOCATOR_V2_VERSION


# ======================================================================================
# 5. Scoring, capacity and backpressure are the legacy implementation
# ======================================================================================


def test_allocate_v2_scores_with_the_same_weights_and_the_same_function() -> None:
    tasks, bindings, readiness = world()
    plan = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=None, now=0.0)
    direct = score_tasks(tasks, (), now=0.0, aging_window_seconds=0.0)
    assert plan.scores[str(T_B)] == direct[str(T_B)]
    assert plan.scores[str(T_B)].parts.keys() == WEIGHTS.keys()


def test_only_the_admitted_tasks_get_a_score_in_the_plan() -> None:
    tasks, bindings, readiness = world()
    plan = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=None)
    assert set(plan.scores) == {str(T_B)}


def test_allocate_v2_respects_the_mission_concurrency_limit() -> None:
    tasks, bindings, readiness = world()
    open_attempt = attempt_row(str(T_B), 1, AttemptStatus.RUNNING)
    plan = allocate_v2(tasks, (open_attempt,), bindings, readiness, concurrency_limit=1)
    assert plan.grants == ()
    assert plan.open_attempts == 1
    assert plan.slots == 0


def test_allocate_v2_counts_a_waiting_attempt_as_free_capacity() -> None:
    tasks, bindings, readiness = world()
    open_attempt = attempt_row(str(T_B), 1, AttemptStatus.RUNNING)
    plan = allocate_v2(
        tasks,
        (open_attempt,),
        bindings,
        readiness,
        concurrency_limit=1,
        waiting_attempt_ids=frozenset({open_attempt.id}),
    )
    assert plan.granted_task_ids == (str(T_B),)


def test_allocate_v2_grants_the_configured_number_of_candidates() -> None:
    tasks, bindings, readiness = world()
    plan = allocate_v2(
        tasks, (), bindings, readiness, concurrency_limit=None, candidates_per_task=3
    )
    assert [ordinal for _item, ordinal in plan.grants] == [1, 2, 3]


def test_raised_backpressure_halves_the_concurrency_limit_as_the_legacy_gate_does() -> None:
    tasks, bindings, readiness = world()
    pressure = BackpressureState(level=RAISED, raised={"verification_queue": {"value": 9}})
    plan = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=4, pressure=pressure)
    assert plan.concurrency_limit == 2
    assert plan.pressure == RAISED


def test_under_pressure_a_never_attempted_task_keeps_the_exploration_slot() -> None:
    tasks, bindings, readiness = world()
    pressure = BackpressureState(level=RAISED, raised={"verification_queue": {"value": 9}})
    plan = allocate_v2(
        tasks,
        (),
        bindings,
        readiness,
        concurrency_limit=4,
        pressure=pressure,
        exploration_slots=1,
    )
    assert plan.granted_task_ids == (str(T_B),)


def test_under_pressure_a_formula_tier_task_with_no_exploration_slot_waits() -> None:
    tasks, bindings, readiness = world()
    pressure = BackpressureState(level=RAISED, raised={"verification_queue": {"value": 9}})
    plan = allocate_v2(
        tasks,
        (),
        bindings,
        readiness,
        concurrency_limit=4,
        pressure=pressure,
        exploration_slots=0,
    )
    assert plan.grants == ()
    assert plan.eligible == 0


def test_the_pressure_filter_agrees_with_the_legacy_inline_one() -> None:
    """The legacy body may not be edited, so the helper is checked against it."""

    rows = [
        task_row("t-1", kind="conflict"),
        task_row("t-2", ready_at=0.0),
        task_row("t-3"),
        task_row("t-4"),
    ]
    attempts = (attempt_row("t-4", 1, AttemptStatus.COMPLETED),)
    scores = score_tasks(rows, attempts, now=1_000.0, aging_window_seconds=1.0)
    kept = allocator_module._pressure_keep(rows, scores, attempts, 1)
    pressure = BackpressureState(level=RAISED, raised={"verification_queue": {"value": 9}})
    legacy = allocate(
        rows,
        attempts,
        concurrency_limit=None,
        now=1_000.0,
        aging_window_seconds=1.0,
        pressure=pressure,
        exploration_slots=1,
    )
    assert {task.id for task in kept} == {task.id for task, _ordinal in legacy.grants}


def test_the_plan_json_names_the_v2_version_and_carries_the_refusals() -> None:
    tasks, bindings, readiness = world()
    payload = allocate_v2(tasks, (), bindings, readiness, concurrency_limit=2).to_json()
    assert payload["allocator_version"] == ALLOCATOR_V2_VERSION
    assert [grant["task_id"] for grant in payload["grants"]] == [str(T_B)]
    assert payload["grants"][0]["occurrence_id"] == str(OCC_B)
    assert [item["reason"] for item in payload["refusals"]] == [
        str(ReadinessReason.NEEDS_REFINEMENT)
    ]


def test_the_frontier_json_is_readable_without_the_allocation() -> None:
    tasks, bindings, readiness = world()
    payload = evaluate_frontier_v2(tasks, bindings, readiness).to_json()
    assert payload["admitted"] == [str(T_B)]
    assert payload["refusals"][0]["task_id"] == str(T_ROOT)


def test_an_empty_world_allocates_nothing_and_refuses_nothing() -> None:
    computed = evaluate_frontier_v2((), {}, {})
    assert computed == FrontierV2()
    plan = allocate_v2((), (), {}, {}, concurrency_limit=None)
    assert plan == AllocationPlanV2(scores={})


# ======================================================================================
# 6. Mutation self-check: break the gate, and the suite must notice
# ======================================================================================


def _status_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: the gate reads the status string instead of the binding's form."""

    def status_gate(legacy_status: str, *, form: Any = None) -> Any:
        from agent_orchestrator.graph.eligibility import LegacyReadyVerdict

        if legacy_status == str(TaskStatus.BLOCKED):
            return LegacyReadyVerdict(False, ReadinessReason.NEEDS_REFINEMENT, "blocked")
        return LegacyReadyVerdict(False, None, "READY is eligibility")

    monkeypatch.setattr(allocator_module, "legacy_ready_is_not_eligibility", status_gate)


def test_mutant_a_status_based_gate_stops_intercepting_the_ready_compound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, bindings, readiness = world()
    real = evaluate_frontier_v2(tasks, bindings, readiness).refusal_for(str(T_ROOT))
    assert real is not None and real.reason is ReadinessReason.NEEDS_REFINEMENT
    _status_gate(monkeypatch)
    mutated = evaluate_frontier_v2(tasks, bindings, readiness).refusal_for(str(T_ROOT))
    assert mutated is not None
    assert mutated.reason is not ReadinessReason.NEEDS_REFINEMENT


def test_mutant_dropping_the_gate_mark_check_admits_a_forged_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, bindings, _ = world()
    forged = admitted_for_b()
    object.__setattr__(forged, "_token", object())
    monkeypatch.setattr(
        EligiblePrimitiveTask, "gate_passed", property(lambda self: True), raising=False
    )
    admitted = frontier_v2(tasks, bindings, {str(T_B): forged})
    assert [str(item.task_id) for item in admitted] == [str(T_B)]


def test_mutant_treating_a_missing_binding_as_legacy_would_dispatch_corruption() -> None:
    """The shape of the wrong implementation, written out so the refusal is pinned."""

    tasks, _bindings, readiness = world()

    def permissive(rows: Any, bindings: Any, admissions: Any) -> list[EligiblePrimitiveTask]:
        return [admissions[task.id] for task in rows if task.id in admissions]

    assert permissive(tasks, {}, readiness) != frontier_v2(tasks, {}, readiness)
    assert frontier_v2(tasks, {}, readiness) == []


def test_mutant_skipping_the_paused_check_would_allocate_a_paused_route() -> None:
    tasks, bindings, readiness = world(paused=frozenset({str(T_B)}))
    assert frontier_v2(tasks, bindings, readiness) == []
    unpaused, bindings, readiness = world()
    assert len(frontier_v2(unpaused, bindings, readiness)) == 1


def test_mutant_ignoring_the_task_id_match_would_transfer_an_admission() -> None:
    tasks = [task_row(str(T_A))]
    bindings = {str(T_A): primitive_binding()}
    stolen = {str(T_A): admitted_for_b()}
    assert frontier_v2(tasks, bindings, stolen) == []
    refusal = evaluate_frontier_v2(tasks, bindings, stolen).refusals[0]
    assert refusal == FrontierRefusal(
        task_id=str(T_A), reason=ReadinessReason.GRAPH_INTEGRITY, detail=refusal.detail
    )


def test_mutant_a_frontier_that_ignored_capacity_would_overrun_the_limit() -> None:
    tasks, bindings, readiness = world()
    open_attempt = attempt_row(str(T_B), 1, AttemptStatus.RUNNING)
    unbounded = allocate_v2(
        tasks,
        (open_attempt,),
        bindings,
        readiness,
        concurrency_limit=None,
        candidates_per_task=2,
    )
    bounded = allocate_v2(
        tasks,
        (open_attempt,),
        bindings,
        readiness,
        concurrency_limit=1,
        candidates_per_task=2,
    )
    assert [ordinal for _item, ordinal in unbounded.grants] == [2]
    assert bounded.grants == ()
