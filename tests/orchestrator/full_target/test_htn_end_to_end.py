# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2: the hierarchical mode, end to end.

P2.3b left four blockers and this suite is the witness that each one is gone:

(a) ``commit_plan_revision`` created no ``Task`` row, so the allocator saw no work at
    all and a hierarchical Mission could not dispatch anything.  §1–§4 below.
(b) ``_next_attempt`` asked only the *form* gate, so a DATA consumer whose producer
    had not been accepted was dispatched with no inputs and ran anyway; and
    ``_recorded_outputs`` returned nothing, so the index it would have read was
    empty.  §5–§6.
(c) the Planner's prompt and package were chosen independently of the mode, so a
    real model was asked for a plan-revision proposal while holding the DAG package.
    §10.
(d) ``commit_graph_change`` had no gate on a hierarchical Mission.  §9.

Two properties run through all of it and are asserted rather than described: the
**budget conservation equation** holds after every commit (§21.5), and a Mission is
never COMPLETED without its root ``GoalResolution`` — "wrongly declared complete = 0".

The last section is the mutation self-check: each mutant is a plausible wrong
implementation, and an assertion the real tests make has to catch it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import Env, method, out, param, ref, step, task_binding  # noqa: E402

from agent_orchestrator.artifacts.input_bindings import (  # noqa: E402
    AcceptedOutput,
    DisclosureState,
    ResourceIdentity,
)
from agent_orchestrator.contracts import (  # noqa: E402
    Budget,
    MissionStatus,
    TaskStatus,
)
from agent_orchestrator.contracts.evidence_state import (  # noqa: E402
    Availability,
    ObservationRecord,
    QueryCompleteness,
    TruthValue,
    Validity,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (  # noqa: E402
    OccurrenceId,
    ReadItem,
    ReadItemKind,
    SemanticReadSet,
    TaskForm,
    TaskRef,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.contracts.resolution import (  # noqa: E402
    AcceptanceId,
    DeliveryReceipt,
    DeliveryStage,
)
from agent_orchestrator.contracts.semantic_base import (  # noqa: E402
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
)
from agent_orchestrator.graph.eligibility import ReadinessReason  # noqa: E402
from agent_orchestrator.knowledge.predicates import (  # noqa: E402
    PredicateRegistry,
)
from agent_orchestrator.knowledge.validity import (  # noqa: E402
    NO_SUBJECT,
    witness_subject,
)
from agent_orchestrator.orchestrator import occurrence_tasks  # noqa: E402
from agent_orchestrator.orchestrator.accepted_outputs import (  # noqa: E402
    accepted_output_from_json,
    accepted_output_json,
    check_declared,
    declared_output_ports,
)
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    HIERARCHICAL_GRAPH_CHANGE_REFUSED,
    HIERARCHICAL_JUDGMENT_REFUSED,
    CommitRejected,
    CommitService,
    MissionSpec,
    mission_account,
    task_account,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    ASSEMBLY_MISSING,
    DISPATCH_WITHHELD,
    MISSION_STALLED,
    WITNESS_KEY_TAKEN,
    HierarchicalDispatch,
    is_hierarchical,
)
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    COMPOUND_TOKENS,
    CONTEXT_FORM,
    CONTEXT_MATERIALISED_BY,
    CONTEXT_OCCURRENCE,
    CONTEXT_PLAN_REVISION,
    MATERIALISER,
    Materialisation,
    share_tokens,
    task_pool_tokens,
)
from agent_orchestrator.orchestrator.plan_commits import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    LEGACY_SEMANTICS,
    OCCURRENCES_MATERIALISED,
    PLAN_REVISION_COMMITTED,
    PlanCommitRejected,
    PlanPrincipal,
)
from agent_orchestrator.orchestrator.resolution_commits import (  # noqa: E402
    GOAL_RESOLUTION_COMMITTED,
    eligible_root_receipts,
)
from agent_orchestrator.planning.htn.observation_pipeline import (  # noqa: E402
    NO_OBSERVER,
    build_index,
    gather,
    observe_predicate,
    record_observation,
)
from agent_orchestrator.planning.htn.observers import (  # noqa: E402
    COMPLETE_COVERAGE,
    Observation,
    ObservationOutcome,
    unavailable,
)
from agent_orchestrator.planning.htn.planner_package import (  # noqa: E402
    HIERARCHICAL_PACKAGE_VERSION,
    applicability_reports,
    hierarchical_planner_package,
    method_library,
    open_goals,
    pending_primitives,
    recorded_facts,
)
from agent_orchestrator.storage import acceptance_receipt_schema, schema  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.storage.store import Store, StoreError  # noqa: E402
from agent_orchestrator.testing.fixtures import plan_revision_proposal_step  # noqa: E402

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
FUEL = 8
MISSION_TOKENS = 200_000
HEX_A = "a" * 64


# ======================================================================================
# The world: one hierarchical Mission whose root method has a DATA edge inside it
# ======================================================================================


def _env(mission: str) -> Env:
    env = Env(mission=mission)
    env.register_type(
        "plan.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-root",),
        domain="plan",
    )
    env.register_type(
        "plan.leaf",
        parameters=(("subject", "string"),),
        outputs=(("result", "plan.result"),),
        capabilities=("plan.read",),
        domain="plan",
    )
    env.register_type(
        "plan.review",
        parameters=(("subject", "string"),),
        inputs=(("result", "plan.result", True),),
        outputs=(("verdict", "plan.verdict"),),
        capabilities=("plan.read",),
        domain="plan",
    )
    # §16: a second consumer of the *same* read-only producer, so "shared sub-goal
    # executed once" has something to be true about at the dispatch level.
    env.register_type(
        "plan.audit",
        parameters=(("subject", "string"),),
        inputs=(("result", "plan.result", True),),
        outputs=(("finding", "plan.finding"),),
        capabilities=("plan.read",),
        domain="plan",
    )
    return env


def _outer(method_id: str = "plan.outer"):
    """root (compound) → leaf (primitive) → review (primitive, consumes leaf.result)."""

    return method(
        method_id,
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


def _spec(key: str, *, mode: str, tokens: int | None = MISSION_TOKENS) -> MissionSpec:
    return MissionSpec(
        goal="交付一个可验收的层次计划",
        success_criteria=("file:a.md",),
        tenant_id="tenant-p23c",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=tokens, max_attempts=12),
        orchestration_semantics_version=mode,
    )


@dataclass
class World:
    service: CommitService
    mission: Any
    env: Env
    contract: Any
    dispatch: HierarchicalDispatch
    principal: PlanPrincipal
    path: Path
    recorded_delivery: set[str] = field(default_factory=set)

    @property
    def store(self) -> Store:
        return self.service.store

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.service.store)

    @property
    def duties(self) -> ObligationStore:
        return ObligationStore(self.service.store)

    def reply(self, **changes: Any) -> str:
        return _proposal_text(self.contract, **changes)

    def plan(self, text: str | None = None, *, command_id: str = "cmd-a"):
        return self.dispatch.apply_planner_reply(
            self.mission.id,
            text if text is not None else self.reply(),
            principal=self.principal,
            command_id=command_id,
        )

    def events(self, kind: str) -> list[Any]:
        return [e for e in self.store.list_events(self.mission.id) if e.type == kind]

    def tasks(self) -> dict[str, Any]:
        return {task.id: task for task in self.store.list_tasks(self.mission.id)}

    def network(self):
        return self.dispatch.network(self.mission.id)

    def occurrence_of(self, task_id: str) -> str:
        for spec in self.network().occurrences:
            if str(spec.task_id) == task_id:
                return str(spec.occurrence_id)
        raise AssertionError(f"no occurrence for {task_id}")

    def admit_demand(self) -> None:
        """TG decision 9: an occurrence competes only while a demand for its duty lives.

        P2.3c part 2d routes the bootstrap through ``CommitService`` so that this
        fixture uses the same audited entry point production does; duties the commit
        path already admitted for the slot that adopted them are skipped.
        """

        seen: set[str] = set()
        for spec in self.network().occurrences:
            duty = str(spec.obligation_id)
            if duty in seen or not self.duties.exists(self.mission.id, spec.obligation_id):
                continue
            seen.add(duty)
            if not self.duties.account(self.mission.id, spec.obligation_id).has_admitted_demand:
                self.service.admit_obligation_demand(
                    self.mission.id,
                    spec.obligation_id,
                    principal="mission-submitter",
                    requester={"kind": "mission_root"},
                    evidence={"mission_id": self.mission.id, "occurrence": str(spec.occurrence_id)},
                )

    def reopen(self) -> World:
        """Re-open the same library, as a restarted process would."""

        self.store.close()
        service = CommitService(Store.open(self.path))
        mission = service.store.get_mission(self.mission.id)
        # The Env reads its counters out of the store (review P2-16), so it has to
        # follow the reopened one; the closed handle would raise on the next look.
        self.env.semantics = HtnStore(service.store)
        return dataclasses.replace(
            self,
            service=service,
            mission=mission,
            dispatch=HierarchicalDispatch(service.store, service, planning=self.env),
        )


def _proposal_text(contract: Any, **changes: Any) -> str:
    reference = contract.method_ref()
    base: dict[str, Any] = {
        "proposal_id": "prop-1",
        "expected_plan_revision": 0,
        "read_set": [
            {
                "kind": "method",
                "id": reference.method_id,
                "semantic_revision": reference.version,
                "content_hash": reference.content_hash,
            }
        ],
        "operations": [
            {
                "op": "refine",
                "goal_id": ROOT_TASK,
                "obligation_id": ROOT_DUTY,
                "method_ref": {
                    "id": reference.method_id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            }
        ],
    }
    base.update(changes)
    return plan_revision_proposal_step(**base)


def build_world(
    tmp_path,
    *,
    mode: str = HIERARCHICAL_SEMANTICS,
    key: str = "p23c",
    tokens: int | None = MISSION_TOKENS,
    name: str = "orchestrator.db",
) -> World:
    path = Path(tmp_path) / name
    service = CommitService(Store.open(path))
    mission, _ = service.create_mission(_spec(key, mode=mode, tokens=tokens))
    env = _env(mission.id)
    contract = _outer()
    receipt = env.admit(contract)
    assert receipt.admitted, receipt.problems
    binding = task_binding(
        env,
        "plan.goal",
        task_id=ROOT_TASK,
        obligation=ROOT_DUTY,
        parameters={"subject": "alpha"},
    )
    if mode == HIERARCHICAL_SEMANTICS:
        ObligationStore(service.store).register(
            Obligation(
                obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
                mission_id=mission.id,
                requirement_refs=("req-1",),
                goal_signature_id="plan.goal",
            ),
            recursion_fuel=FUEL,
        )
        HtnStore(service.store).put_task_semantics(mission.id, binding)
        HtnStore(service.store).register_method(
            contract, env.registry.registration(contract.method_ref())
        )
    # The real loop calls this before it creates the first Planner intent, and
    # PLANNING is the state the activation rule moves *out of* — so a fixture that
    # skipped it would be testing a transition the deployment never makes.  Both modes,
    # because the legacy witnesses below compare the two at the same phase.
    service.begin_planning(mission.id)
    # Review P2-16: the double counts the *same rows* the real world counts, so a
    # START-lane test here sees ``support_revision`` move with the evidence instead
    # of sitting at the fixture's constant 1.
    env.semantics = HtnStore(service.store)
    return World(
        service=service,
        mission=mission,
        env=env,
        contract=contract,
        dispatch=HierarchicalDispatch(service.store, service, planning=env),
        principal=PlanPrincipal("manager-1", "mission", 0),
        path=path,
    )


def committed(tmp_path, *, demand: bool = False, **kwargs) -> World:
    world = build_world(tmp_path, **kwargs)
    outcome = world.plan()
    assert outcome.committed, outcome.last_reason
    if demand:
        world.admit_demand()
    return world


@pytest.fixture
def world(tmp_path) -> World:
    return committed(tmp_path)


@pytest.fixture
def live(tmp_path) -> World:
    return committed(tmp_path, demand=True)


# ======================================================================================
# 1. occurrence → Task: the bridge P2.3b did not have
# ======================================================================================


def test_every_occurrence_of_the_committed_plan_has_a_task_row(world: World) -> None:
    rows = world.tasks()
    assert {str(spec.task_id) for spec in world.network().occurrences} == set(rows)
    assert len(rows) == 3  # root compound + leaf + review


def test_the_task_row_names_its_occurrence_and_plan_revision(world: World) -> None:
    leaf = world.tasks()[_leaf_task(world)]
    assert leaf.context[CONTEXT_OCCURRENCE] == world.occurrence_of(leaf.id)
    assert leaf.context[CONTEXT_PLAN_REVISION] == 1
    assert leaf.context[CONTEXT_MATERIALISED_BY] == MATERIALISER


def test_a_primitive_is_ready_and_a_compound_is_blocked(world: World) -> None:
    rows = world.tasks()
    assert rows[ROOT_TASK].status is TaskStatus.BLOCKED
    assert rows[ROOT_TASK].context[CONTEXT_FORM] == str(TaskForm.COMPOUND)
    assert rows[_leaf_task(world)].status is TaskStatus.READY


def test_no_occurrence_encodes_its_ordering_in_dependency_ids(world: World) -> None:
    """§18.5 constraint 4: ORDER is the typed network's answer, not a second copy."""

    assert all(task.dependency_ids == () for task in world.tasks().values())


def test_every_materialised_row_is_work_kind(world: World) -> None:
    assert {task.kind for task in world.tasks().values()} == {"work"}


def test_the_row_carries_the_duty_criteria_it_owes(world: World) -> None:
    leaf = world.tasks()[_leaf_task(world)]
    assert leaf.success_criteria  # never empty: §6.3 "完成条件不清"
    assert leaf.root_goal == world.mission.goal


def test_the_commit_appends_one_materialisation_event(world: World) -> None:
    events = world.events(OCCURRENCES_MATERIALISED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["plan_revision"] == 1
    assert sorted(item["task_id"] for item in payload["tasks"]) == sorted(world.tasks())


def test_each_materialised_task_gets_a_task_committed_event(world: World) -> None:
    committed_events = world.events("TaskCommitted")
    assert {e.task_id for e in committed_events} == set(world.tasks())
    assert all(e.payload["dependencies"] == [] for e in committed_events)


def test_every_materialised_row_has_a_budget_account_under_the_mission(world: World) -> None:
    ledger = world.service.ledger
    for task_id in world.tasks():
        account = ledger.account(task_account(task_id))
        assert account.parent_id == mission_account(world.mission.id)
        assert account.scope == "task"


def test_the_account_prefix_is_the_services_own_and_not_a_second_spelling(world: World) -> None:
    """The half-finished part-2 code spelled ``task:``/``mission:`` by hand.

    ``open_account`` could only report that as "unknown budget account": the parent it
    was given did not exist.  The names come from one place now, and this is the
    assertion that says so.
    """

    assert mission_account(world.mission.id).startswith("budget:")
    assert task_account(ROOT_TASK) == f"budget:{ROOT_TASK}"
    with sqlite3.connect(world.path) as connection:
        ids = {row[0] for row in connection.execute("SELECT account_id FROM budget_accounts")}
    assert task_account(ROOT_TASK) in ids
    assert f"task:{ROOT_TASK}" not in ids


# ======================================================================================
# 2. the budget conservation equation (§21.5)
# ======================================================================================


def test_the_conservation_equation_holds_after_the_commit(world: World) -> None:
    equation = world.events(OCCURRENCES_MATERIALISED)[0].payload["budget"]
    assert equation["holds"] is True
    assert equation["committed_tokens"] + equation["granted_tokens"] <= equation["pool_tokens"]


def test_the_pool_is_the_mission_budget_minus_the_system_reserve(world: World) -> None:
    assert task_pool_tokens(world.mission) == MISSION_TOKENS


def test_a_compound_holds_no_tokens(world: World) -> None:
    assert world.tasks()[ROOT_TASK].budget.max_tokens == COMPOUND_TOKENS


def test_the_two_primitives_share_the_pool(world: World) -> None:
    rows = world.tasks()
    shares = [rows[task_id].budget.max_tokens for task_id in rows if task_id != ROOT_TASK]
    assert shares == [MISSION_TOKENS // 2, MISSION_TOKENS // 2]
    assert sum(shares) <= MISSION_TOKENS


def test_the_sum_of_every_row_never_exceeds_the_pool(world: World) -> None:
    total = sum(int(task.budget.max_tokens or 0) for task in world.tasks().values())
    assert total <= task_pool_tokens(world.mission)


def test_the_share_divisor_counts_the_compounds_nobody_refined_yet() -> None:
    """A later refinement has to be payable, so an open compound reserves a share."""

    assert share_tokens(100, funded_now=2, reserved_subtrees=0) == 50
    assert share_tokens(100, funded_now=1, reserved_subtrees=1) == 50
    assert share_tokens(None, 3, 1) is None


def test_a_mission_with_no_token_ceiling_conserves_vacuously() -> None:
    assert task_pool_tokens(_unbounded()) is None
    equation = Materialisation(pool_tokens=None).conservation()
    assert equation["holds"] is True


def test_a_pool_that_cannot_fund_the_new_primitives_is_refused(tmp_path) -> None:
    """A plan whose work can pay for nothing is refused, not opened with a dead account."""

    world = build_world(tmp_path, tokens=1, key="p23c-poor")
    outcome = world.plan()
    assert outcome.committed is False
    assert outcome.last_reason == "BUDGET_INSUFFICIENT"
    assert world.store.list_tasks(world.mission.id) == []


def test_the_refusal_writes_no_plan_revision_and_no_account(tmp_path) -> None:
    world = build_world(tmp_path, tokens=1, key="p23c-poor2")
    assert world.plan().committed is False
    assert world.semantics.active_plan_revision(world.mission.id) is None
    assert world.events(PLAN_REVISION_COMMITTED) == []
    assert PlanCommitRejected is not None  # the refusal type the round translated


# ======================================================================================
# 3. PLANNING → ACTIVE by the formal rule
# ======================================================================================


def test_the_mission_becomes_active_when_dispatchable_work_is_committed(world: World) -> None:
    assert world.store.get_mission(world.mission.id).status is MissionStatus.ACTIVE


def test_the_activation_is_recorded_on_the_commit_event(world: World) -> None:
    payload = world.events(PLAN_REVISION_COMMITTED)[0].payload
    assert payload["mission_status"] == str(MissionStatus.ACTIVE)
    assert payload["materialised_tasks"]


def test_a_mission_before_its_first_revision_is_still_planning(tmp_path) -> None:
    world = build_world(tmp_path, key="p23c-planning")
    assert world.store.get_mission(world.mission.id).status is MissionStatus.PLANNING
    assert world.store.list_tasks(world.mission.id) == []


# ======================================================================================
# 4. crash recovery: the same revision twice is one materialisation
# ======================================================================================


def test_a_restart_after_the_commit_does_not_materialise_twice(world: World) -> None:
    before = {task.id: task.budget.max_tokens for task in world.tasks().values()}
    restarted = world.reopen()
    assert {t.id: t.budget.max_tokens for t in restarted.store.list_tasks(world.mission.id)} == (
        before
    )
    assert len(restarted.events(OCCURRENCES_MATERIALISED)) == 1


def test_a_restart_does_not_charge_the_pool_twice(world: World) -> None:
    restarted = world.reopen()
    total = sum(
        int(task.budget.max_tokens or 0) for task in restarted.store.list_tasks(world.mission.id)
    )
    assert total <= task_pool_tokens(world.mission)


def test_a_second_identical_reply_does_not_materialise_a_second_time(world: World) -> None:
    """Re-refining an already refined goal never doubles the board or the budget."""

    from agent_orchestrator.planning.htn.compiler import CompilationRefused

    before = {task.id: (task.version, task.budget.max_tokens) for task in world.tasks().values()}
    with pytest.raises(CompilationRefused):
        world.plan(command_id="cmd-b")
    after = {task.id: (task.version, task.budget.max_tokens) for task in world.tasks().values()}
    assert after == before
    assert len(world.events(OCCURRENCES_MATERIALISED)) == 1


# ======================================================================================
# 5. the dispatch gate: readiness, not the READY string
# ======================================================================================


def test_a_compound_is_never_admitted(live: World) -> None:
    admissions = live.dispatch.admissions(live.mission.id)
    assert ROOT_TASK not in admissions.readiness
    refusal = admissions.refusal_for(ROOT_TASK)
    assert refusal is not None and refusal.reason is ReadinessReason.NEEDS_REFINEMENT


def test_the_producer_is_admitted_and_the_data_consumer_is_not(live: World) -> None:
    admissions = live.dispatch.admissions(live.mission.id)
    assert _leaf_task(live) in admissions.readiness
    review = admissions.refusal_for(_review_task(live))
    assert review is not None and review.reason is ReadinessReason.WAITING_DATA


def test_the_admission_is_the_gates_own_record(live: World) -> None:
    admitted = live.dispatch.admissions(live.mission.id).readiness[_leaf_task(live)]
    assert admitted.gate_passed is True
    assert str(admitted.task_id) == _leaf_task(live)
    assert admitted.input_manifest_hash


def test_an_occurrence_with_no_admitted_demand_is_not_selected(world: World) -> None:
    """TG decision 9: no live demand is NOT_SELECTED, never an implicit permission."""

    admissions = world.dispatch.admissions(world.mission.id)
    assert admissions.readiness == {}
    assert {item.reason for item in admissions.refusals} == {
        ReadinessReason.NOT_SELECTED,
        ReadinessReason.NEEDS_REFINEMENT,
    }


def test_the_allocator_grants_only_admitted_tasks(live: World) -> None:
    from agent_orchestrator.scheduling.allocator import allocate_v2

    admissions = live.dispatch.admissions(live.mission.id)
    plan = allocate_v2(
        list(live.tasks().values()),
        [],
        admissions.bindings,
        admissions.readiness,
        concurrency_limit=4,
    )
    assert plan.granted_task_ids == (_leaf_task(live),)


def test_writing_ready_onto_the_compound_row_changes_no_answer(live: World) -> None:
    row = live.tasks()[ROOT_TASK]
    live.store.update_task(
        dataclasses.replace(row, status=TaskStatus.READY, version=row.version + 1),
        expected_version=row.version,
    )
    admissions = live.dispatch.admissions(live.mission.id)
    assert ROOT_TASK not in admissions.readiness


def test_the_withheld_reasons_are_recorded_once_per_revision_and_reason(live: World) -> None:
    admissions = live.dispatch.admissions(live.mission.id)
    live.dispatch.record_withheld(live.mission.id, admissions)
    live.dispatch.record_withheld(live.mission.id, admissions)
    events = live.events(DISPATCH_WITHHELD)
    assert len(events) == len(admissions.refusals)
    assert {e.payload["reason"] for e in events} == {str(i.reason) for i in admissions.refusals}


def test_the_withheld_record_keeps_the_detail_codes_unmerged(live: World) -> None:
    admissions = live.dispatch.admissions(live.mission.id)
    live.dispatch.record_withheld(live.mission.id, admissions)
    waiting = [
        e
        for e in live.events(DISPATCH_WITHHELD)
        if e.payload["reason"] == str(ReadinessReason.WAITING_DATA)
    ]
    assert waiting and waiting[0].payload["detail_codes"]


# ======================================================================================
# 6. the accepted-output index (§24.1 decision 4)
# ======================================================================================


def test_the_plan_declares_one_output_port_for_the_producer(world: World) -> None:
    network = world.network()
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    assert set(declared_output_ports(network, producer)) == {"result"}


def test_an_undeclared_port_is_refused_rather_than_indexed(world: World) -> None:
    network = world.network()
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    bogus = _accepted_output(world, port="not-a-port")
    with pytest.raises(ContractError, match="no declared output port"):
        check_declared(network, producer, [bogus])


def test_a_relabelled_schema_is_refused(world: World) -> None:
    network = world.network()
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    other = VersionedRef(id="plan.other", version=1, content_hash=HEX_A)
    with pytest.raises(ContractError, match="data requirement declares"):
        check_declared(network, producer, [_accepted_output(world, schema=other)])


def test_a_declared_output_round_trips_through_the_row(world: World) -> None:
    output = _accepted_output(world)
    assert accepted_output_from_json(accepted_output_json(output)) == output


def test_the_codec_keeps_provisional_rather_than_defaulting_it(world: World) -> None:
    output = dataclasses.replace(_accepted_output(world), provisional=True)
    assert accepted_output_from_json(accepted_output_json(output)).provisional is True


def test_a_recorded_output_reaches_the_resolver(world: World) -> None:
    _store_acceptance(world, "acc-leaf")
    output = _accepted_output(world)
    world.semantics.insert_acceptance_output(
        world.mission.id,
        acceptance_id="acc-leaf",
        output_port=output.output_port,
        artifact_id=output.artifact_id,
        producer_occurrence=str(output.producer_occurrence),
        producer_task_ref=str(output.producer_task_ref),
        producer_result_id=output.producer_result_id,
        support_revision=output.support_revision,
        content_hash=output.content_hash,
        source_revision=output.source_revision,
        document=accepted_output_json(output),
    )
    index = world.dispatch.accepted_outputs(world.mission.id, world.network())
    assert [item.artifact_id for item in index.outputs] == [output.artifact_id]


def test_an_output_of_an_occurrence_the_plan_dropped_is_not_offered(world: World) -> None:
    _store_acceptance(world, "acc-gone")
    output = dataclasses.replace(
        _accepted_output(world), producer_occurrence=OccurrenceId("occ-retired")
    )
    world.semantics.insert_acceptance_output(
        world.mission.id,
        acceptance_id="acc-gone",
        output_port=output.output_port,
        artifact_id=output.artifact_id,
        producer_occurrence="occ-retired",
        producer_task_ref=str(output.producer_task_ref),
        producer_result_id=output.producer_result_id,
        support_revision=output.support_revision,
        content_hash=output.content_hash,
        source_revision=output.source_revision,
        document=accepted_output_json(output),
    )
    index = world.dispatch.accepted_outputs(world.mission.id, world.network())
    assert index.outputs == ()


# ======================================================================================
# 7. migration 17: the accept-side receipts and the output index
# ======================================================================================


def test_migration_seventeen_is_additive_and_eighteen_is_the_head() -> None:
    assert schema.SCHEMA_VERSION == 18
    assert schema.MIGRATIONS[16].ddl is acceptance_receipt_schema.DDL
    assert "ALTER TABLE" not in acceptance_receipt_schema.DDL.upper()


def test_the_three_new_tables_exist_and_are_strict(world: World) -> None:
    rows = {
        name: sql
        for name, sql in world.store.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        )
    }
    for table in acceptance_receipt_schema.TABLES:
        assert table in rows and "STRICT" in rows[table]


def test_a_delivery_receipt_is_a_library_record_not_a_command_argument(world: World) -> None:
    """AER §6.1: a receipt handed in on a command is a claim, not a record."""

    from agent_orchestrator.orchestrator.resolution_commits import CommitGoalResolutionCommand

    assert (
        CommitGoalResolutionCommand.__dataclass_fields__["delivery_receipts"].type
        == "tuple[str, ...]"
    )


def test_a_delivery_receipt_object_on_the_command_is_refused(world: World) -> None:
    receipt = DeliveryReceipt(
        receipt_id="dlv-1",
        mission_id=world.mission.id,
        acceptance_id=AcceptanceId("acc-leaf"),
        stage=DeliveryStage.CONFIRMED,
        observed_at_ms=1,
        operation_id="op-1",
    )
    with pytest.raises(ContractError) as caught:
        _root_command_with(world, delivery_receipts=(receipt,))
    assert "record_delivery_receipt" in str(caught.value)


def test_a_receipt_quoting_an_unstored_acceptance_cannot_be_recorded(world: World) -> None:
    receipt = DeliveryReceipt(
        receipt_id="dlv-nobody",
        mission_id=world.mission.id,
        acceptance_id=AcceptanceId("acc-nobody"),
        stage=DeliveryStage.CONFIRMED,
        observed_at_ms=1,
        operation_id="op-1",
    )
    with pytest.raises(StoreError) as caught:
        world.service.record_delivery_receipt(
            world.mission.id, receipt, command_id="cmd-dlv-nobody"
        )
    assert "DELIVERY_RECEIPT_INVALID" in str(caught.value)
    assert world.semantics.list_delivery_receipts(world.mission.id) == ()


def test_a_receipt_for_another_mission_cannot_be_recorded(world: World) -> None:
    receipt = DeliveryReceipt(
        receipt_id="dlv-elsewhere",
        mission_id="mission-elsewhere",
        acceptance_id=AcceptanceId("acc-leaf"),
        stage=DeliveryStage.CONFIRMED,
        observed_at_ms=1,
        operation_id="op-1",
    )
    with pytest.raises(StoreError, match="DELIVERY_RECEIPT_INVALID"):
        world.service.record_delivery_receipt(world.mission.id, receipt, command_id="cmd-dlv-else")


def test_the_accept_side_is_wired_onto_the_one_commit_service() -> None:
    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitsMixin

    assert issubclass(CommitService, ResolutionCommitsMixin)
    assert CommitService.accept_review is ResolutionCommitsMixin.accept_review


# ======================================================================================
# 8. the root resolution gate: "wrongly declared complete = 0"
# ======================================================================================


def test_the_root_review_is_not_ready_before_the_children_are_accepted(live: World) -> None:
    assert live.dispatch.root_review_ready(live.mission.id) is False


def test_the_mission_is_not_terminal_before_its_root_resolution(live: World) -> None:
    assert live.dispatch.terminal(live.mission.id) is False


def test_the_root_resolution_inputs_report_the_missing_anchor(live: World) -> None:
    inputs = live.dispatch.root_resolution_inputs(live.mission.id)
    assert inputs.reason in {"ROOT_REVIEW_PACKAGE_MISSING", "REQUIREMENTS_MISSING"}
    assert inputs.complete is False


def test_the_root_resolution_is_not_offered_while_a_child_is_unaccepted(live: World) -> None:
    outcome = live.dispatch.attempt_root_resolution(
        live.mission.id, principal=live.principal, command_id="cmd-root-1"
    )
    assert outcome.committed is False
    assert outcome.reason == "ROOT_REVIEW_NOT_READY"
    assert live.semantics.list_goal_resolutions(live.mission.id) == ()


def test_a_missing_root_requirement_forms_the_resolution_zero_times(live: World) -> None:
    """§21.5: "缺根要求时根 Resolution 形成 0 次"."""

    for _ in range(3):
        live.dispatch.attempt_root_resolution(
            live.mission.id, principal=live.principal, command_id="cmd-root-loop"
        )
    assert live.semantics.list_goal_resolutions(live.mission.id) == ()
    assert live.store.get_mission(live.mission.id).status is not MissionStatus.COMPLETED


def test_the_accept_side_refuses_a_plan_principal(world: World) -> None:
    """The two Commit halves authenticate against two principal types, on purpose.

    ``PlanPrincipal`` carries the manager epoch a *plan* commit is checked against;
    the accept side wants a ``ResolutionPrincipal``.  Handing one to the other is
    ``BAD_PRINCIPAL`` — a caller that has not said whose accept authority it claims —
    and this is the assertion that keeps the trigger from quietly doing it.
    """

    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    with pytest.raises(ResolutionCommitRejected) as caught:
        world.service.commit_goal_resolution(_root_command_with(world), world.principal)
    assert caught.value.reason == "BAD_PRINCIPAL"


def test_the_root_trigger_converts_the_principal_for_the_accept_side() -> None:
    import inspect

    source = inspect.getsource(HierarchicalDispatch.attempt_root_resolution)
    assert "ResolutionPrincipal(" in source


def test_the_root_trigger_reads_the_composition_verdict_rather_than_asserting_it() -> None:
    """Hard-coding ``composition_obligation_passed=True`` would be the trigger claiming,
    on the reviewer's behalf, that the composition held — the exact shape
    "wrongly declared complete = 0" forbids."""

    import inspect

    source = inspect.getsource(HierarchicalDispatch.attempt_root_resolution)
    assert "composition_obligation_passed=inputs.record.verdict is ReviewVerdict.ACCEPT" in source
    assert "composition_obligation_passed=True" not in source


def test_the_root_trigger_decides_nothing_itself() -> None:
    """Every rule that forms a root resolution lives in the Commit transaction."""

    import inspect

    source = inspect.getsource(HierarchicalDispatch.attempt_root_resolution)
    assert "commit_goal_resolution" in source
    for decided in ("acceptance_rules", "acceptable(", "COMPLETED"):
        assert decided not in source


def test_the_root_trigger_records_why_a_refusal_happened(live: World) -> None:
    """ "The Mission did not complete" is not a diagnosis; the reason code is."""

    outcome = live.dispatch.attempt_root_resolution(
        live.mission.id, principal=live.principal, command_id="cmd-root-record"
    )
    assert outcome.to_json()["reason"] == "ROOT_REVIEW_NOT_READY"
    assert outcome.to_json()["resolution_id"] is None


def test_the_contributions_are_read_from_the_acceptances_not_the_projection(
    live: World,
) -> None:
    assert live.dispatch.root_contributions(live.mission.id) == {}


# ======================================================================================
# 9. the Manager's graph-change gate (§18.5 rule 2)
# ======================================================================================


def test_a_graph_change_against_a_hierarchical_mission_is_refused(world: World) -> None:
    with pytest.raises(CommitRejected, match="SEMANTICS_IS_HIERARCHICAL"):
        world.service.commit_graph_change(
            world.mission.id, _noop_change(), source={"intent_id": "i-1"}
        )


def test_the_refusal_points_at_the_door_that_is_open(world: World) -> None:
    with pytest.raises(CommitRejected):
        world.service.commit_graph_change(
            world.mission.id, _noop_change(), source={"intent_id": "i-2"}
        )
    events = world.events(HIERARCHICAL_GRAPH_CHANGE_REFUSED)
    assert events and events[0].payload["redirect"] == "commit_plan_revision"


def test_the_refusal_writes_no_task_and_no_graph_version(world: World) -> None:
    before = {task.id: task.version for task in world.tasks().values()}
    with pytest.raises(CommitRejected):
        world.service.commit_graph_change(
            world.mission.id, _noop_change(), source={"intent_id": "i-3"}
        )
    assert {task.id: task.version for task in world.tasks().values()} == before


def test_a_legacy_mission_still_reaches_the_legacy_graph_change(tmp_path) -> None:
    """The gate is the *mode*, not a global switch: legacy is untouched."""

    legacy = build_world(tmp_path, mode=LEGACY_SEMANTICS, key="p23c-legacy-gate")
    with pytest.raises(CommitRejected) as caught:
        legacy.service.commit_graph_change(
            legacy.mission.id, _noop_change(), source={"intent_id": "i-4"}
        )
    assert "SEMANTICS_IS_HIERARCHICAL" not in str(caught.value)
    assert legacy.events(HIERARCHICAL_GRAPH_CHANGE_REFUSED) == []


# ======================================================================================
# 10. the hierarchical Planner package (P2.3b blocker c)
# ======================================================================================


def test_the_seed_package_names_the_open_root_goal(tmp_path) -> None:
    fresh = build_world(tmp_path, key="p23c-pkg")
    package = hierarchical_planner_package(
        fresh.mission, fresh.dispatch.seed_network(fresh.mission.id), registry=fresh.env.registry
    )
    goals = package["plan"]["open_compound_goals"]
    assert [item["goal_id"] for item in goals] == [ROOT_TASK]
    assert goals[0]["obligation_id"] == ROOT_DUTY


def test_the_package_carries_the_method_ref_triple_verbatim(tmp_path) -> None:
    fresh = build_world(tmp_path, key="p23c-pkg2")
    entries = method_library(fresh.env.registry, ["plan.goal"])
    assert entries, "the seed registry holds a method for the root signature"
    reference = fresh.contract.method_ref()
    assert any(item["method_ref"]["content_hash"] == reference.content_hash for item in entries)


def test_a_refined_goal_is_no_longer_offered_for_refinement(world: World) -> None:
    assert open_goals(world.network()) == ()


def test_the_package_shows_the_committed_primitives(world: World) -> None:
    listed = {item["task_id"] for item in pending_primitives(world.network())}
    assert listed == {_leaf_task(world), _review_task(world)}


def test_the_package_states_its_own_version_and_output_contract(world: World) -> None:
    package = hierarchical_planner_package(
        world.mission, world.network(), registry=world.env.registry
    )
    assert package["package_version"] == HIERARCHICAL_PACKAGE_VERSION
    assert package["output_contract"] == "<plan_revision_proposal>{json}</plan_revision_proposal>"
    assert package["mode"] == "hierarchical"


def test_the_package_separates_available_and_unavailable_capabilities(world: World) -> None:
    package = hierarchical_planner_package(
        world.mission,
        world.network(),
        registry=world.env.registry,
        capabilities=["plan.read"],
        unavailable_capabilities=["plan.write"],
    )
    assert package["operators"] == {
        "available_capabilities": ["plan.read"],
        "unavailable_capabilities": ["plan.write"],
    }


def test_the_event_handler_chooses_the_hierarchical_prompt_with_the_package() -> None:
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._create_planner_intent)
    assert "_hierarchical_planner_package" in source
    assert "_hierarchical_planner_template" in source


class _Pinned:
    """Just enough ``Orchestrator`` to answer ``_template``: a domain and a pin table.

    The two collaborators the chooser reads are ``commit.domain_for`` (the frozen
    domain profile) and ``policy_for``’s ``prompt_versions`` (the frozen pin), so a
    stub that supplies exactly those runs the **real** chooser against a real pin.
    """

    def __init__(self, pin: str | None) -> None:
        from agent_orchestrator.governance.domains import resolve_domain

        self._domain = resolve_domain(None)
        self._pin = pin

    class _Commit:
        def __init__(self, domain: Any) -> None:
            self._domain = domain

        def domain_for(self, mission_id: str) -> Any:
            return self._domain

    @property
    def commit(self) -> Any:
        return self._Commit(self._domain)

    def policy_for(self, mission_id: str) -> dict[str, Any]:
        return {"prompt_versions": {} if self._pin is None else {"planner": self._pin}}

    def choose(self) -> Any:
        from agent_orchestrator.orchestrator.event_handler import Orchestrator

        return Orchestrator._hierarchical_planner_template(self, "m-1")  # type: ignore[arg-type]

    def _template(self, template: Any, mission_id: str) -> Any:
        from agent_orchestrator.orchestrator.event_handler import Orchestrator

        return Orchestrator._template(self, template, mission_id)  # type: ignore[arg-type]


def test_a_legacy_prompt_pin_does_not_reach_the_hierarchical_branch() -> None:
    """P2.3c part 2b: the mode picks the prompt, a policy pin only picks the version.

    Every code-domain deployment freezes ``prompt_versions["planner"]`` to a DAG
    Planner version, and ``template_for`` honours a pin for any template of the same
    *role* — so the hierarchical branch was handed the legacy prompt while holding
    the hierarchical package.  That is the half-mode §18.5 rule 1 forbids, and it is
    what made every real-model round come back ``proposal_unreadable``.

    Part 2d (review P2-21) drives the real chooser instead of reading its source.
    """

    from agent_orchestrator.runtime.role_templates import PLANNER, PLANNER_HIERARCHICAL_V3

    assert _Pinned(PLANNER.prompt_version).choose() is PLANNER_HIERARCHICAL_V3
    assert _Pinned(None).choose() is PLANNER_HIERARCHICAL_V3


def test_a_pin_from_an_older_package_version_does_not_apply_to_this_package() -> None:
    """Review P1-8: the prompt and the package are chosen together, now in code.

    ``planner-hierarchical-v2`` is a hierarchical prompt, so the old membership test
    honoured the pin — and handed the model a prompt that says "this package gives
    you no observation ids, never write kind=fact" together with a package whose
    ``facts`` section is the only place a legal fact entry can be copied from.  A pin
    only selects among the prompts written against the package this build assembles.
    """

    from agent_orchestrator.runtime.role_templates import (
        HIERARCHICAL_PLANNER_PACKAGE_VERSION,
        HIERARCHICAL_PLANNER_VERSIONS,
        HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE,
        PLANNER_HIERARCHICAL,
        PLANNER_HIERARCHICAL_V1,
        PLANNER_HIERARCHICAL_V3,
        hierarchical_planner_versions,
    )

    # v1 and v2 belong to the older package; v3 is this package’s own.
    assert _Pinned(PLANNER_HIERARCHICAL_V1.prompt_version).choose() is PLANNER_HIERARCHICAL_V3
    assert _Pinned(PLANNER_HIERARCHICAL.prompt_version).choose() is PLANNER_HIERARCHICAL_V3
    assert _Pinned(PLANNER_HIERARCHICAL_V3.prompt_version).choose() is PLANNER_HIERARCHICAL_V3
    assert hierarchical_planner_versions() == frozenset({PLANNER_HIERARCHICAL_V3.prompt_version})
    # the mode-level set is still the union of every group, and nothing is orphaned
    assert (
        frozenset().union(*HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE.values())
        == HIERARCHICAL_PLANNER_VERSIONS
    )
    assert HIERARCHICAL_PLANNER_PACKAGE_VERSION in HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE


# ======================================================================================
# 11. the observation pipeline (§6.6 C28, AER §8.2)
# ======================================================================================


def test_an_observer_for_an_unregistered_predicate_is_refused_at_wiring_time() -> None:
    registry = PredicateRegistry()
    with pytest.raises(ContractError, match="does not hold"):
        build_index(registry, [_FakeObserver("obs-1", ("nobody.knows",))])


def test_two_observers_of_one_predicate_are_refused() -> None:
    registry, signature = _registry_with("demo.flag")
    del signature
    with pytest.raises(ContractError, match="two observers"):
        build_index(
            registry,
            [_FakeObserver("obs-1", ("demo.flag",)), _FakeObserver("obs-2", ("demo.flag",))],
        )


def test_an_unavailable_observer_is_observer_unavailable_and_records_nothing(
    world: World,
) -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [_FakeObserver("obs-1", ("demo.flag",), answer=None)])
    outcome = observe_predicate(index, signature.predicate_ref, {}, now_ms=1)
    assert outcome.reason is ReadinessReason.OBSERVER_UNAVAILABLE
    recorded = record_observation(world.semantics, world.mission.id, outcome)
    assert recorded.recorded is False
    assert world.semantics.list_observations(world.mission.id) == ()


def test_a_predicate_nobody_observes_is_unavailable_not_false() -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [])
    outcome = observe_predicate(index, signature.predicate_ref, {}, now_ms=1)
    assert outcome.observation.observer_id == NO_OBSERVER
    assert outcome.observation.polarity is None


def test_an_observer_that_raises_is_an_outage_not_a_polarity() -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [_FakeObserver("obs-1", ("demo.flag",), boom=True)])
    outcome = observe_predicate(index, signature.predicate_ref, {}, now_ms=1)
    assert outcome.reason is ReadinessReason.OBSERVER_UNAVAILABLE
    assert "RuntimeError" in outcome.observation.detail


def test_an_observed_record_is_stored_and_reported_ready(world: World) -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [_FakeObserver("obs-1", ("demo.flag",), answer=True)])
    results = gather(
        index, world.semantics, world.mission.id, [(signature.predicate_ref, {})], now_ms=7
    )
    assert results[0].recorded is True and results[0].reason is None
    assert len(world.semantics.list_observations(world.mission.id)) == 1


def test_one_unavailable_observer_does_not_stop_the_batch(world: World) -> None:
    registry, one = _registry_with("demo.flag")
    other = _register(registry, "demo.other")
    index = build_index(
        registry,
        [
            _FakeObserver("obs-1", ("demo.flag",), answer=True),
            _FakeObserver("obs-2", ("demo.other",), answer=None),
        ],
    )
    results = gather(
        index,
        world.semantics,
        world.mission.id,
        [(one.predicate_ref, {}), (other.predicate_ref, {})],
        now_ms=7,
    )
    assert [item.recorded for item in results] == [True, False]


def test_a_predicate_read_at_the_wrong_content_hash_is_unavailable() -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [_FakeObserver("obs-1", ("demo.flag",), answer=True)])
    wrong = VersionedRef(id="demo.flag", version=1, content_hash="b" * 64)
    outcome = observe_predicate(index, wrong, {}, now_ms=1)
    assert outcome.reason is ReadinessReason.OBSERVER_UNAVAILABLE


# ======================================================================================
# 12. legacy zero-regression, and two Missions in one library
# ======================================================================================


def test_a_legacy_mission_materialises_nothing_and_stays_legacy(tmp_path) -> None:
    legacy = build_world(tmp_path, mode=LEGACY_SEMANTICS, key="p23c-legacy")
    assert legacy.store.list_tasks(legacy.mission.id) == []
    assert legacy.events(OCCURRENCES_MATERIALISED) == []
    assert legacy.store.get_mission(legacy.mission.id).status is MissionStatus.PLANNING


def test_a_legacy_mission_is_refused_at_the_accept_door(tmp_path) -> None:
    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    legacy = build_world(tmp_path, mode=LEGACY_SEMANTICS, key="p23c-legacy2")
    with pytest.raises(ResolutionCommitRejected) as caught:
        legacy.service.record_delivery_receipt(
            legacy.mission.id,
            DeliveryReceipt(
                receipt_id="dlv-x",
                mission_id=legacy.mission.id,
                acceptance_id=AcceptanceId("acc-x"),
                stage=DeliveryStage.CONFIRMED,
                observed_at_ms=1,
                operation_id="op-x",
            ),
            command_id="cmd-x",
        )
    assert caught.value.reason == "SEMANTICS_NOT_HIERARCHICAL"


def test_two_missions_in_one_library_do_not_disturb_each_other(tmp_path) -> None:
    first = committed(tmp_path, key="p23c-a", name="shared.db")
    second_service = first.service
    mission, _ = second_service.create_mission(_spec("p23c-b", mode=LEGACY_SEMANTICS))
    assert second_service.store.list_tasks(mission.id) == []
    assert len(first.tasks()) == 3
    assert sum(1 for _ in first.events(OCCURRENCES_MATERIALISED)) == 1


def test_the_legacy_allocator_entry_is_still_the_one_without_bindings() -> None:
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._decide)
    assert "allocate_v2(" in source
    assert "plan = allocate(" in source


# ======================================================================================
# 13. one Orchestrator cycle over a hierarchical Mission
# ======================================================================================


def test_a_run_over_a_hierarchical_mission_does_not_complete_it_without_a_resolution(
    tmp_path,
) -> None:
    """The invariant, driven through the real loop rather than asserted on a gate."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    async def case() -> None:
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=5
        )
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(config, provider) as orchestrator:
            service = orchestrator.commit
            mission, _ = service.create_mission(_spec("p23c-run", mode=HIERARCHICAL_SEMANTICS))
            env = _env(mission.id)
            contract = _outer()
            assert env.admit(contract).admitted
            ObligationStore(orchestrator.store).register(
                Obligation(
                    obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
                    mission_id=mission.id,
                    requirement_refs=("req-1",),
                    goal_signature_id="plan.goal",
                ),
                recursion_fuel=FUEL,
            )
            semantics = HtnStore(orchestrator.store)
            semantics.put_task_semantics(
                mission.id,
                task_binding(
                    env,
                    "plan.goal",
                    task_id=ROOT_TASK,
                    obligation=ROOT_DUTY,
                    parameters={"subject": "alpha"},
                ),
            )
            semantics.register_method(contract, env.registry.registration(contract.method_ref()))
            # CREATED → ACTIVE is not an edge the Mission state machine has; PLANNING
            # is, and it is the one the loop itself takes before the first Planner
            # round.  A fixture that skipped it would leave the Mission in CREATED and
            # the loop would start planning a plan that is already committed.
            service.begin_planning(mission.id)
            dispatch = orchestrator.install_hierarchical(planning=env)
            assert dispatch.apply_planner_reply(
                mission.id,
                _proposal_text(contract),
                principal=PlanPrincipal("manager-1", "mission", 0),
                command_id="cmd-run",
            ).committed
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final.status is not MissionStatus.COMPLETED
            assert semantics.list_goal_resolutions(mission.id) == ()

    asyncio.run(case())


# ======================================================================================
# 14. mutation self-check
# ======================================================================================


def test_mutant_a_compound_given_a_primitives_share_would_overdraw_the_pool(
    world: World,
) -> None:
    """Handing every occurrence the same share double-counts the compound's subtree."""

    rows = world.tasks()
    mutated = sum(MISSION_TOKENS // 2 for _ in rows)
    assert mutated > MISSION_TOKENS
    real = sum(int(task.budget.max_tokens or 0) for task in rows.values())
    assert real <= MISSION_TOKENS


def test_mutant_an_equation_over_one_network_would_respend_the_pool_each_revision() -> None:
    """``committed_before`` is what makes the equation survive a second revision."""

    naive = Materialisation(pool_tokens=100, committed_tokens=0)
    assert naive.conservation()["holds"] is True
    honest = Materialisation(pool_tokens=100, committed_tokens=100)
    assert honest.conservation()["holds"] is True
    overdrawn = dataclasses.replace(honest, committed_tokens=101)
    assert overdrawn.conservation()["holds"] is False


def test_mutant_spelling_the_account_prefix_by_hand_finds_no_parent(world: World) -> None:
    from agent_orchestrator.governance.budgets import BudgetError

    with pytest.raises(BudgetError, match="unknown budget account"):
        world.service.ledger.open_account(
            account_id="task:mutant",
            scope="task",
            parent_id=f"mission:{world.mission.id}",
            mission_id=world.mission.id,
            limits=Budget(max_tokens=1),
        )


def test_mutant_allocating_on_the_ready_string_would_dispatch_the_data_consumer(
    live: World,
) -> None:
    """The legacy READY arithmetic has nothing to say about a DATA port."""

    rows = live.tasks()
    ready_by_status = {task.id for task in rows.values() if task.status is TaskStatus.READY}
    admitted = set(live.dispatch.admissions(live.mission.id).readiness)
    assert _review_task(live) in ready_by_status
    assert _review_task(live) not in admitted


def test_mutant_an_index_built_by_guessing_the_port_would_accept_anything(
    world: World,
) -> None:
    network = world.network()
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    guessed = _accepted_output(world, port="report")
    assert guessed.output_port not in declared_output_ports(network, producer)
    with pytest.raises(ContractError):
        check_declared(network, producer, [guessed])


def test_mutant_recording_whatever_came_back_would_store_an_outage(world: World) -> None:
    registry, signature = _registry_with("demo.flag")
    index = build_index(registry, [_FakeObserver("obs-1", ("demo.flag",), answer=None)])
    outcome = observe_predicate(index, signature.predicate_ref, {}, now_ms=1)
    # The mutant is "store outcome.observation whatever it says"; the real code can
    # not, because an unavailable observation carries no record at all.
    assert outcome.observation.record is None
    assert record_observation(world.semantics, world.mission.id, outcome).recorded is False


def test_mutant_taking_the_delivery_receipt_from_the_command_would_be_a_claim(
    world: World,
) -> None:
    import inspect

    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitsMixin

    source = inspect.getsource(ResolutionCommitsMixin._check_delivery)
    assert "find_delivery_receipt" in source
    assert "command.delivery_receipts" in source  # the ids, read back from the library


def test_mutant_completing_on_the_settled_flag_would_declare_the_mission_done() -> None:
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._decide)
    assert "_root_resolution_formed" in source
    formed = inspect.getsource(event_handler.Orchestrator._root_resolution_formed)
    assert "attempt_root_resolution" in formed
    assert "status" not in formed.split('"""')[2]


# ======================================================================================
# helpers
# ======================================================================================


def _leaf_task(world: World) -> str:
    return _task_of(world, "plan.leaf")


def _review_task(world: World) -> str:
    return _task_of(world, "plan.review")


def _task_of(world: World, signature: str) -> str:
    for spec in world.network().occurrences:
        binding = world.network().binding_for_occurrence(spec.occurrence_id)
        if str(binding.goal_signature.signature_id) == signature:
            return str(spec.task_id)
    raise AssertionError(f"no occurrence for {signature}")


def _unbounded():
    from agent_orchestrator.contracts import Mission

    return Mission(
        id="mission-unbounded",
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(),
        tenant_id="t",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key="unbounded",
    )


def _store_acceptance(world: World, acceptance_id: str) -> None:
    """A minimal stored ``Acceptance`` so the output index's foreign key holds.

    The index rows quote an acceptance by id and the schema enforces it: an accepted
    output of an acceptance nobody stored is not a record of anything.
    """

    import dataclasses as _dc

    from agent_orchestrator.contracts.evidence_state import Validity
    from agent_orchestrator.contracts.resolution import Acceptance, ReviewRecordId

    package = _dc.replace(_stub_package(world), package_id=f"pkg-{acceptance_id}")
    record = _dc.replace(
        _stub_record(world),
        record_id=ReviewRecordId(f"rec-{acceptance_id}"),
        package_id=package.package_id,
    )
    world.semantics.insert_review_package(package)
    world.semantics.insert_review_record(record, official=False)
    world.semantics.insert_acceptance(
        Acceptance(
            acceptance_id=AcceptanceId(acceptance_id),
            mission_id=world.mission.id,
            task_id=TaskRef(_leaf_task(world)),
            obligation_id=ROOT_DUTY,
            requirements_revision=1,
            contract_revision=1,
            input_manifest_hash=HEX_A,
            review_record_id=ReviewRecordId(record.record_id),
            accepted_at_ms=1,
            validity=Validity.CURRENT,
        )
    )


def _accepted_output(
    world: World, *, port: str = "result", schema: VersionedRef | None = None
) -> AcceptedOutput:
    network = world.network()
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    declared = declared_output_ports(network, producer)
    reference = (
        schema
        or declared.get(port)
        or VersionedRef(id="plan.result", version=1, content_hash=HEX_A)
    )
    return AcceptedOutput(
        producer_occurrence=producer,
        producer_task_ref=TaskRef(_leaf_task(world)),
        output_port=port,
        producer_result_id="result-1",
        acceptance_id="acc-leaf",
        support_revision=1,
        artifact_id="artifact-1",
        content_hash=HEX_A,
        schema_ref=reference,
        source_revision="rev-1",
        source_identity=ResourceIdentity(namespace="workspace:leaf", path="report.md"),
        disclosure=DisclosureState.DISCLOSABLE,
    )


def _noop_change():
    from agent_orchestrator.graph.changes import TaskGraphChange

    return TaskGraphChange(
        base_graph_version=1,
        basis={"trigger": "manual"},
        rationale="a Manager proposal that should never reach the legacy path",
        operations=(),
    )


def _root_command_with(world: World, **overrides: Any):
    from agent_orchestrator.contracts.resolution import GoalResolution, GoalResolutionId
    from agent_orchestrator.orchestrator.resolution_commits import CommitGoalResolutionCommand

    del GoalResolution, GoalResolutionId
    return CommitGoalResolutionCommand(
        command_id="cmd-root",
        mission_id=world.mission.id,
        resolution=_stub_resolution(world),
        package=_stub_package(world),
        record=_stub_record(world),
        requirements=_stub_requirements(world),
        witness_id="wit-root",
        independence=_independence(),
        posture=_posture(),
        read_set=_read_set(),
        decided_at_ms=1,
        **overrides,
    )


def _stub_resolution(world: World):
    from agent_orchestrator.contracts.evidence_state import Validity
    from agent_orchestrator.contracts.resolution import (
        CriterionVerdict,
        GoalResolution,
        GoalResolutionId,
        ResolutionCriterion,
        ReviewVerdict,
    )

    return GoalResolution(
        resolution_id=GoalResolutionId("res-stub"),
        mission_id=world.mission.id,
        obligation_id=ROOT_DUTY,
        goal_task_id=ROOT_TASK,
        requirements_version=1,
        contract_revision=1,
        method_instance_id=None,
        input_manifest_hash=HEX_A,
        artifact_refs=(),
        child_resolution_ids=(),
        criteria=(ResolutionCriterion(criterion_id="c-root", verdict=CriterionVerdict.PASS),),
        review_receipt_id="rec-stub",
        verdict=ReviewVerdict.ACCEPT,
        validity=Validity.CURRENT,
    )


def _stub_criterion():
    from agent_orchestrator.contracts.resolution import (
        Criterion,
        CriterionOrigin,
        EvaluationKind,
        RequiredEvidencePolicy,
        RequirementClass,
    )

    return Criterion(
        criterion_id="c-root",
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement="the root goal is satisfied",
        requirement_class=RequirementClass.REQUIRED_OUTCOME,
        evaluation_kind=EvaluationKind.DETERMINISTIC,
        required_evidence_policy=RequiredEvidencePolicy(required_check_ids=("root-suite",)),
    )


def _stub_requirements(world: World):
    from agent_orchestrator.contracts.resolution import (
        CriterionExpr,
        RequirementsRevision,
        RequirementsRevisionId,
    )

    return RequirementsRevision(
        revision_id=RequirementsRevisionId("req-1"),
        mission_id=world.mission.id,
        revision=1,
        criteria=(_stub_criterion(),),
        success_expression=CriterionExpr("c-root"),
    )


def _stub_package(world: World):
    from agent_orchestrator.contracts.resolution import (
        CriterionExpr,
        ReviewBinding,
        ReviewPackage,
        ReviewPurpose,
    )

    binding = ReviewBinding(
        mission_id=world.mission.id,
        obligation_id=ROOT_DUTY,
        subject_ref=TypedRef(kind=TypedRefKind.TASK, id=ROOT_TASK, revision=1, content_hash=HEX_A),
        requirements_revision=1,
        input_manifest_hash=HEX_A,
        policy_ref=TypedRef(
            kind=TypedRefKind.REQUIREMENTS, id="policy-1", revision=1, content_hash=HEX_A
        ),
    )
    return ReviewPackage(
        package_id="pkg-stub",
        purpose=ReviewPurpose.MISSION_FINAL,
        binding=binding,
        criteria=(_stub_criterion(),),
        success_expression=CriterionExpr("c-root"),
    )


def _stub_record(world: World):
    from agent_orchestrator.contracts.resolution import (
        CheckExecution,
        CriterionOutcome,
        CriterionVerdict,
        ReviewRecord,
        ReviewRecordId,
        ReviewVerdict,
    )

    package = _stub_package(world)
    return ReviewRecord(
        record_id=ReviewRecordId("rec-stub"),
        package_id=package.package_id,
        purpose=package.purpose,
        binding=package.binding,
        reviewer_agent_id="agent-reviewer",
        reviewer_turn_id="turn-1",
        evidence_manifest_hash=HEX_A,
        criteria=(
            CriterionOutcome(
                criterion_id="c-root",
                verdict=CriterionVerdict.PASS,
                check_execution=CheckExecution.SUCCEEDED,
            ),
        ),
        verdict=ReviewVerdict.ACCEPT,
    )


def _independence():
    from agent_orchestrator.verification.acceptance_rules import IndependenceFacts

    return IndependenceFacts()


def _posture():
    from agent_orchestrator.verification.acceptance_rules import ExecutionPosture

    return ExecutionPosture()


def _read_set():
    from agent_orchestrator.contracts.htn import SemanticReadSet

    return SemanticReadSet(requirements_revision=1)


def _registry_with(predicate_id: str):
    registry = PredicateRegistry()
    signature = _register(registry, predicate_id)
    return registry, signature


def _register(registry: PredicateRegistry, predicate_id: str):
    from agent_orchestrator.knowledge.predicates import PredicateSignature, WorldAssumption

    signature = PredicateSignature(
        predicate_ref=VersionedRef(id=predicate_id, version=1, content_hash=HEX_A),
        parameters=(),
        world_assumption=WorldAssumption.OPEN,
        observer_ids=("obs-1", "obs-2"),
    )
    registry.register(signature)
    return signature


@dataclass
class _FakeObserver:
    """A scripted observer: answers TRUE, answers nothing, or blows up."""

    _observer_id: str
    _predicates: tuple[str, ...]
    answer: bool | None = None
    boom: bool = False

    @property
    def observer_id(self) -> str:
        return self._observer_id

    def predicate_ids(self) -> tuple[str, ...]:
        return tuple(self._predicates)

    def observe(self, signature, arguments, *, now_ms: int) -> Observation:
        del arguments
        if self.boom:
            raise RuntimeError("the reader fell over")
        predicate = str(signature.predicate_ref.id)
        if self.answer is None:
            return unavailable(self._observer_id, predicate, "service unreachable")
        record = ObservationRecord(
            observation_id=f"obs-{predicate}-{now_ms}",
            proposition_key=content_hash_of({"predicate": predicate}),
            polarity=self.answer,
            source_ref=TypedRef(
                kind=TypedRefKind.OBSERVATION, id=self._observer_id, revision=1, content_hash=HEX_A
            ),
            coverage=QueryCompleteness.BEST_EFFORT,
            observer_id=self._observer_id,
            observed_at_ms=now_ms,
            recorded_at_ms=now_ms,
        )
        return Observation(
            outcome=ObservationOutcome.OBSERVED,
            observer_id=self._observer_id,
            predicate_id=predicate,
            record=record,
        )


def test_the_complete_coverage_constant_is_the_only_one_that_backs_a_denial() -> None:
    assert COMPLETE_COVERAGE is QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    assert occurrence_tasks.MIN_TOKEN_SHARE >= 1


# ======================================================================================
# 15. the leaf acceptance chain (P2.3c part 2b)
# ======================================================================================
#
# Part 2 stopped at "``accept_review`` exists".  Nothing turned a *verified result*
# into the anchors it quotes, so ``acceptance_outputs`` stayed empty and the DATA
# consumer of §5 could never leave ``WAITING_DATA``.  This section is the chain:
# verdict → requirements / package / record / witness → ``Acceptance`` → the output
# index → a consumer whose manifest finally freezes.


@dataclass(frozen=True)
class _Artifact:
    id: str
    path: str
    content_hash: str = HEX_A
    version: str = "1"


def _passing_layers():
    from agent_orchestrator.orchestrator.leaf_acceptance import LayerOutcome

    return (
        LayerOutcome("schema_check", "PASS"),
        LayerOutcome("rule_check", "PASS"),
        LayerOutcome("critic_review", "PASS"),
    )


def _assembly(world: World):
    from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly

    return LeafAcceptanceAssembly(world.store, world.service, dispatch=world.dispatch)


def _accept_leaf(
    world: World,
    *,
    layers=None,
    artifacts=None,
    result_id: str = "result-1",
    now_ms: int = 1_000_000,
    port_claims=None,
    task_id: str | None = None,
):
    """Accept one leaf, stating which file went to which declared port.

    P2.3c part 2d, decision 4: the port↔artifact pairing is *declared* by the Worker,
    never derived from the path.  This helper stands in for that declaration — it
    claims the plan's declared ports in order over the supplied artifacts, which is
    what a Worker writing a single output would have said — and any test that cares
    about the claim itself passes ``port_claims`` explicitly.
    """

    from agent_orchestrator.runtime.output_blocks import PortClaim

    leaf = _leaf_task(world) if task_id is None else task_id
    items = tuple(
        artifacts if artifacts is not None else (_Artifact("artifact-1", "out/result.json"),)
    )
    claims = port_claims
    if claims is None:
        declared = world.dispatch.declared_output_ports_for(world.mission.id, leaf)
        claims = tuple(
            PortClaim(port_key=item["port"], path=items[index].path)
            for index, item in enumerate(declared)
            if index < len(items)
        )
    return _assembly(world).accept(
        world.mission.id,
        leaf,
        result_id=result_id,
        layers=layers if layers is not None else _passing_layers(),
        artifacts=items,
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=now_ms,
        port_claims=claims,
    )


def test_a_verified_leaf_becomes_a_committed_acceptance(live: World) -> None:
    receipt = _accept_leaf(live)
    assert str(receipt.acceptance.task_id) == _leaf_task(live)
    stored = live.semantics.get_acceptance(receipt.acceptance_id)
    assert stored.to_json() == receipt.acceptance.to_json()


def test_a_revoked_leaf_can_be_accepted_again(live: World) -> None:
    """Review P0-2: rework after a revocation has to be able to land.

    The ACCEPT licence used to be named ``hash(task, now_ms)`` while its row's unique
    key carried no clock, so the second acceptance of one leaf minted a new id onto
    the old key.  ``get_validity_witness`` missed it, the insert hit the index, and
    the ``StoreError`` escaped ``accept()`` into ``_accept_hierarchical_leaf``, which
    records "acceptance refused" — for ever, because the observation count that feeds
    ``support_revision`` does not move during execution.  A revoked leaf could then
    never be re-accepted and ``_require_accepted_work`` could never be satisfied.

    **Mutation**: drop the acceptance from the ACCEPT witness's ``support_refs`` (so
    both licences fall under the empty subject and share one key again) and the
    subject assertion below goes red — there is one licence where there are two
    acceptances, and the second acceptance is standing on the first one's permission.
    """

    first = _accept_leaf(live)
    _revoke(live, str(first.acceptance_id))
    second = _accept_leaf(live, result_id="result-2", now_ms=1_100_000)
    assert str(second.acceptance_id) != str(first.acceptance_id)
    assert live.semantics.get_acceptance(second.acceptance_id).validity is Validity.CURRENT
    # Two acceptances, two licences: each names the acceptance it was taken over.
    licences = [
        item
        for item in live.semantics.list_validity_witnesses(live.mission.id)
        if item.purpose is WitnessPurpose.ACCEPT
    ]
    assert {witness_subject(item) for item in licences} == {
        f"acceptance:{first.acceptance_id}",
        f"acceptance:{second.acceptance_id}",
    }


def test_the_accept_licence_is_named_after_the_key_it_occupies(live: World) -> None:
    """Review P0-2, stated as the property rather than as a symptom.

    The row's identity is (consumer, purpose, scope, epoch, support revision,
    subject); the id is a digest of exactly those, so "already stored" is a keyed
    read and can never disagree with the index.  Replaying the same acceptance is
    therefore one licence, not a refusal.
    """

    from agent_orchestrator.contracts.semantic_base import content_hash_of

    first = _accept_leaf(live)
    again = _accept_leaf(live)
    assert str(again.acceptance_id) == str(first.acceptance_id)
    licences = [
        item
        for item in live.semantics.list_validity_witnesses(live.mission.id)
        if item.purpose is WitnessPurpose.ACCEPT
    ]
    assert len(licences) == 1
    held = licences[0]
    assert (
        held.witness_id
        == "wit-"
        + content_hash_of(
            {
                "consumer": str(held.consumer_ref.id),
                "purpose": str(WitnessPurpose.ACCEPT),
                "scope": held.scope_id,
                "epoch": int(held.scope_epoch),
                "support_revision": int(held.support_revision),
                "subject": witness_subject(held),
            }
        )[:32]
    ), "the id carries the key and nothing else — no clock"


def test_the_review_anchors_are_frozen_in_the_store_before_the_command(live: World) -> None:
    """AER §7: the commit re-reads both and compares content hashes."""

    receipt = _accept_leaf(live)
    package_id = live.events("AcceptanceCommitted")[0].payload["review_package_id"]
    package = live.semantics.get_review_package(package_id)
    official = live.semantics.official_review_record(package_id)
    assert official is not None
    assert official.record_id == receipt.acceptance.review_record_id
    assert package.requirements_content_hash is not None


def test_each_criterion_is_gated_on_the_layers_that_actually_ran(live: World) -> None:
    from agent_orchestrator.orchestrator.leaf_acceptance import LayerOutcome, check_ids

    layers = (*_passing_layers(), LayerOutcome("action_gate", "ERROR"))
    assert check_ids(layers) == ("critic_review", "rule_check", "schema_check")
    _accept_leaf(live, layers=layers)
    revision = live.semantics.latest_requirements_revision(live.mission.id)
    assert revision is not None
    gates = {
        item.criterion_id: item.required_evidence_policy.required_check_ids
        for item in revision.criteria
    }
    expected = {"critic_review", "rule_check", "schema_check"}
    assert all(set(value) == expected for value in gates.values())


def test_a_failing_layer_is_never_turned_into_an_acceptance(live: World) -> None:
    from agent_orchestrator.orchestrator.leaf_acceptance import LayerOutcome
    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    with pytest.raises(ResolutionCommitRejected) as refused:
        _accept_leaf(live, layers=(LayerOutcome("rule_check", "FAIL"),))
    assert refused.value.reason == "NOT_ACCEPTABLE"
    assert live.semantics.list_acceptances(live.mission.id) == ()


def test_a_layer_that_could_not_run_is_not_a_passed_check(live: World) -> None:
    from agent_orchestrator.orchestrator.leaf_acceptance import LayerOutcome
    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    with pytest.raises(ResolutionCommitRejected):
        _accept_leaf(live, layers=(LayerOutcome("rule_check", "ERROR"),))


def test_a_compound_goal_is_never_accepted_by_a_review_of_its_own(live: World) -> None:
    with pytest.raises(ContractError, match="commit_goal_resolution"):
        _assembly(live).accept(
            live.mission.id,
            ROOT_TASK,
            result_id="result-root",
            layers=_passing_layers(),
            now_ms=1_000_000,
        )


def test_the_acceptance_writes_the_output_index_at_the_declared_port(live: World) -> None:
    receipt = _accept_leaf(live)
    rows = live.semantics.list_acceptance_outputs(live.mission.id)
    assert len(rows) == 1
    assert rows[0]["output_port"] == "result"
    assert rows[0]["artifact_id"] == "artifact-1"
    assert rows[0]["acceptance_id"] == receipt.acceptance_id
    assert rows[0]["producer_occurrence"] == live.occurrence_of(_leaf_task(live))


def test_the_indexed_schema_is_the_edges_and_not_the_producers_claim(live: World) -> None:
    _accept_leaf(live)
    row = live.semantics.list_acceptance_outputs(live.mission.id)[0]
    producer = OccurrenceId(live.occurrence_of(_leaf_task(live)))
    declared = declared_output_ports(live.network(), producer)
    assert row["schema_ref"] == declared["result"].to_json()


def test_an_undeclared_port_is_refused_and_writes_no_acceptance(live: World) -> None:
    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    assembly = _assembly(live)
    with pytest.raises(ResolutionCommitRejected) as refused:
        assembly.accept(
            live.mission.id,
            _leaf_task(live),
            result_id="result-2",
            layers=_passing_layers(),
            artifacts=(_Artifact("artifact-9", "out/result.json"),),
            producer_agent_ids=("agent-worker",),
            reviewer_agent_id="agent-critic",
            now_ms=1_000_000,
            # The command is built by the assembly; the refusal is provoked by
            # renaming the port on the way in, which is what a producer relabelling
            # its own output looks like from the Commit's side.
            namespace="workspace",
        ) if False else _accept_with_port(assembly, live, "verdict")
    assert refused.value.reason == "OUTPUT_NOT_DECLARED"
    assert live.semantics.list_acceptances(live.mission.id) == ()
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()


def _accept_with_port(assembly, world: World, port: str):
    """Drive the same assembly but state a port the plan does not declare."""

    import agent_orchestrator.orchestrator.leaf_acceptance as module

    original = module.accepted_outputs_for

    def relabelled(ports, **kwargs):
        outputs = original(ports, **kwargs)
        return tuple(dataclasses.replace(item, output_port=port) for item in outputs)

    from agent_orchestrator.runtime.output_blocks import PortClaim

    module.accepted_outputs_for = relabelled
    try:
        return assembly.accept(
            world.mission.id,
            _leaf_task(world),
            result_id="result-2",
            layers=_passing_layers(),
            artifacts=(_Artifact("artifact-9", "out/result.json"),),
            producer_agent_ids=("agent-worker",),
            reviewer_agent_id="agent-critic",
            now_ms=1_000_000,
            # An honest claim on the way in; the relabelling above is what renames the
            # port afterwards, which is a producer relabelling its own output.
            port_claims=(PortClaim(port_key="result", path="out/result.json"),),
        )
    finally:
        module.accepted_outputs_for = original


# ================================================= part 2d, decision 4: declared ports
def test_the_port_a_claim_names_is_the_port_the_artifact_is_filed_at(live: World) -> None:
    """Part 2c's smoke round 9, as a test.

    Two files, one declared port, and no name in common between them.  The old rule
    paired by substring or by "one port and one file", so this either mis-filed the
    wrong artifact or filed nothing and left the consumer in ``WAITING_DATA`` for
    ever.  The claim says which file it is, and that is the whole rule.
    """

    from agent_orchestrator.runtime.output_blocks import PortClaim

    receipt = _accept_leaf(
        live,
        artifacts=(
            _Artifact("artifact-a", "notes/scratch.md"),
            _Artifact("artifact-b", "build/report-2026.json"),
        ),
        port_claims=(PortClaim(port_key="result", path="build/report-2026.json"),),
    )
    rows = live.semantics.list_acceptance_outputs(live.mission.id)
    assert [(row["output_port"], row["artifact_id"]) for row in rows] == [("result", "artifact-b")]
    assert rows[0]["acceptance_id"] == receipt.acceptance_id
    # And the consumer is licensed to run on it.
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_000_000)
    assert live.dispatch.input_witnesses(live.mission.id, _review_task(live))


def test_an_unclaimed_extra_artifact_is_kept_as_evidence_and_not_indexed(live: World) -> None:
    """A debug file or a log is a normal thing to produce; it is not an output.

    §10.2 forbids choosing between two hashes for one destination — it does not
    forbid producing files nobody asked for.  They stay artifacts (evidence of the
    run) and simply do not enter the index, and the acceptance is **not** refused.
    """

    from agent_orchestrator.runtime.output_blocks import PortClaim

    _accept_leaf(
        live,
        artifacts=(
            _Artifact("artifact-a", "out/result.json"),
            _Artifact("artifact-spare", "out/debug.log"),
        ),
        port_claims=(PortClaim(port_key="result", path="out/result.json"),),
    )
    rows = live.semantics.list_acceptance_outputs(live.mission.id)
    assert [row["artifact_id"] for row in rows] == ["artifact-a"]
    assert live.semantics.list_acceptances(live.mission.id)


def test_a_required_consumed_port_nobody_claimed_refuses_the_acceptance(live: World) -> None:
    """TG §4.3: an unprovable binding is refused, never left empty.

    Leaving it empty is what part 2c did, and the consequence was a Mission that sat
    in ``WAITING_DATA`` until an operator killed it.  The refusal is recorded and
    does *not* undo the verification: the verdict is a fact that happened, and this
    is §9.1's "content defect → a new content attempt against the same duty".
    """

    from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

    with pytest.raises(ResolutionCommitRejected) as refused:
        _accept_leaf(live, port_claims=())
    assert refused.value.reason == "OUTPUT_PORT_UNCLAIMED"
    assert "result" in str(refused.value)
    assert live.semantics.list_acceptances(live.mission.id) == ()
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()


def test_a_port_with_no_consumer_needs_no_claim(live: World) -> None:
    """Nothing consumes the review leaf, so it declares no port and claims none."""

    assert live.dispatch.declared_output_ports_for(live.mission.id, _review_task(live)) == ()
    _accept_leaf(
        live,
        task_id=_review_task(live),
        result_id="result-review",
        artifacts=(_Artifact("artifact-2", "out/verdict.json"),),
    )
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()


def test_no_port_is_ever_derived_from_an_artifact_path(live: World) -> None:
    """Decision 4's mutation self-check, and review P1-5's defect in one test.

    The old fallbacks were "the port name appears somewhere in the path" (so a port
    called ``facts`` claimed ``artifacts/anything.json`` — the reviewer's probe) and
    "one port, one artifact, so they go together".  Both are the guess TG design
    §10.2 forbids.

    **Mutation**: put either fallback back into ``accepted_outputs_for`` and let the
    claim list be empty.  The first assertion below goes red (a port appears out of
    nowhere) and so does
    ``test_a_required_consumed_port_nobody_claimed_refuses_the_acceptance``
    (a refusal turns into a pass).
    """

    import inspect

    from agent_orchestrator.contracts.htn import OccurrenceId, TaskRef
    from agent_orchestrator.orchestrator import leaf_acceptance as module

    assert (
        module.accepted_outputs_for(
            {"facts": _schema_of(live)},
            occurrence=OccurrenceId(live.occurrence_of(_leaf_task(live))),
            task_id=TaskRef(_leaf_task(live)),
            result_id="result-x",
            acceptance_id="acc-x",
            support_revision=0,
            artifacts=(_Artifact("artifact-z", "artifacts/unrelated-output.json"),),
            namespace="workspace",
            claims=(),
        )
        == ()
    ), "no claim, no index entry — a path is not a port"
    assert "_port_for" not in inspect.getsource(module), "the guessing helper is gone"


def _schema_of(world: World):
    producer = OccurrenceId(world.occurrence_of(_leaf_task(world)))
    return declared_output_ports(world.network(), producer)["result"]


def test_a_leaf_whose_output_nobody_consumes_indexes_nothing(live: World) -> None:
    """§24.1 decision 4: an index entry exists only where the plan drew an edge."""

    _assembly(live).accept(
        live.mission.id,
        _review_task(live),
        result_id="result-review",
        layers=_passing_layers(),
        artifacts=(_Artifact("artifact-2", "out/verdict.json"),),
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=1_000_000,
    )
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()


def test_the_recorded_output_reaches_the_data_consumer(live: World) -> None:
    """The whole point of the chain: ``_recorded_outputs`` finally has something."""

    before = live.dispatch.admissions(live.mission.id)
    assert before.refusal_for(_review_task(live)).reason is ReadinessReason.WAITING_DATA
    _accept_leaf(live)
    outputs = live.dispatch._recorded_outputs(live.mission.id, live.network())
    assert [item.output_port for item in outputs] == ["result"]
    assert outputs[0].artifact_id == "artifact-1"


def test_a_replay_of_the_same_acceptance_does_not_double_write_the_index(live: World) -> None:
    _accept_leaf(live)
    _accept_leaf(live)
    assert len(live.semantics.list_acceptance_outputs(live.mission.id)) == 1
    assert len(live.semantics.list_acceptances(live.mission.id)) == 1


# ======================================================================================
# 16. a shared read-only sub-goal is executed once (P2.3c part 2b)
# ======================================================================================
#
# §8.3: two slots may bind one goal occurrence.  Part 2 asserted that at the
# *compiler* level only; what a deployment cares about is that the shared occurrence
# becomes one Task row, is dispatched once, and that its single acceptance feeds
# both consumers — which needed the occurrence→Task bridge and the output index to
# exist before it could be stated at all.


def _outer_shared():
    """root → leaf (read-only) → {review, audit}, both consuming ``leaf.result``."""

    return method(
        "plan.outer-shared",
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
            step(
                "audit",
                "plan.audit",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"), ("c-root", "audit", "c-audited")),
        finalizer="review",
    )


@pytest.fixture
def shared(tmp_path) -> World:
    world = build_world(tmp_path, key="p23c-shared")
    contract = _outer_shared()
    receipt = world.env.admit(contract)
    assert receipt.admitted, receipt.problems
    HtnStore(world.store).register_method(
        contract, world.env.registry.registration(contract.method_ref())
    )
    world = dataclasses.replace(world, contract=contract)
    outcome = world.plan()
    assert outcome.committed, outcome.last_reason
    world.admit_demand()
    return world


def test_the_shared_producer_is_one_occurrence_with_two_consumers(shared: World) -> None:
    network = shared.network()
    producer = OccurrenceId(shared.occurrence_of(_leaf_task(shared)))
    consumers = {
        str(item.consumer_occurrence)
        for item in network.data_requirements
        if item.producer_occurrence == producer
    }
    assert len(consumers) == 2
    assert sum(1 for spec in network.occurrences if spec.occurrence_id == producer) == 1


def test_the_shared_producer_materialises_exactly_one_task_row(shared: World) -> None:
    rows = shared.tasks()
    assert len(rows) == 4  # root + leaf + review + audit
    assert sum(1 for task_id in rows if task_id == _leaf_task(shared)) == 1


def test_only_the_shared_producer_is_dispatchable_before_it_is_accepted(shared: World) -> None:
    admissions = shared.dispatch.admissions(shared.mission.id)
    assert set(admissions.readiness) == {_leaf_task(shared)}
    waiting = {
        str(item.task_id)
        for item in admissions.refusals
        if item.reason is ReadinessReason.WAITING_DATA
    }
    assert len(waiting) == 2


def test_one_acceptance_of_the_shared_producer_feeds_both_consumers(shared: World) -> None:
    """Executed once, indexed once, read twice — the whole point of sharing."""

    _accept_leaf(shared)
    rows = shared.semantics.list_acceptance_outputs(shared.mission.id)
    assert len(rows) == 1
    outputs = shared.dispatch._recorded_outputs(shared.mission.id, shared.network())
    assert len(outputs) == 1
    network = shared.network()
    issued = shared.dispatch.issue_input_witnesses(shared.mission.id, network, now_ms=1_000_000)
    assert len(issued) == 2  # one licence per consumer, over one acceptance
    resolved = [
        shared.dispatch.resolved_inputs(shared.mission.id, network, spec)
        for spec in network.occurrences
        if str(spec.task_id) not in {ROOT_TASK, _leaf_task(shared)}
    ]
    assert len(resolved) == 2
    assert all(item.manifest is not None and item.manifest.is_frozen for item in resolved)
    assert {binding.artifact_id for item in resolved for binding in item.manifest.bindings} == {
        "artifact-1"
    }


def test_a_second_acceptance_of_the_shared_producer_is_not_a_second_execution(
    shared: World,
) -> None:
    _accept_leaf(shared)
    _accept_leaf(shared)
    assert len(shared.semantics.list_acceptances(shared.mission.id)) == 1
    assert len(shared.semantics.list_acceptance_outputs(shared.mission.id)) == 1


def test_the_consumer_becomes_dispatchable_once_the_licence_is_issued(live: World) -> None:
    """The last link: accepted output + START witness → the DATA gate opens.

    Issuing the witness is I19's "recompute rather than reuse the old TRUE" and it is
    what ``_decide`` now does before reading readiness — without it the consumer sat
    in ``WAITING_DATA`` no matter what the deployment had accepted.
    """

    assert (
        live.dispatch.admissions(live.mission.id).refusal_for(_review_task(live)).reason
        is ReadinessReason.WAITING_DATA
    )
    _accept_leaf(live)
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_000_000)
    admissions = live.dispatch.admissions(live.mission.id)
    assert _review_task(live) in admissions.readiness


def test_a_witness_for_another_consumer_does_not_license_this_one(live: World) -> None:
    """§11.5: a witness is not a transferable token."""

    _accept_leaf(live)
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_000_000)
    held = live.dispatch.input_witnesses(live.mission.id, _review_task(live))
    assert held and all(witness.consumer_ref.id == _review_task(live) for witness in held.values())
    assert live.dispatch.input_witnesses(live.mission.id, ROOT_TASK) == {}


def test_the_decide_loop_issues_the_licence_before_it_reads_readiness() -> None:
    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._decide)
    issued = source.index("issue_input_witnesses")
    read = source.index("new_mode.admissions(mission.id)")
    assert issued < read


# ======================================================================================
# 17. P2.3c part 2c — the independent review's findings, as behaviour
#
# Each test below is one finding.  Where the review's own probe asserted a *spelling*
# (F9), the assertion here is what the code does; where it found a gate that could be
# absent (F1) or a path nobody had executed (F6), the test drives the real thing.
# ======================================================================================


def _unassembled(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS):
    """A committed hierarchical plan on an Orchestrator with **no** assembly installed."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = committed(evidence, key=f"p23c-bare-{mode}", mode=mode, demand=True)
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)
    return world, Orchestrator(config, RoleScriptedProvider({"planner": []}))


def test_a_hierarchical_mission_with_no_assembly_creates_no_attempt(tmp_path) -> None:
    """Review F1: fail-closed.  The gates are not installed, so nothing is dispatched.

    Before this, ``_new_mode`` answered None for "no assembly" exactly as it does for
    "legacy Mission", and ``_decide`` handed the occurrence rows to the legacy
    ``allocate()`` — which grants on the ``TaskStatus.READY`` string and would have
    dispatched the DATA consumer before its producer was ever accepted.
    """

    world, orchestrator = _unassembled(tmp_path)

    async def case() -> None:
        async with orchestrator as loop:
            mission = loop.store.get_mission(world.mission.id)
            assert is_hierarchical(mission)
            assert loop.hierarchical is None
            assert await loop._decide(mission) is False
            tasks = loop.store.list_tasks(mission.id)
            assert tasks, "the plan did commit rows; the refusal is about dispatching them"
            assert [a for task in tasks for a in loop.store.list_attempts(task.id)] == []

    asyncio.run(case())


def test_the_missing_assembly_is_recorded_once_for_the_mission(tmp_path) -> None:
    """A deployment fact, so it is said once and not every cycle (review F1)."""

    world, orchestrator = _unassembled(tmp_path)

    async def case() -> None:
        async with orchestrator as loop:
            mission = loop.store.get_mission(world.mission.id)
            for _ in range(3):
                assert await loop._decide(mission) is False
            events = [
                event
                for event in loop.store.list_events(mission.id)
                if event.type == ASSEMBLY_MISSING
            ]
            assert len(events) == 1
            assert events[0].payload["semantics"] == HIERARCHICAL_SEMANTICS
            assert "install_hierarchical" in events[0].payload["detail"]

    asyncio.run(case())


def test_the_decide_gate_refuses_before_the_legacy_allocator_is_consulted(
    tmp_path, monkeypatch
) -> None:
    """Review P2-9 (mutation M02): the ``at="decide"`` half of the fail-closed rule.

    Deleting the guard at the top of ``_decide`` left the whole suite green, because
    ``_next_attempt``'s own guard still refused every dispatch.  Behaviourally that is
    still fail-closed — but the property the guard exists for is that a hierarchical
    Mission with no assembly never reaches the **legacy allocator** at all, and that
    the refusal is recorded at the place it happened.  Neither had a test.
    """

    from agent_orchestrator.orchestrator import event_handler as module

    world, orchestrator = _unassembled(tmp_path)
    consulted: list[str] = []
    monkeypatch.setattr(
        module,
        "allocate",
        lambda *args, **kwargs: (
            consulted.append("allocate")
            or (_ for _ in ()).throw(
                AssertionError("the legacy allocator was consulted for a hierarchical Mission")
            )
        ),
    )

    async def case() -> None:
        async with orchestrator as loop:
            mission = loop.store.get_mission(world.mission.id)
            assert await loop._decide(mission) is False
            assert consulted == []
            events = [
                event
                for event in loop.store.list_events(mission.id)
                if event.type == ASSEMBLY_MISSING
            ]
            assert [event.payload["at"] for event in events] == ["decide"]

    asyncio.run(case())


def test_the_missing_assembly_also_closes_the_direct_dispatch_entry(tmp_path) -> None:
    """``_next_attempt`` has callers other than ``_decide`` (review F1)."""

    world, orchestrator = _unassembled(tmp_path)

    async def case() -> None:
        async with orchestrator as loop:
            mission = loop.store.get_mission(world.mission.id)
            task = next(item for item in loop.store.list_tasks(mission.id) if item.id != ROOT_TASK)
            assert await loop._next_attempt(mission, task, ()) is False
            assert loop.store.list_attempts(task.id) == []

    asyncio.run(case())


def test_a_legacy_mission_on_the_same_bare_orchestrator_is_untouched(tmp_path) -> None:
    """The refusal separates two worlds ``_new_mode`` used to answer None for."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    legacy = build_world(evidence, mode=LEGACY_SEMANTICS, key="p23c-bare-legacy")
    mission_id = legacy.mission.id
    legacy.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case() -> None:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            mission = loop.store.get_mission(mission_id)
            await loop._decide(mission)
            events = loop.store.list_events(mission_id)
            assert [item for item in events if item.type == ASSEMBLY_MISSING] == []

    asyncio.run(case())


# --------------------------------------------------------------------------------------
# F7: budget conservation across two refinement rounds
# --------------------------------------------------------------------------------------


def _two_level_env(mission: str) -> Env:
    env = _env(mission)
    env.register_type(
        "plan.sub",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-sub",),
        domain="plan",
    )
    env.register_type(
        "plan.work",
        parameters=(("subject", "string"),),
        criteria=("c-work",),
        capabilities=("plan.read",),
        domain="plan",
    )
    return env


def _outer_with_compound():
    """root → {leaf (primitive), sub (compound)} — the compound is refined next round."""

    return method(
        "plan.outer-nested",
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step("sub", "plan.sub", TaskForm.COMPOUND, {"subject": param("subject")}),
        ),
        # The root's criterion is carried by the *primitive* step: a compound that is
        # later refined leaves the execution projection, and a coverage claim resting
        # on it would be lost the moment round two expands it.
        links=(("c-root", "leaf", "c-leaf-done"),),
        finalizer="leaf",
    )


def _inner_two_leaves():
    """sub → {work-a, work-b}, the children round two has to be able to pay for."""

    return method(
        "plan.inner",
        "plan.sub",
        parameter_schema="plan.sub.params",
        steps=(
            step(
                "work-a",
                "plan.work",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "work-b",
                "plan.work",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-sub", "work-a", "c-work"), ("c-sub", "work-b", "c-work")),
        finalizer="work-b",
    )


@dataclass
class _TwoRounds:
    world: World
    inner: Any

    @property
    def conservation(self) -> list[dict[str, Any]]:
        return [
            event.payload["budget_conservation"]
            for event in self.world.events(PLAN_REVISION_COMMITTED)
        ]

    def granted(self) -> int:
        return sum(int(task.budget.max_tokens or 0) for task in self.world.tasks().values())


@pytest.fixture
def two_rounds(tmp_path) -> _TwoRounds:
    """One Mission, two refinement rounds: the only world where §21.5 has a second half.

    Review F7.  Every world in this suite refined the root once and stopped, so
    ``reserved_subtrees`` was always 0 on the integration path and the conservation
    equation's whole point — that round two is funded out of what round one *left* —
    had no witness.  Mutating ``committed`` to 0 left all 2329 tests green.
    """

    world = build_world(tmp_path, key="p23c-two-rounds")
    env = _two_level_env(world.mission.id)
    outer, inner = _outer_with_compound(), _inner_two_leaves()
    for contract in (outer, inner):
        receipt = env.admit(contract)
        assert receipt.admitted, receipt.problems
        HtnStore(world.store).register_method(
            contract, env.registry.registration(contract.method_ref())
        )
    world = dataclasses.replace(world, env=env, contract=outer)
    world.dispatch.planning = env
    assert world.plan(command_id="cmd-round-1").committed
    return _TwoRounds(world=world, inner=inner)


def _refine_child(rounds: _TwoRounds) -> Any:
    """Round two: refine the compound child the first round put on the board."""

    world = rounds.world
    network = world.network()
    child = next(
        spec
        for spec in network.occurrences
        if str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id)
        == "plan.sub"
    )
    reference = rounds.inner.method_ref()
    text = _proposal_text(
        rounds.inner,
        proposal_id="prop-2",
        expected_plan_revision=int(network.plan_revision),
        operations=[
            {
                "op": "refine",
                "goal_id": str(child.task_id),
                "obligation_id": str(child.obligation_id),
                "method_ref": {
                    "id": reference.method_id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            }
        ],
    )
    return world.plan(text, command_id="cmd-round-2")


def test_the_first_round_holds_a_share_for_the_compound_nobody_refined(
    two_rounds: _TwoRounds,
) -> None:
    equation = two_rounds.conservation[0]
    assert equation["reserved_subtrees"] == 1
    assert equation["funded_now"] == 1  # only the primitive leaf is funded this round
    assert equation["committed_tokens"] == 0
    assert equation["holds"] is True


def test_the_second_round_is_funded_out_of_what_the_first_one_left(
    two_rounds: _TwoRounds,
) -> None:
    """The equation is stated over the *store*, not over this network (§21.5)."""

    first = two_rounds.conservation[0]
    outcome = _refine_child(two_rounds)
    assert outcome.committed, outcome.last_reason
    second = two_rounds.conservation[1]
    assert second["committed_tokens"] == first["granted_tokens"]
    assert second["funded_now"] == 2
    assert second["reserved_subtrees"] == 0
    assert second["share_tokens"] < first["share_tokens"]


def test_two_rounds_never_grant_more_than_the_pool(two_rounds: _TwoRounds) -> None:
    """The mutant the review found alive: ``committed = 0`` overdraws here by half a pool."""

    pool = task_pool_tokens(two_rounds.world.mission)
    assert _refine_child(two_rounds).committed
    assert two_rounds.granted() <= pool
    equations = two_rounds.conservation
    assert sum(item["granted_tokens"] for item in equations) <= pool
    # Had the equation been written over this network alone, round two would have
    # divided the *whole* pool again and the sum would be half a pool over.
    respent = equations[0]["granted_tokens"] + 2 * (pool // 2)
    assert respent > pool


def test_the_conservation_report_accounts_for_every_committed_token(
    two_rounds: _TwoRounds,
) -> None:
    """Review F15: ``held_by_reused`` is only part of ``committed_tokens``."""

    assert _refine_child(two_rounds).committed
    second = two_rounds.conservation[1]
    reused = sum(int(value) for value in second["held_by_reused"].values())
    assert reused + second["held_elsewhere"] == second["committed_tokens"]


# --------------------------------------------------------------------------------------
# F6 / F4 / F5: the root resolution happy path, executed end to end
# --------------------------------------------------------------------------------------

ROOT_CRITERION = "c-root-final"
ROOT_NOW_MS = 2_000_000


def _final_criterion(criterion_id: str = ROOT_CRITERION):
    """A root criterion with no gated check, so the formula turns on the verdict alone."""

    from agent_orchestrator.contracts.resolution import (
        Criterion,
        CriterionOrigin,
        EvaluationKind,
        RequiredEvidencePolicy,
        RequirementClass,
    )

    return Criterion(
        criterion_id=criterion_id,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement="the mission goal is satisfied as a whole",
        requirement_class=RequirementClass.REQUIRED_OUTCOME,
        evaluation_kind=EvaluationKind.SEMANTIC,
        required_evidence_policy=RequiredEvidencePolicy(),
    )


def _publish_final_requirements(world: World, *, criteria=None, delivery: str | None = None):
    from agent_orchestrator.contracts.resolution import (
        CriterionExpr,
        RequirementsRevision,
        RequirementsRevisionId,
    )

    latest = world.semantics.latest_requirements_revision(world.mission.id)
    revision = 1 if latest is None else int(latest.revision) + 1
    published = RequirementsRevision(
        revision_id=RequirementsRevisionId(f"req-final-{revision}"),
        mission_id=world.mission.id,
        revision=revision,
        criteria=tuple(criteria or (_final_criterion(),)),
        success_expression=CriterionExpr(ROOT_CRITERION),
        delivery_contract_ref=delivery,
    )
    world.semantics.insert_requirements_revision(published)
    return published


def _final_package(world: World, requirements):
    from agent_orchestrator.contracts.resolution import (
        CriterionExpr,
        ReviewBinding,
        ReviewPackage,
        ReviewPackageId,
        ReviewPurpose,
    )

    # A root review names the inputs it was cut over, and ``accept``/``resolve`` both
    # re-read that manifest from the library: a digest nobody stored is
    # ``INPUT_MANIFEST_UNKNOWN``, which is the right answer and not what this test is
    # about.  So the empty manifest is stored and its own hash used.
    manifest = world.semantics.insert_input_manifest(
        world.mission.id, ROOT_TASK, {"consumer_task_ref": ROOT_TASK, "bindings": []}
    )
    binding = ReviewBinding(
        mission_id=world.mission.id,
        obligation_id=ROOT_DUTY,
        subject_ref=TypedRef(kind=TypedRefKind.TASK, id=ROOT_TASK, revision=1, content_hash=HEX_A),
        requirements_revision=int(requirements.revision),
        input_manifest_hash=manifest,
        policy_ref=TypedRef(
            kind=TypedRefKind.REQUIREMENTS, id="policy-final", revision=1, content_hash=HEX_A
        ),
    )
    package = ReviewPackage(
        package_id=ReviewPackageId(f"pkg-final-{int(requirements.revision)}"),
        purpose=ReviewPurpose.MISSION_FINAL,
        binding=binding,
        criteria=tuple(requirements.criteria),
        success_expression=CriterionExpr(ROOT_CRITERION),
        requirements_content_hash=requirements.content_hash(),
    )
    world.semantics.insert_review_package(package)
    return package


def _final_record(world: World, package, *, verdicts=None, verdict=None):
    from agent_orchestrator.contracts.resolution import (
        CheckExecution,
        CriterionOutcome,
        CriterionVerdict,
        ReviewRecord,
        ReviewRecordId,
        ReviewVerdict,
    )

    reported = verdicts if verdicts is not None else {ROOT_CRITERION: CriterionVerdict.PASS}
    record = ReviewRecord(
        record_id=ReviewRecordId(f"rec-final-{int(package.binding.requirements_revision)}"),
        package_id=package.package_id,
        purpose=package.purpose,
        binding=package.binding,
        reviewer_agent_id="agent-final-reviewer",
        reviewer_turn_id="turn-final",
        evidence_manifest_hash=HEX_A,
        criteria=tuple(
            CriterionOutcome(
                criterion_id=key,
                verdict=value,
                check_execution=CheckExecution.SUCCEEDED,
            )
            for key, value in reported.items()
        ),
        verdict=verdict or ReviewVerdict.ACCEPT,
    )
    world.semantics.insert_review_record(record, official=True)
    return record


def _final_witness(world: World, *, now_ms: int = ROOT_NOW_MS):
    from agent_orchestrator.contracts.evidence_state import (
        Availability,
        TruthValue,
        Validity,
        ValidityWitness,
        WitnessDecision,
        WitnessPurpose,
    )

    witness = ValidityWitness(
        witness_id="wit-root-final",
        consumer_ref=TypedRef(
            kind=TypedRefKind.TASK,
            id=ROOT_TASK,
            revision=1,
            content_hash=content_hash_of(ROOT_TASK),
        ),
        purpose=WitnessPurpose.ACCEPT,
        truth=TruthValue.TRUE,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=WitnessDecision.USABLE,
        scope_id="mission",
        scope_epoch=world.semantics.epoch(world.mission.id, "mission"),
        support_revision=0,
        as_of_ms=int(now_ms),
    )
    world.semantics.insert_validity_witness(world.mission.id, witness, subject=NO_SUBJECT)
    return witness


def _accept_every_child(world: World) -> None:
    """Both gating children of the adopted root method, through the real chain."""

    _accept_leaf(world)
    _assembly(world).accept(
        world.mission.id,
        _review_task(world),
        result_id="result-review",
        layers=_passing_layers(),
        artifacts=(_Artifact("artifact-review", "out/verdict.json"),),
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=1_100_000,
    )


def _ready_for_root(world: World, *, verdicts=None, verdict=None, delivery: str | None = None):
    """Everything a root ``GoalResolution`` is read from, stored the way a deployment would."""

    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_every_child(world)
    requirements = _publish_final_requirements(world, delivery=delivery)
    package = _final_package(world, requirements)
    record = _final_record(world, package, verdicts=verdicts, verdict=verdict)
    _final_witness(world)
    return requirements, package, record


def _offer_root(world: World, **kwargs: Any):
    from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal as _Principal

    return world.dispatch.attempt_root_resolution(
        world.mission.id,
        principal=_Principal(
            "manager-1", "mission", world.semantics.epoch(world.mission.id, "mission")
        ),
        command_id=f"{world.mission.id}:root-resolution",
        **kwargs,
    )


@pytest.fixture
def resolvable(tmp_path) -> World:
    world = committed(tmp_path, key="p23c-root", demand=True)
    _ready_for_root(world)
    return world


def test_the_root_review_is_ready_once_every_gating_child_is_accepted(
    resolvable: World,
) -> None:
    assert resolvable.dispatch.root_review_ready(resolvable.mission.id) is True


def test_the_root_resolution_quotes_the_revision_its_review_was_cut_over(
    resolvable: World,
) -> None:
    """Review P1-7: the root is judged against the revision the reviewer saw."""

    from agent_orchestrator.contracts.resolution import ReviewPurpose

    package = next(
        item
        for item in resolvable.semantics.list_review_packages(
            resolvable.mission.id, purpose=ReviewPurpose.MISSION_FINAL
        )
    )
    outcome = _offer_root(resolvable)
    assert outcome.committed, f"{outcome.reason}: {outcome.detail}"
    stored = resolvable.semantics.adopted_goal_resolution(resolvable.mission.id, ROOT_DUTY)
    assert stored is not None
    assert int(stored.requirements_version) == int(package.binding.requirements_revision)


def test_a_leaf_accepted_after_the_root_review_refuses_the_root_resolution(
    resolvable: World,
) -> None:
    """Review P1-7: a leaf acceptance after the cut invalidates the root review.

    ``RequirementsRevision`` is a Mission-level object, but every leaf acceptance
    publishes one carrying *that leaf's* coverage criteria — so "the Mission's current
    requirements" is really "the last leaf that happened to be accepted".  The root
    resolution used to read ``latest_requirements_revision``, and the only reason no
    test saw it is that ``_ready_for_root`` accepts every child *before* publishing
    the final revision; the real loop accepts leaves from ``_collect_attempt`` with no
    ordering relative to the root review at all.  The root then claimed coverage of a
    criterion nobody had reviewed, which is §21.5's "wrongly declared complete".

    The root now reads the revision its ``ReviewPackage`` was bound to, so the
    read-set channel can see that the Mission's requirements have moved underneath the
    review and refuses: the repair is to cut the root review again against the current
    revision, not to resolve against one the reviewer never saw.
    """

    from agent_orchestrator.contracts.resolution import (
        CriterionExpr,
        RequirementsRevision,
        RequirementsRevisionId,
        ReviewPurpose,
    )

    package = next(
        item
        for item in resolvable.semantics.list_review_packages(
            resolvable.mission.id, purpose=ReviewPurpose.MISSION_FINAL
        )
    )
    reviewed = int(package.binding.requirements_revision)
    # Exactly the shape ``leaf_acceptance._requirements`` publishes, one leaf later.
    resolvable.semantics.insert_requirements_revision(
        RequirementsRevision(
            revision_id=RequirementsRevisionId("req-a-later-leaf"),
            mission_id=resolvable.mission.id,
            revision=reviewed + 1,
            criteria=(_final_criterion("c-a-later-leaf"),),
            success_expression=CriterionExpr("c-a-later-leaf"),
        )
    )

    outcome = _offer_root(resolvable)
    assert not outcome.committed
    assert outcome.reason == "READ_SET_STALE"
    assert f"was read at {reviewed}" in outcome.detail
    assert resolvable.semantics.adopted_goal_resolution(resolvable.mission.id, ROOT_DUTY) is None


def test_the_root_resolution_is_formed_from_the_stored_anchors(resolvable: World) -> None:
    """Review F6(3): the happy path, executed.

    Every §8 test before this stopped at ``ROOT_REVIEW_NOT_READY`` — the early return
    in ``attempt_root_resolution`` — so the package / record / witness lookup, the
    read-set, the ``CompoundFacts`` assembly and the principal conversion had never
    run at all.  Three findings (F4, F5, F6) were living in that unexecuted code.
    """

    outcome = _offer_root(resolvable)
    assert outcome.committed, f"{outcome.reason}: {outcome.detail}"
    stored = resolvable.semantics.adopted_goal_resolution(resolvable.mission.id, ROOT_DUTY)
    assert stored is not None
    assert str(stored.goal_task_id) == ROOT_TASK
    assert resolvable.dispatch.terminal(resolvable.mission.id) is True


def test_the_formed_resolution_restates_the_reviews_verdicts(resolvable: World) -> None:
    """Review F4: the criteria are read, not asserted."""

    from agent_orchestrator.contracts.resolution import CriterionVerdict

    assert _offer_root(resolvable).committed
    stored = resolvable.semantics.adopted_goal_resolution(resolvable.mission.id, ROOT_DUTY)
    assert {item.criterion_id: item.verdict for item in stored.criteria} == {
        ROOT_CRITERION: CriterionVerdict.PASS
    }


def test_a_criterion_the_review_never_judged_is_unknown_and_stops_the_resolution(
    tmp_path,
) -> None:
    """Review F4, the part that used to be written as ``PASS`` unconditionally.

    A criterion that is neither a hard constraint nor named by the success expression
    is not checked by ``_check_resolution_identity`` (it only refuses a *contradiction*
    and a *missing required* one), so the old code stored an unevidenced ``PASS`` in a
    permanent record — the exact shape the same function refuses for
    ``composition_obligation_passed``.  Reading the record instead writes ``UNKNOWN``,
    and the AER §6.2 formula then refuses the resolution rather than the trigger
    answering on the reviewer's behalf.
    """

    from agent_orchestrator.contracts.resolution import CriterionVerdict
    from agent_orchestrator.orchestrator.hierarchical_dispatch import _root_criteria

    world = committed(tmp_path, key="p23c-root-unknown", demand=True)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_every_child(world)
    requirements = _publish_final_requirements(
        world, criteria=(_final_criterion(), _final_criterion("c-side-note"))
    )
    package = _final_package(world, requirements)
    record = _final_record(world, package, verdicts={ROOT_CRITERION: CriterionVerdict.PASS})
    _final_witness(world)

    restated = {item.criterion_id: item.verdict for item in _root_criteria(requirements, record)}
    assert restated == {
        ROOT_CRITERION: CriterionVerdict.PASS,
        "c-side-note": CriterionVerdict.UNKNOWN,
    }
    outcome = _offer_root(world)
    assert outcome.committed is False
    assert outcome.reason == "NOT_ACCEPTABLE"
    assert world.semantics.list_goal_resolutions(world.mission.id) == ()


def test_a_rejected_review_refuses_the_resolution_rather_than_asserting_the_composition(
    tmp_path,
) -> None:
    """Review F9 / mutant M15, as behaviour instead of a source string.

    ``composition_obligation_passed`` is the record's own verdict.  A REJECTED review
    therefore refuses through the AER §6.2 formula; the previous test for this asserted
    that one line of source text was present, which a same-spelling rewrite passes.
    """

    from agent_orchestrator.contracts.resolution import CriterionVerdict, ReviewVerdict

    world = committed(tmp_path, key="p23c-root-reject", demand=True)
    _ready_for_root(
        world,
        verdicts={ROOT_CRITERION: CriterionVerdict.FAIL},
        verdict=ReviewVerdict.REJECTED,
    )
    outcome = _offer_root(world)
    assert outcome.committed is False
    assert outcome.reason == "NOT_ACCEPTABLE"
    assert "COMPOSITION_OBLIGATION_FAILED" in outcome.detail
    assert world.semantics.list_goal_resolutions(world.mission.id) == ()


def test_the_trigger_hands_the_accept_side_a_resolution_principal(resolvable: World) -> None:
    """Review F9 / mutant M14, as behaviour: the accept side refuses a ``PlanPrincipal``.

    The conversion is asserted by the fact that the commit *succeeds* while
    ``commit_goal_resolution`` refuses a ``PlanPrincipal`` with ``BAD_PRINCIPAL`` —
    a trigger that passed its own principal through could not have committed.
    """

    from agent_orchestrator.orchestrator.resolution_commits import (
        ResolutionCommitRejected,
        ResolutionPrincipal,
    )

    assert _offer_root(resolvable).committed
    command = _root_command_with(resolvable, is_mission_root=False)
    with pytest.raises(ResolutionCommitRejected) as caught:
        resolvable.service.commit_goal_resolution(command, PlanPrincipal("manager-1", "mission", 0))
    assert caught.value.reason == "BAD_PRINCIPAL"
    assert isinstance(ResolutionPrincipal("manager-1", "mission"), ResolutionPrincipal)


def test_the_mission_reaches_completed_only_through_the_resolution(resolvable: World) -> None:
    """Review F6(1) and (2): the judgment's mode gate, and the compound row rule.

    Two things used to make the forward path unreachable *and* unguarded at once: a
    compound row is materialised BLOCKED and can never be COMPLETED, so the live-set
    sweep refused forever; and ``judge_mission`` had no mode gate, so a hierarchical
    plan that happened to contain only primitives could be completed with no root
    resolution at all.
    """

    judgments = [{"criterion": item, "met": True} for item in resolvable.mission.success_criteria]
    with pytest.raises(CommitRejected) as caught:
        resolvable.service.judge_mission(
            resolvable.mission.id, judgments=judgments, summary="too early"
        )
    assert "ROOT_RESOLUTION_MISSING" in str(caught.value)
    refusals = resolvable.events(HIERARCHICAL_JUDGMENT_REFUSED)
    assert refusals and refusals[0].payload["redirect"] == "commit_goal_resolution"

    assert _offer_root(resolvable).committed
    judged = resolvable.service.judge_mission(
        resolvable.mission.id, judgments=judgments, summary="done"
    )
    assert judged.status is MissionStatus.COMPLETED
    # Neither row ever said so: the compound is still BLOCKED and the leaves are still
    # READY.  In this mode the Task row is a display index and the Acceptance is the
    # record that the work was accepted (§18.5), which is what the judgment reads.
    assert resolvable.store.get_task(ROOT_TASK).status is TaskStatus.BLOCKED
    assert {
        task.status
        for task in resolvable.store.list_tasks(resolvable.mission.id)
        if task.id != ROOT_TASK
    } == {TaskStatus.READY}


def test_a_leaf_whose_acceptance_was_revoked_stops_the_judgment(resolvable: World) -> None:
    """The other half of review F6(2): the rule reads Acceptances, it does not skip them.

    Simply dropping the status sweep for a hierarchical Mission would have judged a
    Mission whose leaves nobody accepts any more; the same question is asked of the
    record that answers it instead.  The root resolution already stands here, so this
    is the acceptance rule refusing and not the resolution gate.
    """

    assert _offer_root(resolvable).committed
    leaf = next(
        item
        for item in resolvable.semantics.list_acceptances(resolvable.mission.id)
        if str(item.task_id) == _leaf_task(resolvable)
    )
    _revoke(resolvable, str(leaf.acceptance_id))
    judgments = [{"criterion": item, "met": True} for item in resolvable.mission.success_criteria]
    with pytest.raises(CommitRejected) as caught:
        resolvable.service.judge_mission(
            resolvable.mission.id, judgments=judgments, summary="early"
        )
    assert "current Acceptance" in str(caught.value)


def test_a_damaged_plan_refuses_the_judgment_instead_of_crashing(resolvable: World) -> None:
    """Review P2-15: ``judge_mission`` is a commit entry, so it answers with a refusal.

    ``_judgment_network`` reads the plan back, and a plan member whose Task has no
    semantic binding used to leave here as ``PlanIntegrityError`` — a ``RuntimeError``
    nothing on this path catches.  ``_decide`` hit ``root_review_ready`` first and
    stopped the Mission there, so it only showed under a race; the public entry had no
    answer at all.
    """

    from agent_orchestrator.contracts.htn import OccurrenceId, OccurrenceSpec, Requiredness

    assert _offer_root(resolvable).committed
    revision = int(resolvable.semantics.active_plan_revision(resolvable.mission.id).revision)
    resolvable.semantics.insert_plan_membership(
        resolvable.mission.id,
        revision,
        OccurrenceSpec(
            occurrence_id=OccurrenceId("occ-orphan"),
            task_id=TaskRef("task-orphan"),
            obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            form=TaskForm.PRIMITIVE,
            requiredness=Requiredness.OPTIONAL_AUTHORIZED,
        ),
        instance_id=None,
    )
    judgments = [{"criterion": item, "met": True} for item in resolvable.mission.success_criteria]
    with pytest.raises(CommitRejected) as caught:
        resolvable.service.judge_mission(
            resolvable.mission.id, judgments=judgments, summary="damaged"
        )
    assert "does not read back" in str(caught.value)


def test_a_current_acceptance_on_a_failed_row_does_not_pass_the_judgment(
    resolvable: World,
) -> None:
    """Review P2-14: the Acceptance and the row disagreeing is a contradiction.

    The judgment read only the Acceptances, on the strength of a docstring claiming
    "nothing in this mode ever writes COMPLETED onto the row" — which is false:
    ``_collect_attempt`` calls ``accept_result`` and pushes the row to COMPLETED
    before the leaf acceptance runs.  So a leaf whose row had since gone FAILED, with
    its Acceptance still CURRENT, was judged as accepted work.
    """

    assert _offer_root(resolvable).committed
    leaf = _leaf_task(resolvable)
    row = resolvable.store.get_task(leaf)
    # The row moves under the Acceptance, which is the situation being pinned; the
    # product never lifts a row this way.
    resolvable.store.update_task(
        dataclasses.replace(row, status=TaskStatus.FAILED, version=row.version + 1),
        expected_version=row.version,
    )
    judgments = [{"criterion": item, "met": True} for item in resolvable.mission.success_criteria]
    with pytest.raises(CommitRejected) as caught:
        resolvable.service.judge_mission(
            resolvable.mission.id, judgments=judgments, summary="contradicted"
        )
    assert "disagree" in str(caught.value)
    assert leaf in str(caught.value)


def test_a_legacy_mission_is_judged_without_asking_for_a_resolution(tmp_path) -> None:
    """The mode gate is a gate on the *mode*: legacy judgment is byte-for-byte unchanged."""

    import inspect

    from agent_orchestrator.orchestrator.commit_service import CommitService as _Service

    source = inspect.getsource(_Service._judgment_network)
    assert "if not is_hierarchical(mission):" in source
    legacy = build_world(tmp_path, mode=LEGACY_SEMANTICS, key="p23c-judge-legacy")
    assert legacy.service._judgment_network(legacy.mission) is None
    legacy.service._require_root_resolution(legacy.mission.id)  # no refusal, no event
    assert [
        event
        for event in legacy.store.list_events(legacy.mission.id)
        if event.type == HIERARCHICAL_JUDGMENT_REFUSED
    ] == []


# --------------------------------------------------------------------------------------
# F5: the trigger chooses the receipts it may name
# --------------------------------------------------------------------------------------


def _record_receipt(world: World, *, receipt_id: str, acceptance_id: str, obligation: str):
    from agent_orchestrator.contracts.resolution import DeliveryReceipt

    del obligation
    return world.semantics.record_delivery_receipt(
        world.mission.id,
        DeliveryReceipt(
            receipt_id=receipt_id,
            mission_id=world.mission.id,
            acceptance_id=AcceptanceId(acceptance_id),
            stage=DeliveryStage.CONFIRMED,
            observed_at_ms=1,
            operation_id="op-send-1",
            evidence_refs=(
                TypedRef(kind=TypedRefKind.ARTIFACT, id="proof-1", revision=1, content_hash=HEX_A),
            ),
        ),
        command_id=f"cmd-{receipt_id}",
        intent_hash=content_hash_of(receipt_id),
    )


def test_an_out_of_closure_receipt_does_not_block_the_root_resolution(
    resolvable: World,
) -> None:
    """Review F5: the trigger used to name *every* recorded receipt.

    ``_check_delivery`` refuses the whole command for one invalid receipt — the right
    rule — so a single receipt quoting an Acceptance outside the root's duty closure
    made the resolution permanently unformable, with the same refusal replayed every
    cycle.  A liveness defect the trigger created for itself.
    """

    from agent_orchestrator.contracts.evidence_state import Validity
    from agent_orchestrator.contracts.resolution import Acceptance, ReviewRecordId

    stray_record = ReviewRecordId("rec-stray")
    package = dataclasses.replace(_stub_package(resolvable), package_id="pkg-stray")
    resolvable.semantics.insert_review_package(package)
    resolvable.semantics.insert_review_record(
        dataclasses.replace(
            _stub_record(resolvable), record_id=stray_record, package_id="pkg-stray"
        ),
        official=False,
    )
    resolvable.semantics.insert_acceptance(
        Acceptance(
            acceptance_id=AcceptanceId("acc-stray"),
            mission_id=resolvable.mission.id,
            task_id=TaskRef(_leaf_task(resolvable)),
            obligation_id="obl-elsewhere",
            requirements_revision=1,
            contract_revision=1,
            input_manifest_hash=HEX_A,
            review_record_id=stray_record,
            accepted_at_ms=1,
            validity=Validity.CURRENT,
        )
    )
    _record_receipt(
        resolvable,
        receipt_id="rcpt-stray",
        acceptance_id="acc-stray",
        obligation="obl-elsewhere",
    )
    chosen = eligible_root_receipts(
        resolvable.semantics,
        resolvable.mission.id,
        obligation_id=ROOT_DUTY,
        method_instance_id=str(
            resolvable.network()
            .adopted_instance_for(OccurrenceId(resolvable.occurrence_of(ROOT_TASK)))
            .instance_id
        ),
        required_stage=DeliveryStage.CONFIRMED,
    )
    assert "rcpt-stray" not in chosen


class _Trigger:
    """Just enough ``Orchestrator`` to run the real ``_root_resolution_formed``.

    The method reads four things off ``self`` — ``commit``, ``_owner``, ``_note`` and
    ``_plan_integrity_stop`` — so a stub carrying those runs the **wiring** rather
    than a copy of it.  Review P1-3: every test of the receipt filter called
    ``eligible_root_receipts`` directly, so the branch that calls it had never been
    executed and the mutation that hands over every recorded receipt survived the
    whole suite.
    """

    def __init__(self, world: World) -> None:
        self._world = world
        self.notes: list[str] = []

    _owner = "orchestrator-1"

    @property
    def commit(self) -> Any:
        return self._world.service

    def _note(self, text: str) -> None:
        self.notes.append(text)

    async def _plan_integrity_stop(self, mission: Any, error: Exception) -> None:
        raise error

    async def formed(self) -> bool:
        from agent_orchestrator.orchestrator.event_handler import Orchestrator

        return await Orchestrator._root_resolution_formed(  # type: ignore[arg-type]
            self, self._world.mission, self._world.dispatch
        )


@pytest.fixture
def delivered(tmp_path) -> World:
    """A resolvable Mission whose goal declares a delivery contract."""

    world = committed(tmp_path, key="p23c-delivery", demand=True)
    _ready_for_root(world, delivery="contract-ship-it")
    return world


def _stray_receipt(world: World, *, receipt_id: str) -> None:
    """One recorded receipt quoting an Acceptance outside the root's duty closure."""

    from agent_orchestrator.contracts.evidence_state import Validity
    from agent_orchestrator.contracts.resolution import Acceptance, ReviewRecordId

    stray_record = ReviewRecordId(f"rec-{receipt_id}")
    package = dataclasses.replace(_stub_package(world), package_id=f"pkg-{receipt_id}")
    world.semantics.insert_review_package(package)
    world.semantics.insert_review_record(
        dataclasses.replace(
            _stub_record(world), record_id=stray_record, package_id=f"pkg-{receipt_id}"
        ),
        official=False,
    )
    world.semantics.insert_acceptance(
        Acceptance(
            acceptance_id=AcceptanceId(f"acc-{receipt_id}"),
            mission_id=world.mission.id,
            task_id=TaskRef(_leaf_task(world)),
            obligation_id="obl-elsewhere",
            requirements_revision=1,
            contract_revision=1,
            input_manifest_hash=HEX_A,
            review_record_id=stray_record,
            accepted_at_ms=1,
            validity=Validity.CURRENT,
        )
    )
    _record_receipt(
        world,
        receipt_id=receipt_id,
        acceptance_id=f"acc-{receipt_id}",
        obligation="obl-elsewhere",
    )


def test_the_trigger_names_only_the_receipts_inside_the_root_closure(delivered: World) -> None:
    """Review P1-3 (mutation M09): the delivery branch, executed end to end.

    ``delivery_contract_ref`` appeared nowhere in this suite, so the whole
    ``if ... delivery_contract_ref:`` branch of ``_root_resolution_formed`` — the
    required stage *and* the receipt filter — had never run.  Here the goal declares
    a contract, the library holds one receipt inside the root's duty closure and one
    outside it, and the root resolution has to form quoting only the first.  Handing
    over every recorded receipt (the pre-F5 wiring) makes ``_check_delivery`` refuse
    the whole command with ``DELIVERY_RECEIPT_INVALID``.
    """

    live = next(
        item
        for item in delivered.semantics.list_acceptances(delivered.mission.id)
        if str(item.obligation_id) != "obl-elsewhere"
    )
    _record_receipt(
        delivered,
        receipt_id="rcpt-live",
        acceptance_id=str(live.acceptance_id),
        obligation=str(live.obligation_id),
    )
    _stray_receipt(delivered, receipt_id="rcpt-stray")

    trigger = _Trigger(delivered)
    assert asyncio.run(trigger.formed()) is True, trigger.notes

    committed_events = delivered.events(GOAL_RESOLUTION_COMMITTED)
    assert len(committed_events) == 1
    payload = committed_events[0].payload
    assert payload["is_mission_root"] is True
    assert payload["required_delivery_stage"] == str(DeliveryStage.CONFIRMED)
    assert payload["delivery_receipt_id"] == "rcpt-live"


def test_a_goal_with_no_delivery_contract_names_no_receipts(resolvable: World) -> None:
    """The other side of the same branch: no contract, no stage, no receipts."""

    trigger = _Trigger(resolvable)
    assert asyncio.run(trigger.formed()) is True, trigger.notes
    payload = resolvable.events(GOAL_RESOLUTION_COMMITTED)[0].payload
    assert payload["required_delivery_stage"] is None
    assert payload["delivery_receipt_id"] is None


def test_an_in_closure_receipt_is_chosen_and_a_revoked_one_is_not(resolvable: World) -> None:

    acceptances = resolvable.semantics.list_acceptances(resolvable.mission.id)
    assert acceptances
    live = acceptances[0]
    _record_receipt(
        resolvable,
        receipt_id="rcpt-live",
        acceptance_id=str(live.acceptance_id),
        obligation=str(live.obligation_id),
    )
    instance = resolvable.network().adopted_instance_for(
        OccurrenceId(resolvable.occurrence_of(ROOT_TASK))
    )
    chosen = eligible_root_receipts(
        resolvable.semantics,
        resolvable.mission.id,
        obligation_id=ROOT_DUTY,
        method_instance_id=str(instance.instance_id),
        required_stage=DeliveryStage.CONFIRMED,
    )
    assert "rcpt-live" in chosen
    _revoke(resolvable, str(live.acceptance_id))
    after = eligible_root_receipts(
        resolvable.semantics,
        resolvable.mission.id,
        obligation_id=ROOT_DUTY,
        method_instance_id=str(instance.instance_id),
        required_stage=DeliveryStage.CONFIRMED,
    )
    assert "rcpt-live" not in after


def test_the_delivery_receipt_is_read_from_the_library_not_taken_from_the_command(
    resolvable: World,
) -> None:
    """Review F9 / mutant M08, as behaviour: a receipt id nobody recorded is refused."""

    outcome = _offer_root(
        resolvable,
        required_delivery_stage=DeliveryStage.CONFIRMED,
        delivery_receipt_ids=("rcpt-never-recorded",),
    )
    assert outcome.committed is False
    assert outcome.reason in {"DELIVERY_RECEIPT_INVALID", "DELIVERY_CONTRACT_UNDECLARED"}


# --------------------------------------------------------------------------------------
# F12: the same root command, offered twice, is one command
# --------------------------------------------------------------------------------------


def test_the_same_root_command_a_millisecond_later_replays_instead_of_conflicting(
    resolvable: World,
) -> None:
    """Review F12.  ``decided_at_ms`` is when the formula was evaluated, not what it did.

    While the clock was part of the intent hash, every re-offer of an unchanged root
    resolution under the fixed ``{mission}:root-resolution`` id carried a *different*
    intent, so one anomaly turned into a permanent ``COMMAND_PAYLOAD_CONFLICT``.
    """

    first = _offer_root(resolvable)
    assert first.committed
    resolvable.store.advance(5.0) if hasattr(resolvable.store, "advance") else None
    again = _offer_root(resolvable)
    assert again.committed is True
    assert again.reason in {"", "ALREADY_RESOLVED"}
    assert len(resolvable.semantics.list_goal_resolutions(resolvable.mission.id)) == 1


def test_the_clock_is_not_part_of_the_root_commands_intent(resolvable: World) -> None:
    command = _root_command_with(resolvable)
    later = dataclasses.replace(command, decided_at_ms=command.decided_at_ms + 10_000)
    assert later.intent_hash() == command.intent_hash()
    other = dataclasses.replace(command, witness_id="wit-other")
    assert other.intent_hash() != command.intent_hash()


# --------------------------------------------------------------------------------------
# F2 / F3: the accepted-output index has one writer and one validity rule
# --------------------------------------------------------------------------------------


def test_the_output_index_has_exactly_one_writer_in_the_source_tree() -> None:
    """Review F2: the gate has to be un-bypassable, not merely present somewhere.

    ``check_against_ports`` is called by ``accept_review`` inside its own transaction.
    This is the guard that keeps a *second* writer from appearing later without it —
    the same shape as ``test_no_module_outside_storage_writes_the_new_tables_in_sql``.
    """

    root = Path(__file__).resolve().parents[3] / "src" / "agent_orchestrator"
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "insert_acceptance_output(" in path.read_text()
    }
    assert callers == {"storage/htn_store.py", "orchestrator/resolution_commits.py"}
    writer = (root / "orchestrator" / "resolution_commits.py").read_text()
    gate = writer.index("check_against_ports(")
    write = writer.index("insert_acceptance_output(")
    assert gate < write, "the port check must run before the row is written"


def _revoke(world: World, acceptance_id: str) -> None:
    """Revoke a stored Acceptance.

    Written with SQL because nothing in ``src/`` revokes one yet: the *supersede /
    revoke* command is a later slice.  The rules that read validity are live today,
    though, and they are what these tests are about — so the row is moved by hand,
    both the column the SQL join reads and the document the object readers parse.
    """

    from agent_orchestrator.contracts.evidence_state import Validity
    from simple_harness.contracts import canonical_json

    revoked = dataclasses.replace(
        world.semantics.get_acceptance(acceptance_id), validity=Validity.REVOKED
    )
    document = revoked.to_json()
    world.store.connection.execute(
        "UPDATE acceptances SET validity = ?, acceptance_json = ?, content_hash = ?"
        " WHERE acceptance_id = ?",
        (
            str(Validity.REVOKED),
            canonical_json(document),
            content_hash_of(document),
            str(acceptance_id),
        ),
    )
    world.store.connection.commit()


def test_an_output_whose_acceptance_was_revoked_is_no_longer_offered(live: World) -> None:
    """Review F3: ``list_acceptance_outputs`` joins ``acceptances`` and filters validity.

    The other lane of the rule ``root_contributions`` already applied: a superseded or
    revoked Acceptance is history, and feeding its output to a consumer would bind
    downstream work to an acceptance nobody holds any more.
    """

    receipt = _accept_leaf(live)
    assert len(live.semantics.list_acceptance_outputs(live.mission.id)) == 1
    _revoke(live, str(receipt.acceptance_id))
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()
    index = live.dispatch.accepted_outputs(live.mission.id, live.network())
    assert index.outputs == ()


def test_a_revoked_acceptance_stops_licensing_the_data_consumer(live: World) -> None:

    receipt = _accept_leaf(live)
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_000_000)
    assert _review_task(live) in live.dispatch.admissions(live.mission.id).readiness
    _revoke(live, str(receipt.acceptance_id))
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_100_000)
    assert _review_task(live) not in live.dispatch.admissions(live.mission.id).readiness


def test_a_revoked_contribution_is_not_a_root_contribution(live: World) -> None:
    """Review F3 / mutant M21: the ``Validity.CURRENT`` filter, with a witness at last."""

    receipt = _accept_leaf(live)
    before = live.dispatch.root_contributions(live.mission.id)
    assert any(str(receipt.acceptance_id) in value for value in before.values())
    _revoke(live, str(receipt.acceptance_id))
    after = live.dispatch.root_contributions(live.mission.id)
    assert not any(str(receipt.acceptance_id) in value for value in after.values())


# --------------------------------------------------------------------------------------
# F8: one read
# --------------------------------------------------------------------------------------


def test_the_admission_report_and_the_hashed_manifest_come_from_one_read(live: World) -> None:
    """Review F8: ``read()`` computed the projection and threw it away.

    ``admissions()`` then read it again, so the readiness report it reported came out
    of snapshot A and the manifest it hashed out of snapshot B.  The window was narrow
    — one ``_decide``, no transaction — but "one read" was the safety argument the
    docstring made, and an argument that is not true cannot be the reason.
    """

    view = live.dispatch.read(live.mission.id)
    assert view.accepted is not None
    assert view.licences == live.dispatch.input_witness_index(live.mission.id)

    calls: list[str] = []
    original = type(live.dispatch).accepted_outputs

    def counting(self, mission_id, network, **kwargs):
        calls.append(mission_id)
        return original(self, mission_id, network, **kwargs)

    type(live.dispatch).accepted_outputs = counting
    try:
        live.dispatch.admissions(live.mission.id)
    finally:
        type(live.dispatch).accepted_outputs = original
    assert calls == [live.mission.id], "the projection is fetched once per admissions() call"


# --------------------------------------------------------------------------------------
# F11 / M17: the dispatch transaction re-checks, and what it accepts
# --------------------------------------------------------------------------------------


def test_a_task_with_no_admission_is_not_dispatched_by_the_direct_entry(tmp_path) -> None:
    """Review mutant M17 (TG §8.3): the second gate, exercised by a caller ``_decide`` is not."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = committed(evidence, key="p23c-recheck", demand=True)
    review_task, env = _review_task(world), world.env
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case() -> None:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            loop.install_hierarchical(planning=env)
            mission = loop.store.get_mission(world.mission.id)
            task = loop.store.get_task(review_task)
            # The DATA consumer's producer has not been accepted, so the readiness gate
            # holds no admission for it — whatever its ``TaskStatus`` says.
            assert task.status is TaskStatus.READY
            assert await loop._next_attempt(mission, task, ()) is False
            assert loop.store.list_attempts(review_task) == []

    asyncio.run(case())


def test_an_object_that_merely_claims_the_flag_is_not_an_admission(tmp_path) -> None:
    """Review F11: ``isinstance``, not a duck-typed ``gate_passed`` probe."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    class _Forged:
        gate_passed = True

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = committed(evidence, key="p23c-forged", demand=True)
    review_task, env = _review_task(world), world.env
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case() -> None:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            loop.install_hierarchical(planning=env)
            mission = loop.store.get_mission(world.mission.id)
            task = loop.store.get_task(review_task)
            assert await loop._next_attempt(mission, task, (), admission=_Forged()) is False
            assert loop.store.list_attempts(review_task) == []

    asyncio.run(case())


# --------------------------------------------------------------------------------------
# M13 / M16: the two other surviving mutants
# --------------------------------------------------------------------------------------


def test_a_consumer_the_plan_drew_no_edge_for_gets_an_empty_manifest_and_no_admission(
    live: World,
) -> None:
    """Review mutant M13: ``resolved_inputs`` degenerating to an empty manifest.

    The DATA gate's second line of defence is real — ``required_ports_satisfied``
    refuses a binding whose required port nothing fills — and this is the assertion
    that says so rather than assuming it.
    """

    network = live.network()
    spec = next(item for item in network.occurrences if str(item.task_id) == _review_task(live))
    result = live.dispatch.resolved_inputs(live.mission.id, network, spec)
    assert result.manifest is not None
    assert result.manifest.is_frozen is False
    refusal = live.dispatch.admissions(live.mission.id).refusal_for(_review_task(live))
    assert refusal.reason is ReadinessReason.WAITING_DATA


def test_two_reasons_for_one_occurrence_leave_two_withheld_records(live: World) -> None:
    """Review mutant M16: the idempotency key really does carry the reason.

    The existing test's world gave every occurrence a *different* reason, and the key
    also carries the task id — so dropping ``reason`` from the key merged nothing and
    the mutant lived.  Here one occurrence is withheld for two different reasons.
    """

    from agent_orchestrator.orchestrator.hierarchical_dispatch import (
        DispatchAdmissions,
        DispatchRefusal,
    )

    task = _review_task(live)
    for reason in (ReadinessReason.WAITING_DATA, ReadinessReason.STALE_BINDING):
        live.dispatch.record_withheld(
            live.mission.id,
            DispatchAdmissions(
                plan_revision=1,
                refusals=(
                    DispatchRefusal(
                        task_id=task,
                        occurrence_id=live.occurrence_of(task),
                        reason=reason,
                        detail_codes=("x",),
                        detail="",
                    ),
                ),
            ),
        )
    keys = {
        event.idempotency_key for event in live.events(DISPATCH_WITHHELD) if event.task_id == task
    }
    assert len(keys) == 2


# --------------------------------------------------------------------------------------
# F16 / the smoke's two repairs: applicability and facts
# --------------------------------------------------------------------------------------


def test_the_applicability_section_reports_the_axes_the_report_really_has(
    tmp_path,
) -> None:
    """Review F16: every axis used to be read under a name ``ApplicabilityReport`` lacks."""

    from agent_orchestrator.planning.htn.applicability import ApplicabilityStatus

    world = build_world(tmp_path, key="p23c-applic")
    world.env.register_predicate("plan.ready", closed=False)
    contract = method(
        "plan.gated",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(
            {
                "op": "predicate",
                "predicate_ref": ref("plan.ready").to_json(),
                "arguments": {"subject": param("subject")},
            },
        ),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )
    assert world.env.admit(contract).admitted
    HtnStore(world.store).register_method(
        contract, world.env.registry.registration(contract.method_ref())
    )
    entries = world.dispatch.method_applicability(world.mission.id)
    assert entries, "a method whose precondition nobody observed is refused, and reported"
    rendered = applicability_reports(entries)
    gated = next(item for item in rendered if item["method_ref"]["method_id"] == "plan.gated")
    assert gated["verdict"] == str(ApplicabilityStatus.NEEDS_EVIDENCE)
    assert gated["unknown_preconditions"], "the unknown proposition is named"
    assert gated["goal_signature_id"] == "plan.goal"


def test_the_facts_section_quotes_an_observation_the_read_set_checker_accepts(
    world: World,
) -> None:
    """The smoke's ``READ_SET_UNRESOLVED``, closed: the entry is shown, not invented."""

    from agent_orchestrator.orchestrator._read_set import SemanticReadSetChecker

    record = ObservationRecord(
        observation_id="obsrec-facts-1",
        proposition_key="plan.ready#alpha",
        polarity=True,
        source_ref=TypedRef(
            kind=TypedRefKind.OBSERVATION, id="obs-1", revision=1, content_hash=HEX_A
        ),
        observed_at_ms=10,
        recorded_at_ms=10,
        coverage=QueryCompleteness.BEST_EFFORT,
        observer_id="observer-1",
    )
    world.semantics.insert_observation(world.mission.id, record)
    entries = recorded_facts(world.semantics.list_observations(world.mission.id))
    assert [item["read_set_entry"]["id"] for item in entries] == ["obsrec-facts-1"]
    quoted = entries[0]["read_set_entry"]
    item = ReadItem(
        kind=ReadItemKind.FACT,
        id=quoted["id"],
        semantic_revision=int(quoted["semantic_revision"]),
        content_hash=str(quoted["content_hash"]),
    )
    checker = SemanticReadSetChecker(world.store, world.semantics, mission_id=world.mission.id)
    verdict = checker.verify(
        SemanticReadSet(requirements_revision=0, observation_revisions=(item,))
    )
    assert verdict.stale == () and verdict.unresolved == ()


def _record_look(world: World, key: str, *, at_ms: int, identity: str) -> None:
    world.semantics.insert_observation(
        world.mission.id,
        ObservationRecord(
            observation_id=identity,
            proposition_key=key,
            polarity=True,
            source_ref=TypedRef(
                kind=TypedRefKind.OBSERVATION, id="obs-1", revision=1, content_hash=HEX_A
            ),
            observed_at_ms=at_ms,
            recorded_at_ms=at_ms,
            coverage=QueryCompleteness.BEST_EFFORT,
            observer_id="observer-1",
        ),
    )


def test_a_proposition_is_looked_at_once_under_one_plan_revision(world: World) -> None:
    """Review P1-4: the cap part 2c claimed but no test held.

    ``run_evidence_round`` skips an ask whose proposition this Mission has already
    read, and removing that skip (mutation M23) used to leave the whole suite green —
    while the real-model round it was written for recorded the same proposition
    several hundred times in seconds.
    """

    revision = int(world.network().plan_revision)
    committed_at = world.dispatch.plan_revision_committed_at(world.mission.id, revision)
    assert committed_at > 0, "the fixture really did commit a revision"

    assert world.dispatch.propositions_looked_at(world.mission.id, plan_revision=revision) == (
        frozenset()
    )
    _record_look(world, "plan.ready#alpha", at_ms=committed_at + 5, identity="obsrec-now")
    assert world.dispatch.propositions_looked_at(world.mission.id, plan_revision=revision) == (
        frozenset({"plan.ready#alpha"})
    )


def test_a_plan_revision_re_opens_the_look(world: World) -> None:
    """Review P1-4: the cap used to be for the Mission's whole lifetime.

    Evidence could then never refresh — a precondition repaired while the plan ran was
    never read again, which contradicts I19's "recompute, do not reuse the old TRUE"
    that ``issue_start_witnesses`` argues for one file over.  An observation taken
    before the current revision was committed no longer counts as having looked, so
    committing a revision gives every proposition exactly one more read.
    """

    revision = int(world.network().plan_revision)
    committed_at = world.dispatch.plan_revision_committed_at(world.mission.id, revision)
    _record_look(world, "plan.ready#alpha", at_ms=committed_at - 1, identity="obsrec-before")
    assert world.dispatch.propositions_looked_at(world.mission.id, plan_revision=revision) == (
        frozenset()
    ), "a read from before this revision is not a read under it"
    # The Mission-lifetime rule would have counted it, whenever it was taken.
    assert world.semantics.list_observations(world.mission.id)
    # and a revision this Mission never committed is no barrier at all
    assert world.dispatch.plan_revision_committed_at(world.mission.id, revision + 99) == 0
    assert world.dispatch.propositions_looked_at(
        world.mission.id, plan_revision=revision + 99
    ) == frozenset({"plan.ready#alpha"})


def test_the_facts_section_carries_references_and_never_an_inference(world: World) -> None:
    """Review P2-12: the section hands over what was observed, not what follows.

    Mutation M24 — add an ``inferred_holds`` field to every entry — used to leave the
    whole suite green.  A conclusion computed here would be this package deciding the
    question the refinement round decides, with no record that it did, so the set of
    fields an entry may carry is now checked rather than merely intended.
    """

    from agent_orchestrator.planning.htn.planner_package import (
        FACT_ENTRY_FIELDS,
        refuse_fact_inference,
    )

    _record_look(world, "plan.ready#alpha", at_ms=10, identity="obsrec-guard")
    entries = recorded_facts(world.semantics.list_observations(world.mission.id))
    assert entries and all(set(item) <= FACT_ENTRY_FIELDS for item in entries)
    with pytest.raises(ContractError, match="inference"):
        refuse_fact_inference([{**entries[0], "inferred_holds": True}])


def test_the_quoted_read_set_entry_is_computed_by_the_checker(world: World) -> None:
    """Review P2-13: one formula for "what revision is this", not two.

    ``ReadSetChecker.read_item`` says in as many words that the proposing side and the
    checking side writing that formula twice is how they stop agreeing — and this
    package was the second place it was written.  The entry the Planner is told to
    copy is produced by the checker that will re-check it.
    """

    from agent_orchestrator.orchestrator._read_set import SemanticReadSetChecker

    _record_look(world, "plan.ready#alpha", at_ms=10, identity="obsrec-checker")
    checker = SemanticReadSetChecker(world.store, world.semantics, mission_id=world.mission.id)
    entries = recorded_facts(
        world.semantics.list_observations(world.mission.id), read_item=checker.read_item
    )
    quoted = entries[0]["read_set_entry"]
    assert quoted == checker.read_item(ReadItemKind.FACT, "obsrec-checker").to_json()
    verdict = checker.verify(
        SemanticReadSet(
            requirements_revision=0,
            observation_revisions=(
                ReadItem(
                    kind=ReadItemKind.FACT,
                    id=quoted["id"],
                    semantic_revision=int(quoted["semantic_revision"]),
                    content_hash=str(quoted["content_hash"]),
                ),
            ),
        )
    )
    assert verdict.stale == () and verdict.unresolved == ()


def test_only_the_newest_observation_of_a_proposition_is_offered(world: World) -> None:
    """A superseded record is an entry guaranteed to refuse the commit."""

    for index, identity in enumerate(("obsrec-old", "obsrec-new"), start=1):
        world.semantics.insert_observation(
            world.mission.id,
            ObservationRecord(
                observation_id=identity,
                proposition_key="plan.ready#alpha",
                polarity=True,
                source_ref=TypedRef(
                    kind=TypedRefKind.OBSERVATION, id="obs-1", revision=1, content_hash=HEX_A
                ),
                observed_at_ms=index * 10,
                recorded_at_ms=index * 10,
                coverage=QueryCompleteness.BEST_EFFORT,
                observer_id="observer-1",
            ),
        )
    entries = recorded_facts(world.semantics.list_observations(world.mission.id))
    assert [item["read_set_entry"]["id"] for item in entries] == ["obsrec-new"]


# ======================================================================================
# 18. P2.3c part 2c — the START-precondition lane the real-model smoke found missing
#
# A leaf whose parent method carries an ``applicable_when`` inherits that condition as
# a SELECT :class:`PreconditionRef` (``grounding.task_binding_for``), and TG §9 refuses
# to dispatch such an occurrence without a ``purpose=START`` ValidityWitness.  Nothing
# in the product issued one: ``issue_input_witnesses`` covers the DATA lane only, so in
# every deployment the first leaf under a gated method sat in ``WAITING_EVIDENCE``
# (``witness_missing``) for ever.  These tests drive the lane end to end.
# ======================================================================================


def _gated(tmp_path, *, key: str = "p23c-precondition") -> World:
    """A committed plan whose root method is gated on an observed precondition."""

    world = build_world(tmp_path, key=key)
    world.env.register_predicate("plan.ready", closed=False)
    contract = method(
        "plan.gated",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(
            {
                "op": "predicate",
                "predicate_ref": ref("plan.ready").to_json(),
                "arguments": {"subject": param("subject")},
            },
        ),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )
    assert world.env.admit(contract).admitted
    HtnStore(world.store).register_method(
        contract, world.env.registry.registration(contract.method_ref())
    )
    world.env.say("plan.ready", {"subject": "alpha"}, TruthValue.TRUE)
    gated = dataclasses.replace(world, contract=contract)
    outcome = gated.plan()
    assert outcome.committed, outcome.last_reason
    gated.admit_demand()
    return gated


def _gated_leaf(world: World) -> str:
    leaf = next(
        spec
        for spec in world.network().occurrences
        if spec.form is TaskForm.PRIMITIVE  # the only primitive this method opens
    )
    return str(world.network().binding_for_occurrence(leaf.occurrence_id).task_id)


def test_a_leaf_under_a_gated_method_inherits_the_condition_as_a_start_precondition(
    tmp_path,
) -> None:
    """The premise: this is not a property of the fixture's wording but of grounding."""

    world = _gated(tmp_path)
    binding = world.network().binding_for_task(TaskRef(_gated_leaf(world)))
    assert [str(item.phase) for item in binding.precondition_refs] == ["SELECT"]


def test_without_a_start_witness_the_gated_leaf_is_withheld_not_dispatched(
    tmp_path,
) -> None:
    """The defect the smoke found: the duty is admitted, the plan is fine, nothing runs."""

    world = _gated(tmp_path)
    refusal = world.dispatch.admissions(world.mission.id).refusal_for(_gated_leaf(world))
    assert refusal.reason is ReadinessReason.WAITING_EVIDENCE
    assert refusal.detail_codes == ("witness_missing",)


def test_issuing_the_start_witness_makes_the_gated_leaf_dispatchable(tmp_path) -> None:
    """I19's "recompute rather than reuse": the condition is re-read, then licensed."""

    world = _gated(tmp_path)
    issued = world.dispatch.issue_start_witnesses(world.mission.id, now_ms=1_000_000)
    assert issued, "a gated leaf must be offered a licence"
    assert _gated_leaf(world) in world.dispatch.admissions(world.mission.id).readiness


def test_the_start_witness_names_its_condition_and_its_one_consumer(tmp_path) -> None:
    """§11.5: a witness is not a transferable token, and it says what it was taken for."""

    world = _gated(tmp_path)
    world.dispatch.issue_start_witnesses(world.mission.id, now_ms=1_000_000)
    leaf = _gated_leaf(world)
    held = world.dispatch.start_witnesses(world.mission.id, leaf)
    digests = {
        item.condition_digest
        for item in world.network().binding_for_task(TaskRef(leaf)).precondition_refs
    }
    assert set(held) == digests
    witness = held[sorted(digests)[0]]
    assert witness.purpose is WitnessPurpose.START
    assert witness.consumer_ref.id == leaf
    assert witness.support_refs, "the licence names the observation it was taken over"
    assert world.dispatch.start_witnesses(world.mission.id, ROOT_TASK) == {}


def test_a_condition_the_world_no_longer_supports_is_licensed_as_blocked(tmp_path) -> None:
    """§6.6 rule 2 / I18: the old TRUE is not reused, and UNKNOWN never opens the gate."""

    world = _gated(tmp_path)
    world.env.say("plan.ready", {"subject": "alpha"}, TruthValue.UNKNOWN)
    issued = world.dispatch.issue_start_witnesses(world.mission.id, now_ms=2_000_000)
    assert [item.decision for item in issued] == [WitnessDecision.BLOCKED]
    assert [item.truth for item in issued] == [TruthValue.UNKNOWN]
    refusal = world.dispatch.admissions(world.mission.id).refusal_for(_gated_leaf(world))
    assert refusal.reason is ReadinessReason.WAITING_EVIDENCE
    assert refusal.detail_codes == ("witness_truth_UNKNOWN",)


def test_a_witness_the_evidence_outran_is_replaced_rather_than_reused(tmp_path) -> None:
    """The licence carries the support revision it was taken at, so a later read wins."""

    world = _gated(tmp_path)
    world.env.say("plan.ready", {"subject": "alpha"}, TruthValue.UNKNOWN)
    world.dispatch.issue_start_witnesses(world.mission.id, now_ms=2_000_000)
    world.env.say("plan.ready", {"subject": "alpha"}, TruthValue.TRUE)
    # A real ``PlanningWorld`` counts the observations behind its snapshot, so learning
    # a fact moves the support revision; the in-memory Env fixture pins it at 1, which
    # would make "the world moved" untestable.  Moving it is the point of the test.
    taken = world.env.snapshot
    world.env.snapshot = lambda *a, **k: dataclasses.replace(  # type: ignore[method-assign]
        taken(*a, **k), support_revision=2
    )
    world.dispatch.issue_start_witnesses(world.mission.id, now_ms=3_000_000)
    held = world.dispatch.start_witnesses(world.mission.id, _gated_leaf(world))
    assert [item.decision for item in held.values()] == [WitnessDecision.USABLE]
    assert _gated_leaf(world) in world.dispatch.admissions(world.mission.id).readiness


def test_the_decide_loop_issues_the_start_licence_before_it_reads_readiness() -> None:
    """Without this the lane exists and is never used — which is what the smoke saw."""

    import inspect

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator._decide)
    assert source.index("issue_start_witnesses") < source.index("new_mode.admissions(mission.id)")


# --------------------------------------------------------------------------------------
# The other half of the smoke's finding: idling with work left over is a stop, not silence
# --------------------------------------------------------------------------------------


def _stalled(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS):
    """A committed plan whose leaves every gate withholds, on a real Orchestrator."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    # ``demand=False``: TG decision 9 withholds every occurrence, which is the shape of
    # "the plan is committed and nothing may run" without needing a real model.  A
    # legacy Mission has no plan to commit through this entry at all (§18.5 rule 1),
    # so it is built and left exactly as the legacy world builds it.
    world = (
        committed(evidence, key=f"p23c-stall-{mode}", mode=mode, demand=False)
        if mode == HIERARCHICAL_SEMANTICS
        else build_world(evidence, key=f"p23c-stall-{mode}", mode=mode)
    )
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)
    return world, Orchestrator(config, RoleScriptedProvider({"planner": []})), mode


def _install(loop: Any, world: World) -> None:
    """Hand the loop this Env, pointed at the store the **loop** opened.

    ``_stalled`` closes the fixture's handle and the Orchestrator opens its own over
    the same file; since review P2-16 the Env reads its evidence counters out of a
    store, so it has to be the live one.
    """

    world.env.semantics = HtnStore(loop.store)
    loop.install_hierarchical(planning=world.env)


def test_an_idle_hierarchical_mission_with_withheld_work_records_the_stall(tmp_path) -> None:
    """The loop stops looking; the reasons are written down rather than left implied."""

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            await loop.run()
            return loop.store.get_mission(world.mission.id), list(
                loop.store.list_events(world.mission.id)
            )

    mission, events = asyncio.run(case())
    stalls = [item for item in events if item.type == MISSION_STALLED]
    assert len(stalls) == 1, "one stall, recorded once"
    payload = stalls[0].payload
    assert payload["code"] == "hierarchical_no_dispatchable_work"
    assert payload["withheld"], "the refusals the gates produced travel with the record"
    assert {item["reason"] for item in payload["withheld"]} == {
        "NOT_SELECTED",
        "NEEDS_REFINEMENT",
    }
    assert payload["fingerprint"], "the stall carries its own identity (§9.1)"
    # The *record* is still not the verdict.  The verdict is the second step below, and
    # the two events together are the whole narrative: where it is stuck, then that one
    # more cycle found it stuck in exactly the same place, then the cycle ends.
    assert [item.type for item in events].index(MISSION_STALLED) < [
        item.type for item in events
    ].index("MissionFailed")
    assert mission.status is MissionStatus.FAILED


def test_a_stall_that_survives_the_confirm_cycle_stops_the_mission(tmp_path) -> None:
    """P2.3c part 2d, decision 2 (memo test 2).

    §15 makes a Mission a **bounded** execution cycle: "somebody could admit a demand
    later" belongs to the next cycle, not to this one, and a局 that never ends cannot
    enter the paired evaluation §21.5 asks for.  §9.1 licenses stopping on *repeated*
    no progress, so the stop comes after one more complete cycle that read the world
    again and found it unchanged.
    """

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            await loop.run()
            return loop.store.get_mission(world.mission.id)

    mission = asyncio.run(case())
    assert mission.status is MissionStatus.FAILED
    assert mission.final_report["stop_reason"] == "no_dispatchable_work"
    assert mission.final_report["detail"]["confirmed_after_one_more_cycle"] is True


def test_the_stop_report_names_every_withheld_occurrence_and_outstanding_duty(
    tmp_path,
) -> None:
    """Memo test 3, and §6.4's shape: the expanded structure and the open duties.

    ``BOUND_REACHED`` is the model: the report says what was expanded and what is
    still owed, and never that the goal is impossible (§7.4 / TG §8.2 — a missing
    input, an unavailable observer and an UNKNOWN external action are three different
    causes and are not all converted into "the task failed").
    """

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            await loop.run()
            return loop.store.get_mission(world.mission.id), loop.hierarchical.admissions(
                world.mission.id
            )

    mission, admissions = asyncio.run(case())
    detail = mission.final_report["detail"]
    assert len(detail["withheld"]) == len(admissions.refusals), "every refusal, not a sample"
    assert all(item["reason"] and item["detail_codes"] for item in detail["withheld"])
    assert detail["outstanding_obligations"], "the duties still owed are named"
    assert all(
        set(item) >= {"obligation_id", "has_admitted_demand", "remaining_fuel"}
        for item in detail["outstanding_obligations"]
    )
    assert detail["plan_revision"] == int(admissions.plan_revision)
    # §7.4: this is a bound, not a verdict about the goal.
    assert mission.final_report["stop_reason"] not in {
        "insufficient_evidence",
        "mission_criteria_unmet",
        "planning_failed",
        "no_progress",
    }


def test_a_stall_that_the_confirm_cycle_clears_does_not_fail_the_mission(tmp_path) -> None:
    """Memo test 1, and decision 2's first mutation self-check.

    A demand admitted from outside, an approval granted, an observation recorded —
    each is exactly the kind of change that makes the very same plan runnable, and
    part 2c's smoke moved twice on changes of that size.  §9.1 only licenses a stop on
    a *repeated* lack of progress, so the confirmation **re-reads the world** and
    compares; it does not act on the verdict it was handed.

    Driven directly rather than through two full runs: what is under test is the
    comparison, and a second loop would only be asserting that the scheduler idles.

    **Mutation**: make ``_confirm_and_stop_stalled`` treat the fingerprint it was
    handed as the current one (skip the re-read) and this goes red — the Mission is
    stopped although its world had moved.
    """

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            mission = loop.store.get_mission(world.mission.id)
            assert mission.status is MissionStatus.ACTIVE
            # A stall was recorded under a world that no longer holds.
            loop._stalled_at[world.mission.id] = "a fingerprint from a world that moved"
            await loop._confirm_and_stop_stalled()
            return loop.store.get_mission(world.mission.id)

    mission = asyncio.run(case())
    assert mission.status is MissionStatus.ACTIVE, "a changed world is not a confirmed stall"


def test_the_stall_path_stops_after_exactly_one_confirm_cycle(tmp_path) -> None:
    """Memo test 7 — decision 2's second mutation self-check.

    The confirmation is **one** cycle, hard-coded.  A ``while`` here would turn an idle
    loop into a busy one, which is the failure this whole path exists to end.

    **Mutation**: wrap the body of ``_confirm_and_stop_stalled`` in a retry loop and
    the count below stops being 1.
    """

    world, orchestrator, _ = _stalled(tmp_path)
    calls: list[str] = []

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            real = loop.hierarchical.admissions

            def counted(mission_id, *args, **kwargs):
                calls.append(str(mission_id))
                return real(mission_id, *args, **kwargs)

            loop.hierarchical.admissions = counted  # type: ignore[method-assign]
            loop._stalled_at[world.mission.id] = "not the current fingerprint"
            await loop._confirm_and_stop_stalled()

    asyncio.run(case())
    assert calls == [world.mission.id], "the confirmation reads the admissions once"


def test_the_mission_is_never_marked_completed_by_the_stall_path(tmp_path) -> None:
    """Memo test 5 — §21.5's two hard invariants, asserted on this path directly.

    A stop can never become a completion: it does not go through
    ``attempt_root_resolution`` and it never writes ``COMPLETED``.  The budget
    conservation also has to hold at the end of the局, because ``fail_mission``
    cascades the stop (cancels open work, closes intents, releases reservations).
    """

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            await loop.run()
            from agent_orchestrator.contracts.htn import ObligationId

            duties = ObligationStore(loop.store)
            return (
                loop.store.get_mission(world.mission.id),
                list(loop.store.list_events(world.mission.id)),
                [
                    duties.account(world.mission.id, ObligationId(str(duty))).remaining_fuel
                    for duty in duties.obligation_ids(world.mission.id)
                ],
            )

    mission, events, fuel = asyncio.run(case())
    assert mission.status is not MissionStatus.COMPLETED
    kinds = [item.type for item in events]
    assert "MissionCompleted" not in kinds
    assert "GoalResolutionCommitted" not in kinds, "no root Resolution was formed"
    assert fuel and all(item >= 0 for item in fuel), "no duty is over-drawn at the end"


def test_a_legacy_mission_that_idles_is_never_stopped_by_this_path(tmp_path) -> None:
    """Memo test 4: the stop is hierarchical-only, like the record above it."""

    world, orchestrator, _ = _stalled(tmp_path, mode="legacy")

    async def case():
        async with orchestrator as loop:
            loop.install_hierarchical(planning=None)
            await loop.run()
            return loop.store.get_mission(world.mission.id), [
                item.type for item in loop.store.list_events(world.mission.id)
            ]

    mission, kinds = asyncio.run(case())
    assert MISSION_STALLED not in kinds
    if mission.status is MissionStatus.FAILED:
        assert mission.final_report["stop_reason"] != "no_dispatchable_work"


def test_a_legacy_mission_that_idles_is_not_reported_as_stalled(tmp_path) -> None:
    """The rule reads ``_new_mode``; a legacy Mission is none of its business."""

    world, orchestrator, _ = _stalled(tmp_path, mode="legacy")

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            await loop._record_hierarchical_stall()
            return [
                item
                for item in loop.store.list_events(world.mission.id)
                if item.type == MISSION_STALLED
            ]

    assert asyncio.run(case()) == []


def test_an_occurrence_that_was_admitted_and_never_ran_is_named_in_the_record(tmp_path) -> None:
    """The gates said yes and the work still did not run: that belongs in the record too.

    It is the half an operator cannot see from the readiness reports — every gate
    passed, so there is no refusal to read, and the reason the occurrence did not run
    lives in the allocator (budget, concurrency, attempt policy) instead.  Naming it
    beside the withheld ones is what makes the record answer "why is nothing
    happening" rather than "why is the plan not ready".
    """

    world, orchestrator, _ = _stalled(tmp_path)

    async def case():
        async with orchestrator as loop:
            _install(loop, world)
            duties = ObligationStore(loop.store)
            for spec in loop.hierarchical.network(world.mission.id).occurrences:
                if (
                    duties.exists(world.mission.id, spec.obligation_id)
                    and not duties.account(world.mission.id, spec.obligation_id).has_admitted_demand
                ):
                    loop.commit.admit_obligation_demand(
                        world.mission.id,
                        spec.obligation_id,
                        principal="mission-submitter",
                        requester={"kind": "mission_root"},
                        evidence={"occurrence": str(spec.occurrence_id)},
                    )
            admitted = sorted(loop.hierarchical.admissions(world.mission.id).readiness)
            assert admitted, "the fixture must really have an admissible occurrence"
            await loop._record_hierarchical_stall()
            records = [
                item
                for item in loop.store.list_events(world.mission.id)
                if item.type == MISSION_STALLED
            ]
            return admitted, records

    admitted, records = asyncio.run(case())
    assert len(records) == 1
    assert records[0].payload["admitted_not_dispatched"] == admitted
    assert records[0].payload["unfinished"], "the rows that are still open are named"


def test_a_data_licence_and_a_precondition_licence_are_held_at_once(tmp_path) -> None:
    """P2.3c part 2d, decision 1: the two START lanes no longer contend for one row.

    Migration 16 keyed ``validity_witnesses`` on (mission, consumer, purpose, scope,
    epoch, support revision) and made it UNIQUE, so a leaf that both consumes an
    accepted output and sits under a gated method could hold only one of its two
    licences — and waited for the one that lost.  Migration 18 puts the licensed
    *subject* in the key: an acceptance and a condition set are different subjects,
    so both rows exist and the occurrence is licensed for what it actually needs.
    """

    from agent_orchestrator.contracts.evidence_state import ValidityWitness
    from agent_orchestrator.contracts.semantic_base import TypedRef

    world = _gated(tmp_path)
    leaf = _gated_leaf(world)
    snapshot = world.env.snapshot()
    epoch = world.semantics.epoch(world.mission.id, "mission")
    data_lane = ValidityWitness(
        witness_id="wit-in-occupied",
        consumer_ref=TypedRef(kind=TypedRefKind.TASK, id=leaf, revision=1, content_hash=HEX_A),
        purpose=WitnessPurpose.START,
        truth=TruthValue.TRUE,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=WitnessDecision.USABLE,
        scope_id="mission",
        scope_epoch=epoch,
        support_revision=int(snapshot.support_revision),
        as_of_ms=1_000,
        support_refs=(
            TypedRef(
                kind=TypedRefKind.ACCEPTANCE, id="acc-upstream", revision=1, content_hash=HEX_A
            ),
        ),
    )
    world.semantics.insert_validity_witness(
        world.mission.id, data_lane, subject=witness_subject(data_lane)
    )
    issued = world.dispatch.issue_start_witnesses(world.mission.id, now_ms=1_000_000)
    assert issued, "the precondition lane is stored beside the DATA lane, not instead of it"
    assert not world.events(WITNESS_KEY_TAKEN), "there is nothing anomalous to report"
    held = world.semantics.list_validity_witnesses(world.mission.id)
    assert {witness_subject(item) for item in held} >= {
        "acceptance:acc-upstream",
    }, "the DATA licence is still there under its own subject"
    assert world.dispatch.start_witnesses(world.mission.id, leaf), "and the leaf is licensed"


def test_two_conclusions_about_the_same_subject_still_conflict(tmp_path) -> None:
    """The key still refuses one reading of one world giving two answers.

    Decision 1 widened the key by exactly one dimension; it did not open it.  A second
    licence naming the *same* subject, epoch and support revision as one already held
    is a contradiction, and it is recorded rather than swallowed.
    """

    from agent_orchestrator.contracts.evidence_state import ValidityWitness
    from agent_orchestrator.contracts.semantic_base import TypedRef

    world = _gated(tmp_path)
    leaf = _gated_leaf(world)
    snapshot = world.env.snapshot()
    epoch = world.semantics.epoch(world.mission.id, "mission")
    issued = world.dispatch.issue_start_witnesses(world.mission.id, now_ms=1_000_000)
    assert issued, "the precondition lane issued its licence"
    rival = ValidityWitness(
        witness_id="wit-pre-rival",
        consumer_ref=TypedRef(kind=TypedRefKind.TASK, id=leaf, revision=1, content_hash=HEX_A),
        purpose=WitnessPurpose.START,
        truth=TruthValue.UNKNOWN,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=WitnessDecision.BLOCKED,
        scope_id="mission",
        scope_epoch=epoch,
        support_revision=int(snapshot.support_revision),
        as_of_ms=1_000,
        reason_codes=tuple(
            code
            for code in next(
                item for item in issued if str(item.consumer_ref.id) == leaf
            ).reason_codes
        ),
    )
    stored = world.dispatch._record_witness(world.mission.id, rival, subject=witness_subject(rival))
    assert stored is None, "the same subject at the same reading may not hold two verdicts"
    world.dispatch._witness_key_taken(world.mission.id, rival, subject=witness_subject(rival))
    taken = world.events(WITNESS_KEY_TAKEN)
    assert len(taken) == 1
    assert taken[0].payload["consumer"] == leaf
    assert taken[0].payload["subject_digest"].startswith("conditions:")
    assert taken[0].payload["reason_codes"], "the record says which licence was refused"


def test_an_assembly_with_no_planning_world_issues_no_start_licence(tmp_path) -> None:
    """Fail closed: no predicates and no snapshot means no licence, not a crash.

    ``_decide`` calls this on every cycle, so a deployment that installed the
    assembly without a ``PlanningWorld`` would otherwise take a ``ContractError``
    straight out of the shared loop — the occurrences are already withheld with
    ``witness_missing``, which is the right answer, and the deployment defect is
    reported by its own event rather than by an exception here.
    """

    world = _gated(tmp_path)
    bare = HierarchicalDispatch(world.store, world.service)
    assert bare.issue_start_witnesses(world.mission.id, now_ms=1_000_000) == ()
    assert bare.start_witnesses(world.mission.id, _gated_leaf(world)) == {}
