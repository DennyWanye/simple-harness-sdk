# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1: grounding a parameterised method, and keeping its partial order partial.

Two claims are pinned here.

*Grounding is a type check with stable identities.*  ``ground_method`` refuses a
wrong-typed binding, a missing parameter, a primitive goal, a mismatched
signature and — above all — a method the world does not currently support: a
FALSE, UNKNOWN or CONFLICT precondition produces a :class:`GroundingError`, not a
draft carrying a witness for a world that does not hold (§6.6 rule 2, ADR-07).
The occurrence, task and duty of every slot come from ``(instance_id, slot_key)``
alone, so re-grounding the same method on the same goal lands on the same nodes.

*A partial order stays partial.*  ``A`` and ``B`` are independent and ``C``
consumes both.  The compiled increment contains exactly the declared ORDER edges
plus one DATA edge per declared port link — never a chain that quietly serialises
``A`` before ``B`` — and the projection accepts both linearisations (§7.4: a
partial order is not silently linearised; TG §6).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))

from htn_world import (  # noqa: E402
    BUDGET,
    Env,
    atom,
    method,
    out,
    param,
    root_network,
    step,
    task_binding,
)

from agent_orchestrator.contracts.evidence_state import PreconditionPhase, TruthValue  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    ObligationRelation,
    ReleaseCondition,
    Requiredness,
    ReusePolicy,
    SideEffectKind,
    TaskForm,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.graph.projection_validation import (  # noqa: E402
    kahn_order,
    validate_execution_projection,
)
from agent_orchestrator.graph.task_network import ProjectionEdgeKind  # noqa: E402
from agent_orchestrator.planning.htn.applicability import (  # noqa: E402
    ApplicabilityStatus,
    assess_method,
)
from agent_orchestrator.planning.htn.compiler import (  # noqa: E402
    CompilationRefused,
    compile_refinement,
    compile_refinement_bundle,
)
from agent_orchestrator.planning.htn.grounding import (  # noqa: E402
    GroundingError,
    data_flows,
    ground_method,
    instance_identity,
    slot_identity,
)
from agent_orchestrator.planning.htn.registry import (  # noqa: E402
    RejectionCode,
    implied_orderings,
)


def demo_env() -> Env:
    """A three-step domain: ``A`` and ``B`` independent, ``C`` consuming both."""

    env = Env()
    env.register_predicate("demo.ready", (("subject", "string"),))
    env.register_predicate("demo.blocked", (("subject", "string"),))
    env.register_type(
        "demo.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-done",),
        domain="demo",
    )
    env.register_type(
        "demo.a",
        parameters=(("subject", "string"),),
        outputs=(("ra", "demo.ra"),),
        capabilities=("demo.read",),
    )
    env.register_type(
        "demo.b",
        parameters=(("subject", "string"),),
        outputs=(("rb", "demo.rb"),),
        capabilities=("demo.read",),
    )
    env.register_type(
        "demo.c",
        parameters=(("subject", "string"),),
        inputs=(("pa", "demo.ra", True), ("pb", "demo.rb", True)),
        outputs=(("rc", "demo.rc"),),
        capabilities=("demo.read",),
    )
    return env


def parallel_method(method_id: str = "demo.parallel"):
    return method(
        method_id,
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "a",
                "demo.a",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
            step(
                "b",
                "demo.b",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
            step(
                "c",
                "demo.c",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pa": out("a", "ra"), "pb": out("b", "rb")},
                capabilities=("demo.read",),
            ),
        ),
        links=(("c-done", "c", "c-produced"),),
        finalizer="c",
    )


def ready_env(truth: TruthValue = TruthValue.TRUE, subject: str = "alpha") -> Env:
    env = demo_env()
    env.say("demo.ready", {"subject": subject}, truth)
    return env


def grounded(env: Env, *, subject: str = "alpha", bindings=None):
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": subject})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        bindings or {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
    )
    return contract, binding, report, draft


# ======================================================================== identity


def test_grounding_produces_one_slot_per_step() -> None:
    _, _, _, draft = grounded(ready_env())
    assert {binding.slot_key for binding in draft.child_bindings} == {"a", "b", "c"}


def test_slot_identities_derive_from_instance_and_slot() -> None:
    _, _, _, draft = grounded(ready_env())
    for binding in draft.child_bindings:
        expected = slot_identity(draft.instance_id, binding.slot_key, draft.obligation_id)
        assert binding.occurrence_id == expected.occurrence_id


def test_slot_identity_is_injective_across_slots() -> None:
    _, _, _, draft = grounded(ready_env())
    occurrences = [binding.occurrence_id for binding in draft.child_bindings]
    assert len(set(occurrences)) == len(occurrences)


def test_regrounding_the_same_inputs_returns_the_same_draft() -> None:
    first = grounded(ready_env())[3]
    second = grounded(ready_env())[3]
    assert first.to_json() == second.to_json()


def test_two_parameter_sets_produce_two_instances() -> None:
    env = ready_env()
    env.say("demo.ready", {"subject": "beta"}, TruthValue.TRUE)
    alpha = grounded(env, subject="alpha")[3]
    beta = grounded(env, subject="beta")[3]
    assert alpha.instance_id != beta.instance_id


def test_two_parameter_sets_produce_disjoint_occurrences() -> None:
    env = ready_env()
    env.say("demo.ready", {"subject": "beta"}, TruthValue.TRUE)
    alpha = grounded(env, subject="alpha")[3]
    beta = grounded(env, subject="beta")[3]
    left = {binding.occurrence_id for binding in alpha.child_bindings}
    right = {binding.occurrence_id for binding in beta.child_bindings}
    assert not left & right


def test_two_parameter_sets_carry_their_own_bindings() -> None:
    env = ready_env()
    env.say("demo.ready", {"subject": "beta"}, TruthValue.TRUE)
    alpha = grounded(env, subject="alpha")[3]
    beta = grounded(env, subject="beta")[3]
    assert [item.value for item in alpha.grounded_parameters] == ["alpha"]
    assert [item.value for item in beta.grounded_parameters] == ["beta"]


def test_parameters_digest_separates_the_two_groundings() -> None:
    env = ready_env()
    env.say("demo.ready", {"subject": "beta"}, TruthValue.TRUE)
    assert (
        grounded(env, subject="alpha")[3].parameters_digest()
        != grounded(env, subject="beta")[3].parameters_digest()
    )


def test_instance_identity_is_a_pure_function_of_its_parts() -> None:
    _, binding, _, draft = grounded(ready_env())
    assert draft.instance_id == instance_identity(
        goal_id=binding.task_id,
        goal_occurrence_id=draft.effective_goal_occurrence_id,
        method_ref=draft.method_ref,
        parameters_digest=draft.parameters_digest(),
    )


def test_refines_parent_slots_carry_the_parent_duty() -> None:
    _, binding, _, draft = grounded(ready_env())
    assert all(item.obligation_id == binding.obligation_id for item in draft.child_bindings)


def test_independent_authorized_slot_opens_its_own_duty() -> None:
    env = ready_env()
    identity = slot_identity(
        draft_instance := "mi-1",  # noqa: F841
        "extra",
        "obl-root",
        ObligationRelation.INDEPENDENT_AUTHORIZED,
    )
    assert identity.obligation_id != "obl-root"
    del env


def test_refines_parent_relation_reuses_the_parent_duty_exactly() -> None:
    identity = slot_identity("mi-1", "extra", "obl-root", ObligationRelation.REFINES_PARENT)
    assert identity.obligation_id == "obl-root"


def test_slot_keys_that_share_a_prefix_do_not_collide() -> None:
    left = slot_identity("mi-1", "a", "obl-root")
    right = slot_identity("mi-1", "a:b", "obl-root")
    assert left.occurrence_id != right.occurrence_id


def test_the_draft_pins_the_method_content_hash() -> None:
    contract, _, _, draft = grounded(ready_env())
    assert draft.method_ref == contract.method_ref()


def test_the_draft_records_a_select_phase_witness_per_precondition() -> None:
    contract, _, _, draft = grounded(ready_env())
    assert len(draft.precondition_witnesses) == len(contract.applicable_when)
    assert all(
        witness.phase is PreconditionPhase.SELECT for witness in draft.precondition_witnesses
    )


def test_every_witness_records_the_true_that_authorised_the_choice() -> None:
    _, _, _, draft = grounded(ready_env())
    assert all(witness.truth is TruthValue.TRUE for witness in draft.precondition_witnesses)


def test_the_draft_names_the_occurrence_it_refines() -> None:
    _, binding, _, draft = grounded(ready_env())
    assert str(draft.effective_goal_occurrence_id) == str(binding.task_id)


# ===================================================================== type checks


def test_a_wrongly_typed_binding_is_refused() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    with pytest.raises(GroundingError, match="type-check"):
        ground_method(
            binding,
            contract,
            {"subject": 7},
            report,
            catalog=env.catalog,
            schemas=env.schemas,
        )


def test_a_missing_parameter_is_refused() -> None:
    """A schema field the task does not carry is a refusal, not a ``None``."""

    env = ready_env()
    env.register_schema("demo.strict.params", (("subject", "string"), ("revision", "string")))
    contract = method(
        "demo.strict",
        "demo.goal",
        parameter_schema="demo.strict.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "a",
                "demo.a",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("demo.read",),
            ),
        ),
        links=(("c-done", "a", "c-produced"),),
        finalizer="a",
    )
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert report.status is ApplicabilityStatus.APPLICABLE
    with pytest.raises(GroundingError, match="missing argument"):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


def test_a_surplus_binding_does_not_reach_the_instance() -> None:
    _, _, _, draft = grounded(ready_env(), bindings={"unrelated": "x"})
    assert [item.name for item in draft.grounded_parameters] == ["subject"]


def test_a_primitive_goal_cannot_be_refined() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.a", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    with pytest.raises(GroundingError, match="primitive"):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


def test_a_signature_mismatch_is_refused() -> None:
    env = ready_env()
    env.register_type(
        "demo.other-goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-done",),
    )
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.other-goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    with pytest.raises(GroundingError, match="goal signature"):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


@pytest.mark.parametrize(
    ("truth", "status"),
    [
        (TruthValue.FALSE, ApplicabilityStatus.PRECONDITION_FALSE),
        (TruthValue.UNKNOWN, ApplicabilityStatus.NEEDS_EVIDENCE),
        (TruthValue.CONFLICT, ApplicabilityStatus.CONFLICT),
    ],
)
def test_a_method_that_does_not_apply_is_never_grounded(truth, status) -> None:
    env = ready_env(truth)
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert report.status is status
    with pytest.raises(GroundingError, match=str(status)):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


def test_an_unavailable_capability_stops_grounding() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding,
        contract,
        env.snapshot(),
        env.capabilities(unavailable=("demo.read",)),
        registry=env.predicates,
    )
    assert report.status is ApplicabilityStatus.CAPABILITY_UNAVAILABLE
    with pytest.raises(GroundingError):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


def test_a_true_that_rests_on_a_constant_never_grounds() -> None:
    """§6.6 rule 2: an authorisation gate accepts only evidence-backed leaves."""

    env = demo_env()
    contract = method(
        "demo.constant-true",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=({"op": "constant", "value": True},),
        steps=(step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),),
        links=(("c-done", "a", "c-produced"),),
        finalizer="a",
    )
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert report.status is ApplicabilityStatus.APPLICABLE
    assert report.authorization is not None and not report.authorization.allowed
    with pytest.raises(GroundingError, match="evidence-backed"):
        ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)


def test_an_unregistered_step_type_stops_grounding() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    empty = Env()
    empty.register_schema("demo.goal.params", (("subject", "string"),))
    with pytest.raises(GroundingError):
        ground_method(binding, contract, {}, report, catalog=empty.catalog, schemas=env.schemas)


def test_grounding_does_not_mutate_the_task_binding() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    before = binding.to_json()
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    assert binding.to_json() == before


# ================================================================== partial order


def compiled(env: Env, *, subject: str = "alpha"):
    contract, binding, _, draft = grounded(env, subject=subject)
    network = root_network(env, binding)
    return (
        contract,
        binding,
        draft,
        compile_refinement_bundle(
            draft,
            network,
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        ),
    )


def test_the_declared_data_flow_is_the_only_source_of_data_edges() -> None:
    contract, _, _, bundle = compiled(ready_env())
    assert len(bundle.delta.data_requirements) == len(data_flows(contract))


def test_data_edges_bind_the_declared_output_port() -> None:
    _, _, draft, bundle = compiled(ready_env())
    slots = {item.slot_key: item.occurrence_id for item in draft.child_bindings}
    pairs = {
        (
            requirement.producer_occurrence,
            requirement.output_port,
            requirement.consumer_occurrence,
            requirement.input_port,
        )
        for requirement in bundle.delta.data_requirements
    }
    assert (slots["a"], "ra", slots["c"], "pa") in pairs
    assert (slots["b"], "rb", slots["c"], "pb") in pairs


def test_a_data_edge_carries_the_producing_port_schema() -> None:
    env = ready_env()
    _, _, _, bundle = compiled(env)
    for requirement in bundle.delta.data_requirements:
        assert requirement.schema_ref.id.startswith("demo.")


def test_no_order_edge_is_invented_between_independent_steps() -> None:
    _, _, draft, bundle = compiled(ready_env())
    slots = {item.slot_key: item.occurrence_id for item in draft.child_bindings}
    for constraint in bundle.delta.order_constraints:
        assert {constraint.before, constraint.after} != {slots["a"], slots["b"]}


def test_the_method_declares_no_order_so_the_delta_carries_none() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert bundle.delta.order_constraints == ()


def test_both_linearisations_of_the_independent_pair_are_legal() -> None:
    _, _, draft, bundle = compiled(ready_env())
    projection = bundle.network.execution_projection()
    slots = {item.slot_key: item.occurrence_id for item in draft.child_bindings}
    successors = {node: [] for node in projection.node_ids()}
    for edge in projection.edges:
        successors[edge.source].append(edge.target)
    placed, remaining = kahn_order(successors, projection.ordinals())
    assert not remaining
    a_node = projection.entry_node_id(slots["a"])
    b_node = projection.entry_node_id(slots["b"])
    assert b_node not in _reachable(successors, a_node)
    assert a_node not in _reachable(successors, b_node)
    assert len(placed) == len(projection.nodes)


def _reachable(successors, start):
    seen: set[str] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        for target in successors.get(node, ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def test_declared_order_compiles_to_exit_to_entry() -> None:
    env = ready_env()
    contract = method(
        "demo.ordered",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
        ),
        ordering=(("a", "b"),),
        links=(("c-done", "b", "c-produced"),),
        finalizer="b",
    )
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    bundle = compile_refinement_bundle(
        draft,
        root_network(env, binding),
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    slots = {item.slot_key: item.occurrence_id for item in draft.child_bindings}
    projection = bundle.network.execution_projection()
    order_edges = [edge for edge in projection.edges if edge.kind is ProjectionEdgeKind.ORDER]
    assert len(order_edges) == 1
    assert order_edges[0].source == projection.exit_node_id(slots["a"])
    assert order_edges[0].target == projection.entry_node_id(slots["b"])


def test_order_constraints_release_on_acceptance_by_default() -> None:
    env = ready_env()
    contract = method(
        "demo.ordered-2",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
        ),
        ordering=(("a", "b"),),
        links=(("c-done", "b", "c-produced"),),
        finalizer="b",
    )
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    delta = compile_refinement(
        draft,
        root_network(env, binding),
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    assert all(
        constraint.release_condition is ReleaseCondition.ACCEPTED
        for constraint in delta.order_constraints
    )


def test_data_flow_implies_an_ordering_for_the_admission_cycle_check() -> None:
    contract = parallel_method()
    assert set(implied_orderings(contract)) == {("a", "c"), ("b", "c")}


def test_the_compiled_projection_is_acyclic() -> None:
    _, _, _, bundle = compiled(ready_env())
    report = validate_execution_projection(bundle.network.execution_projection(), BUDGET)
    assert report.ok, [problem.detail for problem in report.problems]


def test_compilation_is_deterministic() -> None:
    first = compiled(ready_env())[3]
    second = compiled(ready_env())[3]
    assert first.delta.to_json() == second.delta.to_json()


def test_compilation_does_not_mutate_the_current_network() -> None:
    env = ready_env()
    contract, binding, _, draft = grounded(env)
    network = root_network(env, binding)
    before = len(network.occurrences)
    compile_refinement(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
    )
    assert len(network.occurrences) == before


def test_the_delta_lists_every_new_occurrence() -> None:
    _, _, draft, bundle = compiled(ready_env())
    assert {spec.occurrence_id for spec in bundle.delta.occurrences} == {
        item.occurrence_id for item in draft.child_bindings
    }


def test_new_occurrences_inherit_the_declared_form() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert all(spec.form is TaskForm.PRIMITIVE for spec in bundle.delta.occurrences)


def test_refines_parent_slots_are_required() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert all(spec.requiredness is Requiredness.REQUIRED for spec in bundle.delta.occurrences)


def test_the_delta_claims_the_root_criterion() -> None:
    _, binding, _, bundle = compiled(ready_env())
    claims = {
        criterion
        for claim in bundle.delta.obligation_coverage
        if claim.obligation_id == binding.obligation_id
        for criterion in claim.criterion_ids
    }
    assert claims == {"c-done"}


def test_the_read_set_names_the_method_that_was_read() -> None:
    contract, _, _, bundle = compiled(ready_env())
    assert [item.id for item in bundle.delta.read_set.method_revisions] == [contract.method_id]


def test_the_read_set_names_the_refined_goal() -> None:
    _, binding, _, bundle = compiled(ready_env())
    assert str(binding.task_id) in {item.id for item in bundle.delta.read_set.goal_revisions}


def test_the_read_set_records_every_precondition_witness() -> None:
    _, _, draft, bundle = compiled(ready_env())
    assert {item.id for item in bundle.delta.read_set.observation_revisions} == {
        witness.condition_digest for witness in draft.precondition_witnesses
    }


def test_the_budget_requirement_counts_the_new_primitives() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert bundle.budget_requirement.new_primitive_occurrences == 3
    assert bundle.budget_requirement.new_compound_occurrences == 0


def test_the_compilation_records_all_ten_steps() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert len(bundle.steps) == 10
    assert bundle.steps[-1].startswith("10 not taken here")


def test_new_task_bindings_are_produced_for_every_created_slot() -> None:
    _, _, draft, bundle = compiled(ready_env())
    assert len(bundle.task_bindings) == len(draft.child_bindings)


def test_a_new_task_binding_carries_the_operator_of_its_type() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert all(binding.operator_ref is not None for binding in bundle.task_bindings)


def test_a_new_task_binding_carries_the_ports_of_its_type() -> None:
    _, _, _, bundle = compiled(ready_env())
    consumer = next(binding for binding in bundle.task_bindings if binding.input_ports)
    assert {port.port_key for port in consumer.input_ports} == {"pa", "pb"}


def test_a_new_task_binding_records_where_it_sits_in_the_method() -> None:
    _, _, draft, bundle = compiled(ready_env())
    for binding in bundle.task_bindings:
        assert binding.occurrence_binding is not None
        assert binding.occurrence_binding.method_instance_id == draft.instance_id


def test_the_parent_binding_adopts_the_instance() -> None:
    _, _, draft, bundle = compiled(ready_env())
    assert bundle.parent_binding.adopted_method_instance_id == draft.instance_id


# ================================================================== port refusals


def test_an_output_port_the_producer_does_not_declare_is_refused() -> None:
    env = ready_env()
    contract = method(
        "demo.bad-port",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step(
                "c",
                "demo.c",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pa": out("a", "nope"), "pb": out("a", "ra")},
            ),
        ),
        links=(("c-done", "c", "c-produced"),),
        finalizer="c",
    )
    receipt = env.admit(contract)
    assert RejectionCode.PORT_UNAVAILABLE in receipt.codes()


def test_a_port_schema_mismatch_is_refused_at_compile_time() -> None:
    env = ready_env()
    contract = method(
        "demo.mismatched-schema",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step(
                "c",
                "demo.c",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pa": out("b", "rb"), "pb": out("a", "ra")},
            ),
        ),
        links=(("c-done", "c", "c-produced"),),
        finalizer="c",
    )
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused, match="schema"):
        compile_refinement(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        )


def test_a_step_reading_its_own_output_is_refused() -> None:
    env = ready_env()
    contract = method(
        "demo.self-read",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step(
                "c",
                "demo.c",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pa": out("a", "ra"), "pb": out("c", "rc")},
            ),
        ),
        links=(("c-done", "c", "c-produced"),),
        finalizer="c",
    )
    receipt = env.admit(contract)
    assert RejectionCode.MALFORMED_DEFINITION in receipt.codes()


def test_a_method_ordering_cycle_is_refused_at_admission() -> None:
    env = ready_env()
    contract = method(
        "demo.cycle",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
        ),
        ordering=(("a", "b"), ("b", "a")),
        links=(("c-done", "b", "c-produced"),),
        finalizer="b",
    )
    receipt = env.admit(contract)
    assert RejectionCode.ORDERING_CYCLE in receipt.codes()


def test_a_data_flow_cycle_is_refused_at_admission() -> None:
    env = ready_env()
    env.register_type(
        "demo.d",
        parameters=(("subject", "string"),),
        inputs=(("pc", "demo.rc", True),),
        outputs=(("ra", "demo.ra"),),
        capabilities=("demo.read",),
    )
    contract = method(
        "demo.data-cycle",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step(
                "d",
                "demo.d",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pc": out("c", "rc")},
            ),
            step(
                "c",
                "demo.c",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "pa": out("d", "ra"), "pb": out("b", "rb")},
            ),
        ),
        links=(("c-done", "c", "c-produced"),),
        finalizer="c",
    )
    receipt = env.admit(contract)
    assert RejectionCode.ORDERING_CYCLE in receipt.codes()


def test_the_registry_reports_a_concrete_cycle() -> None:
    env = ready_env()
    contract = method(
        "demo.cycle-2",
        "demo.goal",
        parameter_schema="demo.goal.params",
        applicable=(atom("demo.ready", {"subject": param("subject")}),),
        steps=(
            step("a", "demo.a", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step("b", "demo.b", TaskForm.PRIMITIVE, {"subject": param("subject")}),
        ),
        ordering=(("a", "b"), ("b", "a")),
        links=(("c-done", "b", "c-produced"),),
        finalizer="b",
    )
    receipt = env.admit(contract)
    detail = next(
        problem.detail
        for problem in receipt.problems
        if problem.code is RejectionCode.ORDERING_CYCLE
    )
    assert "->" in detail


def test_a_draft_built_against_other_inputs_is_refused() -> None:
    env = ready_env()
    env.say("demo.ready", {"subject": "beta"}, TruthValue.TRUE)
    contract, binding, _, draft = grounded(env, subject="alpha")
    other = task_binding(env, "demo.goal", parameters={"subject": "beta"})
    network = root_network(env, other)
    with pytest.raises(CompilationRefused):
        compile_refinement(
            draft,
            network,
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        )
    del binding


def test_a_method_hash_mismatch_is_refused() -> None:
    env = ready_env()
    contract, binding, _, draft = grounded(env)
    other = parallel_method("demo.parallel-2")
    env.admit(other)
    with pytest.raises(CompilationRefused, match="content hash"):
        compile_refinement(
            draft,
            root_network(env, binding),
            method=other,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        )
    del contract


def test_an_unadmitted_method_is_not_compiled() -> None:
    env = ready_env()
    contract = parallel_method()
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused, match="unregistered"):
        compile_refinement(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        )


def test_the_read_only_effect_of_a_step_reaches_its_binding() -> None:
    _, _, _, bundle = compiled(ready_env())
    assert all(
        binding.side_effect_kind is SideEffectKind.EXTERNAL_READ for binding in bundle.task_bindings
    )


def test_no_slot_is_shared_without_a_sharing_index() -> None:
    _, _, draft, _ = compiled(ready_env())
    assert all(item.reuse_policy is ReusePolicy.NEW_WORK for item in draft.child_bindings)


def test_the_contract_refuses_a_new_work_slot_that_borrows_a_goal() -> None:
    from agent_orchestrator.contracts.htn import ChildBinding

    with pytest.raises(ContractError, match="NEW_WORK"):
        ChildBinding(
            instance_id="mi-1",
            slot_key="a",
            occurrence_id="occ-1",
            obligation_id="obl-1",
            reuse_policy=ReusePolicy.NEW_WORK,
            goal_occurrence_id="occ-2",
        )


def test_the_goal_type_must_be_registered_to_ground() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    stripped = Env()
    stripped.register_schema("demo.goal.params", (("subject", "string"),))
    with pytest.raises(GroundingError, match="goal type"):
        ground_method(
            binding,
            contract,
            {},
            report,
            catalog=stripped.catalog,
            schemas=env.schemas,
        )


def test_an_unregistered_parameter_schema_stops_grounding() -> None:
    env = ready_env()
    contract = parallel_method()
    env.admit(contract)
    binding = task_binding(env, "demo.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    from agent_orchestrator.planning.htn.registry import SchemaCatalog

    with pytest.raises(GroundingError, match="parameter schema"):
        ground_method(
            binding,
            contract,
            {},
            report,
            catalog=env.catalog,
            schemas=SchemaCatalog(),
        )
