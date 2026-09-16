# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3a red tests: one plan revision is committed atomically, or not at all.

Four properties, in order of how much they would cost to get wrong:

1. **A legacy Mission is untouched.**  ``commit_plan_revision`` refuses it at the
   door, leaves all thirty-one migration-16 tables empty and appends no event; the
   Mission's stored event bytes are identical before and after the refused call
   (§18.5 rule 1 and rule 3).
2. **The two gates are two gates.**  The integer ``base_graph_version`` decides
   first and is never automatically rebased in this mode; the semantic read-set
   decides second, channel by channel, and one stale item is enough (ADR-13, C19,
   C29).
3. **Nothing is half-written.**  A failure anywhere in the write half leaves no
   plan revision, no membership, no method instance and no receipt.
4. **A replay is not a second commit.**  The same command twice yields the same
   receipt; the same id with a different intent is a conflict, not a replay.

The last block is the mutation self-check: each mutant is a plausible wrong
implementation, and the assertion the real tests make must catch it.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from simple_harness.contracts import canonical_json

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import Env, method, out, param, root_network, step, task_binding  # noqa: E402

from agent_orchestrator.contracts import Budget, ContractError  # noqa: E402
from agent_orchestrator.contracts.evidence_state import (  # noqa: E402
    ObservationRecord,
    Validity,
)
from agent_orchestrator.contracts.htn import (  # noqa: E402
    AbsenceRead,
    MethodRegistryStatus,
    ObligationRelation,
    OccurrenceId,
    ReadItem,
    ReadItemKind,
    RunningWorkPolicy,
    ScopeEpochRead,
    SupportSetRead,
    TaskForm,
)
from agent_orchestrator.contracts.obligations import (  # noqa: E402
    Obligation,
    ShapeChange,
)
from agent_orchestrator.contracts.resolution import (  # noqa: E402
    Acceptance,
    AllExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequirementClass,
    RequirementsRevision,
    ReviewBinding,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewVerdict,
)
from agent_orchestrator.contracts.semantic_base import (  # noqa: E402
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.graph.task_network import (  # noqa: E402
    DEFAULT_PROJECTION_BUDGET,
    TaskNetworkSnapshot,
)
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    CommitService,
    MissionSpec,
)
from agent_orchestrator.orchestrator.obligation_commits import (  # noqa: E402
    DEMAND_ADMITTED,
    DEMAND_WITHDRAWN,
)
from agent_orchestrator.orchestrator.plan_commits import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    LEGACY_SEMANTICS,
    PLAN_REVISION_COMMITTED,
    SEMANTICS_KEY,
    CommitPlanCommand,
    PlanCommitRejected,
    PlanPrincipal,
)
from agent_orchestrator.planning.htn.applicability import assess_method  # noqa: E402
from agent_orchestrator.planning.htn.compiler import (  # noqa: E402
    BudgetRequirement,
    compile_refinement_bundle,
)
from agent_orchestrator.planning.htn.grounding import ground_method  # noqa: E402
from agent_orchestrator.storage.htn_schema import TABLES  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.storage.store import Store  # noqa: E402

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
FUEL = 8
HEX_OTHER = "f" * 64


# ------------------------------------------------------------------ the world builders
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
    return env


def _outer(method_id: str = "plan.outer"):
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


def _spec(key: str, *, mode: str) -> MissionSpec:
    return MissionSpec(
        goal="交付一个可验收的层次计划",
        success_criteria=("file:a.md",),
        tenant_id="tenant-p23a",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=200_000, max_attempts=12),
        orchestration_semantics_version=mode,
    )


@dataclass
class World:
    service: CommitService
    mission: Any
    env: Env
    contract: Any
    binding: Any
    draft: Any
    bundle: Any
    command: CommitPlanCommand
    principal: PlanPrincipal

    @property
    def store(self) -> Store:
        return self.service.store

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.service.store)

    @property
    def duties(self) -> ObligationStore:
        return ObligationStore(self.service.store)

    def commit(self, command: CommitPlanCommand | None = None, principal=None):
        return self.service.commit_plan_revision(
            command or self.command, principal or self.principal
        )


def _world(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS, key: str = "p23a") -> World:
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec(key, mode=mode))
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
    network = root_network(env, binding)
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
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    command = CommitPlanCommand(
        command_id="cmd-1",
        mission_id=mission.id,
        delta=bundle.delta,
        network=bundle.network,
        task_bindings=bundle.task_bindings,
        base_graph_version=1,
        issued_by="manager-1",
        scope_id="mission",
        source={"intent_id": "plan-1"},
    )
    return World(
        service=service,
        mission=mission,
        env=env,
        contract=contract,
        binding=binding,
        draft=draft,
        bundle=bundle,
        command=command,
        principal=PlanPrincipal("manager-1", "mission", 0),
    )


def _with_read_set(world: World, **changes) -> CommitPlanCommand:
    read_set = dataclasses.replace(world.command.delta.read_set, **changes)
    delta = dataclasses.replace(world.command.delta, read_set=read_set)
    return dataclasses.replace(world.command, delta=delta)


def _new_table_counts(service: CommitService) -> dict[str, int]:
    return {
        table: int(
            service.store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        )
        for table in TABLES
    }


def _payload_bytes(service: CommitService, mission_id: str) -> list[tuple[str, str]]:
    rows = service.store.connection.execute(
        "SELECT type, payload_json FROM events WHERE mission_id = ? ORDER BY seq", (mission_id,)
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _end(world: World) -> None:
    """Take the Mission to a terminal state (CREATED may only be cancelled)."""

    world.service.cancel_mission(world.mission.id)


def _events(world: World, kind: str) -> list[Any]:
    return [e for e in world.store.list_events(world.mission.id) if e.type == kind]


def _other_intent(world: World) -> CommitPlanCommand:
    """The same command id asking for something else — a different *payload*.

    ``source`` is metadata and deliberately outside the intent hash, so the
    difference has to be something the commit would actually act on.
    """

    budget = dataclasses.replace(world.command.structure_budget, budget_version=99)
    return dataclasses.replace(world.command, structure_budget=budget)


def _refusal(world: World, command: CommitPlanCommand | None = None, principal=None) -> str:
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(command, principal)
    return caught.value.reason


# ====================================================================== the legacy door
def test_a_legacy_mission_is_refused_at_the_door(tmp_path):
    world = _world(tmp_path, mode=LEGACY_SEMANTICS)
    assert _refusal(world) == "SEMANTICS_NOT_HIERARCHICAL"


def test_the_refused_legacy_command_leaves_every_new_table_empty(tmp_path):
    world = _world(tmp_path, mode=LEGACY_SEMANTICS)
    _refusal(world)
    assert _new_table_counts(world.service) == dict.fromkeys(TABLES, 0)


def test_the_refused_legacy_command_appends_no_event(tmp_path):
    world = _world(tmp_path, mode=LEGACY_SEMANTICS)
    before = _payload_bytes(world.service, world.mission.id)
    _refusal(world)
    assert _payload_bytes(world.service, world.mission.id) == before
    assert all(kind != PLAN_REVISION_COMMITTED for kind, _ in before)


def test_the_legacy_branch_never_reaches_the_semantic_store(tmp_path, monkeypatch):
    """The door is closed before the first migration-16 read, not after it."""

    world = _world(tmp_path, mode=LEGACY_SEMANTICS)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the legacy branch reached the semantic layer")

    monkeypatch.setattr("agent_orchestrator.orchestrator.plan_commits.HtnStore", _explode)
    monkeypatch.setattr("agent_orchestrator.orchestrator.plan_commits.ObligationStore", _explode)
    assert _refusal(world) == "SEMANTICS_NOT_HIERARCHICAL"


def test_the_server_side_default_is_legacy(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    spec = MissionSpec(
        goal="g", success_criteria=("file:a.md",), tenant_id="t", idempotency_key="default"
    )
    assert spec.orchestration_semantics_version == LEGACY_SEMANTICS
    mission, _ = service.create_mission(spec)
    assert SEMANTICS_KEY not in (mission.final_report or {})


def test_the_default_does_not_change_the_mission_spec_hash(tmp_path):
    """A Host that re-sends the same request after upgrading must not get a conflict."""

    del tmp_path
    spec = MissionSpec(
        goal="g", success_criteria=("file:a.md",), tenant_id="t", idempotency_key="hash"
    )
    assert SEMANTICS_KEY not in spec.to_json()
    hierarchical = dataclasses.replace(spec, orchestration_semantics_version=HIERARCHICAL_SEMANTICS)
    assert hierarchical.to_json()[SEMANTICS_KEY] == HIERARCHICAL_SEMANTICS
    assert spec.to_json() != hierarchical.to_json()


def test_the_plan_pack_spelling_is_the_same_mode(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(
        MissionSpec(
            goal="g",
            success_criteria=("file:a.md",),
            tenant_id="t",
            idempotency_key="alias",
            orchestration_semantics_version="full-target-v1",
        )
    )
    assert (mission.final_report or {})[SEMANTICS_KEY] == HIERARCHICAL_SEMANTICS


def test_an_unknown_semantics_version_is_refused_before_anything_is_written(tmp_path):
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    with pytest.raises(CommitRejected):
        service.create_mission(
            MissionSpec(
                goal="g",
                success_criteria=("file:a.md",),
                tenant_id="t",
                idempotency_key="bad",
                orchestration_semantics_version="v2-maybe",
            )
        )
    assert service.store.list_missions() == []


def test_a_hierarchical_task_graph_without_bindings_is_refused(tmp_path):
    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("graph-gate", mode=HIERARCHICAL_SEMANTICS))
    planning = service.begin_planning(mission.id)
    proposal = TaskGraphProposal.from_json(
        {
            "tasks": [
                {
                    "key": "A",
                    "goal": "任务 A",
                    "rationale": "A 服务根目标",
                    "dependencies": [],
                    "success_criteria": ["file:a.md"],
                    "verification_policy": ["format_check"],
                    "allowed_tools": list(TOOLS),
                    "budget": {"max_tokens": 20_000, "max_attempts": 3},
                    "outputs": ["a.md"],
                }
            ]
        }
    )
    with pytest.raises(PlanCommitRejected) as caught:
        service.commit_task_graph(
            mission.id, proposal, base_version=planning.version, source={"planner": "fixture"}
        )
    assert caught.value.reason == "MISSING_SEMANTIC_BINDING"
    assert service.store.list_tasks(mission.id) == []


def test_a_legacy_task_graph_commit_is_byte_for_byte_what_it_was(tmp_path):
    """§18.5 rule 3: the hierarchical gate may not touch an old event's payload."""

    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    def run(root: Path) -> list[tuple[str, str]]:
        service = CommitService(Store.open(root / "orchestrator.db"))
        mission, _ = service.create_mission(_spec("legacy-graph", mode=LEGACY_SEMANTICS))
        planning = service.begin_planning(mission.id)
        service.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json(
                {
                    "tasks": [
                        {
                            "key": "A",
                            "goal": "任务 A",
                            "rationale": "A 服务根目标",
                            "dependencies": [],
                            "success_criteria": ["file:a.md"],
                            "verification_policy": ["format_check"],
                            "allowed_tools": list(TOOLS),
                            "budget": {"max_tokens": 20_000, "max_attempts": 3},
                            "outputs": ["a.md"],
                        }
                    ]
                }
            ),
            base_version=planning.version,
            source={"planner": "fixture"},
        )
        return _payload_bytes(service, mission.id)

    first = run(tmp_path / "one")
    second = run(tmp_path / "two")
    assert first == second
    assert {kind for kind, _ in first} == {
        "MissionCreated",
        "MissionPlanning",
        "TaskCommitted",
        "TaskGraphCommitted",
        "MissionActivated",
        "PolicyBound",
    } & {kind for kind, _ in first}
    for kind, payload in first:
        assert SEMANTICS_KEY not in payload, kind
        assert "plan_revision" not in payload, kind


# ================================================================== identity and replay
def test_a_forged_principal_cannot_present_someone_elses_command(tmp_path):
    world = _world(tmp_path)
    assert (
        _refusal(world, principal=PlanPrincipal("manager-2", "mission", 0)) == "PRINCIPAL_MISMATCH"
    )


def test_a_principal_may_not_commit_into_another_scope(tmp_path):
    world = _world(tmp_path)
    command = dataclasses.replace(world.command, scope_id="team-b")
    assert _refusal(world, command) == "SCOPE_NOT_AUTHORIZED"


def test_the_same_command_twice_is_one_commit_and_one_receipt(tmp_path):
    world = _world(tmp_path)
    first = world.commit()
    second = world.commit()
    assert second.to_json() == first.to_json()
    assert len(world.semantics.list_plan_revisions(world.mission.id)) == 1
    assert len(_events(world, PLAN_REVISION_COMMITTED)) == 1


def test_the_same_command_id_with_another_intent_is_a_conflict(tmp_path):
    world = _world(tmp_path)
    world.commit()
    other = _other_intent(world)
    assert _refusal(world, other) == "COMMAND_PAYLOAD_CONFLICT"
    assert len(world.semantics.list_plan_revisions(world.mission.id)) == 1


def test_a_replay_still_answers_after_the_mission_has_ended(tmp_path):
    """§7.4: the receipt of a command that succeeded outlives the Mission."""

    world = _world(tmp_path)
    first = world.commit()
    _end(world)
    assert world.commit().to_json() == first.to_json()


def test_a_new_command_on_an_ended_mission_is_refused(tmp_path):
    world = _world(tmp_path)
    _end(world)
    assert _refusal(world) == "MISSION_NOT_WRITABLE"


# ================================================================== the two gates
def _raise_epoch(world: World, scope: str, *, to: int) -> int:
    """Move a scope's epoch to ``to``.  The first bump *creates* epoch 0."""

    current = -1
    while current < to:
        current = world.semantics.bump_epoch(world.mission.id, scope, bumped_by="manager-2")
    return current


def test_a_bumped_manager_epoch_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    assert _raise_epoch(world, "mission", to=1) == 1
    assert _refusal(world) == "MANAGER_EPOCH_STALE"


def test_a_principal_behind_the_epoch_is_refused_even_with_a_fresh_read_set(tmp_path):
    """A re-read proposal does not re-authorise the manager that produced it."""

    world = _world(tmp_path)
    current = _raise_epoch(world, "mission", to=1)
    moved = _with_read_set(world, manager_epoch=current)
    assert _refusal(world, moved) == "MANAGER_EPOCH_STALE"
    assert world.principal.manager_epoch == 0


def test_a_stale_integer_graph_version_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    command = dataclasses.replace(world.command, base_graph_version=7)
    assert _refusal(world, command) == "GRAPH_VERSION_STALE"


def test_a_stale_integer_base_is_never_rebased_automatically(tmp_path):
    """ADR-13 C19: the legacy touched-overlap replay is not reachable from here."""

    world = _world(tmp_path)
    command = dataclasses.replace(world.command, base_graph_version=7)
    _refusal(world, command)
    assert world.semantics.list_plan_revisions(world.mission.id) == ()
    assert _events(world, PLAN_REVISION_COMMITTED) == []


def _set_graph_version(world: World, value: int) -> None:
    """Move the Mission's integer graph version, the way a legacy graph change would."""

    mission = world.store.get_mission(world.mission.id)
    moved = dataclasses.replace(
        mission,
        final_report={**dict(mission.final_report or {}), "graph_version": value},
        version=mission.version + 1,
    )
    with world.store.transaction():
        world.store.update_mission(moved, expected_version=mission.version)


def test_a_base_behind_the_current_graph_version_is_refused(tmp_path):
    """The gate is equality, not "not newer than".

    A ``>=`` written where ``==`` belongs looks right on every ahead-of-current case
    and silently admits every behind-current one — which is the *normal* stale
    proposal: another manager committed while this one was compiling.
    """

    world = _world(tmp_path)
    _set_graph_version(world, 5)
    command = dataclasses.replace(world.command, base_graph_version=1)
    assert _refusal(world, command) == "GRAPH_VERSION_STALE"
    assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_a_base_behind_the_current_graph_version_is_not_rebased_either(tmp_path):
    world = _world(tmp_path)
    _set_graph_version(world, 5)
    command = dataclasses.replace(world.command, base_graph_version=4)
    assert _refusal(world, command) == "GRAPH_VERSION_STALE"
    assert _events(world, PLAN_REVISION_COMMITTED) == []


def test_the_integer_gate_names_both_versions_in_either_direction(tmp_path):
    world = _world(tmp_path)
    _set_graph_version(world, 5)
    with pytest.raises(PlanCommitRejected) as behind:
        world.commit(dataclasses.replace(world.command, base_graph_version=2))
    assert "version 2" in str(behind.value) and "current is 5" in str(behind.value)
    with pytest.raises(PlanCommitRejected) as ahead:
        world.commit(dataclasses.replace(world.command, base_graph_version=9))
    assert "version 9" in str(ahead.value) and "current is 5" in str(ahead.value)


def test_a_base_that_matches_the_moved_graph_version_passes_the_gate(tmp_path):
    """The positive half: the gate is not simply "refuse anything but 1"."""

    world = _world(tmp_path)
    _set_graph_version(world, 5)
    world.commit(dataclasses.replace(world.command, base_graph_version=5))
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_the_integer_gate_decides_before_the_read_set(tmp_path):
    """Both are stale; the cheap one that every Mission shares answers first."""

    world = _world(tmp_path)
    world.semantics.put_task_semantics(
        world.mission.id, dataclasses.replace(world.binding, contract_revision=2)
    )
    command = dataclasses.replace(world.command, base_graph_version=7)
    assert _refusal(world, command) == "GRAPH_VERSION_STALE"


# ========================================================= the read-set, channel by channel
def _criterion(identifier: str = "c-1") -> Criterion:
    return Criterion(
        criterion_id=identifier,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"criterion {identifier} is satisfied",
        requirement_class=RequirementClass.REQUIRED_OUTCOME,
        evaluation_kind=EvaluationKind.SEMANTIC,
    )


def test_a_moved_requirements_revision_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    world.semantics.insert_requirements_revision(
        RequirementsRevision(
            revision_id="requirements-3",  # type: ignore[arg-type]
            mission_id=world.mission.id,
            revision=3,
            criteria=(_criterion(),),
            success_expression=AllExpr((CriterionExpr("c-1"),)),
        )
    )
    assert _refusal(world) == "READ_SET_STALE"


def test_a_re_contracted_goal_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    world.semantics.put_task_semantics(
        world.mission.id,
        dataclasses.replace(world.binding, contract_revision=2, contract_hash=HEX_OTHER),
    )
    assert _refusal(world) == "READ_SET_STALE"


def test_a_method_whose_definition_moved_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    moved = tuple(
        dataclasses.replace(item, content_hash=HEX_OTHER)
        for item in world.command.delta.read_set.method_revisions
    )
    assert _refusal(world, _with_read_set(world, method_revisions=moved)) == "READ_SET_STALE"


def test_a_suspended_method_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    registration = world.env.registry.registration(world.contract.method_ref())
    world.semantics.set_method_registration(
        dataclasses.replace(registration, status=MethodRegistryStatus.SUSPENDED)
    )
    assert _refusal(world) == "READ_SET_STALE"


def test_a_method_the_store_does_not_hold_cannot_be_re_checked(tmp_path):
    world = _world(tmp_path)
    unknown = (
        ReadItem(
            kind=ReadItemKind.METHOD, id="plan.nowhere", semantic_revision=1, content_hash=HEX_OTHER
        ),
    )
    assert _refusal(world, _with_read_set(world, method_revisions=unknown)) == "READ_SET_UNRESOLVED"


def _observe(world: World, name: str, key: str, at: int) -> ObservationRecord:
    record = ObservationRecord(
        observation_id=name,
        proposition_key=key,
        polarity=True,
        source_ref=TypedRef(
            kind=TypedRefKind.OBSERVATION, id=name, revision=1, content_hash="a" * 64
        ),
        observed_at_ms=at,
        recorded_at_ms=at,
    )
    world.semantics.insert_observation(world.mission.id, record)
    return record


def test_a_fresh_observation_read_passes(tmp_path):
    world = _world(tmp_path)
    _observe(world, "obs-1", "p-alpha", 10)
    item = world.service.read_item_for(world.mission.id, ReadItemKind.FACT, "obs-1")
    world.commit(_with_read_set(world, observation_revisions=(item,)))
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_a_superseded_observation_refuses_the_proposal(tmp_path):
    """AER I02: a counter-record leaves the original untouched and still invalidates it."""

    world = _world(tmp_path)
    _observe(world, "obs-1", "p-alpha", 10)
    item = world.service.read_item_for(world.mission.id, ReadItemKind.FACT, "obs-1")
    _observe(world, "obs-2", "p-alpha", 20)
    command = _with_read_set(world, observation_revisions=(item,))
    assert _refusal(world, command) == "READ_SET_STALE"


def test_a_fact_that_names_nothing_the_store_holds_is_unresolvable(tmp_path):
    world = _world(tmp_path)
    ghost = (
        ReadItem(
            kind=ReadItemKind.FACT, id="obs-ghost", semantic_revision=0, content_hash=HEX_OTHER
        ),
    )
    assert (
        _refusal(world, _with_read_set(world, observation_revisions=ghost)) == "READ_SET_UNRESOLVED"
    )


def _review_chain(world: World) -> None:
    """The package and the record an Acceptance must point at (AER §6.1)."""

    binding = ReviewBinding(
        mission_id=world.mission.id,
        obligation_id=ROOT_DUTY,
        subject_ref=TypedRef(
            kind=TypedRefKind.TASK, id=ROOT_TASK, revision=1, content_hash="a" * 64
        ),
        requirements_revision=0,
        input_manifest_hash="b" * 64,
        policy_ref=TypedRef(
            kind=TypedRefKind.REQUIREMENTS, id="policy-1", revision=1, content_hash="a" * 64
        ),
    )
    world.semantics.insert_review_package(
        ReviewPackage(
            package_id="package-1",  # type: ignore[arg-type]
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=binding,
            criteria=(_criterion(),),
            success_expression=AllExpr((CriterionExpr("c-1"),)),
        )
    )
    world.semantics.insert_review_record(
        ReviewRecord(
            record_id="rev-1",  # type: ignore[arg-type]
            package_id="package-1",  # type: ignore[arg-type]
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=binding,
            reviewer_agent_id="independent-agent",
            reviewer_turn_id="turn-2",
            evidence_manifest_hash="a" * 64,
            criteria=(
                CriterionOutcome(
                    criterion_id="c-1",
                    verdict=CriterionVerdict.PASS,
                    check_execution=CheckExecution.SUCCEEDED,
                ),
            ),
            verdict=ReviewVerdict.ACCEPT,
        ),
        official=True,
    )


def _acceptance(world: World, validity: Validity) -> Acceptance:
    return Acceptance(
        acceptance_id="acc-1",  # type: ignore[arg-type]
        mission_id=world.mission.id,
        task_id=ROOT_TASK,  # type: ignore[arg-type]
        obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
        requirements_revision=0,
        contract_revision=1,
        input_manifest_hash="b" * 64,
        review_record_id="rev-1",  # type: ignore[arg-type]
        accepted_at_ms=100,
        validity=validity,
    )


def test_a_fresh_acceptance_read_passes(tmp_path):
    world = _world(tmp_path)
    _review_chain(world)
    world.semantics.insert_acceptance(_acceptance(world, Validity.CURRENT))
    item = world.service.read_item_for(world.mission.id, ReadItemKind.ACCEPTANCE, "acc-1")
    world.commit(_with_read_set(world, acceptance_revisions=(item,)))
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_a_revoked_acceptance_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    _review_chain(world)
    world.semantics.insert_acceptance(_acceptance(world, Validity.CURRENT))
    item = world.service.read_item_for(world.mission.id, ReadItemKind.ACCEPTANCE, "acc-1")
    revoked = _world(tmp_path / "revoked", key="p23a-revoked")
    _review_chain(revoked)
    revoked.semantics.insert_acceptance(_acceptance(revoked, Validity.REVOKED))
    command = _with_read_set(revoked, acceptance_revisions=(item,))
    assert _refusal(revoked, command) == "READ_SET_STALE"


def test_a_duty_that_was_re_planned_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    item = world.service.read_item_for(world.mission.id, ReadItemKind.OBLIGATION, ROOT_DUTY)
    world.commit(_with_read_set(world, obligation_revisions=(item,)))  # fresh: it passes
    second = _world(tmp_path / "moved", key="p23a-moved")
    moved_item = second.service.read_item_for(second.mission.id, ReadItemKind.OBLIGATION, ROOT_DUTY)
    second.duties.note_shape_change(
        second.mission.id,
        ROOT_DUTY,  # type: ignore[arg-type]
        ShapeChange.METHOD_SWITCHED,
        detail="another manager switched the method",
    )
    command = _with_read_set(second, obligation_revisions=(moved_item,))
    assert _refusal(second, command) == "READ_SET_STALE"


def _approval(world: World, version: int, state: str = "granted") -> None:
    world.store.put_approval(
        {
            "request_id": "auth-1",
            "kind": "plan",
            "mission_id": world.mission.id,
            "subject_key": ROOT_TASK,
            "state": state,
            "version": version,
        }
    )


def test_a_fresh_authority_read_passes(tmp_path):
    world = _world(tmp_path)
    _approval(world, 1)
    item = world.service.read_item_for(world.mission.id, ReadItemKind.AUTHORITY, "auth-1")
    world.commit(_with_read_set(world, authority_revisions=(item,)))
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_a_revoked_authority_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    _approval(world, 1)
    item = world.service.read_item_for(world.mission.id, ReadItemKind.AUTHORITY, "auth-1")
    _approval(world, 2, state="revoked")
    assert _refusal(world, _with_read_set(world, authority_revisions=(item,))) == "READ_SET_STALE"


def _support(world: World, members) -> Any:
    return world.semantics.insert_justification_set(
        world.mission.id,
        "support-1",
        subject_kind="task",
        subject_id=ROOT_TASK,
        members=members,
        member_revision=1,
    )


def test_a_support_set_whose_members_changed_refuses_the_proposal(tmp_path):
    """C29: the positives are untouched; only the member digest can tell."""

    world = _world(tmp_path)
    positive = (
        TypedRef(kind=TypedRefKind.OBSERVATION, id="obs-1", revision=1, content_hash="a" * 64),
        True,
    )
    stored = _support(world, [positive])
    read = SupportSetRead(
        support_set_id="support-1", revision=1, member_digest=stored.member_digest
    )
    world.commit(_with_read_set(world, support_sets=(read,)))  # fresh: it passes

    other = _world(tmp_path / "counter", key="p23a-counter")
    counter = (
        TypedRef(kind=TypedRefKind.OBSERVATION, id="obs-2", revision=1, content_hash="c" * 64),
        False,
    )
    _support(other, [positive, counter])
    command = _with_read_set(other, support_sets=(read,))
    assert _refusal(other, command) == "READ_SET_STALE"


def test_a_support_set_the_store_does_not_hold_is_unresolvable(tmp_path):
    world = _world(tmp_path)
    read = SupportSetRead(support_set_id="support-ghost", revision=1, member_digest="a" * 64)
    assert _refusal(world, _with_read_set(world, support_sets=(read,))) == "READ_SET_UNRESOLVED"


def test_a_bumped_validity_epoch_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    read = ScopeEpochRead(scope_id="evidence", validity_epoch=0)
    world.commit(_with_read_set(world, scope_epochs=(read,)))  # fresh: it passes

    other = _world(tmp_path / "epoch", key="p23a-epoch")
    _raise_epoch(other, "evidence", to=1)
    assert _refusal(other, _with_read_set(other, scope_epochs=(read,))) == "READ_SET_STALE"


def test_an_absence_that_became_true_refuses_the_proposal(tmp_path):
    world = _world(tmp_path)
    read = AbsenceRead(predicate="no_obligation", scope_id="obl-later", range_revision=0)
    world.commit(_with_read_set(world, absences=(read,)))  # nothing is there: it passes

    other = _world(tmp_path / "absence", key="p23a-absence")
    other.duties.register(
        Obligation(
            obligation_id="obl-later",  # type: ignore[arg-type]
            mission_id=other.mission.id,
            requirement_refs=("req-2",),
            goal_signature_id="plan.goal",
        ),
        recursion_fuel=1,
    )
    assert _refusal(other, _with_read_set(other, absences=(read,))) == "READ_SET_STALE"


def test_an_absence_predicate_this_deployment_cannot_check_is_unresolvable(tmp_path):
    world = _world(tmp_path)
    read = AbsenceRead(predicate="no_unicorns", scope_id="anywhere", range_revision=0)
    assert _refusal(world, _with_read_set(world, absences=(read,))) == "READ_SET_UNRESOLVED"


def test_one_stale_channel_is_enough_and_the_others_are_still_reported(tmp_path):
    world = _world(tmp_path)
    world.semantics.put_task_semantics(
        world.mission.id, dataclasses.replace(world.binding, contract_revision=2)
    )
    _raise_epoch(world, "evidence", to=1)
    read = ScopeEpochRead(scope_id="evidence", validity_epoch=0)
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(_with_read_set(world, scope_epochs=(read,)))
    assert caught.value.reason == "READ_SET_STALE"
    assert "goal" in caught.value.detail and "validity_epoch" in caught.value.detail


# ================================================================== the plan revision gate
def test_a_delta_based_on_the_wrong_plan_revision_is_refused(tmp_path):
    world = _world(tmp_path)
    world.commit()
    assert (
        _refusal(world, dataclasses.replace(world.command, command_id="cmd-2"))
        == "PLAN_REVISION_STALE"
    )


def test_a_certificate_for_another_revision_is_refused(tmp_path):
    world = _world(tmp_path)
    network = dataclasses.replace(world.bundle.network, plan_revision=5)
    command = dataclasses.replace(world.command, network=network)
    assert _refusal(world, command) == "PLAN_REVISION_STALE"


# ================================================================== structure, re-checked
def test_a_network_that_merged_into_a_cycle_is_refused(tmp_path):
    """TG §7.3: two increments that each add one edge can still close a loop."""

    from agent_orchestrator.contracts.htn import OrderConstraint, ReleaseCondition

    world = _world(tmp_path)
    first, second = (spec.occurrence_id for spec in world.command.delta.occurrences)
    looped = dataclasses.replace(
        world.bundle.network,
        order_constraints=(
            OrderConstraint(
                before=first, after=second, release_condition=ReleaseCondition.ACCEPTED
            ),
            OrderConstraint(
                before=second, after=first, release_condition=ReleaseCondition.ACCEPTED
            ),
        ),
    )
    command = dataclasses.replace(world.command, network=looped)
    assert _refusal(world, command) == "STRUCTURE_INVALID"
    assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_a_network_that_does_not_contain_the_delta_is_refused(tmp_path):
    world = _world(tmp_path)
    command = dataclasses.replace(world.command, network=root_network(world.env, world.binding))
    assert _refusal(world, command) == "PLAN_REVISION_STALE"


def test_a_network_from_another_mission_is_refused(tmp_path):
    world = _world(tmp_path)
    foreign = dataclasses.replace(world.bundle.network, mission_id="mission-elsewhere")
    assert _refusal(world, dataclasses.replace(world.command, network=foreign)) == (
        "STRUCTURE_INVALID"
    )


# ================================================================== the budget
def test_a_structure_budget_that_cannot_hold_the_plan_reports_the_bound(tmp_path):
    world = _world(tmp_path)
    tight = dataclasses.replace(world.command.structure_budget, max_live_tasks=1)
    assert _refusal(world, dataclasses.replace(world.command, structure_budget=tight)) == (
        "BOUND_REACHED"
    )


def test_an_under_declared_cost_is_refused(tmp_path):
    world = _world(tmp_path)
    lying = BudgetRequirement(
        obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
        new_occurrences=0,
        new_primitive_occurrences=0,
        new_compound_occurrences=0,
        referenced_occurrences=0,
        new_order_constraints=0,
        new_data_requirements=0,
        max_fan_out=0,
    )
    command = dataclasses.replace(world.command, budget_requirement=lying)
    assert _refusal(world, command) == "BUDGET_REQUIREMENT_MISMATCH"


def test_a_truthful_cost_declaration_passes(tmp_path):
    world = _world(tmp_path)
    honest = dataclasses.replace(
        world.bundle.budget_requirement,
        new_occurrences=len(world.command.delta.occurrences),
        new_order_constraints=len(world.command.delta.order_constraints),
        new_data_requirements=len(world.command.delta.data_requirements),
    )
    world.commit(dataclasses.replace(world.command, budget_requirement=honest))
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_an_opening_the_parent_cannot_fund_is_refused(tmp_path):
    """And it keeps its own name: "cannot afford" is not "nobody asked" (§7.4)."""

    world = _world(tmp_path)
    _admit_root_demand(world)
    command = _with_child_duty(world, "obl-child", fuel=FUEL + 5)
    assert _refusal(world, command) == "BUDGET_INSUFFICIENT"
    assert world.duties.obligation_ids(world.mission.id) == (ROOT_DUTY,)


# ================================================================== work in flight
def test_a_replaced_occurrence_loses_its_dispatch_generation(tmp_path):
    world = _world(tmp_path)
    world.commit()
    target = world.command.delta.occurrences[0]
    before = world.semantics.task_semantics_of(world.mission.id, str(target.task_id))
    follow_up = _second_revision(world, superseded=(target.occurrence_id,))
    world.commit(follow_up)
    after = world.semantics.task_semantics_of(world.mission.id, str(target.task_id))
    assert int(after.dispatch_generation) == int(before.dispatch_generation) + 1
    assert int(after.contract_revision) == int(before.contract_revision) + 1


def test_a_replaced_occurrence_is_registered_for_reconciliation(tmp_path):
    world = _world(tmp_path)
    world.commit()
    target = world.command.delta.occurrences[0]
    world.commit(_second_revision(world, superseded=(target.occurrence_id,)))
    dirty = world.semantics.list_dirty(world.mission.id)
    assert [entry.subject_id for entry in dirty] == [str(target.occurrence_id)]
    assert dirty[0].reason == "dispatch_generation_revoked"


def test_naming_an_occurrence_this_mission_does_not_hold_is_refused(tmp_path):
    world = _world(tmp_path)
    command = dataclasses.replace(
        world.command, superseded_occurrences=(OccurrenceId("occ-elsewhere"),)
    )
    assert _refusal(world, command) == "RUNNING_WORK_NOT_RECONCILED"


def test_retiring_a_method_under_the_retain_policy_is_refused(tmp_path):
    world = _world(tmp_path)
    world.commit()
    instance = world.command.delta.method_instances[0].instance_id
    follow_up = _second_revision(world, retired=(instance,))
    assert _refusal(world, follow_up) == "RUNNING_WORK_NOT_RECONCILED"


def _second_revision(
    world: World, *, superseded=(), retired=(), command_id: str = "cmd-2"
) -> CommitPlanCommand:
    """A do-nothing second revision on top of the first, for the in-flight tests."""

    delta = dataclasses.replace(
        world.command.delta,
        delta_id="delta-second",
        base_plan_revision=1,
        occurrences=(),
        method_instances=(),
        data_requirements=(),
        retired_instance_ids=tuple(retired),
        referenced_occurrences=tuple(
            spec.occurrence_id for spec in world.command.delta.occurrences
        ),
    )
    network = dataclasses.replace(world.bundle.network, plan_revision=2)
    return dataclasses.replace(
        world.command,
        command_id=command_id,
        delta=delta,
        network=network,
        task_bindings=(),
        superseded_occurrences=tuple(superseded),
        running_work_policy=(
            RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE
            if superseded
            else RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED
        ),
    )


# ================================================================== the successful commit
def test_the_new_revision_is_the_only_active_one(tmp_path):
    world = _world(tmp_path)
    world.commit()
    world.commit(_second_revision(world))
    states = {
        revision.revision: revision.state
        for revision in world.semantics.list_plan_revisions(world.mission.id)
    }
    assert states == {1: "RETIRED", 2: "ACTIVE"}
    assert world.semantics.active_plan_revision(world.mission.id).revision == 2


def test_the_membership_of_the_new_revision_is_the_whole_plan(tmp_path):
    world = _world(tmp_path)
    world.commit()
    members = world.semantics.list_plan_memberships(world.mission.id, 1)
    assert {str(spec.occurrence_id) for spec in members} == {
        str(spec.occurrence_id) for spec in world.bundle.network.occurrences
    }


def test_every_new_task_receives_its_semantic_binding(tmp_path):
    world = _world(tmp_path)
    world.commit()
    stored = {
        str(binding.task_id) for binding in world.semantics.list_task_semantics(world.mission.id)
    }
    assert stored == {ROOT_TASK} | {str(binding.task_id) for binding in world.bundle.task_bindings}


def test_the_adopted_method_instance_and_its_children_are_stored(tmp_path):
    world = _world(tmp_path)
    world.commit()
    instance = world.command.delta.method_instances[0]
    assert (
        world.semantics.method_instance_state(world.mission.id, str(instance.instance_id))
        == "ADOPTED"
    )
    children = world.semantics.list_child_occurrences(world.mission.id, str(instance.instance_id))
    assert {child.slot_key for child in children} == {"leaf", "review"}


def test_the_data_requirements_land_in_the_new_revision(tmp_path):
    world = _world(tmp_path)
    world.commit()
    stored = world.semantics.list_data_requirements(world.mission.id, 1)
    assert [item.requirement_id for item in stored] == [
        item.requirement_id for item in world.bundle.network.data_requirements
    ]


def test_the_read_set_is_indexed_against_the_proposal(tmp_path):
    world = _world(tmp_path)
    world.commit()
    proposal_id = world.command.delta.delta_id
    assert (
        world.semantics.get_read_set(world.mission.id, proposal_id).to_json()
        == world.command.delta.read_set.to_json()
    )
    kinds = {
        item["subject_type"]
        for item in world.semantics.list_read_set_items(world.mission.id, proposal_id)
    }
    assert {"requirements", "manager_epoch", "task", "method"} <= kinds


def test_the_commit_appends_one_plan_revision_committed_event(tmp_path):
    world = _world(tmp_path)
    world.commit()
    events = _events(world, PLAN_REVISION_COMMITTED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["plan_revision"] == 1 and payload["base_plan_revision"] == 0
    assert payload["base_graph_version"] == 1
    assert payload["delta_id"] == world.command.delta.delta_id
    assert payload["added_occurrences"] == sorted(
        str(spec.occurrence_id) for spec in world.command.delta.occurrences
    )
    assert payload["pending_dispatch"] == payload["added_occurrences"]
    assert payload["revoked_dispatch_generations"] == {}


def test_the_event_payload_is_canonical(tmp_path):
    world = _world(tmp_path)
    world.commit()
    row = world.store.connection.execute(
        "SELECT payload_json FROM events WHERE type = ?", (PLAN_REVISION_COMMITTED,)
    ).fetchone()
    assert row[0] == canonical_json(json.loads(row[0]))


def test_the_receipt_carries_the_intent_the_read_set_and_the_versions(tmp_path):
    world = _world(tmp_path)
    receipt = world.commit()
    assert receipt.command_id == "cmd-1"
    assert receipt.base_plan_revision == 0 and receipt.new_plan_revision == 1
    assert receipt.intent_hash == world.command.intent_hash()
    assert receipt.read_set.to_json() == world.command.delta.read_set.to_json()
    assert receipt.read_set_hash == content_hash_of(world.command.delta.read_set.to_json())
    assert receipt.output_identity["plan_revision"] == 1
    assert receipt.detail["base_graph_version"] == 1


def test_the_commit_dispatches_nothing(tmp_path):
    """Registered, not dispatched.

    P2.3c part 2 changed what "registered" means — the commit now materialises one
    ``Task`` row per occurrence, because a plan whose occurrences never reach the
    ``tasks`` table can never be scheduled at all.  What it did **not** change is the
    property this test is about: committing a plan creates no Attempt and no service
    intent.  Deciding *which* row runs is the scheduler's, one cycle later, and it
    still has to pass the readiness gate to do it.
    """

    world = _world(tmp_path)
    receipt = world.commit()
    assert receipt.output_identity["pending_dispatch"]
    assert world.store.list_intents("PENDING", "CLAIMED", "RUNNING") == []
    rows = world.store.list_tasks(world.mission.id)
    assert {task.id for task in rows} == {
        str(spec.task_id) for spec in world.command.network.occurrences
    }
    assert all(not world.store.list_attempts(task.id) for task in rows)


# ================================================================== atomicity and concurrency
def test_a_failure_in_the_write_half_rolls_everything_back(tmp_path, monkeypatch):
    world = _world(tmp_path)
    before = _new_table_counts(world.service)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store went away mid-write")

    monkeypatch.setattr(HtnStore, "record_commit_receipt", _boom)
    with pytest.raises(RuntimeError):
        world.commit()
    assert _new_table_counts(world.service) == before
    assert _events(world, PLAN_REVISION_COMMITTED) == []


def test_two_managers_proposing_at_once_leave_only_the_first_one_standing(tmp_path):
    """The second read the root goal before the first re-contracted it."""

    world = _world(tmp_path)
    world.commit()
    assert _refusal(world, _other_intent(world)) == "COMMAND_PAYLOAD_CONFLICT"

    fresh = _world(tmp_path / "race", key="p23a-race")
    rival = dataclasses.replace(fresh.command, command_id="cmd-rival")
    fresh.semantics.put_task_semantics(
        fresh.mission.id,
        dataclasses.replace(fresh.binding, contract_revision=2, contract_hash=HEX_OTHER),
    )
    assert _refusal(fresh, rival) == "READ_SET_STALE"


# =========================================== review round: the gaps the mutants found
# Each test below exists because a plausible wrong implementation survived the first
# round of self-checks.  The mutant is named in the docstring, not just implied.


def test_an_unsigned_command_is_refused_rather_than_attributed(tmp_path):
    """Mutant: ``if command.issued_by and ...`` — a blank issuer skips the comparison,
    so any principal holding the scope could commit a proposal it did not author, and
    the receipt would name a manager that never issued it."""

    world = _world(tmp_path)
    unsigned = dataclasses.replace(world.command, issued_by="")
    assert _refusal(world, unsigned) == "PRINCIPAL_MISMATCH"
    assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_an_unsigned_command_is_refused_for_every_principal(tmp_path):
    world = _world(tmp_path)
    unsigned = dataclasses.replace(world.command, issued_by="")
    assert _refusal(world, unsigned, PlanPrincipal("manager-2", "mission", 0)) == (
        "PRINCIPAL_MISMATCH"
    )


def test_a_whitespace_issuer_is_not_an_issuer(tmp_path):
    world = _world(tmp_path)
    assert _refusal(world, dataclasses.replace(world.command, issued_by="   ")) == (
        "PRINCIPAL_MISMATCH"
    )


def test_the_activation_is_inside_the_commit_transaction(tmp_path, monkeypatch):
    """§9.4: PREPARED → ACTIVE is not a second step.

    Mutant: activate the revision after the transaction commits.  Then a failure in
    the switch leaves a PREPARED revision durably on disk, and the Mission has half
    an old plan and half a new one.  Here the switch itself is made to fail: nothing
    at all may survive.
    """

    world = _world(tmp_path)

    def _explode(self, mission_id, revision):  # noqa: ANN001, ANN202
        raise RuntimeError("the activation failed")

    monkeypatch.setattr(HtnStore, "activate_plan_revision", _explode)
    with pytest.raises(RuntimeError):
        world.commit()
    assert world.semantics.list_plan_revisions(world.mission.id) == ()
    assert world.semantics.active_plan_revision(world.mission.id) is None
    assert world.semantics.list_plan_memberships(world.mission.id, 1) == ()
    assert _events(world, PLAN_REVISION_COMMITTED) == []


def test_no_prepared_revision_is_left_behind_by_a_failed_activation(tmp_path, monkeypatch):
    """The state a two-step activation would leave: a row in ``PREPARED``."""

    world = _world(tmp_path)
    monkeypatch.setattr(
        HtnStore,
        "activate_plan_revision",
        lambda self, mission_id, revision: (_ for _ in ()).throw(RuntimeError("no")),
    )
    with pytest.raises(RuntimeError):
        world.commit()
    assert _new_table_counts(world.service)["plan_revisions"] == 0


# ---------------------------------------------------------- a choice is a method instance
def _twin_instance(original, instance_id: str = "mi-twin"):
    """A second method instance over the same slots — a shared sub-goal (TG §12)."""

    return dataclasses.replace(
        original,
        instance_id=instance_id,  # type: ignore[arg-type]
        child_bindings=tuple(
            dataclasses.replace(child, instance_id=instance_id)  # type: ignore[arg-type]
            for child in original.child_bindings
        ),
    )


def test_the_snapshot_itself_refuses_two_adopted_methods_over_one_occurrence(tmp_path):
    """The invariant's first line of defence, checked where it already lives.

    ``TaskNetworkSnapshot`` will not *exist* with two adopted instances over one
    occurrence, so a proposer cannot compile that certificate at all.  The test is
    here, next to the commit's own check, because the commit's check only makes sense
    as the *second* line and a reader has to be able to see both.
    """

    world = _world(tmp_path)
    network = world.bundle.network
    twin = _twin_instance(network.method_instances[0])
    with pytest.raises(ContractError) as caught:
        dataclasses.replace(
            network,
            method_instances=(*network.method_instances, twin),
            adopted_instance_ids=(*network.adopted_instance_ids, twin.instance_id),
        )
    assert "alternatives are OR, not AND" in str(caught.value)


def test_a_delta_may_not_adopt_a_method_instance_the_certificate_omits(tmp_path):
    """Mutant: write ``delta.method_instances`` as ADOPTED without checking them.

    This is the hole the snapshot invariant cannot see: the write half inserts every
    instance the *delta* carries, so one the network never held lands an adopted row
    that no certificate was ever checked against — and two of those over one
    occurrence is the unresolved OR, assembled in the database.
    """

    world = _world(tmp_path)
    twin = _twin_instance(world.bundle.network.method_instances[0])
    delta = dataclasses.replace(
        world.command.delta,
        method_instances=(*world.command.delta.method_instances, twin),
    )
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(dataclasses.replace(world.command, delta=delta))
    assert caught.value.reason == "OR_NOT_RESOLVED"
    assert "past the certificate" in str(caught.value)
    assert world.semantics.list_plan_revisions(world.mission.id) == ()
    assert world.semantics.list_method_instances(world.mission.id) == ()


def test_a_second_adopted_instance_may_not_join_one_the_store_already_holds(tmp_path):
    """The store side of the same rule: revision 1 adopted an instance for the root
    occurrence, so revision 2 may not adopt another one there without retiring it."""

    world = _world(tmp_path)
    world.commit()
    follow_up = _second_revision(world)
    twin = _twin_instance(world.bundle.network.method_instances[0], "mi-second")
    delta = dataclasses.replace(follow_up.delta, method_instances=(twin,))
    network = dataclasses.replace(
        follow_up.network,
        method_instances=(*follow_up.network.method_instances, twin),
    )
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(dataclasses.replace(follow_up, delta=delta, network=network))
    assert caught.value.reason == "OR_NOT_RESOLVED"
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_an_unadopted_alternative_in_the_certificate_is_allowed(tmp_path):
    """An *alternative* is legitimate; an unresolved choice is not.  Keeping the
    second instance un-adopted is how a candidate is recorded without being run."""

    world = _world(tmp_path)
    network = world.bundle.network
    twin = _twin_instance(network.method_instances[0])
    command = dataclasses.replace(
        world.command,
        network=dataclasses.replace(network, method_instances=(*network.method_instances, twin)),
    )
    world.commit(command)
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1
    assert {
        str(item.instance_id)
        for item in world.semantics.list_method_instances(world.mission.id, state="ADOPTED")
    } == {str(network.method_instances[0].instance_id)}


def test_several_dependencies_at_the_graph_layer_are_read_as_and(tmp_path):
    """The other half of the same rule, stated as a *positive*.

    ``C`` depending on both ``A`` and ``B``, with both typed relations recorded, is
    admitted — the graph layer has one reading and it is AND.  The v2 cross-table
    check is representation drift, not an OR gate, and this test says so out loud so
    nobody re-reads it as one.
    """

    from agent_orchestrator.graph.task_graph import validate_graph_v2

    mission = _v2_mission(tmp_path, key="and-semantics")
    validated = validate_graph_v2(
        mission,
        _graph(_node("A"), _node("B"), _node("C", dependencies=["A", "B"])),
        structure_budget=dataclasses.replace(V2_BUDGET, max_fan_out=4, max_depth=4),
        typed_order=_typed(C=("A", "B")),
    )
    assert validated.order[-1] == "C"


# ------------------------------------------------------------- the network must preserve
def _shrunken_second_revision(world: World, **overrides):
    """A second revision whose network has lost the first revision's second node."""

    command = _second_revision(world, **overrides)
    network = command.network
    root_binding = next(
        binding for binding in network.task_bindings if str(binding.task_id) == ROOT_TASK
    )
    return dataclasses.replace(
        command,
        # the delta must not *reference* what the network no longer holds either, or the
        # dangling-endpoint check answers first and says something different
        delta=dataclasses.replace(command.delta, referenced_occurrences=()),
        network=dataclasses.replace(
            network,
            occurrences=network.occurrences[:1],
            task_bindings=(dataclasses.replace(root_binding, adopted_method_instance_id=None),),
            order_constraints=(),
            data_requirements=(),
            typed_edges=(),
            method_instances=(),
            adopted_instance_ids=(),
            obligation_coverage=(),
            required_obligations=(),
        ),
    )


def test_a_network_that_silently_drops_an_occurrence_is_refused(tmp_path):
    """Mutant: check only that the delta's own occurrences are present.

    Then a revision may *lose* work: the smaller network projects perfectly, the duty
    behind the dropped occurrence has nobody working on it, and nothing records that
    anyone decided that.
    """

    world = _world(tmp_path)
    world.commit()
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(_shrunken_second_revision(world))
    assert caught.value.reason == "PLAN_NOT_PRESERVED"
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_the_refusal_names_the_dropped_occurrence(tmp_path):
    world = _world(tmp_path)
    world.commit()
    dropped = str(world.bundle.network.occurrences[1].occurrence_id)
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(_shrunken_second_revision(world))
    assert dropped in str(caught.value)
    assert "never by omission" in str(caught.value)


def test_a_dropped_occurrence_that_is_superseded_passes_the_preservation_check(tmp_path):
    """Replacement by *decision* is what the check asks for, so naming the occurrence
    as superseded gets past it — whatever the remaining structural gates then say."""

    world = _world(tmp_path)
    world.commit()
    dropped = tuple(spec.occurrence_id for spec in world.bundle.network.occurrences[1:])
    command = _shrunken_second_revision(world, superseded=dropped)
    try:
        world.commit(command)
    except PlanCommitRejected as error:
        assert error.reason != "PLAN_NOT_PRESERVED", str(error)


def test_the_first_revision_has_nothing_to_preserve(tmp_path):
    """No plan in force, so no occurrence can be dropped — the check must not refuse
    the very first commit."""

    world = _world(tmp_path)
    world.commit()
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1


def test_an_occurrence_nobody_superseded_keeps_its_dispatch_generation(tmp_path):
    """Only the replaced subject loses its execution right (§9.4).

    Mutant: revoke every occurrence in the plan on each revision.  Then every commit
    invalidates every dispatch in flight, and the "retain if bindings unchanged"
    policy means nothing.
    """

    world = _world(tmp_path)
    world.commit()
    target, other = (spec for spec in world.command.delta.occurrences)
    before = world.semantics.task_semantics_of(world.mission.id, str(other.task_id))
    world.commit(_second_revision(world, superseded=(target.occurrence_id,)))
    after = world.semantics.task_semantics_of(world.mission.id, str(other.task_id))
    assert int(after.dispatch_generation) == int(before.dispatch_generation)
    assert int(after.contract_revision) == int(before.contract_revision)


# ------------------------------------------------------- the delta must be commit-ready
def test_a_delta_whose_bindings_disagree_with_the_network_is_refused(tmp_path):
    """Mutant: skip ``delta.assert_consistent_with``.

    The occurrence carries *copies* of the binding's duty and form; the binding stays
    the authority.  A disagreement admitted here means the plan and the task
    semantics say different things about the same work, and whichever the scheduler
    happens to read wins.
    """

    world = _world(tmp_path)
    delta = world.command.delta
    leaf = delta.occurrences[0]
    assert leaf.form is TaskForm.PRIMITIVE
    command = dataclasses.replace(
        world.command,
        delta=dataclasses.replace(
            delta,
            occurrences=(
                dataclasses.replace(leaf, form=TaskForm.COMPOUND),
                *delta.occurrences[1:],
            ),
        ),
    )
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(command)
    assert caught.value.reason == "STRUCTURE_INVALID"
    assert world.semantics.list_plan_revisions(world.mission.id) == ()


def _opening(world: World, obligation_id: str, *, fuel: int):
    from agent_orchestrator.contracts.htn import BudgetInheritance, ObligationOpening

    return ObligationOpening(
        obligation_id=obligation_id,  # type: ignore[arg-type]
        parent_obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
        relation="refines_parent",
        requirement_refs=("req-1",),
        goal_signature=world.binding.goal_signature,
        budget_inheritance=BudgetInheritance.INHERIT_PARENT_FUEL_SHARE,
        fuel_share=fuel,
    )


def _register_duty(world: World, obligation_id: str) -> None:
    world.duties.register(
        Obligation(
            obligation_id=obligation_id,  # type: ignore[arg-type]
            mission_id=world.mission.id,
            requirement_refs=("req-1",),
            goal_signature_id="plan.goal",
        ),
        recursion_fuel=2,
    )


def test_re_opening_an_existing_duty_is_a_named_refusal(tmp_path):
    """Mutant: leave ``require_commit_ready`` in the write half.

    There it ran *after* the openings were persisted, where a duty this delta just
    opened is indistinguishable from one that already existed — so it raised a bare
    ``ContractError`` with no reason name, from inside the write, for a case the
    proposer could not act on.
    """

    world = _world(tmp_path)
    _register_duty(world, "obl-existing")
    delta = dataclasses.replace(
        world.command.delta, obligation_openings=(_opening(world, "obl-existing", fuel=1),)
    )
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(dataclasses.replace(world.command, delta=delta))
    assert caught.value.reason == "DELTA_NOT_COMMIT_READY"
    assert "referenced, not opened again" in str(caught.value)


def _admit_root_demand(world: World) -> None:
    """The Mission's own duty is asked for by the requirements it was created from."""

    world.service.admit_obligation_demand(
        world.mission.id,
        ROOT_DUTY,
        principal="manager-1",
        requester={"kind": "mission_root"},
        evidence={"requirement_refs": ["req-1"]},
    )


def _adopting_slot_for(world: World, duty: str, *, fuel: int = 2):
    """The delta, network and bindings with the first slot adopting ``duty``.

    P2.3c part 2d, decision 3: an opening is only legitimate when a slot of this
    same delta adopts the duty it opens (TG §9.2's ``DemandRef``).  This helper
    builds exactly that shape, so the tests below exercise the real rule rather than
    a duty floating free of the plan that wanted it.
    """

    delta = world.command.delta
    draft = delta.method_instances[0]
    first = draft.child_bindings[0]
    occurrence = first.occurrence_id
    adopted = dataclasses.replace(
        draft,
        child_bindings=(
            dataclasses.replace(first, obligation_id=duty),
            *draft.child_bindings[1:],
        ),
    )
    task_of = {str(spec.occurrence_id): str(spec.task_id) for spec in delta.occurrences}
    owner = task_of[str(occurrence)]

    def _respec(specs):
        return tuple(
            dataclasses.replace(spec, obligation_id=duty)
            if spec.occurrence_id == occurrence
            else spec
            for spec in specs
        )

    def _rebind(bindings):
        return tuple(
            dataclasses.replace(item, obligation_id=duty) if str(item.task_id) == owner else item
            for item in bindings
        )

    return (
        dataclasses.replace(
            delta,
            method_instances=(adopted, *delta.method_instances[1:]),
            occurrences=_respec(delta.occurrences),
            obligation_openings=(_opening(world, duty, fuel=fuel),),
        ),
        dataclasses.replace(
            world.command.network,
            occurrences=_respec(world.command.network.occurrences),
            method_instances=(adopted, *world.command.network.method_instances[1:]),
            task_bindings=_rebind(world.command.network.task_bindings),
        ),
        _rebind(world.command.task_bindings),
    )


def _with_child_duty(world: World, duty: str, *, fuel: int = 2) -> CommitPlanCommand:
    delta, network, bindings = _adopting_slot_for(world, duty, fuel=fuel)
    return dataclasses.replace(world.command, delta=delta, network=network, task_bindings=bindings)


def test_a_refining_opening_gets_its_demand_from_the_slot_that_adopted_it(tmp_path):
    """P2.3c part 2d, decision 3 (memo test 1).

    Before it, nothing on the production path ever admitted a demand: a duty this
    delta opened was registered, funded, materialised as a Task — and permanently
    undispatchable, because ``has_admitted_demand`` stayed false and TG §6's gate
    (correctly) withheld it.  The slot that adopted the duty is the consumer, so the
    admission happens in this same transaction and says so.

    It also covers the positive case the old commit-ready placement made impossible:
    with the duty set read *after* the openings were written, every legitimate
    opening looked like a re-opening.
    """

    world = _world(tmp_path)
    _admit_root_demand(world)
    world.commit(_with_child_duty(world, "obl-child"))
    assert set(world.duties.obligation_ids(world.mission.id)) == {ROOT_DUTY, "obl-child"}
    assert world.semantics.active_plan_revision(world.mission.id).revision == 1
    assert world.duties.account(world.mission.id, "obl-child").has_admitted_demand is True
    committed = _events(world, PLAN_REVISION_COMMITTED)[0]
    assert committed.payload["opened_obligations"] == ["obl-child"]
    assert committed.payload["admitted_demands"] == ["obl-child"]


def test_the_admission_event_names_the_principal_and_the_requesting_slot(tmp_path):
    """Memo test 5: an admission is a record with a name on it, not a bit flip."""

    world = _world(tmp_path)
    _admit_root_demand(world)
    command = _with_child_duty(world, "obl-child")
    world.commit(command)
    admissions = _events(world, DEMAND_ADMITTED)
    assert [item.payload["obligation_id"] for item in admissions] == [ROOT_DUTY, "obl-child"]
    child = admissions[-1].payload
    assert child["principal"] == "manager-1"
    assert child["relation"] == str(ObligationRelation.REFINES_PARENT)
    assert child["parent_obligation_id"] == ROOT_DUTY
    assert child["plan_revision"] == 1
    requester = child["requester"]
    assert requester["kind"] == "method_slot"
    adopted = command.delta.method_instances[0]
    assert requester["method_instance_id"] == str(adopted.instance_id)
    assert requester["slot_key"] == str(adopted.child_bindings[0].slot_key)
    assert requester["occurrence_id"] == str(adopted.child_bindings[0].occurrence_id)
    assert child["evidence"]["delta_id"] == command.delta.delta_id
    # The Mission root's own admission says what *it* rests on: the requirements.
    assert admissions[0].payload["requester"]["kind"] == "mission_root"
    assert admissions[0].payload["evidence"]["requirement_refs"] == ["req-1"]


def test_an_opening_no_adopted_slot_asks_for_is_refused(tmp_path):
    """Memo test 2: a duty with no consumer is refused, and nothing is written.

    TG §3.2 is explicit that a candidate outside the approved execution scope gets
    **no** real demand.  Opening it anyway would create a duty that is funded out of
    its parent's allowance and can never be worked on — which is what part 2c's
    smoke sat in.
    """

    world = _world(tmp_path)
    _admit_root_demand(world)
    delta = dataclasses.replace(
        world.command.delta, obligation_openings=(_opening(world, "obl-child", fuel=2),)
    )
    command = dataclasses.replace(world.command, delta=delta)
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(command)
    assert caught.value.reason == "DEMAND_NOT_ADMITTED"
    assert "asks for the work of obl-child" in str(caught.value)
    assert set(world.duties.obligation_ids(world.mission.id)) == {ROOT_DUTY}
    assert world.semantics.list_plan_revisions(world.mission.id) == ()
    assert _new_table_counts(world.service)["plan_revisions"] == 0


def test_a_child_of_a_duty_nobody_demands_is_refused(tmp_path):
    """Memo test 3: an interest cannot be inherited from a parent that holds none."""

    world = _world(tmp_path)  # the root duty's demand is deliberately *not* admitted
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(_with_child_duty(world, "obl-child"))
    assert caught.value.reason == "DEMAND_NOT_ADMITTED"
    assert "has no admitted demand" in str(caught.value)
    assert set(world.duties.obligation_ids(world.mission.id)) == {ROOT_DUTY}


def test_an_independent_opening_without_an_authorization_ref_is_refused(tmp_path):
    """Memo test 4, at the contract where §6.1 puts it.

    An independently authorised duty stands on its own authority, so it does not
    inherit the parent's demand — and it may not be built at all without naming the
    authority that authorised it.  The refusal is the contract's, which is why no
    commit can route around it.
    """

    from agent_orchestrator.contracts.htn import BudgetInheritance, ObligationOpening

    world = _world(tmp_path)
    with pytest.raises(ContractError, match="authorised it"):
        ObligationOpening(
            obligation_id="obl-independent",  # type: ignore[arg-type]
            parent_obligation_id=ROOT_DUTY,  # type: ignore[arg-type]
            relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
            requirement_refs=("req-1",),
            goal_signature=world.binding.goal_signature,
            budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
            grant_ref="grant-1",
        )


def test_a_second_slot_binding_one_opening_is_refused_rather_than_shared(tmp_path):
    """§24.1 decision 9: a second consumer registers its own DemandRef.

    Two slots of one delta binding one newly opened duty is the shared-work case,
    and sharing is an explicit second admission — not one admission two slots quietly
    lean on, where whichever withdraws first takes the other's work away.
    """

    world = _world(tmp_path)
    _admit_root_demand(world)
    delta, network, bindings = _adopting_slot_for(world, "obl-child")
    draft = delta.method_instances[0]
    both = dataclasses.replace(
        draft,
        child_bindings=tuple(
            dataclasses.replace(item, obligation_id="obl-child") for item in draft.child_bindings
        ),
    )
    command = dataclasses.replace(
        world.command,
        delta=dataclasses.replace(delta, method_instances=(both,)),
        network=dataclasses.replace(network, method_instances=(both,)),
        task_bindings=bindings,
    )
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit(command)
    assert caught.value.reason == "DEMAND_NOT_ADMITTED"
    assert "registers its own DemandRef" in str(caught.value)


def test_retiring_the_adopting_slot_withdraws_only_its_own_demand(tmp_path):
    """Memo test 6: retiring a branch ends *its* interest and nobody else's.

    §24.1 decision 9 says a retiring branch removes **its own** adoption relation.
    The rule is exercised on the commit path's own helper rather than through a
    second full revision: a revision that retires the root's only adopted method and
    replaces it with nothing is refused for being structurally incomplete long before
    the demand question is reached, so driving the helper is what isolates *this*
    rule instead of testing the structure gate twice.

    Two cases, one world: nothing else binds the duty (released), and another
    adopted slot still binds it (kept — that is the shared sub-goal).
    """

    world = _world(tmp_path)
    _admit_root_demand(world)
    first = _with_child_duty(world, "obl-child")
    world.commit(first)
    semantics = HtnStore(world.store)
    duties = ObligationStore(world.store)
    retired_ids = (first.delta.method_instances[0].instance_id,)

    # Case A: another adopted slot still binds the duty, so the demand stays.
    still_wanted = dataclasses.replace(
        first,
        command_id="cmd-shared",
        delta=dataclasses.replace(
            first.delta, delta_id="delta-shared", retired_instance_ids=retired_ids
        ),
    )
    assert (
        world.service._withdraw_retired_demands(semantics, duties, still_wanted, plan_revision=2)
        == []
    )
    assert duties.account(world.mission.id, "obl-child").has_admitted_demand is True
    assert _events(world, DEMAND_WITHDRAWN) == []

    # Case B: the retired instance was the last consumer.
    orphaning = dataclasses.replace(
        first,
        command_id="cmd-retire",
        delta=dataclasses.replace(
            first.delta,
            delta_id="delta-retire",
            method_instances=(),
            retired_instance_ids=retired_ids,
        ),
    )
    assert world.service._withdraw_retired_demands(
        semantics, duties, orphaning, plan_revision=2
    ) == ["obl-child"]
    assert duties.account(world.mission.id, "obl-child").has_admitted_demand is False
    # The root duty belongs to a different consumer and is untouched.
    assert duties.account(world.mission.id, ROOT_DUTY).has_admitted_demand is True
    withdrawals = _events(world, DEMAND_WITHDRAWN)
    assert [item.payload["obligation_id"] for item in withdrawals] == ["obl-child"]
    assert withdrawals[0].payload["evidence"]["retired_method_instances"] == [str(retired_ids[0])]


def test_a_model_proposal_cannot_state_that_a_demand_was_admitted(tmp_path) -> None:
    """Memo test 7 — decision 3's codec mutation self-check.

    §18.5: the model proposes the shape of the work, never its authority.  "Somebody
    is asking for this duty" is an authority claim, and after decision 3 it is also
    the thing that makes an occurrence dispatchable — so a proposal that asserted it
    would be admitting its own work.  The typed codec refuses the key outright rather
    than reading and discarding it, and the fact that the key is refused is what makes
    the refusal visible to whoever wrote it.

    **Mutation**: list ``demand_admitted`` among ``ObligationOpening.from_json``'s
    optional fields (or drop the strict unknown-key check in ``fields_of``) and this
    test goes red — the payload is accepted, and nothing downstream would notice
    that the plan, not the commit, decided who wanted the work.
    """

    from agent_orchestrator.contracts.htn import ObligationOpening

    world = _world(tmp_path)
    payload = _opening(world, "obl-child", fuel=2).to_json()
    assert "demand_admitted" not in payload, "the contract does not carry the bit at all"
    assert "has_admitted_demand" not in payload
    with pytest.raises(ContractError, match="demand_admitted"):
        ObligationOpening.from_json({**payload, "demand_admitted": True})
    # And the honest payload still decodes, so the refusal is about the claim and not
    # about strictness in general.
    assert ObligationOpening.from_json(payload).obligation_id == "obl-child"


def test_nothing_outside_the_commit_path_admits_a_demand() -> None:
    """Memo test 8 — decision 3's static mutation self-check.

    ``demand_admitted`` is what makes an occurrence dispatchable, so flipping it is
    an authorisation act and belongs to one audited entry point.  The four files
    below are the ledger primitive, its store, the audited entry point and the
    commit-time rule that calls it; anywhere else is a second, unwritten way in.

    **Mutation**: add one bare ``ledger.admit_demand(...)`` to ``event_handler.py``
    and this test goes red.
    """

    import agent_orchestrator

    root = Path(agent_orchestrator.__file__).parent
    allowed = {
        "contracts/obligations.py",
        "storage/obligation_store.py",
        "orchestrator/obligation_commits.py",
        "planning/htn/compiler.py",
    }
    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "admit_demand(" in path.read_text(encoding="utf-8")
        and str(path.relative_to(root)) not in allowed
    )
    assert offenders == [], (
        "a demand may only be admitted through CommitService.admit_obligation_demand; "
        f"{offenders} name the ledger primitive directly"
    )


def test_the_commit_ready_gate_refuses_before_anything_is_written(tmp_path):
    world = _world(tmp_path)
    _register_duty(world, "obl-existing")
    delta = dataclasses.replace(
        world.command.delta, obligation_openings=(_opening(world, "obl-existing", fuel=1),)
    )
    assert _refusal(world, dataclasses.replace(world.command, delta=delta)) == (
        "DELTA_NOT_COMMIT_READY"
    )
    assert set(world.duties.obligation_ids(world.mission.id)) == {ROOT_DUTY, "obl-existing"}
    assert world.semantics.list_plan_revisions(world.mission.id) == ()
    assert _new_table_counts(world.service)["plan_revisions"] == 0


# ============================================== the v2 graph gate (graph/task_graph.py)
# The hierarchical admission gate for the *projected* DAG.  ``validate_graph`` keeps
# judging a legacy Mission against the two module constants — which is what every
# existing receipt and rejection message was produced under — so the two gates are
# tested against each other here, not one in terms of the other.
V2_BUDGET = dataclasses.replace(
    DEFAULT_PROJECTION_BUDGET, budget_version=7, max_nodes=3, max_depth=2, max_fan_out=1
)


def _node(key: str, dependencies=(), outputs=()):
    return {
        "key": key,
        "goal": f"任务 {key}",
        "rationale": f"{key} 服务根目标",
        "dependencies": list(dependencies),
        "success_criteria": [f"file:{key.lower()}.md"],
        "verification_policy": ["format_check"],
        "allowed_tools": list(TOOLS),
        "budget": {"max_tokens": 20_000, "max_attempts": 3},
        "outputs": list(outputs) or [f"{key.lower()}.md"],
    }


def _graph(*nodes):
    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    return TaskGraphProposal.from_json({"tasks": list(nodes)})


def _v2_mission(tmp_path, key: str = "v2"):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec(key, mode=HIERARCHICAL_SEMANTICS))
    return mission


def _typed(**pairs):
    """The ORDER/DATA relations behind the projected dependencies."""

    return {key: tuple(value) for key, value in pairs.items()}


def _v2_rejection(mission, proposal, **kwargs):
    from agent_orchestrator.graph.task_graph import GraphRejected, validate_graph_v2

    with pytest.raises(GraphRejected) as caught:
        validate_graph_v2(mission, proposal, structure_budget=V2_BUDGET, **kwargs)
    return caught.value


def test_v2_admits_a_plan_that_fits_the_structure_budget(tmp_path):
    from agent_orchestrator.graph.task_graph import validate_graph_v2

    mission = _v2_mission(tmp_path)
    validated = validate_graph_v2(
        mission,
        _graph(_node("A"), _node("B", dependencies=["A"])),
        structure_budget=V2_BUDGET,
        typed_order=_typed(B=("A",)),
    )
    assert validated.order == ("A", "B")
    assert validated.roots == ("A",) and validated.leaves == ("B",)


def test_v2_reports_the_bound_and_the_budget_version_rather_than_trimming(tmp_path):
    """ADR-08: ``bound_reached`` with the dimension named — never a silently
    shortened plan and never "the goal is impossible"."""

    mission = _v2_mission(tmp_path)
    error = _v2_rejection(
        mission,
        _graph(
            _node("A"),
            _node("B", dependencies=["A"]),
            _node("C", dependencies=["A"]),
            _node("D", dependencies=["A"]),
        ),
        typed_order=_typed(B=("A",), C=("A",), D=("A",)),
    )
    assert error.reason == "bound_reached"
    assert "max_nodes=3" in str(error) and "v7" in str(error)


def test_v2_bounds_the_depth_against_the_budget_not_the_module_constant(tmp_path):
    mission = _v2_mission(tmp_path)
    error = _v2_rejection(
        mission,
        _graph(_node("A"), _node("B", dependencies=["A"]), _node("C", dependencies=["B"])),
        typed_order=_typed(B=("A",), C=("B",)),
    )
    assert error.reason == "bound_reached"
    assert "max_depth=2" in str(error)


def test_v2_bounds_the_fan_out(tmp_path):
    mission = _v2_mission(tmp_path)
    error = _v2_rejection(
        mission,
        _graph(_node("A"), _node("B", dependencies=["A"]), _node("C", dependencies=["A"])),
        typed_order=_typed(B=("A",), C=("A",)),
    )
    assert error.reason == "bound_reached"
    assert "max_fan_out=1" in str(error)


def test_v2_refuses_a_dependency_no_typed_relation_backs(tmp_path):
    """Representation drift, direction 1: the projection a scheduler reads would
    order work the plan never ordered.  Nothing here is about OR — the graph layer
    reads several dependencies as AND, and the choice invariant is checked at the
    delta layer (``_check_or_resolved_at_method_layer``)."""

    mission = _v2_mission(tmp_path)
    error = _v2_rejection(
        mission, _graph(_node("A"), _node("B", dependencies=["A"])), typed_order=_typed()
    )
    assert error.reason == "representation_drift"
    assert "order work the plan never ordered" in str(error)


def test_v2_refuses_a_typed_relation_the_projection_dropped(tmp_path):
    """The other direction: the edge exists in the side table and not in the DAG the
    scheduler would read, so the work would be dispatched before its producer."""

    mission = _v2_mission(tmp_path)
    error = _v2_rejection(mission, _graph(_node("A"), _node("B")), typed_order=_typed(B=("A",)))
    assert error.reason == "representation_drift"
    assert "before its producer" in str(error)


def test_v2_refuses_typed_relations_about_a_node_the_proposal_lacks(tmp_path):
    mission = _v2_mission(tmp_path)
    error = _v2_rejection(mission, _graph(_node("A")), typed_order=_typed(Z=("A",)))
    assert error.reason == "representation_drift" and "'Z'" in str(error)


def test_v2_still_refuses_a_cycle_and_names_it_as_one(tmp_path):
    """A cycle is a cycle in both modes, with the same reason and the ring named —
    the new cross-table check must not relabel an old defect as a new one."""

    mission = _v2_mission(tmp_path)
    error = _v2_rejection(
        mission,
        _graph(_node("A", dependencies=["B"]), _node("B", dependencies=["A"])),
        typed_order=_typed(A=("B",), B=("A",)),
    )
    assert error.reason == "cycle" and "A -> B -> A" in str(error)


def test_v2_keeps_the_per_node_contract_checks(tmp_path):
    mission = _v2_mission(tmp_path)
    node = _node("A")
    node["rationale"] = "  "
    error = _v2_rejection(mission, _graph(node), typed_order=_typed())
    assert error.reason == "contract" and "rationale" in str(error)


def test_v2_keeps_the_mission_budget_check(tmp_path):
    mission = _v2_mission(tmp_path)
    node = _node("A")
    node["budget"] = {"max_tokens": 500_000, "max_attempts": 3}
    error = _v2_rejection(mission, _graph(node), typed_order=_typed())
    assert error.reason == "budget"


def test_the_legacy_gate_is_not_the_v2_gate(tmp_path):
    """A graph the *budget* refuses is still admitted by ``validate_graph``, because
    the legacy gate answers to ``MAX_TASKS`` / ``MAX_GRAPH_DEPTH`` and to nothing
    else.  If this ever fails, the v1 gate has started reading the new budget."""

    from agent_orchestrator.graph.task_graph import validate_graph

    mission = _v2_mission(tmp_path)
    proposal = _graph(_node("A"), _node("B", dependencies=["A"]), _node("C", dependencies=["B"]))
    assert validate_graph(mission, proposal).order == ("A", "B", "C")
    assert _v2_rejection(mission, proposal, typed_order=_typed(B=("A",), C=("B",))).reason == (
        "bound_reached"
    )


def test_the_legacy_gate_never_asks_about_typed_relations(tmp_path):
    """``dependencies`` alone is the whole relation in the legacy mode, so a graph
    with no side table at all is fine there — and refused by v2."""

    from agent_orchestrator.graph.task_graph import validate_graph

    mission = _v2_mission(tmp_path)
    proposal = _graph(_node("A"), _node("B", dependencies=["A"]))
    assert validate_graph(mission, proposal).order == ("A", "B")
    assert _v2_rejection(mission, proposal, typed_order=_typed()).reason == "representation_drift"


# ============================================ the durable fact, and the frozen bytes
def test_a_missing_semantic_binding_leaves_a_durable_rejection(tmp_path):
    """Mutant: raise and write nothing.

    Every other graph rejection records a ``TaskGraphRejected`` event — the refusal
    itself is a fact somebody has to be able to read later.  A hierarchical refusal
    that only raised would be the one kind of rejection with no trace, and the reason
    name would exist only in a stack trace.
    """

    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("durable", mode=HIERARCHICAL_SEMANTICS))
    planning = service.begin_planning(mission.id)
    proposal = TaskGraphProposal.from_json({"tasks": [_node("A")]})
    with pytest.raises(PlanCommitRejected) as caught:
        service.commit_task_graph(
            mission.id, proposal, base_version=planning.version, source={"intent_id": "i-1"}
        )
    assert caught.value.reason == "MISSING_SEMANTIC_BINDING"
    rejected = [e for e in service.store.list_events(mission.id) if e.type == "TaskGraphRejected"]
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "MISSING_SEMANTIC_BINDING"
    assert (
        "MISSING" in rejected[0].payload["detail"] or "semantic" in (rejected[0].payload["detail"])
    )
    assert service.store.list_tasks(mission.id) == []


def test_the_hierarchical_refusal_keeps_its_own_type_and_reason(tmp_path):
    """The durable fact is shared with the legacy path; the *name* is not laundered.

    A ``CommitRejected`` carrying the reason inside a message string would leave the
    caller with nothing to branch on, and ``MISSING_SEMANTIC_BINDING`` is a machine
    name the proposer acts on (§18.5).
    """

    from agent_orchestrator.graph.task_graph import TaskGraphProposal
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("typed-refusal", mode=HIERARCHICAL_SEMANTICS))
    planning = service.begin_planning(mission.id)
    with pytest.raises(PlanCommitRejected):
        service.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": [_node("A")]}),
            base_version=planning.version,
            source={"intent_id": "i-2"},
        )
    assert issubclass(PlanCommitRejected, Exception)
    assert not issubclass(PlanCommitRejected, CommitRejected)


def test_a_legacy_graph_rejection_still_answers_with_commit_rejected(tmp_path):
    """The legacy branch of the same ``except`` did not change shape."""

    from agent_orchestrator.graph.task_graph import TaskGraphProposal
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("legacy-refusal", mode=LEGACY_SEMANTICS))
    planning = service.begin_planning(mission.id)
    blank = _node("A")
    blank["rationale"] = "  "
    with pytest.raises(CommitRejected) as caught:
        service.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": [blank]}),
            base_version=planning.version,
            source={"intent_id": "i-3"},
        )
    assert "contract" in str(caught.value)
    rejected = [e for e in service.store.list_events(mission.id) if e.type == "TaskGraphRejected"]
    assert rejected and rejected[0].payload["reason"] == "contract"


#: The canonical bytes of every event a legacy Mission's task-graph commit appends,
#: hashed once and pinned here as a literal.  The "run it twice and compare" test
#: beside it only proves the path is *deterministic*; this one proves it is the same
#: path it was before P2.3a existed (§18.5 rule 3).  If this digest moves, an event
#: an old Mission already wrote has changed shape, and a golden that recomputed
#: itself from the live code could never say so.
LEGACY_GRAPH_EVENT_DIGEST = "e6f14c8549711be84db3275702a28fadfd1013dff36a6d645bfe5c004314a34b"


def _legacy_graph_world(root: Path) -> tuple[CommitService, str]:
    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    service = CommitService(Store.open(root / "orchestrator.db"))
    mission, _ = service.create_mission(_spec("legacy-golden", mode=LEGACY_SEMANTICS))
    planning = service.begin_planning(mission.id)
    service.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json({"tasks": [_node("A"), _node("B", dependencies=["A"])]}),
        base_version=planning.version,
        source={"planner": "fixture"},
    )
    return service, mission.id


def _event_digest(service: CommitService, mission_id: str) -> str:
    import hashlib

    body = canonical_json(
        [
            {"type": kind, "payload": json.loads(payload)}
            for kind, payload in _payload_bytes(service, mission_id)
        ]
    )
    return hashlib.sha256(body.encode()).hexdigest()


def test_the_legacy_graph_commit_matches_the_frozen_event_digest(tmp_path):
    service, mission_id = _legacy_graph_world(tmp_path)
    assert _event_digest(service, mission_id) == LEGACY_GRAPH_EVENT_DIGEST


def test_the_frozen_digest_is_not_recomputed_from_the_live_code(tmp_path):
    """A golden that hashes whatever the code currently emits is always green.

    So the constant above is asserted to be a literal 64-hex string that this module
    does not derive — the whole point of pinning it.
    """

    del tmp_path
    assert len(LEGACY_GRAPH_EVENT_DIGEST) == 64
    assert set(LEGACY_GRAPH_EVENT_DIGEST) <= set("0123456789abcdef")


def test_the_two_gates_report_a_shared_node_defect_identically(tmp_path):
    """v1 and v2 run *one* per-node body, so the same defect reads the same either way.

    Before the dedupe the two gates carried near-identical copies of about fifty
    lines; a fix applied to one of them would silently diverge the other's messages,
    and every existing receipt was produced by the v1 wording.
    """

    from agent_orchestrator.graph.task_graph import GraphRejected, validate_graph

    mission = _v2_mission(tmp_path, key="shared-body")
    blank = _node("A")
    blank["rationale"] = "  "
    proposal = _graph(blank)
    with pytest.raises(GraphRejected) as legacy:
        validate_graph(mission, proposal)
    v2 = _v2_rejection(mission, proposal, typed_order=_typed())
    assert (legacy.value.reason, legacy.value.detail) == (v2.reason, v2.detail)


def test_the_two_gates_report_a_shared_tool_defect_identically(tmp_path):
    from agent_orchestrator.graph.task_graph import GraphRejected, validate_graph

    mission = _v2_mission(tmp_path, key="shared-tools")
    node = _node("A")
    node["allowed_tools"] = ["some_tool_the_mission_never_allowed"]
    proposal = _graph(node)
    with pytest.raises(GraphRejected) as legacy:
        validate_graph(mission, proposal)
    v2 = _v2_rejection(mission, proposal, typed_order=_typed())
    assert (legacy.value.reason, legacy.value.detail) == (v2.reason, v2.detail)
    assert legacy.value.reason == "tools"


# ================================================================== mutation self-checks
def _mutate(monkeypatch, name: str, body) -> None:
    monkeypatch.setattr(CommitService, name, body)


def test_mutation_a_commit_that_skips_the_semantics_gate_is_caught(tmp_path, monkeypatch):
    """Without the door, a legacy Mission's command reaches the migration-16 tables."""

    world = _world(tmp_path, mode=LEGACY_SEMANTICS)
    reached: list[str] = []

    class Spy(HtnStore):
        def __init__(self, store):  # noqa: ANN001
            reached.append("semantic layer")
            super().__init__(store)

    monkeypatch.setattr(
        "agent_orchestrator.orchestrator.plan_commits.semantics_of",
        lambda mission: HIERARCHICAL_SEMANTICS,
    )
    monkeypatch.setattr("agent_orchestrator.orchestrator.plan_commits.HtnStore", Spy)
    with pytest.raises(PlanCommitRejected) as caught:
        world.commit()
    assert caught.value.reason != "SEMANTICS_NOT_HIERARCHICAL"
    with pytest.raises(AssertionError):  # what the door test asserts no longer holds
        assert not reached


def test_mutation_an_automatic_rebase_of_a_stale_base_is_caught(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def rebase(mission, command):  # noqa: ANN001
        return None  # "close enough, replay it anyway"

    _mutate(monkeypatch, "_check_integer_gate", staticmethod(rebase))
    command = dataclasses.replace(world.command, base_graph_version=7)
    world.commit(command)
    with pytest.raises(AssertionError):
        assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_mutation_a_read_set_check_that_passes_unknown_subjects_is_caught(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def lenient(self, semantics, mission_id, item):  # noqa: ANN001
        return int(item.semantic_revision), item.content_hash

    _mutate(monkeypatch, "_goal_state", lenient)
    world.semantics.put_task_semantics(
        world.mission.id,
        dataclasses.replace(world.binding, contract_revision=2, contract_hash=HEX_OTHER),
    )
    world.commit()
    with pytest.raises(AssertionError):
        assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_mutation_a_two_step_activation_is_caught(tmp_path, monkeypatch):
    """PREPARED that is never activated leaves a Mission with no plan at all."""

    world = _world(tmp_path)
    monkeypatch.setattr(HtnStore, "activate_plan_revision", lambda self, m, r: None)
    world.commit()
    with pytest.raises(AssertionError):
        assert world.semantics.active_plan_revision(world.mission.id) is not None


def test_mutation_an_idempotency_key_that_ignores_the_payload_is_caught(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.commit()

    def blind(semantics, command, intent):  # noqa: ANN001
        return semantics.get_commit_receipt(command.command_id)

    _mutate(monkeypatch, "_replayed_receipt", staticmethod(blind))
    other = _other_intent(world)
    replayed = world.commit(other)
    with pytest.raises(AssertionError):
        assert replayed.intent_hash == other.intent_hash()


def test_mutation_a_revocation_that_does_not_move_the_generation_is_caught(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.commit()
    target = world.command.delta.occurrences[0]
    before = world.semantics.task_semantics_of(world.mission.id, str(target.task_id))
    _mutate(monkeypatch, "_revoke_running_work", lambda self, semantics, command: {})
    world.commit(_second_revision(world, superseded=(target.occurrence_id,)))
    after = world.semantics.task_semantics_of(world.mission.id, str(target.task_id))
    with pytest.raises(AssertionError):
        assert int(after.dispatch_generation) == int(before.dispatch_generation) + 1


def test_mutation_a_structure_check_on_the_delta_only_is_caught(tmp_path, monkeypatch):
    from agent_orchestrator.contracts.htn import OrderConstraint, ReleaseCondition

    world = _world(tmp_path)
    _mutate(monkeypatch, "_check_structure", lambda self, semantics, command: None)
    first, second = (spec.occurrence_id for spec in world.command.delta.occurrences)
    looped = dataclasses.replace(
        world.bundle.network,
        order_constraints=(
            OrderConstraint(
                before=first, after=second, release_condition=ReleaseCondition.ACCEPTED
            ),
            OrderConstraint(
                before=second, after=first, release_condition=ReleaseCondition.ACCEPTED
            ),
        ),
    )
    world.commit(dataclasses.replace(world.command, network=looped))
    with pytest.raises(AssertionError):
        assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_mutation_a_budget_check_that_never_refuses_is_caught(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _mutate(
        monkeypatch,
        "_check_budget",
        staticmethod(lambda semantics, obligations, command: None),
    )
    tight = dataclasses.replace(world.command.structure_budget, max_live_tasks=1)
    world.commit(dataclasses.replace(world.command, structure_budget=tight))
    with pytest.raises(AssertionError):
        assert world.semantics.list_plan_revisions(world.mission.id) == ()


def test_the_network_snapshot_type_is_what_the_command_promises(tmp_path):
    world = _world(tmp_path)
    assert isinstance(world.command.network, TaskNetworkSnapshot)
    assert int(world.command.network.plan_revision) == 1
