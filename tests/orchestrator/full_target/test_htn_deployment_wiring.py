# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2b: the three wirings part 2 left open, and the scenarios they unlock.

Part 2's §13 named them: there was no deployment-side ``PlanningWorld``, no path
from a verified leaf to an ``Acceptance``, and no assembly that pointed the
observers at anything.  Each section below is the witness that one of them is now
real, and the last two sections are the scenarios that could not be written before:

§1  the deployment's ``PlanningWorld`` — derived capabilities, store-backed
    evidence, a seed library that went through the admission protocol.
§2  the observer assembly — one reader per predicate, chosen by the deployment.
§3  the leaf acceptance chain — verdict → anchors → ``Acceptance`` →
    ``acceptance_outputs`` → a DATA consumer that can finally resolve.
§4  the evidence round — an UNKNOWN precondition becomes a read-only occurrence
    that the observer index executes, and an observer that is down is
    ``OBSERVER_UNAVAILABLE`` with nothing written.
§5  OR methods: a precondition the observer denies moves the choice to the other
    branch.
§6  a shared read-only subgoal is executed once.
§7  the MethodSynthesizer's dispatch — typed context, ``mission_planning``, author
    locked to MODEL.
§8  mutation self-check.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from agent_orchestrator.contracts.evidence_state import TruthValue, WitnessPurpose  # noqa: E402
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.graph.eligibility import ReadinessReason  # noqa: E402
from agent_orchestrator.knowledge.validity import witness_subject  # noqa: E402
from agent_orchestrator.planning.htn.observation_pipeline import (  # noqa: E402
    build_index,
    observe_predicate,
    record_observation,
)
from agent_orchestrator.planning.htn.observers import Observation, unavailable  # noqa: E402
from agent_orchestrator.planning.htn.observers.code import code_observers  # noqa: E402
from agent_orchestrator.planning.htn.seed_methods import seed_content_hash  # noqa: E402
from agent_orchestrator.planning.htn.world import (  # noqa: E402
    CAPABILITY_LAYERS,
    DeploymentPlanningWorld,
    assign_readers,
    build_planning_world,
    capability_records,
    declared_capability_ids,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.store import Store  # noqa: E402


def ref(identifier: str, version: int = 1):
    from agent_orchestrator.contracts.semantic_base import VersionedRef

    return VersionedRef(
        id=identifier, version=version, content_hash=seed_content_hash(identifier, version)
    )


@pytest.fixture
def worktree(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=root,
        check=True,
    )
    return root


# ======================================================================================
# 1. the deployment's PlanningWorld
# ======================================================================================


def test_the_assembled_world_satisfies_the_structural_type() -> None:
    """``install_hierarchical`` asks for four attributes and two calls (§18.5)."""

    world = build_planning_world("m-1")
    for name in ("catalog", "schemas", "registry", "predicates"):
        assert getattr(world, name) is not None
        assert not callable(getattr(world, name))
    assert world.capabilities().records
    assert world.snapshot().scope_id == "mission"


def test_the_seed_methods_went_through_the_admission_protocol() -> None:
    """A registered method is one the registry admitted, not one someone inserted."""

    world = build_planning_world("m-1", domains=("code",))
    refs = world.registry.method_refs()
    assert {item.method_id for item in refs} == {
        "code.fix-by-patch",
        "code.fix-by-revert",
        "code.review-changes-directly",
        "code.review-changes-recursively",
    }
    for reference in refs:
        assert world.registry.definition(reference) is not None


def test_a_capability_is_registered_only_when_an_operator_implements_it() -> None:
    """§14.2 'registered': there is a primitive task type with a real operator."""

    world = build_planning_world("m-1", domains=("code",), deployed_layers=("code_test",))
    table = {item.capability_id: item for item in world.records}
    assert set(table) == set(declared_capability_ids(world.catalog))
    assert all(item.registered for item in table.values())


def test_a_capability_whose_layer_is_not_deployed_is_unavailable_with_a_reason() -> None:
    """The table is derived from this host, not declared by the domain data."""

    world = build_planning_world("m-1", domains=("code",), deployed_layers=())
    table = {item.capability_id: item for item in world.records}
    assert CAPABILITY_LAYERS["tests.run"] == "code_test"
    assert not table["tests.run"].available
    assert table["tests.run"].unavailable_reasons() == ("configured",)
    assert table["repo.read"].available


def test_an_unhealthy_tool_and_a_missing_layer_are_different_axes() -> None:
    world = build_planning_world(
        "m-1", domains=("code",), deployed_layers=("code_test",), unhealthy=("repo.write",)
    )
    table = {item.capability_id: item for item in world.records}
    assert table["repo.write"].unavailable_reasons() == ("healthy",)
    assert table["tests.run"].available


def test_an_undeclared_capability_is_not_configured() -> None:
    """Review P1-6: the ``configured`` axis used to be fail-open.

    ``needed is None or needed in layers`` read a *missing* table entry as "needs no
    layer", so a capability this deployment had never heard of came back
    ``configured=True`` and a method could be admitted against a tool nobody
    installed.  The table is the deployment's declaration now: listed with a layer,
    listed with ``None`` (this host runs it as-is), or not listed — and not listed
    is refused with the §14.2 axis that says why.
    """

    world = build_planning_world("m-1", domains=("code",), deployed_layers=("code_test",))
    # A deployment that declares nothing: every capability the domain names is one
    # this host has not promised.
    blank = {
        item.capability_id: item
        for item in capability_records(
            world.catalog, deployed_layers=("code_test",), capability_layers={}
        )
    }
    assert set(blank) == set(declared_capability_ids(world.catalog))
    assert all(item.registered for item in blank.values())
    assert all(not item.configured for item in blank.values())
    assert all(item.unavailable_reasons() == ("configured",) for item in blank.values())
    # A deployment that declares one of them, with ``None`` meaning "no extra layer".
    partial = {
        item.capability_id: item
        for item in capability_records(
            world.catalog, deployed_layers=("code_test",), capability_layers={"repo.read": None}
        )
    }
    assert partial["repo.read"].available
    assert not partial["tests.run"].configured


def test_every_capability_the_shipped_domains_name_is_declared_by_the_deployment() -> None:
    """The fail-closed rule is only usable if the table covers what actually ships."""

    for domain in ("code", "appworld"):
        world = build_planning_world("m-1", domains=(domain,))
        missing = [
            item for item in declared_capability_ids(world.catalog) if item not in CAPABILITY_LAYERS
        ]
        assert missing == [], f"{domain} names capabilities this deployment never declared"


def test_a_capability_named_only_by_a_compound_type_is_not_registered() -> None:
    """Mutation: reporting every mentioned capability as registered would admit a
    method against a tool nothing can dispatch to."""

    from agent_orchestrator.planning.htn.registry import TaskTypeCatalog

    world = build_planning_world("m-1", domains=("code",))
    catalog = TaskTypeCatalog()
    for spec in world.catalog.task_types():
        if spec.form.name == "COMPOUND":
            catalog.register(spec)
        elif spec.operator_ref is None:
            catalog.register(spec)
    orphan = capability_records(catalog)
    assert all(not item.registered for item in orphan)


def test_an_unknown_domain_is_refused_rather_than_producing_an_empty_library() -> None:
    with pytest.raises(ContractError, match="no seed domain"):
        build_planning_world("m-1", domains=("code", "not-a-domain"))


def test_the_evidence_snapshot_is_read_from_the_store_every_time(tmp_path) -> None:
    """A cached snapshot is the stale read ADR-13 refuses; a new observation shows."""

    service, mission = _mission(tmp_path)
    store = service.store
    semantics = HtnStore(store)
    world = build_planning_world(mission.id, domains=("code",), semantics=semantics)
    before = world.snapshot()
    assert before.entries == () and before.support_revision == 0
    _observe(world, semantics, mission.id, polarity=True)
    after = world.snapshot()
    assert after.support_revision == 1
    assert after.entries[0].truth() is TruthValue.TRUE
    assert after.snapshot_id != before.snapshot_id
    store.close()


def test_a_counter_observation_is_not_outvoted_in_the_snapshot(tmp_path) -> None:
    service, mission = _mission(tmp_path)
    store = service.store
    semantics = HtnStore(store)
    world = build_planning_world(mission.id, domains=("code",), semantics=semantics)
    _observe(world, semantics, mission.id, polarity=True)
    _observe(world, semantics, mission.id, polarity=False, at=2)
    entry = world.snapshot().entries[0]
    assert entry.truth() is TruthValue.CONFLICT
    store.close()


def test_the_fixture_env_is_filled_by_the_real_assembly() -> None:
    """``seed_env`` delegates; it no longer keeps a second answer to 'what is a world'."""

    from htn_world import seed_env

    env = seed_env("mission-x")
    assert isinstance(env.world, DeploymentPlanningWorld)
    assert env.catalog is env.world.catalog
    assert env.registry is env.world.registry
    assert env.predicates is env.world.predicates
    assert env.schemas is env.world.schemas


def _mission(tmp_path, key: str = "k"):
    from agent_orchestrator.contracts import Budget
    from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec

    service = CommitService(Store.open(Path(tmp_path) / "db.sqlite3"))
    mission, _ = service.create_mission(
        MissionSpec(
            goal="g",
            success_criteria=("c",),
            tenant_id="t",
            idempotency_key=key,
            allowed_tools=(),
            budget=Budget(max_tokens=200_000, max_attempts=4),
            orchestration_semantics_version="hierarchical",
        )
    )
    return service, mission


def _observe(world, semantics, mission_id: str, *, polarity: bool, at: int = 1) -> None:
    from agent_orchestrator.planning.htn.observers import denial, observed

    signature = world.predicates.require(ref("code.repo-checked-out"))
    arguments = {"repository": "r1"}
    observation = (
        observed(
            signature,
            arguments,
            polarity=True,
            observer_id="code.repo-observer",
            now_ms=at,
        )
        if polarity
        else denial(
            signature,
            arguments,
            observer_id="code.repo-observer",
            now_ms=at,
            coverage_scope="worktree:/tmp",
        )
    )
    semantics.insert_observation(mission_id, observation.record)


# ======================================================================================
# 2. the observer assembly
# ======================================================================================


def test_the_shipped_code_observers_cannot_be_indexed_without_an_assignment(worktree) -> None:
    """Mutation: the pack ships two identities for one CLOSED predicate on purpose."""

    world = build_planning_world("m-1", domains=("code",))
    with pytest.raises(ContractError, match="offered by two observers"):
        build_index(world.predicates, code_observers(worktree))


def test_the_deployment_assigns_one_reader_per_predicate(worktree) -> None:
    world = build_planning_world("m-1", domains=("code",), worktree=worktree)
    mapping = world.observer_index.to_json()["predicates"]
    assert mapping["code.working-tree-clean"] == "code.repo-observer"
    assert set(mapping) == {
        "code.changeset-reviewable",
        "code.changeset-too-large",
        "code.regression-commit-known",
        "code.repo-checked-out",
        "code.test-is-failing",
        "code.working-tree-clean",
    }


def test_the_order_is_the_deployments_precedence_declaration(worktree) -> None:
    """Listing the workspace identity first gives it the shared predicate."""

    readers = tuple(reversed(code_observers(worktree)))
    world = build_planning_world("m-1", domains=("code",), observers=readers)
    mapping = world.observer_index.to_json()["predicates"]
    assert mapping["code.working-tree-clean"] == "code.workspace-observer"
    assert mapping["code.repo-checked-out"] == "code.repo-observer"


def test_an_observer_left_with_nothing_to_read_is_dropped(worktree) -> None:
    readers = code_observers(worktree)
    assigned = assign_readers(readers)
    assert "code.workspace-observer" not in {item.observer_id for item in assigned}


def test_a_deployment_without_a_worktree_installs_no_code_observer() -> None:
    """Not being able to look is not the proposition being false."""

    world = build_planning_world("m-1", domains=("code",))
    assert world.observed_predicate_ids() == ()
    outcome = observe_predicate(
        world.observer_index, ref("code.repo-checked-out"), {"repository": "r"}, now_ms=1
    )
    assert outcome.reason is ReadinessReason.OBSERVER_UNAVAILABLE
    assert outcome.record is None


def test_a_real_observer_reads_the_real_worktree(worktree) -> None:
    world = build_planning_world("m-1", domains=("code",), worktree=worktree)
    outcome = observe_predicate(
        world.observer_index, ref("code.repo-checked-out"), {"repository": str(worktree)}, now_ms=1
    )
    assert outcome.available
    assert outcome.record is not None and outcome.record.polarity is True


def test_a_dirty_worktree_is_an_authoritative_negative(worktree) -> None:
    (worktree / "a.py").write_text("x = 2\n", encoding="utf-8")
    world = build_planning_world("m-1", domains=("code",), worktree=worktree)
    outcome = observe_predicate(
        world.observer_index,
        ref("code.working-tree-clean"),
        {"repository": str(worktree)},
        now_ms=1,
    )
    assert outcome.available
    assert outcome.record is not None
    assert outcome.record.polarity is False
    assert outcome.record.is_authoritative_negative


def test_an_observer_that_is_down_writes_nothing(tmp_path, worktree) -> None:
    """The asymmetry ``record_observation`` exists for."""

    service, mission = _mission(tmp_path)
    semantics = HtnStore(service.store)
    store = service.store
    world = build_planning_world(mission.id, domains=("code",), worktree=worktree)

    class Down:
        observer_id = "code.repo-observer"

        def predicate_ids(self) -> tuple[str, ...]:
            return ("code.repo-checked-out",)

        def observe(self, signature, arguments, *, now_ms: int) -> Observation:
            return unavailable(self.observer_id, "code.repo-checked-out", "service is down")

    index = build_index(world.predicates, (Down(),))
    outcome = record_observation(
        semantics,
        mission.id,
        observe_predicate(index, ref("code.repo-checked-out"), {"repository": "r"}, now_ms=1),
    )
    assert outcome.reason is ReadinessReason.OBSERVER_UNAVAILABLE
    assert outcome.recorded is False
    assert semantics.list_observations(mission.id) == ()
    store.close()


# ======================================================================================
# 3. an UNKNOWN precondition becomes a look, and the look decides the OR
# ======================================================================================
#
# Part 2's §9 could not write these: ``refinement`` produced evidence *requests* and
# nothing executed them, so an UNKNOWN precondition stayed UNKNOWN and the OR choice
# could only be moved by a test asserting the evidence by fiat (``Env.say``).  Here
# the evidence arrives the way a deployment's does: an observer reads, the pipeline
# records, and the next refinement round reads the store.


from htn_world import (  # noqa: E402
    BUDGET,
    Env,
    atom,
    ledger_for,
    method,
    out,
    param,
    root_network,
    step,
    task_binding,
)

from agent_orchestrator.contracts.htn import (  # noqa: E402
    ReusePolicy,
    SideEffectKind,
    TaskForm,
)
from agent_orchestrator.planning.htn import evidence_round  # noqa: E402
from agent_orchestrator.planning.htn.observers import denial, observed  # noqa: E402
from agent_orchestrator.planning.htn.refinement import (  # noqa: E402
    RefinementOutcome,
    planning_frontier,
    refine,
)

PRIMARY = "demo.primary-ready"
FALLBACK = "demo.fallback-ready"


class _ScriptedObserver:
    """A reader with a script.  Answers only what the deployment routed to it."""

    observer_id = "demo.observer"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def predicate_ids(self) -> tuple[str, ...]:
        return (PRIMARY, FALLBACK)

    def observe(self, signature, arguments, *, now_ms: int):
        name = signature.predicate_ref.id
        self.calls.append(name)
        answer = self.answers.get(name)
        if answer is None:
            return unavailable(self.observer_id, name, "this reader has no script for it")
        if answer is False:
            return denial(
                signature,
                arguments,
                observer_id=self.observer_id,
                now_ms=now_ms,
                coverage_scope="demo:alpha",
            )
        return observed(
            signature,
            arguments,
            polarity=True,
            observer_id=self.observer_id,
            now_ms=now_ms,
        )


def _demo_env() -> Env:
    env = Env()
    for name in (PRIMARY, FALLBACK):
        env.register_predicate(
            name, (("subject", "string"),), closed=True, observers=("demo.observer",)
        )
    env.register_type(
        "demo.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-done",),
        domain="demo",
    )
    env.register_type(
        "demo.shared-read",
        parameters=(("subject", "string"),),
        outputs=(("facts", "demo.facts"),),
        capabilities=("demo.read",),
        effect=SideEffectKind.EXTERNAL_READ,
        reuse=ReusePolicy.REUSE_ACCEPTED,
        domain="demo",
    )
    for name, port in (("demo.a", "ra"), ("demo.b", "rb")):
        env.register_type(
            name,
            parameters=(("subject", "string"),),
            inputs=(("facts", "demo.facts", True),),
            outputs=((port, f"demo.{port}"),),
            capabilities=("demo.read",),
            domain="demo",
        )
    env.register_type(
        "demo.observer-type",
        parameters=(("proposition_key", "string"),),
        capabilities=("demo.read",),
        effect=SideEffectKind.EXTERNAL_READ,
        observes=(PRIMARY, FALLBACK),
        domain="demo",
    )
    return env


def _alternative(method_id: str, predicate: str, tail: str):
    return method(
        method_id,
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom(predicate, {"subject": param("subject")}),),
        steps=(
            step(
                "shared",
                "demo.shared-read",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
            step(
                tail,
                f"demo.{tail}",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "facts": out("shared", "facts")},
                capabilities=("demo.read",),
            ),
        ),
        links=(("c-done", tail, f"c-{tail}"),),
        finalizer=tail,
    )


@dataclass
class DemoWorld:
    env: Env
    world: DeploymentPlanningWorld
    observer: _ScriptedObserver
    binding: Any
    store: Any

    def refine(self):
        network = root_network(self.env, self.binding)
        return refine(
            planning_frontier(network),
            network=network,
            registry=self.env.registry,
            catalog=self.env.catalog,
            schemas=self.env.schemas,
            predicates=self.env.predicates,
            snapshot=self.world.snapshot(),
            capabilities=self.env.capabilities(),
            ledger=ledger_for(self.binding, fuel=3, mission=self.env.mission),
            budget=BUDGET,
        )

    def asks(self, decision) -> tuple[Any, ...]:
        """The grounded atoms behind this decision's evidence requests."""

        pending = evidence_round.pending_asks(
            _conditions(),
            parameters={"subject": "alpha"},
            registry=self.env.predicates,
            snapshot=self.world.snapshot(),
        )
        return evidence_round.asks_for_requests(decision.evidence, pending)

    def look(self, asks, *, now_ms: int = 1_000):
        return evidence_round.run_round(
            self.world.observer_index,
            HtnStore(self.store),
            self.env.mission,
            asks,
            now_ms=now_ms,
        )


@pytest.fixture
def demo(tmp_path) -> DemoWorld:
    service, mission = _mission(tmp_path, key="demo")
    env = _demo_env()
    env.mission = mission.id
    env.admit(_alternative("demo.primary", PRIMARY, "a"))
    env.admit(_alternative("demo.fallback", FALLBACK, "b"))
    observer = _ScriptedObserver({PRIMARY: False, FALLBACK: True})
    world = DeploymentPlanningWorld(
        mission_id=mission.id,
        schemas=env.schemas,
        predicates=env.predicates,
        catalog=env.catalog,
        registry=env.registry,
        records=env.capabilities().records,
        observers=build_index(env.predicates, (observer,)),
        semantics=HtnStore(service.store),
    )
    binding = task_binding(
        env,
        "demo.goal",
        task_id="task-root",
        obligation="obl-root",
        parameters={"subject": "alpha"},
    )
    return DemoWorld(env=env, world=world, observer=observer, binding=binding, store=service.store)


def test_an_unknown_precondition_asks_for_evidence_rather_than_dispatching(demo: DemoWorld) -> None:
    """ADR-07: UNKNOWN is a reason to look, never a reason to try the work."""

    decision = demo.refine().decisions[0]
    assert decision.outcome is RefinementOutcome.NEEDS_EVIDENCE
    assert {item.predicate_ref.id for item in decision.evidence} == {PRIMARY, FALLBACK}
    assert all(item.satisfiable for item in decision.evidence)
    assert all(item.occurrence is not None for item in decision.evidence)


def test_the_evidence_occurrence_is_a_read_only_observer_type(demo: DemoWorld) -> None:
    decision = demo.refine().decisions[0]
    for request in decision.evidence:
        assert request.observer is not None
        assert request.observer.read_only
        assert request.binding.side_effect_kind is SideEffectKind.EXTERNAL_READ
        assert request.binding.resource_writes == ()


def test_the_observer_index_executes_the_evidence_occurrence(demo: DemoWorld) -> None:
    decision = demo.refine().decisions[0]
    result = demo.look(demo.asks(decision))
    assert len(result.recorded) == 2
    assert demo.observer.calls == [PRIMARY, FALLBACK]
    stored = HtnStore(demo.store).list_observations(demo.env.mission)
    assert len(stored) == 2


def test_the_denied_alternative_loses_and_the_other_one_is_chosen(demo: DemoWorld) -> None:
    """The scenario §9 could not write: a look moves the OR choice."""

    first = demo.refine().decisions[0]
    assert first.outcome is RefinementOutcome.NEEDS_EVIDENCE
    demo.look(demo.asks(first))
    second = demo.refine().decisions[0]
    assert second.outcome is RefinementOutcome.REFINED
    assert second.draft.method_ref.method_id == "demo.fallback"


def test_the_refuted_alternative_contributes_no_occurrence(demo: DemoWorld) -> None:
    demo.look(demo.asks(demo.refine().decisions[0]))
    decision = demo.refine().decisions[0]
    assert {str(item.slot_key) for item in decision.draft.child_bindings} == {"shared", "b"}


def test_an_observer_that_cannot_answer_leaves_the_choice_unmade(demo: DemoWorld) -> None:
    """OBSERVER_UNAVAILABLE is not FALSE: nothing is written and nothing is chosen."""

    demo.observer.answers = {}
    first = demo.refine().decisions[0]
    result = demo.look(demo.asks(first))
    assert result.recorded == ()
    assert len(result.unavailable) == 2
    assert all(item.reason is ReadinessReason.OBSERVER_UNAVAILABLE for item in result.unavailable)
    assert HtnStore(demo.store).list_observations(demo.env.mission) == ()
    assert demo.refine().decisions[0].outcome is RefinementOutcome.NEEDS_EVIDENCE


def test_an_observer_that_crashes_is_an_outage_and_not_a_polarity(demo: DemoWorld) -> None:
    def explode(signature, arguments, *, now_ms):
        raise RuntimeError("the reader died")

    demo.observer.observe = explode  # type: ignore[method-assign]
    result = demo.look(demo.asks(demo.refine().decisions[0]))
    assert result.recorded == ()
    assert len(result.unavailable) == 2
    assert demo.refine().decisions[0].outcome is RefinementOutcome.NEEDS_EVIDENCE


def test_a_settled_proposition_is_not_looked_at_a_second_time(demo: DemoWorld) -> None:
    """A shared read-only question is asked once; the second round reads the record."""

    demo.look(demo.asks(demo.refine().decisions[0]))
    assert len(demo.observer.calls) == 2
    second = demo.refine().decisions[0]
    assert demo.asks(second) == ()
    assert len(demo.observer.calls) == 2


def test_an_ask_with_no_observer_is_reported_once(demo: DemoWorld) -> None:
    """Review P2-19: one ask, one answer.

    ``run_round`` used to append an unreadable ask to ``unobservable`` and then call
    ``observe_predicate`` on it anyway, so the very same proposition came back twice
    in one result — once as "this deployment has no reader" and once as the
    ``NO_OBSERVER`` outcome saying the same thing.
    """

    ask = evidence_round.EvidenceAsk(
        predicate_ref=ref("demo.nobody-reads-this"),
        arguments={"subject": "alpha"},
        proposition_key="demo.nobody-reads-this#alpha",
    )
    before = len(demo.observer.calls)
    result = demo.look((ask,))
    assert [item.proposition_key for item in result.unobservable] == [ask.proposition_key]
    assert result.outcomes == ()
    assert result.recorded == ()
    assert len(demo.observer.calls) == before, "an ask nobody can read is not a reading"


def test_an_unregistered_observer_type_is_not_looked_at_through_the_index(demo: DemoWorld) -> None:
    """Mutation: running every pending ask would route around the catalogue."""

    asks = evidence_round.pending_asks(
        _conditions(),
        parameters={"subject": "alpha"},
        registry=demo.env.predicates,
        snapshot=demo.world.snapshot(),
    )
    assert len(asks) == 2
    assert evidence_round.asks_for_requests((), asks) == ()


def _conditions():
    from agent_orchestrator.contracts.htn import parse_conditions

    return parse_conditions(
        [
            atom(PRIMARY, {"subject": param("subject")}),
            atom(FALLBACK, {"subject": param("subject")}),
        ],
        "preconditions",
    )


# ======================================================================================
# 4. the MethodSynthesizer's own dispatch
# ======================================================================================
#
# §7.3 source 4 / §18.5 C8: a new *role*, with its own template, its own budget
# account and a typed context that carries no authority vocabulary at all.  Part 2
# built the synthesiser and left it unreachable; this is the wiring.


from agent_orchestrator.contracts.htn import RegistryAuthor  # noqa: E402
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    HierarchicalDispatch,
)
from agent_orchestrator.planning.htn.synthesis import (  # noqa: E402
    SYNTHESIS_AUTHOR,
    authority_claims,
)
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402


@dataclass
class SynthWorld:
    service: Any
    mission: Any
    env: Env
    world: DeploymentPlanningWorld
    dispatch: HierarchicalDispatch


@pytest.fixture
def synth(tmp_path) -> SynthWorld:
    service, mission = _mission(tmp_path, key="synth")
    env = _demo_env()
    env.mission = mission.id
    world = DeploymentPlanningWorld(
        mission_id=mission.id,
        schemas=env.schemas,
        predicates=env.predicates,
        catalog=env.catalog,
        registry=env.registry,
        records=env.capabilities().records,
        observers=build_index(env.predicates, ()),
        semantics=HtnStore(service.store),
    )
    binding = task_binding(
        env,
        "demo.goal",
        task_id="task-root",
        obligation="obl-root",
        parameters={"subject": "alpha"},
    )
    ObligationStore(service.store).register(
        Obligation(
            obligation_id="obl-root",  # type: ignore[arg-type]
            mission_id=mission.id,
            requirement_refs=("req-1",),
            goal_signature_id="demo.goal",
        ),
        recursion_fuel=4,
    )
    HtnStore(service.store).put_task_semantics(mission.id, binding)
    return SynthWorld(
        service=service,
        mission=mission,
        env=env,
        world=world,
        dispatch=HierarchicalDispatch(service.store, service, planning=world),
    )


def test_the_synthesis_request_describes_the_goal_and_the_real_operators(
    synth: SynthWorld,
) -> None:
    request = synth.dispatch.synthesis_request(synth.mission.id, "task-root")
    assert request.goal_task_id == "task-root"
    assert request.obligation_id == "obl-root"
    offered = {item.task_type_id for item in request.operators}
    assert "demo.shared-read" in offered
    assert "demo.goal" not in offered  # a compound is not an operator


def test_the_synthesis_request_carries_no_authority_vocabulary(synth: SynthWorld) -> None:
    """§18.5: a model proposes the shape of the work, never its authority."""

    request = synth.dispatch.synthesis_request(synth.mission.id, "task-root")
    assert authority_claims(request.to_json()) == ()
    assert "mission_id" not in request.to_json()
    assert set(request.forbidden_fields) >= {"mission_id", "registry_status", "budget_account"}


def test_an_unusable_capability_is_reported_as_unavailable(synth: SynthWorld) -> None:
    synth.world.records = capability_records(
        synth.env.catalog, deployed_layers=(), unhealthy=("demo.read",)
    )
    request = synth.dispatch.synthesis_request(synth.mission.id, "task-root")
    assert "demo.read" in request.unavailable_capabilities
    assert request.available_operators == ()


def test_the_reply_is_admitted_with_the_author_locked_to_model(synth: SynthWorld) -> None:
    """§6.3 / §7.3: text that reached us from a model is MODEL-authored, full stop."""

    import inspect

    source = inspect.getsource(HierarchicalDispatch.apply_synthesizer_reply)
    assert (
        "author" not in inspect.signature(HierarchicalDispatch.apply_synthesizer_reply).parameters
    )
    assert "MODEL" in source
    assert SYNTHESIS_AUTHOR is RegistryAuthor.MODEL


def test_a_model_that_awards_itself_a_status_is_refused_and_recorded(synth: SynthWorld) -> None:
    import json as _json

    method_json = _alternative("demo.synthesised", PRIMARY, "a").to_json()
    text = (
        "<method_proposal>"
        + _json.dumps(
            {"method": method_json, "rationale": "r", "registry_status": "ADMITTED"},
            ensure_ascii=False,
        )
        + "</method_proposal>"
    )
    receipt = synth.dispatch.apply_synthesizer_reply(synth.mission.id, text)
    assert not receipt.admitted
    assert any("status" in str(problem).lower() for problem in receipt.problems)


def test_the_synthesizer_intent_is_hierarchical_only_and_lands_on_mission_planning() -> None:
    """The two facts §13 v1.4 and §18.5 ask for, read off the wiring itself."""

    import inspect

    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    source = inspect.getsource(Orchestrator._create_synthesizer_intent)
    assert "self._new_mode(mission)" in source
    assert "mission_account(mission_id)" in source
    assert "METHOD_SYNTHESIZER" in source
    assert "task_account" not in source


# ======================================================================================
# 5. mutation self-check
# ======================================================================================
#
# Each mutant below is a plausible wrong implementation of something part 2b added,
# and each is paired with the assertion above that already catches it.  A mutant that
# nothing catches is a test suite that is describing the code rather than checking it.


def test_mutant_a_world_that_trusts_the_declared_capabilities(worktree) -> None:
    """Mutant: ``capabilities()`` returns every id the domain data mentions, healthy.

    Caught by ``test_a_capability_whose_layer_is_not_deployed_is_unavailable_with_a_reason``:
    a host with no ``code_test`` layer would report ``tests.run`` available and admit
    a method against a test runner it cannot start.
    """

    from agent_orchestrator.planning.htn.applicability import CapabilityRecord, CapabilitySnapshot

    world = build_planning_world("m-1", domains=("code",), worktree=worktree, deployed_layers=())
    mutant = CapabilitySnapshot(
        records=tuple(
            CapabilityRecord(capability_id=item) for item in declared_capability_ids(world.catalog)
        )
    )
    assert all(item.available for item in mutant.records)  # the mutant's claim
    assert not all(item.available for item in world.capabilities().records)  # the real answer


def test_mutant_an_index_that_resolves_two_observers_by_order(worktree) -> None:
    """Mutant: ``build_index`` keeps the last observer it sees instead of refusing.

    Caught by ``test_the_shipped_code_observers_cannot_be_indexed_without_an_assignment``
    plus ``test_the_order_is_the_deployments_precedence_declaration``: the shipped
    pack has two authorised readers for one CLOSED predicate, and silently keeping
    one of them means the deployment never states which.
    """

    readers = code_observers(worktree)
    last_wins = {
        name: observer.observer_id for observer in readers for name in observer.predicate_ids()
    }
    assert last_wins["code.working-tree-clean"] == "code.workspace-observer"
    world = build_planning_world("m-1", domains=("code",), worktree=worktree)
    assert (
        world.observer_index.to_json()["predicates"]["code.working-tree-clean"]
        == "code.repo-observer"
    )


def test_mutant_an_evidence_round_that_records_whatever_came_back(demo: DemoWorld) -> None:
    """Mutant: ``run_round`` stores the observation regardless of the outcome.

    Caught by ``test_an_observer_that_cannot_answer_leaves_the_choice_unmade``: an
    outage would be written as a polarity and the OR choice would be decided by a
    service being down.
    """

    demo.observer.answers = {}
    asks = demo.asks(demo.refine().decisions[0])
    outcomes = [
        observe_predicate(demo.world.observer_index, item.predicate_ref, item.arguments, now_ms=1)
        for item in asks
    ]
    assert all(item.record is None for item in outcomes)  # nothing a mutant could store
    assert HtnStore(demo.store).list_observations(demo.env.mission) == ()


def test_mutant_a_snapshot_cached_at_assembly_time(demo: DemoWorld) -> None:
    """Mutant: ``DeploymentPlanningWorld`` caches ``snapshot()`` on first call.

    Caught by ``test_the_evidence_snapshot_is_read_from_the_store_every_time`` and by
    ``test_the_denied_alternative_loses_and_the_other_one_is_chosen``: a cached
    snapshot means the round *after* an observation still cannot see it, and the OR
    choice can never change however much the deployment looks.
    """

    cached = demo.world.snapshot()
    demo.look(demo.asks(demo.refine().decisions[0]))
    live = demo.world.snapshot()
    assert cached.support_revision == 0
    assert live.support_revision == 2
    assert live.snapshot_id != cached.snapshot_id


# ======================================================================================
# 6. review P0-1, confirmed on the real deployment world
# ======================================================================================
#
# The reviewer's probe showed that a leaf with **both** START lanes — a DATA edge
# whose producer has been accepted, and a gated method whose precondition was
# observed — could never be licensed: the two witnesses shared one row of
# ``validity_witnesses_consumer_idx`` (same Mission, consumer, ``purpose=START``,
# scope, epoch and ``support_revision``), so whichever lane ran first took the key
# and the other was refused as a duplicate.  ``_decide`` issues the DATA lane first,
# so the precondition licence always lost and the consumer sat on
# ``WAITING_EVIDENCE/witness_missing`` for ever.
#
# Part 2d, adjudication 1 gave the row a ``subject_digest``, so the two licences are
# two different permissions about two different subjects and both can exist.  This
# section confirms the repair **on the shipped code domain assembled by
# ``build_planning_world``** — not on a fixture double — because the probe was run
# on the double and the double is what hid the defect (review P2-16).
#
# ``code.fix-by-patch`` is the scenario in the shipped data: the method is gated on
# ``code.repo-checked-out`` and ``code.test-is-failing``, and its ``reproduce`` step
# consumes ``facts.facts`` over a DATA edge.


ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
GOAL_TYPE = "code.fix-failing-test"
REPOSITORY = "repo-1"
FAILING_TEST = "tests/test_kv.py::test_get"


def _both_lane_world(tmp_path):
    """A committed ``code.fix-by-patch`` plan on the real deployment world."""

    from agent_orchestrator.contracts.htn import (
        ObligationId,
        TaskForm,
        TaskRef,
        TaskSemanticBindingV1,
    )
    from agent_orchestrator.contracts.obligations import Obligation
    from agent_orchestrator.contracts.semantic_base import content_hash_of
    from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
    from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal
    from agent_orchestrator.storage.obligation_store import ObligationStore

    service, mission = _mission(tmp_path, key="p23c-both-lanes")
    semantics = HtnStore(service.store)
    world = build_planning_world(
        mission.id, domains=("code",), semantics=semantics, deployed_layers=("code_test",)
    )
    spec = world.catalog.require(ref(GOAL_TYPE))
    ObligationStore(service.store).register(
        Obligation(
            obligation_id=ObligationId(ROOT_DUTY),
            mission_id=mission.id,
            requirement_refs=tuple(spec.goal_signature.coverage_criteria),
            goal_signature_id=GOAL_TYPE,
        ),
        recursion_fuel=8,
    )
    semantics.put_task_semantics(
        mission.id,
        TaskSemanticBindingV1(
            task_id=TaskRef(ROOT_TASK),
            obligation_id=ObligationId(ROOT_DUTY),
            contract_revision=1,
            contract_hash=content_hash_of([ROOT_TASK, GOAL_TYPE]),
            form=TaskForm.COMPOUND,
            goal_signature=spec.goal_signature,
            typed_parameters={"repository": REPOSITORY, "failing_test": FAILING_TEST},
            requirement_refs=tuple(spec.goal_signature.coverage_criteria),
            semantic_scope="mission",
        ),
    )
    service.begin_planning(mission.id)
    _say(world, semantics, mission.id, "code.repo-checked-out", {"repository": REPOSITORY})
    _say(world, semantics, mission.id, "code.test-is-failing", {"test": FAILING_TEST})
    dispatch = HierarchicalDispatch(service.store, service, planning=world)
    outcome = dispatch.apply_planner_reply(
        mission.id,
        _refine_text("code.fix-by-patch"),
        principal=PlanPrincipal("manager-1", "mission", 0),
        command_id="cmd-a",
    )
    assert outcome.committed, outcome.last_reason
    network = dispatch.network(mission.id)
    seen: set[str] = set()
    duties = ObligationStore(service.store)
    for occurrence in network.occurrences:
        duty = str(occurrence.obligation_id)
        if duty in seen or not duties.exists(mission.id, occurrence.obligation_id):
            continue
        seen.add(duty)
        if not duties.account(mission.id, occurrence.obligation_id).has_admitted_demand:
            service.admit_obligation_demand(
                mission.id,
                occurrence.obligation_id,
                principal="mission-submitter",
                requester={"kind": "mission_root"},
                evidence={"mission_id": mission.id},
            )
    return service, mission, semantics, world, dispatch


def _say(world, semantics, mission_id: str, predicate: str, arguments: dict) -> None:
    from agent_orchestrator.planning.htn.observers import observed

    signature = world.predicates.require(ref(predicate))
    observation = observed(
        signature,
        arguments,
        polarity=True,
        observer_id=signature.observer_ids[0],
        now_ms=1_000,
    )
    semantics.insert_observation(mission_id, observation.record)


def _refine_text(method_id: str) -> str:
    from agent_orchestrator.testing.fixtures import plan_revision_proposal_step

    reference = ref(method_id)
    return plan_revision_proposal_step(
        proposal_id="prop-1",
        expected_plan_revision=0,
        read_set=[
            {
                "kind": "method",
                "id": reference.id,
                "semantic_revision": reference.version,
                "content_hash": reference.content_hash,
            }
        ],
        operations=[
            {
                "op": "refine",
                "goal_id": ROOT_TASK,
                "obligation_id": ROOT_DUTY,
                "method_ref": {
                    "id": reference.id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            }
        ],
    )


def _task_of(dispatch, mission_id: str, type_id: str) -> str:
    """The Task row whose semantic binding names this task type."""

    bindings = dispatch.admissions(mission_id).bindings
    for task_id, binding in bindings.items():
        if str(binding.goal_signature.signature_id) == type_id:
            return str(task_id)
    raise AssertionError(f"no occurrence of {type_id} in {sorted(bindings)}")


def _accept(service, dispatch, mission_id: str, task_id: str):
    """Verify and accept one leaf, claiming its declared output port."""

    from agent_orchestrator.orchestrator.leaf_acceptance import (
        LayerOutcome,
        LeafAcceptanceAssembly,
    )
    from agent_orchestrator.runtime.output_blocks import PortClaim

    @dataclass(frozen=True)
    class _Artifact:
        id: str
        path: str
        content_hash: str = "a" * 64
        version: str = "1"

    declared = dispatch.declared_output_ports_for(mission_id, task_id)
    items = tuple(
        _Artifact(f"artifact-{index}", f"out/{item['port']}.json")
        for index, item in enumerate(declared)
    )
    claims = tuple(
        PortClaim(port_key=item["port"], path=items[index].path)
        for index, item in enumerate(declared)
    )
    assembly = LeafAcceptanceAssembly(service.store, service, dispatch=dispatch)
    return assembly.accept(
        mission_id,
        task_id,
        result_id=f"result-{task_id}",
        layers=(
            LayerOutcome("schema_check", "PASS"),
            LayerOutcome("rule_check", "PASS"),
            LayerOutcome("critic_review", "PASS"),
        ),
        artifacts=items,
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=1_000_000,
        port_claims=claims,
    )


def test_a_consumer_with_both_start_lanes_is_licensed_on_the_real_world(tmp_path) -> None:
    """Review P0-1, confirmed where it matters: the shipped domain, the real world.

    ``reproduce`` is licensed by the DATA lane (``facts.facts`` is accepted) *and*
    by the precondition lane (``code.fix-by-patch`` is gated).  Before adjudication 1
    the second licence was refused as a duplicate key and the occurrence never left
    ``WAITING_EVIDENCE``; it is now ``READY_CANDIDATE``.
    """

    service, mission, semantics, world, dispatch = _both_lane_world(tmp_path)
    producer = _task_of(dispatch, mission.id, "code.read-repository-facts")
    consumer = _task_of(dispatch, mission.id, "code.reproduce-failure")

    _accept(service, dispatch, mission.id, producer)

    # exactly the order ``_decide`` uses
    dispatch.issue_input_witnesses(mission.id, dispatch.network(mission.id), now_ms=1_000_000)
    dispatch.issue_start_witnesses(mission.id, now_ms=1_000_000)

    # The precondition lane, looked up the way the readiness gate looks it up.
    assert dispatch.start_witnesses(mission.id, consumer), "no precondition licence"
    # Both lanes, in the library: two rows under one consumer and one purpose, which
    # is exactly the pair the old unique key could not hold.
    licences = [
        item
        for item in semantics.list_validity_witnesses(mission.id)
        if str(item.consumer_ref.id) == consumer and str(item.purpose) == str(WitnessPurpose.START)
    ]
    subjects = {witness_subject(item) for item in licences}
    assert len(subjects) == 2, f"both lanes, two subjects: {sorted(subjects)}"
    assert any(item.startswith("acceptance:") for item in subjects)
    assert any(item.startswith("conditions:") for item in subjects)
    assert len({int(item.support_revision) for item in licences}) == 1, (
        "the two lanes count the same observations, which is why they collided"
    )

    admissions = dispatch.admissions(mission.id)
    refusal = admissions.refusal_for(consumer)
    assert refusal is None, (refusal.reason, refusal.detail_codes) if refusal else None
    assert admissions.admission_for(consumer) is not None


def test_a_start_licence_is_written_at_the_scope_epoch_it_was_taken_in(tmp_path) -> None:
    """Review P2-11 (mutation M12): the **write** side of the epoch barrier.

    ``scope_epoch=self.semantics.epoch(...)`` is what makes a licence stop counting
    once the scope is re-opened, and the read side is guarded by seven behaviour
    tests — but writing a constant ``0`` instead survived the whole suite, because
    every test world sat at epoch 0 and the constant happened to be right.  Here the
    scope is bumped first, so a constant is visibly the wrong number.
    """

    service, mission, semantics, world, dispatch = _both_lane_world(tmp_path)
    producer = _task_of(dispatch, mission.id, "code.read-repository-facts")
    consumer = _task_of(dispatch, mission.id, "code.reproduce-failure")
    _accept(service, dispatch, mission.id, producer)

    # The first bump writes the row at 0; the second is the first real re-opening.
    semantics.bump_epoch(mission.id, "mission", bumped_by="test-operator")
    semantics.bump_epoch(mission.id, "mission", bumped_by="test-operator")
    epoch = int(semantics.epoch(mission.id, "mission"))
    assert epoch > 0, "the scope really was re-opened"

    dispatch.issue_input_witnesses(mission.id, dispatch.network(mission.id), now_ms=1_000_000)
    dispatch.issue_start_witnesses(mission.id, now_ms=1_000_000)
    issued = [
        item
        for item in semantics.list_validity_witnesses(mission.id)
        if str(item.consumer_ref.id) == consumer and str(item.purpose) == str(WitnessPurpose.START)
    ]
    assert issued, "the consumer was licensed at the new epoch"
    assert {int(item.scope_epoch) for item in issued} == {epoch}
