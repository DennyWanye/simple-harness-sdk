# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1: alternatives are OR, a method's slots are AND, and sharing keeps identity.

§8.1: two methods offered for one goal are *alternatives*.  Choosing one does not
dispatch the other and does not block the root, and the slots inside the chosen
method are all necessary without being ordered.

§8.3 / TG §12: two slots bind one goal occurrence only when the whole sharing
signature matches and the task type has declared that reuse is permitted.  Every
near miss in this file is a separate test, because each is a different way a real
system loses money or sends something twice: a different scope, a different
parameter, an unshareable side effect, a type that never opted in, and text that
merely looks alike.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))

from htn_world import (  # noqa: E402
    BUDGET,
    Env,
    acceptance_ref,
    atom,
    ledger_for,
    method,
    out,
    param,
    ref,
    root_network,
    step,
    task_binding,
)

from agent_orchestrator.contracts.evidence_state import TruthValue  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    ReusePolicy,
    SideEffectKind,
    TaskForm,
)
from agent_orchestrator.graph.projection_validation import (  # noqa: E402
    validate_execution_projection,
    validate_refinement_acyclic,
)
from agent_orchestrator.graph.task_network import TaskNetworkSnapshot  # noqa: E402
from agent_orchestrator.planning.htn.applicability import (  # noqa: E402
    ApplicabilityStatus,
    assess_method,
)
from agent_orchestrator.planning.htn.compiler import (  # noqa: E402
    CompilationRefused,
    compile_refinement_bundle,
)
from agent_orchestrator.planning.htn.grounding import (  # noqa: E402
    SharedGoalEntry,
    SharedGoalIndex,
    ShareVerdict,
    SharingSignature,
    ground_method,
    may_share,
)
from agent_orchestrator.planning.htn.refinement import (  # noqa: E402
    RefinementOutcome,
    planning_frontier,
    refine,
)


def or_env() -> Env:
    env = Env()
    env.register_predicate("demo.primary-ready", (("subject", "string"),))
    env.register_predicate("demo.fallback-ready", (("subject", "string"),))
    env.register_type(
        "demo.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-done",),
        domain="demo",
    )
    env.register_type(
        "demo.goal2",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-done2",),
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
    env.register_type(
        "demo.private-read",
        parameters=(("subject", "string"),),
        outputs=(("facts", "demo.facts"),),
        capabilities=("demo.read",),
        domain="demo",
    )
    env.register_type(
        "demo.notify",
        parameters=(("subject", "string"),),
        outputs=(("receipt", "demo.receipt"),),
        capabilities=("demo.write",),
        effect=SideEffectKind.EXTERNAL_EVENT_WRITE,
        reversible=False,
        domain="demo",
    )
    for name, out_port in (("demo.a", "ra"), ("demo.b", "rb")):
        env.register_type(
            name,
            parameters=(("subject", "string"),),
            inputs=(("facts", "demo.facts", True),),
            outputs=((out_port, f"demo.{out_port}"),),
            capabilities=("demo.read",),
            domain="demo",
        )
    return env


def alternative(
    method_id: str,
    predicate: str,
    tail: str,
    *,
    goal: str = "demo.goal",
    criterion: str = "c-done",
    shared: str = "demo.shared-read",
):
    return method(
        method_id,
        goal,
        parameter_schema=f"{goal}.params",
        applicable=(atom(predicate, {"subject": param("subject")}),),
        steps=(
            step(
                "shared",
                shared,
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
        links=((criterion, tail, f"c-{tail}"),),
        finalizer=tail,
    )


def or_pair(env: Env):
    primary = alternative("demo.primary", "demo.primary-ready", "a")
    fallback = alternative("demo.fallback", "demo.fallback-ready", "b")
    env.admit(primary)
    env.admit(fallback)
    return primary, fallback


def refined(env: Env, binding, network=None, *, fuel: int = 3):
    network = network or root_network(env, binding)
    ledger = ledger_for(binding, fuel=fuel, mission=env.mission)
    return refine(
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


# ============================================================================ OR


def test_the_applicable_alternative_is_chosen() -> None:
    env = or_env()
    or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = refined(env, binding)
    decision = report.decisions[0]
    assert decision.outcome is RefinementOutcome.REFINED
    assert decision.chosen is not None
    assert decision.chosen.method.method_id == "demo.fallback"


def test_both_alternatives_are_assessed_before_one_is_chosen() -> None:
    env = or_env()
    or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    decision = refined(env, binding).decisions[0]
    assert {item.method.method_id for item in decision.candidates} == {
        "demo.primary",
        "demo.fallback",
    }


def test_the_refuted_alternative_is_reported_as_refuted() -> None:
    env = or_env()
    or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    decision = refined(env, binding).decisions[0]
    primary = next(item for item in decision.candidates if item.method.method_id == "demo.primary")
    assert primary.report.status is ApplicabilityStatus.PRECONDITION_FALSE
    assert not primary.selectable


def test_the_refuted_alternative_contributes_no_occurrence() -> None:
    env = or_env()
    primary, fallback = or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    draft = refined(env, binding, network).drafts[0]
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=fallback,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    assert all(
        draft.method_ref.method_id != primary.method_id for draft in bundle.delta.method_instances
    )
    assert len(bundle.delta.occurrences) == 2


def test_the_root_is_not_blocked_by_the_alternative_that_lost() -> None:
    env = or_env()
    _, fallback = or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    draft = refined(env, binding, network).drafts[0]
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=fallback,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    report = validate_execution_projection(bundle.network.execution_projection(), BUDGET)
    assert report.ok, [problem.detail for problem in report.problems]


def test_two_applicable_alternatives_leave_the_choice_deterministic() -> None:
    env = or_env()
    or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    first = refined(env, binding).decisions[0]
    second = refined(env, binding).decisions[0]
    assert first.chosen is not None and second.chosen is not None
    assert first.chosen.method.method_id == second.chosen.method.method_id


def test_only_one_alternative_is_adopted_per_occurrence() -> None:
    env = or_env()
    _, fallback = or_pair(env)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    draft = refined(env, binding, network).drafts[0]
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=fallback,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    assert len(bundle.network.adopted_instance_ids) == 1


def test_no_applicable_alternative_asks_for_a_new_method() -> None:
    env = or_env()
    or_pair(env)
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.FALSE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.FALSE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    decision = refined(env, binding).decisions[0]
    assert decision.outcome is RefinementOutcome.NO_APPLICABLE_METHOD
    assert decision.proposal_request is not None
    assert {item.method_id for item in decision.proposal_request.rejected} == {
        "demo.primary",
        "demo.fallback",
    }


def test_a_candidate_comparison_is_bounded_by_the_budget() -> None:
    from dataclasses import replace

    env = or_env()
    for index in range(5):
        env.admit(alternative(f"demo.many-{index}", "demo.primary-ready", "a"))
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    decision = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger_for(binding, mission=env.mission),
        budget=replace(BUDGET, max_candidates=2),
    ).decisions[0]
    assert len(decision.candidates) == 2


# =========================================================================== AND


def test_the_slots_of_one_method_are_all_required() -> None:
    env = or_env()
    _, fallback = or_pair(env)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    draft = refined(env, binding, network).drafts[0]
    alternative_view = network  # keep the pre-compilation network for contrast
    bundle = compile_refinement_bundle(
        draft,
        alternative_view,
        method=fallback,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    view = bundle.network.refinement_view()
    only = view.alternatives_for(binding.task_id)[0]
    assert len(only.required_children) == 2
    assert only.optional_children == ()


def test_and_children_with_no_declared_order_run_in_parallel() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = method(
        "demo.two-independent",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.primary-ready", {"subject": param("subject")}),),
        steps=(
            step(
                "left",
                "demo.private-read",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
            step(
                "right",
                "demo.shared-read",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
        ),
        links=(("c-done", "left", "c-left"),),
        finalizer="left",
    )
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
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
    assert bundle.delta.order_constraints == ()
    assert bundle.delta.data_requirements == ()


# ======================================================================= sharing


def shared_signature(
    env: Env,
    *,
    subject: str = "alpha",
    scope: str | None = None,
    task_type: str = "demo.shared-read",
) -> SharingSignature:
    spec = env.catalog.require(ref(task_type))
    return SharingSignature.of(
        spec,
        {"subject": subject},
        authority_scope=scope or env.mission,
        semantic_scope=scope or env.mission,
    )


def two_root_network(env: Env, *roots) -> TaskNetworkSnapshot:
    """A network with several independent root goals, so neither is an orphan."""

    from agent_orchestrator.contracts.htn import OccurrenceSpec

    return TaskNetworkSnapshot(
        mission_id=env.mission,
        plan_revision=0,
        occurrences=tuple(
            OccurrenceSpec(
                occurrence_id=str(binding.task_id),
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                form=binding.form,
            )
            for binding in roots
        ),
        task_bindings=tuple(roots),
        root_occurrence_ids=tuple(str(binding.task_id) for binding in roots),
        required_obligations=tuple(binding.obligation_id for binding in roots),
    )


def two_consumers(env: Env) -> tuple[TaskNetworkSnapshot, str, str]:
    """Compile the first consumer, then the second one against a sharing index."""

    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    env.say("demo.fallback-ready", {"subject": "alpha"}, TruthValue.TRUE)
    first = alternative("demo.first", "demo.primary-ready", "a")
    second = alternative(
        "demo.second", "demo.fallback-ready", "b", goal="demo.goal2", criterion="c-done2"
    )
    env.admit(first)
    env.admit(second)

    root_one = task_binding(
        env,
        "demo.goal",
        task_id="task-one",
        obligation="obl-one",
        parameters={"subject": "alpha"},
    )
    root_two = task_binding(
        env,
        "demo.goal2",
        task_id="task-two",
        obligation="obl-two",
        parameters={"subject": "alpha"},
    )
    network = two_root_network(env, root_one, root_two)
    report = assess_method(
        root_one, first, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft_one = ground_method(root_one, first, {}, report, catalog=env.catalog, schemas=env.schemas)
    bundle_one = compile_refinement_bundle(
        draft_one,
        network,
        method=first,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    slot = next(item for item in draft_one.child_bindings if item.slot_key == "shared")
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id=slot.occurrence_id,
                task_id=bundle_one.network.occurrence(slot.occurrence_id).task_id,
                obligation_id=slot.obligation_id,
                signature=shared_signature(env),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
            ),
        )
    )
    report_two = assess_method(
        root_two, second, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft_two = ground_method(
        root_two,
        second,
        {},
        report_two,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )
    bundle_two = compile_refinement_bundle(
        draft_two,
        bundle_one.network,
        method=second,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        sharing=index,
    )
    return bundle_two.network, str(draft_one.instance_id), str(slot.occurrence_id)


def test_a_shared_sub_goal_exists_once() -> None:
    env = or_env()
    network, _, shared = two_consumers(env)
    assert sum(1 for spec in network.occurrences if str(spec.occurrence_id) == shared) == 1


def test_a_shared_sub_goal_has_two_consumers() -> None:
    env = or_env()
    network, _, shared = two_consumers(env)
    parents = network.refinement_view().parents_of[shared]
    assert len(parents) == 2


def test_the_second_consumer_references_rather_than_creates_the_shared_goal() -> None:
    env = or_env()
    network, _, shared = two_consumers(env)
    second = next(
        draft for draft in network.method_instances if draft.method_ref.method_id == "demo.second"
    )
    slot = next(item for item in second.child_bindings if item.slot_key == "shared")
    assert str(slot.occurrence_id) == shared
    assert slot.reuse_policy is not ReusePolicy.NEW_WORK


def test_the_shared_network_is_still_acyclic() -> None:
    env = or_env()
    network, _, _ = two_consumers(env)
    assert validate_execution_projection(network.execution_projection(), BUDGET).ok
    assert validate_refinement_acyclic(network).ok


def test_cancelling_one_consumer_keeps_the_shared_goal() -> None:
    env = or_env()
    network, first_instance, shared = two_consumers(env)
    remaining = TaskNetworkSnapshot(
        mission_id=network.mission_id,
        plan_revision=network.plan_revision,
        occurrences=network.occurrences,
        task_bindings=tuple(
            binding
            if binding.adopted_method_instance_id != first_instance
            else _without_adoption(binding)
            for binding in network.task_bindings
        ),
        method_instances=network.method_instances,
        adopted_instance_ids=tuple(
            item for item in network.adopted_instance_ids if str(item) != first_instance
        ),
        root_occurrence_ids=network.root_occurrence_ids,
        order_constraints=network.order_constraints,
        data_requirements=network.data_requirements,
        obligation_coverage=network.obligation_coverage,
        required_obligations=network.required_obligations,
    )
    assert any(str(spec.occurrence_id) == shared for spec in remaining.occurrences)
    assert len(remaining.refinement_view().parents_of[shared]) == 1


def _without_adoption(binding):
    from agent_orchestrator.contracts.htn import TaskSemanticBindingV1

    payload = binding.to_json()
    payload["adopted_method_instance_id"] = None
    return TaskSemanticBindingV1.from_json(payload)


# ============================================================== sharing refusals


def test_a_different_scope_is_a_different_goal() -> None:
    env = or_env()
    left = shared_signature(env)
    right = shared_signature(env, scope="mission-2")
    decision = may_share(left, right, reuse_policy=ReusePolicy.REUSE_ACCEPTED)
    assert decision.verdict is ShareVerdict.SIGNATURE_DIFFERS
    assert "semantic_scope" in decision.differing_fields


def test_different_parameters_are_a_different_goal() -> None:
    env = or_env()
    decision = may_share(
        shared_signature(env, subject="alpha"),
        shared_signature(env, subject="beta"),
        reuse_policy=ReusePolicy.REUSE_ACCEPTED,
    )
    assert decision.verdict is ShareVerdict.SIGNATURE_DIFFERS
    assert "typed_parameters" in decision.differing_fields


def test_a_type_that_never_opted_into_reuse_is_not_shared() -> None:
    env = or_env()
    left = shared_signature(env, task_type="demo.private-read")
    decision = may_share(left, left, reuse_policy=ReusePolicy.NEW_WORK)
    assert decision.verdict is ShareVerdict.REUSE_NOT_PERMITTED


def test_a_side_effecting_goal_is_not_shared_even_when_identical() -> None:
    """§8.3: two identically-parameterised sends are two sends."""

    env = or_env()
    left = shared_signature(env, task_type="demo.notify")
    decision = may_share(left, left, reuse_policy=ReusePolicy.REUSE_ACCEPTED)
    assert decision.verdict is ShareVerdict.SIDE_EFFECT_NOT_SHAREABLE


def test_the_contract_refuses_a_reusing_side_effecting_type_without_an_identity() -> None:
    env = or_env()
    from agent_orchestrator.contracts.models import ContractError

    with pytest.raises(ContractError, match="effect_identity"):
        env.register_type(
            "demo.notify-reusing",
            parameters=(("subject", "string"),),
            outputs=(("receipt", "demo.receipt"),),
            effect=SideEffectKind.EXTERNAL_EVENT_WRITE,
            reuse=ReusePolicy.REUSE_ACCEPTED,
        )


@pytest.mark.parametrize(
    "field",
    [
        "authority_scope",
        "assurance_policy_ref",
        "freshness_policy_ref",
    ],
)
def test_every_signature_field_blocks_sharing_on_its_own(field: str) -> None:
    from dataclasses import replace

    env = or_env()
    left = shared_signature(env)
    right = replace(left, **{field: "other-value"})
    decision = may_share(left, right, reuse_policy=ReusePolicy.REUSE_ACCEPTED)
    assert decision.verdict is ShareVerdict.SIGNATURE_DIFFERS
    assert field in decision.differing_fields


def test_an_identical_signature_with_reuse_is_shareable() -> None:
    env = or_env()
    left = shared_signature(env)
    assert may_share(left, left, reuse_policy=ReusePolicy.REUSE_ACCEPTED).shareable


def test_an_index_miss_reports_why_rather_than_guessing() -> None:
    env = or_env()
    index = SharedGoalIndex()
    entry, decision = index.lookup(shared_signature(env), reuse_policy=ReusePolicy.REUSE_ACCEPTED)
    assert entry is None
    assert "no existing occurrence" in decision.reason


def test_lexical_closeness_produces_a_suggestion_not_a_binding() -> None:
    env = or_env()
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id="occ-existing",
                task_id="task-existing",
                obligation_id="obl-existing",
                signature=shared_signature(env, subject="beta"),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
            ),
        )
    )
    signature = shared_signature(env, subject="alpha")
    entry, _ = index.lookup(signature, reuse_policy=ReusePolicy.REUSE_ACCEPTED)
    assert entry is None
    suggestions = index.suggest(signature, minimum=0.0)
    assert suggestions and all(item.advisory_only for item in suggestions)
    assert suggestions[0].decision.verdict is ShareVerdict.SIGNATURE_DIFFERS


def test_a_suggestion_never_becomes_a_child_binding() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = alternative("demo.suggesting", "demo.primary-ready", "a")
    env.admit(contract)
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id="occ-lookalike",
                task_id="task-lookalike",
                obligation_id="obl-lookalike",
                signature=shared_signature(env, subject="beta"),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
            ),
        )
    )
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )
    assert all(str(item.occurrence_id) != "occ-lookalike" for item in draft.child_bindings)


def test_reuse_of_an_accepted_result_needs_the_acceptance() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = alternative("demo.reusing", "demo.primary-ready", "a")
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id=str(binding.task_id),
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                signature=shared_signature(env),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
                acceptance_ref=None,
            ),
        )
    )
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.reuse_policy is ReusePolicy.SHARE_ACTIVE


def test_an_acceptance_makes_the_slot_a_reuse_of_an_accepted_result() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = alternative("demo.reusing-2", "demo.primary-ready", "a")
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id=str(binding.task_id),
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                signature=shared_signature(env),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
                acceptance_ref=acceptance_ref("acceptance-7"),
            ),
        )
    )
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.reuse_policy is ReusePolicy.REUSE_ACCEPTED


def test_a_shared_occurrence_this_network_lacks_is_refused() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = alternative("demo.dangling", "demo.primary-ready", "a")
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id="occ-elsewhere",
                task_id="task-elsewhere",
                obligation_id="obl-elsewhere",
                signature=shared_signature(env),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
                acceptance_ref=acceptance_ref("acceptance-9"),
            ),
        )
    )
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )
    with pytest.raises(CompilationRefused, match="does not contain"):
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
            sharing=index,
        )


def test_a_shared_slot_creates_no_second_task_binding() -> None:
    env = or_env()
    network, _, shared = two_consumers(env)
    owners = [
        binding
        for binding in network.task_bindings
        if binding.occurrence_binding is not None
        and str(binding.occurrence_binding.occurrence_id) == shared
    ]
    assert len(owners) == 1


def test_the_acceptance_a_slot_reuses_is_a_recorded_read() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = alternative("demo.reads-acceptance", "demo.primary-ready", "a")
    env.admit(contract)
    root = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    other = task_binding(
        env,
        "demo.shared-read",
        task_id="task-shared",
        obligation="obl-shared",
        parameters={"subject": "alpha"},
    )
    from agent_orchestrator.contracts.htn import OccurrenceSpec

    network = root_network(
        env,
        root,
        extra_occurrences=(
            OccurrenceSpec(
                occurrence_id="occ-shared",
                task_id=other.task_id,
                obligation_id=other.obligation_id,
                form=TaskForm.PRIMITIVE,
            ),
        ),
        extra_bindings=(other,),
    )
    index = SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id="occ-shared",
                task_id=other.task_id,
                obligation_id=other.obligation_id,
                signature=shared_signature(env),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
                acceptance_ref=acceptance_ref("acceptance-11"),
            ),
        )
    )
    report = assess_method(
        root, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        root, contract, {}, report, catalog=env.catalog, schemas=env.schemas, sharing=index
    )
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        sharing=index,
    )
    assert [item.id for item in bundle.delta.read_set.acceptance_revisions] == ["acceptance-11"]
    assert "occ-shared" in {str(item) for item in bundle.delta.referenced_occurrences}


# ============================================ the step's own reuse declaration


def reusing_step_method(method_id: str, *, policy, shared: str = "demo.private-read"):
    """A method whose ``shared`` slot declares its own reuse policy."""

    return method(
        method_id,
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.primary-ready", {"subject": param("subject")}),),
        steps=(
            step(
                "shared",
                shared,
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
                reuse_policy=policy,
            ),
            step(
                "a",
                "demo.a",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "facts": out("shared", "facts")},
                capabilities=("demo.read",),
            ),
        ),
        links=(("c-done", "a", "c-a"),),
        finalizer="a",
    )


def ground_with_index(env: Env, contract, index: SharedGoalIndex):
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    return binding, ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        sharing=index,
    )


def index_for(env: Env, task_type: str, *, acceptance: str | None = None) -> SharedGoalIndex:
    return SharedGoalIndex(
        (
            SharedGoalEntry(
                occurrence_id="occ-existing",
                task_id="task-existing",
                obligation_id="obl-existing",
                signature=shared_signature(env, task_type=task_type),
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
                acceptance_ref=None if acceptance is None else acceptance_ref(acceptance),
            ),
        )
    )


def test_a_step_may_declare_reuse_the_task_type_left_at_new_work() -> None:
    """``method-contract-v1``: the step's own declaration wins when it makes one."""

    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = reusing_step_method("demo.step-reuses", policy=ReusePolicy.REUSE_ACCEPTED)
    _, draft = ground_with_index(
        env, contract, index_for(env, "demo.private-read", acceptance="acceptance-21")
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.occurrence_id == "occ-existing"
    assert slot.reuse_policy is ReusePolicy.REUSE_ACCEPTED


def test_a_step_that_declares_new_work_overrides_a_reusable_type() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = reusing_step_method(
        "demo.step-refuses", policy=ReusePolicy.NEW_WORK, shared="demo.shared-read"
    )
    _, draft = ground_with_index(
        env, contract, index_for(env, "demo.shared-read", acceptance="acceptance-22")
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.occurrence_id != "occ-existing"
    assert slot.reuse_policy is ReusePolicy.NEW_WORK


def test_an_undeclared_step_takes_the_task_type_s_default() -> None:
    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = reusing_step_method("demo.step-silent", policy=None, shared="demo.shared-read")
    _, draft = ground_with_index(
        env, contract, index_for(env, "demo.shared-read", acceptance="acceptance-23")
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.occurrence_id == "occ-existing"


def test_a_reusing_slot_names_the_exact_acceptance_it_rests_on() -> None:
    """TG decision 9: reuse binds a specific Acceptance, not "some earlier result"."""

    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = reusing_step_method("demo.step-cites", policy=ReusePolicy.REUSE_ACCEPTED)
    _, draft = ground_with_index(
        env, contract, index_for(env, "demo.private-read", acceptance="acceptance-24")
    )
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.acceptance_ref == acceptance_ref("acceptance-24")


def test_a_slot_sharing_live_work_names_no_acceptance() -> None:
    """I01: live work has not been accepted, so there is nothing to point at."""

    env = or_env()
    env.say("demo.primary-ready", {"subject": "alpha"}, TruthValue.TRUE)
    contract = reusing_step_method("demo.step-shares", policy=ReusePolicy.SHARE_ACTIVE)
    _, draft = ground_with_index(env, contract, index_for(env, "demo.private-read"))
    slot = next(item for item in draft.child_bindings if item.slot_key == "shared")
    assert slot.reuse_policy is ReusePolicy.SHARE_ACTIVE
    assert slot.acceptance_ref is None


def test_the_contract_refuses_an_acceptance_on_a_slot_that_shares_live_work() -> None:
    from agent_orchestrator.contracts.htn import ChildBinding
    from agent_orchestrator.contracts.models import ContractError

    with pytest.raises(ContractError, match="only REUSE_ACCEPTED"):
        ChildBinding(
            instance_id="mi-1",
            slot_key="shared",
            occurrence_id="occ-1",
            obligation_id="obl-1",
            reuse_policy=ReusePolicy.SHARE_ACTIVE,
            acceptance_ref=acceptance_ref("acceptance-25"),
        )


def test_a_step_may_not_declare_reuse_of_an_unshareable_effect() -> None:
    """§8.3: the method does not get to overrule the type's declared side effect."""

    env = or_env()
    contract = reusing_step_method(
        "demo.step-overreaches", policy=ReusePolicy.REUSE_ACCEPTED, shared="demo.notify"
    )
    receipt = env.admit(contract)
    assert not receipt.admitted
    assert any("two such actions are two actions" in item.detail for item in receipt.problems)
