# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1: the seed library, and the generality claim it exists to check.

§7.3 fixes the two pilot domains — ``code`` and ``appworld`` — at at least three
methods each (one of them recursive, two of them alternatives for one goal) and at
least five predicate observer signatures.  §7.1 says the planner must decompose
both without knowing either, so the decisive test in this file is the third one:
``fixtures/htn/domains/widget`` is an invented domain that exists only as JSON, and
it decomposes through exactly the same calls.

The recursion tests pin §6.4 v1.2 end to end: fuel belongs to the *obligation*, a
repeat of the same method and parameters is refused before fuel is spent, an
expansion that repeats an ancestor against the same world snapshot changes no
state, and exhaustion is ``BOUND_REACHED`` with the structure expanded so far kept
— never ``UNSOLVABLE``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))

from htn_world import (  # noqa: E402
    BUDGET,
    DOMAIN_ROOT,
    Env,
    add_domain,
    ledger_for,
    ref,
    root_network,
    seed_env,
    task_binding,
)

from agent_orchestrator.contracts.evidence_state import TruthValue  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    MethodRegistryStatus,
    SideEffectKind,
    TaskForm,
)
from agent_orchestrator.contracts.obligations import FuelStatus  # noqa: E402
from agent_orchestrator.graph.projection_validation import (  # noqa: E402
    validate_execution_projection,
)
from agent_orchestrator.planning.htn.backends.panda import (  # noqa: E402
    PandaToolchain,
    VerificationStatus,
    check_fragment,
    parse_plan,
)
from agent_orchestrator.planning.htn.compiler import (  # noqa: E402
    compile_refinement_bundle,
)
from agent_orchestrator.planning.htn.refinement import (  # noqa: E402
    AttemptPolicy,
    FrontierItem,
    RefinementOutcome,
    bounded_attempt_admissible,
    leaf_decision,
    planning_frontier,
    refine,
)
from agent_orchestrator.planning.htn.registry import method_is_recursive  # noqa: E402
from agent_orchestrator.planning.htn.seed_methods import (  # noqa: E402
    SEED_ROOT,
    available_domains,
    load_domain,
)
from agent_orchestrator.planning.htn.seed_methods.loader import (  # noqa: E402
    DOMAIN_FILES,
    describe,
    seed_content_hash,
)
from agent_orchestrator.planning.htn.validation import (  # noqa: E402
    HddlExport,
    UnsupportedFeature,
    to_hddl,
    unsupported_features,
)

PILOT_DOMAINS = ("appworld", "code")
PANDA_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "panda"


def world_for_code(env: Env) -> Env:
    env.say("code.repo-checked-out", {"repository": "repo-1"}, TruthValue.TRUE)
    env.say("code.test-is-failing", {"test": "test_alpha"}, TruthValue.TRUE)
    env.say("code.working-tree-clean", {"repository": "repo-1"}, TruthValue.TRUE)
    return env


def world_for_appworld(env: Env) -> Env:
    env.say("appworld.app-reachable", {"app": "mail"}, TruthValue.TRUE)
    env.say("appworld.credentials-valid", {"app": "mail"}, TruthValue.TRUE)
    env.say("appworld.api-supports-request", {"app": "mail"}, TruthValue.TRUE)
    return env


def world_for_widget(env: Env) -> Env:
    env.say("widget.design-available", {"widget": "w-1"}, TruthValue.TRUE)
    env.say("widget.parts-in-stock", {"widget": "w-1"}, TruthValue.TRUE)
    return env


def decompose(env: Env, binding, *, fuel: int = 3, network=None, ledger=None):
    network = network or root_network(env, binding)
    ledger = ledger or ledger_for(binding, fuel=fuel, mission=env.mission)
    report = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    return network, ledger, report


def compile_first(env: Env, network, report):
    draft = report.drafts[0]
    contract = env.registry.definition(draft.method_ref)
    assert contract is not None
    return contract, compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )


# =========================================================== the shipped library


def test_both_pilot_domains_ship() -> None:
    assert available_domains() == PILOT_DOMAINS


def test_a_domain_is_exactly_four_files() -> None:
    for name in PILOT_DOMAINS:
        for file in DOMAIN_FILES:
            assert (SEED_ROOT / name / file).is_file()


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_each_domain_ships_at_least_three_methods(name: str) -> None:
    assert len(load_domain(name).methods) >= 3


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_each_domain_ships_a_recursive_method(name: str) -> None:
    assert any(method_is_recursive(item) for item in load_domain(name).methods)


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_each_domain_ships_a_pair_of_alternatives(name: str) -> None:
    domain = load_domain(name)
    by_goal: dict[str, int] = {}
    for contract in domain.methods:
        by_goal[contract.goal_type_ref.id] = by_goal.get(contract.goal_type_ref.id, 0) + 1
    assert any(count >= 2 for count in by_goal.values())


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_each_domain_declares_at_least_five_observed_predicates(name: str) -> None:
    assert len(load_domain(name).observed_predicates) >= 5


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_every_observer_is_read_only(name: str) -> None:
    """§7.3: an observer gathers evidence; looking must not change the answer."""

    for spec in load_domain(name).observer_types:
        assert spec.read_only
        assert spec.resource_writes == ()


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_every_declared_predicate_has_an_observer(name: str) -> None:
    domain = load_domain(name)
    observed = {(item.id, item.version) for item in domain.observed_predicates}
    for signature in domain.predicates:
        assert signature.key in observed


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_each_domain_ships_a_shareable_read_only_sub_goal(name: str) -> None:
    from agent_orchestrator.contracts.htn import ReusePolicy

    domain = load_domain(name)
    shareable = [
        spec for spec in domain.task_types if spec.reuse_policy is not ReusePolicy.NEW_WORK
    ]
    assert shareable
    assert all(spec.read_only for spec in shareable)


@pytest.mark.parametrize("name", PILOT_DOMAINS)
def test_every_seed_method_reaches_trial_admission(name: str) -> None:
    env = seed_env()
    for contract in load_domain(name).methods:
        registration = env.registry.registration(contract.method_ref())
        assert registration is not None
        assert registration.status is MethodRegistryStatus.TRIAL_ADMITTED


def test_a_seed_method_is_never_promoted_past_the_trial() -> None:
    env = seed_env()
    for method_ref in env.registry.method_refs():
        registration = env.registry.registration(method_ref)
        assert registration is not None
        assert registration.status is not MethodRegistryStatus.ADMITTED


def test_the_derived_content_hash_is_stable() -> None:
    assert seed_content_hash("code.fix-failing-test", 1) == seed_content_hash(
        "code.fix-failing-test", 1
    )
    assert seed_content_hash("code.fix-failing-test", 1) != seed_content_hash(
        "code.fix-failing-test", 2
    )


def test_a_domain_summary_is_deterministic() -> None:
    assert describe(load_domain("code")) == describe(load_domain("code"))


def test_an_unknown_directory_is_not_a_domain() -> None:
    from agent_orchestrator.contracts.models import ContractError

    with pytest.raises(ContractError, match="not a seed domain"):
        load_domain("nope")


# ============================================================== decomposition


def test_the_code_domain_decomposes_its_goal() -> None:
    env = world_for_code(seed_env())
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    _, _, report = decompose(env, binding)
    assert report.outcomes == (RefinementOutcome.REFINED,)


def test_the_code_decomposition_compiles_to_a_valid_increment() -> None:
    env = world_for_code(seed_env())
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    network, _, report = decompose(env, binding)
    _, bundle = compile_first(env, network, report)
    assert validate_execution_projection(bundle.network.execution_projection(), BUDGET).ok


def test_the_appworld_domain_decomposes_its_goal() -> None:
    env = world_for_appworld(seed_env())
    binding = task_binding(
        env, "appworld.fulfil-request", parameters={"app": "mail", "request": "send"}
    )
    _, _, report = decompose(env, binding)
    assert report.outcomes == (RefinementOutcome.REFINED,)


def test_the_appworld_decomposition_compiles_to_a_valid_increment() -> None:
    env = world_for_appworld(seed_env())
    binding = task_binding(
        env, "appworld.fulfil-request", parameters={"app": "mail", "request": "send"}
    )
    network, _, report = decompose(env, binding)
    _, bundle = compile_first(env, network, report)
    assert validate_execution_projection(bundle.network.execution_projection(), BUDGET).ok


def test_the_appworld_alternative_is_chosen_when_the_api_does_not_cover_it() -> None:
    env = world_for_appworld(seed_env())
    env.say("appworld.api-supports-request", {"app": "mail"}, TruthValue.FALSE)
    binding = task_binding(
        env, "appworld.fulfil-request", parameters={"app": "mail", "request": "send"}
    )
    _, _, report = decompose(env, binding)
    decision = report.decisions[0]
    assert decision.chosen is not None
    assert decision.chosen.method.method_id == "appworld.fulfil-by-search"


def test_the_two_domains_go_through_the_same_calls() -> None:
    """The generality claim: no branch anywhere decides which domain this is."""

    code = world_for_code(seed_env())
    app = world_for_appworld(seed_env())
    code_report = decompose(
        code,
        task_binding(
            code,
            "code.fix-failing-test",
            parameters={"repository": "repo-1", "failing_test": "test_alpha"},
        ),
    )[2]
    app_report = decompose(
        app,
        task_binding(app, "appworld.fulfil-request", parameters={"app": "mail", "request": "send"}),
    )[2]
    assert code_report.outcomes == app_report.outcomes


# ======================================================== a third, invented domain


def widget_env() -> Env:
    env = seed_env()
    add_domain(env, DOMAIN_ROOT / "widget")
    return world_for_widget(env)


def test_a_third_domain_needs_only_data() -> None:
    env = widget_env()
    assert "widget" in {domain.name for domain in env.domains}


def test_the_invented_domain_decomposes_its_goal() -> None:
    env = widget_env()
    binding = task_binding(env, "widget.build-widget", parameters={"widget": "w-1", "tier": 2})
    _, _, report = decompose(env, binding)
    assert report.outcomes == (RefinementOutcome.REFINED,)


def test_the_invented_domain_compiles_to_a_valid_increment() -> None:
    env = widget_env()
    binding = task_binding(env, "widget.build-widget", parameters={"widget": "w-1", "tier": 2})
    network, _, report = decompose(env, binding)
    _, bundle = compile_first(env, network, report)
    assert validate_execution_projection(bundle.network.execution_projection(), BUDGET).ok


def test_the_invented_domain_picks_its_own_alternative() -> None:
    env = widget_env()
    env.say("widget.parts-in-stock", {"widget": "w-1"}, TruthValue.FALSE)
    env.say("widget.kit-available", {"widget": "w-1"}, TruthValue.TRUE)
    binding = task_binding(env, "widget.build-widget", parameters={"widget": "w-1", "tier": 2})
    _, _, report = decompose(env, binding)
    decision = report.decisions[0]
    assert decision.chosen is not None
    assert decision.chosen.method.method_id == "widget.build-from-kit"


def test_registering_a_third_domain_does_not_disturb_the_first_two() -> None:
    env = widget_env()
    for name in PILOT_DOMAINS:
        for contract in load_domain(name).methods:
            assert env.registry.retrievable(contract.method_ref(), mission_id=env.mission)


# ===================================================================== recursion


def recursive_setup(fuel: int):
    env = seed_env()
    env.say("code.changeset-too-large", {"changeset": "cs-1"}, TruthValue.TRUE)
    env.say("code.changeset-too-large", {"changeset": "chunk"}, TruthValue.TRUE)
    binding = task_binding(
        env, "code.review-changes", parameters={"changeset": "cs-1", "depth_budget": 3}
    )
    network = root_network(env, binding)
    ledger = ledger_for(binding, fuel=fuel, mission=env.mission)
    return env, binding, network, ledger


def expand_once(env: Env, network, ledger, frontier=None):
    report = refine(
        frontier if frontier is not None else planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    if not report.drafts:
        return report, network
    draft = report.drafts[0]
    contract = env.registry.definition(draft.method_ref)
    assert contract is not None
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    return report, bundle.network


def test_the_recursive_method_is_chosen_when_the_changeset_is_too_large() -> None:
    env, _, network, ledger = recursive_setup(3)
    report, _ = expand_once(env, network, ledger)
    decision = report.decisions[0]
    assert decision.chosen is not None
    assert decision.chosen.method.method_id == "code.review-changes-recursively"


def test_the_first_expansion_spends_one_unit_of_fuel() -> None:
    env, binding, network, ledger = recursive_setup(3)
    expand_once(env, network, ledger)
    assert ledger.remaining_fuel(binding.obligation_id) == 2


def test_the_recursion_opens_a_compound_child_of_the_same_goal_type() -> None:
    env, _, network, ledger = recursive_setup(3)
    _, network = expand_once(env, network, ledger)
    signatures = {
        network.binding_for_occurrence(item).goal_signature.signature_id
        for item in planning_frontier(network)
        and [item.occurrence_id for item in planning_frontier(network)]
    }
    assert signatures == {"code.review-changes"}


def test_the_recursion_expands_a_second_level_under_fuel() -> None:
    env, binding, network, ledger = recursive_setup(3)
    _, network = expand_once(env, network, ledger)
    report, network = expand_once(env, network, ledger)
    assert report.outcomes == (RefinementOutcome.REFINED,)
    assert ledger.remaining_fuel(binding.obligation_id) == 1


def test_a_child_duty_spends_the_parent_obligation_s_fuel() -> None:
    """§6.1: splitting a task does not mint a fresh retry allowance."""

    env, binding, network, ledger = recursive_setup(3)
    _, network = expand_once(env, network, ledger)
    expand_once(env, network, ledger)
    assert ledger.obligation_ids() == (binding.obligation_id,)


def test_exhausted_fuel_reports_a_bound_not_an_impossible_goal() -> None:
    env, _, network, ledger = recursive_setup(1)
    _, network = expand_once(env, network, ledger)
    report, _ = expand_once(env, network, ledger)
    assert report.outcomes == (RefinementOutcome.BOUND_REACHED,)


def test_the_bound_report_names_the_obligation_and_its_expansions() -> None:
    env, binding, network, ledger = recursive_setup(1)
    _, network = expand_once(env, network, ledger)
    report, _ = expand_once(env, network, ledger)
    bound = report.decisions[0].bound_report
    assert bound is not None
    assert bound.obligation_id == binding.obligation_id
    assert bound.expansions


def test_the_bound_report_is_serialised_as_bound_reached() -> None:
    env, _, network, ledger = recursive_setup(1)
    _, network = expand_once(env, network, ledger)
    report, _ = expand_once(env, network, ledger)
    bound = report.decisions[0].bound_report
    assert bound is not None
    assert bound.to_json()["status"] == str(FuelStatus.BOUND_REACHED)


def test_the_structure_expanded_so_far_survives_the_bound() -> None:
    env, _, network, ledger = recursive_setup(1)
    _, expanded = expand_once(env, network, ledger)
    before = {str(spec.occurrence_id) for spec in expanded.occurrences}
    _, after_network = expand_once(env, expanded, ledger)
    assert {str(spec.occurrence_id) for spec in after_network.occurrences} == before


def test_the_bound_is_never_reported_as_unsolvable() -> None:
    env, _, network, ledger = recursive_setup(1)
    _, network = expand_once(env, network, ledger)
    report, _ = expand_once(env, network, ledger)
    assert "UNSOLVABLE" not in report.decisions[0].reason
    assert "bound" in report.decisions[0].reason


def test_repeating_the_same_expansion_is_refused_before_fuel_is_spent() -> None:
    env, binding, network, ledger = recursive_setup(3)
    expand_once(env, network, ledger)
    remaining = ledger.remaining_fuel(binding.obligation_id)
    report, _ = expand_once(env, network, ledger)
    assert report.outcomes == (RefinementOutcome.REPEATED_EXPANSION,)
    assert ledger.remaining_fuel(binding.obligation_id) == remaining


def test_an_expansion_that_repeats_an_ancestor_changes_no_state() -> None:
    env, binding, network, ledger = recursive_setup(5)
    _, network = expand_once(env, network, ledger)
    _, network = expand_once(env, network, ledger)
    remaining = ledger.remaining_fuel(binding.obligation_id)
    report, _ = expand_once(env, network, ledger)
    assert report.outcomes == (RefinementOutcome.NO_STATE_CHANGE,)
    assert ledger.remaining_fuel(binding.obligation_id) == remaining


def test_the_no_state_change_reason_names_the_ancestor() -> None:
    env, _, network, ledger = recursive_setup(5)
    _, network = expand_once(env, network, ledger)
    _, network = expand_once(env, network, ledger)
    report, _ = expand_once(env, network, ledger)
    assert "already expanded" in report.decisions[0].reason


def test_the_base_case_method_ends_the_recursion() -> None:
    env = seed_env()
    env.say("code.changeset-reviewable", {"changeset": "cs-1"}, TruthValue.TRUE)
    binding = task_binding(
        env, "code.review-changes", parameters={"changeset": "cs-1", "depth_budget": 1}
    )
    network, _, report = decompose(env, binding)
    decision = report.decisions[0]
    assert decision.chosen is not None
    assert decision.chosen.method.method_id == "code.review-changes-directly"
    del network


# ====================================================================== evidence


def primitive_frontier(env: Env, network, slot_key: str, draft):
    binding = next(item for item in draft.child_bindings if item.slot_key == slot_key)
    spec = network.occurrence(binding.occurrence_id)
    return (FrontierItem.of(spec),)


def patch_leaf(env: Env, *, clean: TruthValue):
    env.say("code.repo-checked-out", {"repository": "repo-1"}, TruthValue.TRUE)
    env.say("code.test-is-failing", {"test": "test_alpha"}, TruthValue.TRUE)
    env.say("code.working-tree-clean", {"repository": "repo-1"}, clean)
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    network, ledger, report = decompose(env, binding)
    contract, bundle = compile_first(env, network, report)
    draft = report.drafts[0]
    return env, bundle.network, ledger, draft


def test_an_unknown_precondition_produces_an_evidence_occurrence() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.UNKNOWN)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.NEEDS_EVIDENCE,)
    assert report.decisions[0].evidence


def test_the_evidence_occurrence_is_read_only() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.UNKNOWN)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    request = report.decisions[0].evidence[0]
    assert request.observer is not None
    assert request.observer.read_only
    assert request.binding is not None
    assert request.binding.resource_writes == ()


def test_the_evidence_occurrence_names_the_predicate_it_would_answer() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.UNKNOWN)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    request = report.decisions[0].evidence[0]
    assert request.predicate_ref.id == "code.working-tree-clean"


def test_the_high_risk_leaf_is_not_dispatched_while_the_precondition_is_unknown() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.UNKNOWN)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.decisions[0].outcome is not RefinementOutcome.LEAF


def test_a_resolved_precondition_makes_the_leaf_a_leaf() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.TRUE)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.LEAF,)


def test_an_unobservable_predicate_says_so_rather_than_guessing() -> None:
    env = seed_env()
    spec = env.catalog.require(ref("code.repo-observer"))
    from agent_orchestrator.planning.htn.refinement import (
        UnknownProposition,
        evidence_requests,
    )

    requests = evidence_requests(
        (
            UnknownProposition(
                proposition_key="p", predicate_ref=ref("code.no-such"), truth=TruthValue.UNKNOWN
            ),
        ),
        for_occurrence="occ-1",
        obligation_id="obl-1",
        catalog=env.catalog,
        semantic_scope=env.mission,
    )
    assert not requests[0].satisfiable
    assert "UNKNOWN" in requests[0].reason
    del spec


# ================================================================= attempt policy


def test_a_read_only_leaf_may_be_tried_before_it_is_planned() -> None:
    env = seed_env()
    spec = env.catalog.require(ref("code.read-repository-facts"))
    assert bounded_attempt_admissible(spec) is AttemptPolicy.BOUNDED_ATTEMPT_FIRST


def test_a_reversible_local_write_may_be_tried() -> None:
    env = seed_env()
    spec = env.catalog.require(ref("code.apply-patch"))
    assert bounded_attempt_admissible(spec) is AttemptPolicy.BOUNDED_ATTEMPT_FIRST


def test_an_irreversible_external_write_is_planned_first() -> None:
    """§7.2: "just try it once" is not how a send gets decided."""

    env = seed_env()
    spec = env.catalog.require(ref("appworld.submit-action"))
    assert spec.side_effect_kind is SideEffectKind.EXTERNAL_STATE_WRITE
    assert bounded_attempt_admissible(spec) is AttemptPolicy.PLAN_BEFORE_ATTEMPT


def test_a_compound_task_is_never_a_leaf() -> None:
    env = world_for_code(seed_env())
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    decision = leaf_decision(
        binding,
        env.catalog.require(ref("code.fix-failing-test")),
        capabilities=env.capabilities(),
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert not decision.is_leaf
    assert "refined by a method" in decision.reason


def test_a_leaf_without_an_operator_is_not_executable_here() -> None:
    env = world_for_code(seed_env())
    binding = task_binding(env, "code.verify-tests", parameters={"repository": "repo-1"})
    decision = leaf_decision(
        binding,
        None,
        capabilities=env.capabilities(),
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert not decision.is_leaf
    assert "no registered operator" in decision.reason


def test_a_missing_capability_stops_a_leaf() -> None:
    env = world_for_code(seed_env())
    binding = task_binding(env, "code.verify-tests", parameters={"repository": "repo-1"})
    decision = leaf_decision(
        binding,
        env.catalog.require(ref("code.verify-tests")),
        capabilities=env.capabilities(unavailable=("tests.run",)),
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert not decision.is_leaf
    assert decision.missing_capabilities == ("tests.run",)


# ========================================================================== HDDL


def code_delta(env: Env | None = None):
    env = world_for_code(env or seed_env())
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    network, _, report = decompose(env, binding)
    contract, bundle = compile_first(env, network, report)
    return env, contract, bundle


def test_a_seed_increment_exports_to_the_supported_fragment() -> None:
    _, contract, bundle = code_delta()
    export = to_hddl(bundle.delta, network=bundle.network, methods={contract.method_id: contract})
    assert isinstance(export, HddlExport)


def test_the_export_keeps_every_occurrence_in_its_map() -> None:
    """§8.3 / TG §12: the model must keep every occurrence, not a shortened trace."""

    _, contract, bundle = code_delta()
    export = to_hddl(bundle.delta, network=bundle.network, methods={contract.method_id: contract})
    assert isinstance(export, HddlExport)
    involved = {str(spec.occurrence_id) for spec in bundle.delta.occurrences} | {
        str(item) for item in bundle.delta.referenced_occurrences
    }
    assert set(export.occurrence_map) == involved


def test_the_export_declares_its_fragment() -> None:
    _, contract, bundle = code_delta()
    export = to_hddl(bundle.delta, network=bundle.network, methods={contract.method_id: contract})
    assert isinstance(export, HddlExport)
    assert export.fragment == "strips-typed-partial-order"


def test_the_export_adds_no_ordering_the_delta_did_not_declare() -> None:
    """§7.4: a partial order is exported as one, never linearised on the way out."""

    _, contract, bundle = code_delta()
    export = to_hddl(bundle.delta, network=bundle.network, methods={contract.method_id: contract})
    assert isinstance(export, HddlExport)
    declared = {(str(item.before), str(item.after)) for item in bundle.delta.order_constraints} | {
        (str(item.producer_occurrence), str(item.consumer_occurrence))
        for item in bundle.delta.data_requirements
    }
    assert export.domain_text.count("(< ") == len(declared)


def test_the_export_is_deterministic() -> None:
    _, contract, first = code_delta()
    _, _, second = code_delta()
    left = to_hddl(first.delta, network=first.network, methods={contract.method_id: contract})
    right = to_hddl(second.delta, network=second.network, methods={contract.method_id: contract})
    assert isinstance(left, HddlExport) and isinstance(right, HddlExport)
    assert left.domain_text == right.domain_text


def test_a_disjunctive_precondition_is_outside_the_fragment() -> None:
    from agent_orchestrator.contracts.htn import AnyCondition

    env, contract, bundle = code_delta()
    widened = type(contract)(
        method_id=contract.method_id,
        method_version=contract.method_version,
        goal_type_ref=contract.goal_type_ref,
        parameter_schema_ref=contract.parameter_schema_ref,
        output_schema_ref=contract.output_schema_ref,
        applicable_when=(AnyCondition(items=contract.applicable_when),),
        exploration_assumptions=contract.exploration_assumptions,
        steps=contract.steps,
        ordering=contract.ordering,
        required_capabilities=contract.required_capabilities,
        expected_effects=contract.expected_effects,
        composition=contract.composition,
        basis_refs=contract.basis_refs,
    )
    features = unsupported_features(
        bundle.delta, network=bundle.network, methods={contract.method_id: widened}
    )
    assert "disjunctive-preconditions" in features
    del env


def test_a_set_valued_port_is_outside_the_fragment() -> None:
    env = Env()
    env.register_predicate("frag.ready")
    env.register_type(
        "frag.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c",),
    )
    env.register_type(
        "frag.collector",
        parameters=(("subject", "string"),),
        inputs=(("many", "frag.many", False),),
        outputs=(("out", "frag.out"),),
        set_port=True,
    )
    from agent_orchestrator.contracts.htn import OccurrenceSpec, ProposedPlanDelta, SemanticReadSet
    from agent_orchestrator.graph.task_network import TaskNetworkSnapshot

    binding = task_binding(
        env, "frag.collector", task_id="task-c", obligation="obl-c", parameters={"subject": "x"}
    )
    network = TaskNetworkSnapshot(
        mission_id=env.mission,
        plan_revision=0,
        occurrences=(
            OccurrenceSpec(
                occurrence_id="occ-c",
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                form=TaskForm.PRIMITIVE,
            ),
        ),
        task_bindings=(binding,),
        root_occurrence_ids=("occ-c",),
    )
    delta = ProposedPlanDelta(
        delta_id="delta-frag",
        mission_id=env.mission,
        base_plan_revision=0,
        read_set=SemanticReadSet(requirements_revision=0),
        occurrences=network.occurrences,
    )
    assert "set-valued-ports" in unsupported_features(delta, network=network)


def test_an_unsupported_export_names_what_it_could_not_express() -> None:
    env = Env()
    env.register_type(
        "frag.collector2",
        parameters=(("subject", "string"),),
        inputs=(("many", "frag.many", False),),
        outputs=(("out", "frag.out"),),
        set_port=True,
    )
    from agent_orchestrator.contracts.htn import OccurrenceSpec, ProposedPlanDelta, SemanticReadSet
    from agent_orchestrator.graph.task_network import TaskNetworkSnapshot

    binding = task_binding(
        env, "frag.collector2", task_id="task-c", obligation="obl-c", parameters={"subject": "x"}
    )
    network = TaskNetworkSnapshot(
        mission_id=env.mission,
        plan_revision=0,
        occurrences=(
            OccurrenceSpec(
                occurrence_id="occ-c",
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                form=TaskForm.PRIMITIVE,
            ),
        ),
        task_bindings=(binding,),
        root_occurrence_ids=("occ-c",),
    )
    delta = ProposedPlanDelta(
        delta_id="delta-frag-2",
        mission_id=env.mission,
        base_plan_revision=0,
        read_set=SemanticReadSet(requirements_revision=0),
        occurrences=network.occurrences,
    )
    export = to_hddl(delta, network=network)
    assert isinstance(export, UnsupportedFeature)
    assert "set-valued-ports" in export.features
    assert not export.supported


def test_a_revision_following_data_policy_is_outside_the_fragment() -> None:
    from agent_orchestrator.contracts.htn import SourceRevisionPolicy

    _, _, bundle = code_delta()
    requirement = bundle.delta.data_requirements[0]
    following = type(requirement)(
        requirement_id=requirement.requirement_id,
        producer_occurrence=requirement.producer_occurrence,
        output_port=requirement.output_port,
        consumer_occurrence=requirement.consumer_occurrence,
        input_port=requirement.input_port,
        schema_ref=requirement.schema_ref,
        assurance_policy_ref=requirement.assurance_policy_ref,
        freshness_policy_ref=requirement.freshness_policy_ref,
        source_revision_policy=SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION,
    )
    from dataclasses import replace

    delta = replace(bundle.delta, data_requirements=(following,))
    assert "revision-following-data" in unsupported_features(delta, network=bundle.network)


# ==================================================================== generality


def test_the_loader_does_not_name_a_domain() -> None:
    import agent_orchestrator.planning.htn.seed_methods.loader as loader

    source = Path(loader.__file__).read_text(encoding="utf-8")
    for needle in ('"code"', "'code'", '"appworld"', "'appworld'"):
        assert needle not in source


def test_the_seed_package_init_does_not_branch_on_a_domain() -> None:
    import agent_orchestrator.planning.htn.seed_methods as package

    source = Path(package.__file__).read_text(encoding="utf-8")
    for needle in ('== "code"', '== "appworld"'):
        assert needle not in source


# ============================================ the exported model must be solvable


HDDL_METHOD_RE = re.compile(r"\(:method\s+(\S+)")
HDDL_TASK_DECL_RE = re.compile(r"\(:task\s+(\S+)")
HDDL_ACTION_RE = re.compile(r"\(:action\s+(\S+)")


def method_blocks(domain_text: str) -> list[str]:
    """Split the domain into its ``(:method …)`` blocks, one string each."""

    blocks: list[str] = []
    cursor = 0
    while True:
        start = domain_text.find("(:method", cursor)
        if start < 0:
            return blocks
        end = domain_text.find("(:method", start + 1)
        stop = domain_text.find("(:action", start + 1)
        if stop >= 0 and (end < 0 or stop < end):
            end = stop
        blocks.append(domain_text[start : end if end > 0 else len(domain_text)])
        cursor = start + 1


def method_task(block: str) -> str:
    """The ``:task`` a method decomposes, as ``"<task> <object>"``."""

    match = re.search(r":task\s+\(([^)]*)\)", block)
    assert match is not None, block
    return match.group(1).strip()


def method_subtasks(block: str) -> list[str]:
    """The task or action calls in a method's ``:subtasks``."""

    body = block.split(":subtasks", 1)[1]
    return [
        f"{head} {rest}".strip()
        for head, rest in re.findall(r"\(t\d+\s+\((\S+)\s*([^)]*)\)\)", body)
    ]


def export_for(env: Env, contract, bundle) -> HddlExport:
    export = to_hddl(bundle.delta, network=bundle.network, methods={contract.method_id: contract})
    assert isinstance(export, HddlExport), export
    del env
    return export


def test_every_abstract_task_has_at_least_one_method() -> None:
    """An abstract task no method decomposes makes every plan unsolvable."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    declared = set(HDDL_TASK_DECL_RE.findall(export.domain_text))
    decomposed = {method_task(block).split()[0] for block in method_blocks(export.domain_text)}
    assert declared
    assert declared <= decomposed


def test_every_declared_task_is_actually_used() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    for task in HDDL_TASK_DECL_RE.findall(export.domain_text):
        assert f"({task} " in export.problem_text or any(
            task in method_task(block) or any(task in call for call in method_subtasks(block))
            for block in method_blocks(export.domain_text)
        )


def test_every_action_is_reachable_from_the_initial_network() -> None:
    """Otherwise the model declares actions no decomposition can ever produce."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    by_task: dict[str, list[str]] = {}
    for block in method_blocks(export.domain_text):
        by_task.setdefault(method_task(block), []).append(block)

    reached_actions: set[str] = set()
    pending = re.findall(r"\(root\d+\s+\(([^)]*)\)\)", export.problem_text)
    seen: set[str] = set()
    while pending:
        task = pending.pop().strip()
        if task in seen:
            continue
        seen.add(task)
        for block in by_task.get(task, []):
            for call in method_subtasks(block):
                head = call.split()[0]
                if head.startswith("a_"):
                    reached_actions.add(head)
                else:
                    pending.append(call)
    declared_actions = set(HDDL_ACTION_RE.findall(export.domain_text))
    assert declared_actions
    assert declared_actions == reached_actions


def test_the_initial_network_contains_only_the_refined_goal() -> None:
    """Listing a parent beside its children would ask for the children twice."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    roots = re.findall(r"\(root\d+\s+\(t_available\s+(\S+)\)\)", export.problem_text)
    parent = bundle.delta.method_instances[0].effective_goal_occurrence_id
    assert roots == [export.occurrence_map[str(parent)]]


def test_no_method_carries_a_free_variable() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    for block in method_blocks(export.domain_text):
        assert ":parameters ()" in block
        assert "?" not in method_task(block)


def test_every_occurrence_is_a_declared_constant() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    constants = export.domain_text.split("(:constants", 1)[1].split("(:predicates", 1)[0]
    for label in export.occurrence_map.values():
        assert f"{label} - occurrence" in constants


def test_one_method_per_exported_occurrence() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    tasks = {method_task(block) for block in method_blocks(export.domain_text)}
    assert tasks == {f"t_available {label}" for label in export.occurrence_map.values()}


def test_an_unexpanded_compound_is_outside_the_fragment() -> None:
    """A compound nobody refined is not a model a planner can solve."""

    env = seed_env()
    env.say("code.changeset-too-large", {"changeset": "cs-1"}, TruthValue.TRUE)
    binding = task_binding(
        env, "code.review-changes", parameters={"changeset": "cs-1", "depth_budget": 3}
    )
    network, _, report = decompose(env, binding)
    _, bundle = compile_first(env, network, report)
    export = to_hddl(bundle.delta, network=bundle.network)
    assert isinstance(export, UnsupportedFeature)
    assert "unexpanded-compound-occurrence" in export.features


def test_a_delta_with_no_decomposition_exports_nothing() -> None:
    from agent_orchestrator.contracts.htn import ProposedPlanDelta, SemanticReadSet

    env = world_for_code(seed_env())
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    network = root_network(env, binding)
    empty = ProposedPlanDelta(
        delta_id="delta-empty",
        mission_id=env.mission,
        base_plan_revision=0,
        read_set=SemanticReadSet(requirements_revision=0),
    )
    export = to_hddl(empty, network=network)
    assert isinstance(export, UnsupportedFeature)
    assert "no-decomposition-to-export" in export.features


def test_the_export_passes_the_panda_adapter_s_fragment_check() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    report = check_fragment(export.domain_text, export.problem_text)
    assert report.supported, report.detection_notes


def plan_text_for(export: HddlExport, bundle) -> str:
    """An IPC hierarchical plan written against the exported model.

    Writing one at all is the point: it can only be done if the exported action and
    method names line up with the decomposition, which is exactly what the previous
    draft of this exporter got wrong.
    """

    by_task = {method_task(block).split()[1]: block for block in method_blocks(export.domain_text)}
    order: list[str] = []
    actions: list[tuple[int, str]] = []
    decompositions: list[tuple[int, str, str, list[int]]] = []
    counter = 0

    def walk(label: str) -> int:
        nonlocal counter
        block = by_task[label]
        calls = method_subtasks(block)
        name = HDDL_METHOD_RE.search(block).group(1)  # type: ignore[union-attr]
        children: list[int] = []
        for call in calls:
            head, _, argument = call.partition(" ")
            if head.startswith("a_"):
                identifier = counter
                counter += 1
                actions.append((identifier, f"{head} {argument}".strip()))
                children.append(identifier)
            else:
                children.append(walk(argument.strip()))
        identifier = counter
        counter += 1
        decompositions.append((identifier, f"t_available {label}", name, children))
        return identifier

    root_label = re.findall(r"\(root\d+\s+\(t_available\s+(\S+)\)\)", export.problem_text)[0]
    root_id = walk(root_label)
    order.append("==>")
    order.extend(f"{identifier} {line}" for identifier, line in sorted(actions))
    order.append(f"root {root_id}")
    for identifier, task, name, children in sorted(decompositions):
        order.append(f"{identifier} {task} -> {name} " + " ".join(str(item) for item in children))
    order.append("<==")
    del bundle
    return "\n".join(order) + "\n"


def test_a_plan_written_against_the_export_parses_into_a_witness() -> None:
    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    witness = parse_plan(plan_text_for(export, bundle))
    assert witness.primitives
    assert witness.decompositions
    assert witness.roots


def test_the_witness_keeps_one_primitive_per_primitive_occurrence() -> None:
    """§8.3 / TG §12: every task occurrence survives into the decomposition witness."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    witness = parse_plan(plan_text_for(export, bundle))
    primitives = {item.arguments[-1] for item in witness.primitives}
    expected = {
        export.occurrence_map[str(spec.occurrence_id)]
        for spec in bundle.delta.occurrences
        if spec.form is TaskForm.PRIMITIVE
    }
    assert primitives == expected


def test_the_stub_parser_verifies_the_exported_model() -> None:
    """A stub verdict, but a real round trip through the adapter (§7.4)."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    toolchain = PandaToolchain.from_paths(parser=str(PANDA_FIXTURES / "stub_parser_true.py"))
    result = toolchain.verify_plan(
        export.domain_text,
        export.problem_text,
        plan_text_for(export, bundle),
        timeout_s=30.0,
    )
    assert result.status is VerificationStatus.VERIFIED
    assert result.witness is not None


def test_the_adapter_reports_solver_unavailable_rather_than_a_pass() -> None:
    """§7.4 / C7: unavailable is UNSUPPORTED, never a silent PASS."""

    env, contract, bundle = code_delta()
    export = export_for(env, contract, bundle)
    result = PandaToolchain().verify_plan(
        export.domain_text, export.problem_text, plan_text_for(export, bundle)
    )
    assert result.status is VerificationStatus.SOLVER_UNAVAILABLE


# ============================================================= evidence and fuel


def test_gathering_evidence_costs_no_recursion_fuel() -> None:
    """§6.4: fuel pays for expansions.  Looking something up is not one."""

    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.UNKNOWN)
    before = ledger.remaining_fuel(draft.obligation_id)
    report = refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.NEEDS_EVIDENCE,)
    assert ledger.remaining_fuel(draft.obligation_id) == before


def test_a_leaf_that_is_ready_costs_no_fuel_either() -> None:
    env, network, ledger, draft = patch_leaf(seed_env(), clean=TruthValue.TRUE)
    before = ledger.remaining_fuel(draft.obligation_id)
    refine(
        primitive_frontier(env, network, "patch", draft),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert ledger.remaining_fuel(draft.obligation_id) == before


def test_a_compound_with_no_applicable_method_costs_no_fuel() -> None:
    env = seed_env()
    binding = task_binding(
        env, "code.review-changes", parameters={"changeset": "cs-1", "depth_budget": 1}
    )
    network = root_network(env, binding)
    ledger = ledger_for(binding, mission=env.mission)
    before = ledger.remaining_fuel(binding.obligation_id)
    report = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.NEEDS_EVIDENCE,)
    assert ledger.remaining_fuel(binding.obligation_id) == before


# ================================ G2: the seed library can express a shared reading


def _assessed_revert(env: Env):
    """Round one: the fix method that reads the repository and delegates the assessment."""

    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method

    env.say("code.regression-commit-known", {"repository": "repo-1"}, TruthValue.TRUE)
    binding = task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )
    contract = _seed_method(env, "code.fix-by-assessed-revert")
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert report.applicable, report.status
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    bundle = compile_refinement_bundle(
        draft,
        root_network(env, binding),
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    return bundle


def _seed_method(env: Env, method_id: str):
    contract = next(
        definition
        for definition in (
            env.registry.definition(reference) for reference in env.registry.method_refs()
        )
        if definition is not None and definition.method_id == method_id
    )
    return contract


def _assess_the_regression(env: Env, bundle):
    """Round two: refine the assessment sub-goal against what round one left behind."""

    from agent_orchestrator.orchestrator.hierarchical_dispatch import shared_goal_index
    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method

    network = bundle.network
    child = next(
        spec
        for spec in network.occurrences
        if str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id)
        == "code.assess-regression"
    )
    parent = network.binding_for_occurrence(child.occurrence_id)
    contract = _seed_method(env, "code.assess-by-reading")
    report = assess_method(
        parent, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert report.applicable, report.status
    sharing = shared_goal_index(network, catalog=env.catalog)
    draft = ground_method(
        parent,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=sharing,
        goal_occurrence_id=child.occurrence_id,
    )
    return compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        sharing=sharing,
    )


def _readings(network) -> list:
    return [
        spec
        for spec in network.occurrences
        if str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id)
        == "code.read-repository-facts"
    ]


def test_the_code_domain_can_now_express_a_shared_read_only_sub_goal() -> None:
    """G2: no pair of seed methods could ever want the same read-only goal.

    ``code.fix-by-patch`` and ``code.fix-by-revert`` both read the repository, but
    they are alternatives for one goal, so only the chosen one is ever grounded — one
    consumer, never two.  The assessment sub-goal is a *child*, not an alternative, so
    the parent's reading and the child's are two slots of one goal.
    """

    env = world_for_code(seed_env())
    first = _assessed_revert(env)
    assert len(_readings(first.network)) == 1
    second = _assess_the_regression(env, first)
    assert len(_readings(second.network)) == 1, "two consumers, one reading"


def test_the_assessment_still_gets_its_input_from_that_one_reading() -> None:
    env = world_for_code(seed_env())
    second = _assess_the_regression(env, _assessed_revert(env))
    shared = str(_readings(second.network)[0].occurrence_id)
    fed = {
        str(requirement.consumer_occurrence)
        for requirement in second.network.data_requirements
        if str(requirement.producer_occurrence) == shared
    }
    assert len(fed) == 2, "the parent's revert and the child's reproduce both read it"


def test_without_the_index_the_seed_pair_reads_the_repository_twice() -> None:
    """The control: the sharing is the index's doing, not a property of the methods."""

    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method

    env = world_for_code(seed_env())
    first = _assessed_revert(env)
    network = first.network
    child = next(
        spec
        for spec in network.occurrences
        if str(network.binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id)
        == "code.assess-regression"
    )
    parent = network.binding_for_occurrence(child.occurrence_id)
    contract = _seed_method(env, "code.assess-by-reading")
    report = assess_method(
        parent, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        parent,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        goal_occurrence_id=child.occurrence_id,
    )
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    assert len(_readings(bundle.network)) == 2
